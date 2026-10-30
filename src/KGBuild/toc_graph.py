#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""toc_graph v2 —— 章节结构图（toc_graph.json）。

这是一个中间产物，不进入最终四层 KG，用于：
  * 检查章节树是否合理（层级、父子、跨度）
  * 给 Web 建图页提供结构视图
  * 后续增量构建时定位受影响的小节

节点 ID 直接复用 TextSegmentation 的 section_id（同一文档内唯一），
边统一为 child --part_of--> parent，避免再引入一套关系名。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from .schema_loader import get_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import get_schema

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "output"


def _walk(nodes: Iterable[Dict[str, Any]], parent_id: Optional[str] = None):
    for n in nodes or []:
        sid = n.get("section_id") or n.get("id") or ""
        yield n, parent_id
        yield from _walk(n.get("children") or [], sid)


def build_toc_graph(toc_doc: Dict[str, Any]) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for sec, parent_id in _walk(toc_doc.get("sections") or []):
        sid = sec.get("section_id") or sec.get("id") or ""
        if not sid or sid in seen:
            continue
        seen.add(sid)
        nodes.append({
            "id": sid,
            "name": sec.get("title") or "",
            "title": sec.get("title") or "",
            "numbering": sec.get("numbering") or "",
            "level": int(sec.get("level") or 1),
            "section_level": sec.get("section_level") or "unknown",
            "path": sec.get("path") or "",
            "parent_id": parent_id or "",
            "char_start": sec.get("char_start"),
            "char_end": sec.get("char_end"),
            "block_count": len(sec.get("blocks") or []),
        })
        if parent_id:
            edges.append({
                "source": sid,
                "target": parent_id,
                "type": "part_of",
                "layer": "structure",
                "description": f"{sid} 属于 {parent_id}",
            })

    return {
        "schema_version": get_schema().version,
        "doc_id": toc_doc.get("doc_id") or "",
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_level": _count(nodes, "level"),
            "by_section_level": _count(nodes, "section_level"),
            "roots": sum(1 for n in nodes if not n["parent_id"]),
        },
    }


def _count(nodes: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n in nodes:
        k = str(n.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="构建 v2 章节结构图")
    ap.add_argument("--in", dest="in_path",
                    default=str(OUT_DIR / "toc_with_entities_and_relations.json"))
    ap.add_argument("--out", default=str(OUT_DIR / "toc_graph.json"))
    args = ap.parse_args(argv)

    in_path = Path(args.in_path)
    if not in_path.exists():
        raise FileNotFoundError(f"未找到输入文件：{in_path}")
    with in_path.open("r", encoding="utf-8") as f:
        toc_doc = json.load(f)
    if isinstance(toc_doc, list):
        raise ValueError("输入是 v1 裸数组；请先用 v2 流水线重新生成")

    graph = build_toc_graph(toc_doc)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    print(f"✅ 章节结构图：{out_path.resolve()}")
    print(f"  节点 {graph['stats']['nodes']} / 边 {graph['stats']['edges']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
