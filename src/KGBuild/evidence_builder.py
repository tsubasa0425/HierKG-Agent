#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evidence_builder v2 —— 从章节树构建 L3 证据层。

两种粒度：
  * section（默认）：每个有正文的小节一条证据，覆盖该节全部 block
  * block：每个可检索 block 一条证据（段落 / 表格 / 图题 / 列表 …）
  * both：两种都生成（ID 分别为 ev_doc:sec 与 ev_doc:sec:block）

小节的 section_id 在 TextSegmentation 阶段已保证唯一，因此证据 ID 天然唯一，
不再使用 v1 的 `#2` 后缀。容器小节（只有子标题、没有正文）生成 derived 证据，
仅用于图导航，不进入检索投影。

输出：evidence.json
{
  "schema_version": "2.0",
  "doc_id": "...",
  "doc_text_path": "...",
  "evidences": [ ... ],
  "stats": { ... }
}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from .schema_loader import get_schema, make_evidence_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import get_schema, make_evidence_id

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "output"


def _walk(nodes: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for n in nodes or []:
        yield n
        yield from _walk(n.get("children") or [])


def _block_text(blocks: List[Dict[str, Any]]) -> str:
    return "\n".join(b.get("text", "") for b in blocks if (b.get("text") or "").strip())


def _span(blocks: List[Dict[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    starts = [b["char_start"] for b in blocks if b.get("char_start") is not None]
    ends = [b["char_end"] for b in blocks if b.get("char_end") is not None]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def _entity_names(section: Dict[str, Any]) -> List[str]:
    return [str(e.get("name")) for e in section.get("entities") or [] if e.get("name")]


def _make_section_evidence(section: Dict[str, Any], doc_id: str) -> Dict[str, Any]:
    blocks = [b for b in section.get("blocks") or [] if (b.get("text") or "").strip()]
    text = _block_text(blocks) or (section.get("content") or "").strip()
    start, end = _span(blocks)
    return {
        "id": make_evidence_id(doc_id, section["section_id"]),
        "doc_id": doc_id,
        "section_id": section["section_id"],
        "section_path": section.get("path") or section.get("title", ""),
        "section_level": section.get("section_level") or "unknown",
        "title": section.get("title") or "",
        "text": text,
        "block_id": "",
        "block_type": "unknown",
        "char_start": start,
        "char_end": end,
        "entities_in_section": _entity_names(section),
        "derived": False,
    }


def _make_block_evidences(section: Dict[str, Any], doc_id: str,
                          min_chars: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in section.get("blocks") or []:
        text = (b.get("text") or "").strip()
        if not text or len(text) < min_chars:
            continue
        out.append({
            "id": make_evidence_id(doc_id, section["section_id"], b.get("block_id") or ""),
            "doc_id": doc_id,
            "section_id": section["section_id"],
            "section_path": section.get("path") or section.get("title", ""),
            "section_level": section.get("section_level") or "unknown",
            "title": section.get("title") or "",
            "text": text,
            "block_id": b.get("block_id") or "",
            "block_type": b.get("type") or "unknown",
            "char_start": b.get("char_start"),
            "char_end": b.get("char_end"),
            "entities_in_section": _entity_names(section),
            "derived": False,
        })
    return out


def _make_derived_evidence(section: Dict[str, Any], doc_id: str) -> Optional[Dict[str, Any]]:
    children = section.get("children") or []
    if not children:
        return None
    child_titles = [c.get("title", "") for c in children if c.get("title")]
    text = (section.get("title") or "") + "\n" + "\n".join(child_titles)
    return {
        "id": make_evidence_id(doc_id, section["section_id"]),
        "doc_id": doc_id,
        "section_id": section["section_id"],
        "section_path": section.get("path") or section.get("title", ""),
        "section_level": section.get("section_level") or "unknown",
        "title": section.get("title") or "",
        "text": text.strip(),
        "block_id": "",
        "block_type": "unknown",
        # 文本由标题拼接而成，与原文偏移不对应，因此不给偏移
        "char_start": None,
        "char_end": None,
        "entities_in_section": [],
        "derived": True,
    }


def build_evidence(toc_doc: Dict[str, Any], doc_id: Optional[str] = None,
                   level: str = "section", include_unknown: bool = False,
                   min_block_chars: int = 0) -> Dict[str, Any]:
    doc_id = doc_id or toc_doc.get("doc_id") or "doc"
    evidences: List[Dict[str, Any]] = []
    ids: set[str] = set()

    for sec in _walk(toc_doc.get("sections") or []):
        sec_id = sec.get("section_id") or sec.get("id")
        if not sec_id:
            continue
        if sec.get("section_level") == "unknown" and not include_unknown:
            continue
        sec["section_id"] = sec_id

        blocks = [b for b in sec.get("blocks") or [] if (b.get("text") or "").strip()]
        has_body = bool(blocks) or bool((sec.get("content") or "").strip())

        produced: List[Dict[str, Any]] = []
        if has_body:
            if level in ("section", "both"):
                produced.append(_make_section_evidence(sec, doc_id))
            if level in ("block", "both"):
                produced.extend(_make_block_evidences(sec, doc_id, min_block_chars))
        else:
            derived = _make_derived_evidence(sec, doc_id)
            if derived:
                produced.append(derived)

        for ev in produced:
            if ev["id"] in ids:
                raise ValueError(f"证据 ID 重复：{ev['id']}（section_id={sec_id}）")
            if not ev["text"]:
                continue
            ids.add(ev["id"])
            evidences.append(ev)

    stats = {
        "evidences": len(evidences),
        "derived": sum(1 for e in evidences if e["derived"]),
        "by_section_level": _count(evidences, "section_level"),
        "by_block_type": _count([e for e in evidences if not e["derived"]], "block_type"),
        "with_entities": sum(1 for e in evidences if e["entities_in_section"]),
        "total_chars": sum(len(e["text"]) for e in evidences),
    }
    return {
        "schema_version": get_schema().version,
        "doc_id": doc_id,
        "doc_text_path": toc_doc.get("doc_text_path"),
        "evidences": evidences,
        "stats": stats,
    }


def _count(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="构建 v2 L3 证据层")
    ap.add_argument("--input", default=str(OUT_DIR / "toc_with_entities_and_relations.json"))
    ap.add_argument("--output", default=str(OUT_DIR / "evidence.json"))
    ap.add_argument("--doc-id", default=None)
    ap.add_argument("--level", default="section", choices=["section", "block", "both"])
    ap.add_argument("--min-block-chars", type=int, default=0)
    ap.add_argument("--include-unknown", action="store_true",
                    help="包含 section_level=unknown 的前置内容")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        raise FileNotFoundError(f"输入文件不存在：{in_path}")
    with in_path.open("r", encoding="utf-8") as f:
        toc_doc = json.load(f)
    if isinstance(toc_doc, list):
        raise ValueError("输入是 v1 裸数组；请先用 v2 流水线重新生成")

    result = build_evidence(toc_doc, doc_id=args.doc_id, level=args.level,
                            include_unknown=args.include_unknown,
                            min_block_chars=args.min_block_chars)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    st = result["stats"]
    print(f"✅ L3 证据层：{out_path.resolve()}")
    print(f"   证据 {st['evidences']} 条（derived {st['derived']}）")
    print(f"   层级分布：{st['by_section_level']}")
    print(f"   含实体证据：{st['with_entities']} / {st['evidences']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
