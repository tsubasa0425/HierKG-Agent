#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conv v2 —— 实体归并 + 证据化定义补全 + 邻域/关系汇总。

职责（不做“用邻域编故事”的 LLM 扩写）：
  1. 把各小节抽取到的同名/别名实体归并为一个节点，保留全部 occurrences
  2. 计算 LayerPolicy 需要的信号：小节覆盖数、出现次数、定义性措辞、标题命中
  3. 仅在实体缺少定义时，用「原文片段」让 LLM 补一句定义（ENRICH_MODE=missing）
  4. 汇总抽取阶段的语义关系，端点折算到规范名
  5. 生成同小节共现邻域，供 Pred 做拓扑补边

输入：toc_with_entities_and_relations.json
输出：conv_entities.json
{
  "schema_version": "2.0",
  "doc_id": "...",
  "entities": [
    {"name","aliases","type","definition","confidence",
     "occurrences":[...], "neighbors":[...], "signals":{...}, "provenance":[...]}
  ],
  "relations": [{"source","target","type","description","keywords","confidence","provenance"}],
  "stats": {...}
}
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
from pathlib import Path
from string import Template
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

try:
    from .schema_loader import Schema, get_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import Schema, get_schema

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(*args, **kwargs):  # type: ignore
        class _Null:
            def update(self, n=1): pass
            def close(self): pass
        return _Null()

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
OUT_DIR = SCRIPT_DIR / "output"
SCHEMA = get_schema()

logger = logging.getLogger("Conv")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

CONV_DEFAULTS: Dict[str, Any] = {
    "TOC_ENT_REL_NAME": "toc_with_entities_and_relations.json",
    "CONV_RESULT_NAME": "conv_entities.json",
    "MAX_NEIGHBORS_PER_ENTITY": 40,
    "ENRICH_MODE": "missing",          # missing | all | none
    "ENRICH_MAX_ENTITIES": 100,
    "ENRICH_MIN_COVERAGE": 2,
    "MAX_DEFINITION_LEN": 300,
    "TEMPERATURE": 0.2,
    "MAX_TOKENS": 500,
    "DRY_RUN": False,
    "PROMPT_VERSION": "conv.v2",
    "PROMPT_SYSTEM": "",
    "PROMPT_USER_TEMPLATE": "",
}
API_DEFAULTS: Dict[str, Any] = {
    "API_KEY": "", "API_BASE": "", "MODEL_NAME": "",
    "TIMEOUT_SECS": 120, "RETRIES": 3, "RETRY_BACKOFF_BASE": 2,
    "RATE_LIMIT_QPS": 0, "EXTRA_THROTTLE_SEC": 0,
    "CHAT_COMPLETIONS_PATH": "/chat/completions", "WORKERS": 8,
}
DEFINITION_CUES = ("是指", "定义为", "称为", "是一种", "指的是", "所谓", "即")


# ---------------------------------------------------------------------------
# 配置 / LLM
# ---------------------------------------------------------------------------

def _load_yaml(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_config() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    cfg_path = CONFIG_DIR / "config.yaml"
    merged: Dict[str, Any] = {}
    if cfg_path.exists():
        merged = dict(_load_yaml(cfg_path))
        for rel in merged.get("include_files", []) or []:
            p = (CONFIG_DIR / str(rel)).resolve()
            if p.exists():
                merged.update(_load_yaml(p))
    if "ConvConfig" not in merged:
        merged.update(_load_yaml(CONFIG_DIR / "conv.yaml"))
    cfg = dict(CONV_DEFAULTS)
    cfg.update(merged.get("ConvConfig") or {})
    api = dict(API_DEFAULTS)
    api.update(merged.get("APIConfig") or {})
    return cfg, api


CFG, API = _load_config()
_rate_lock = threading.Lock()
_last_ts = 0.0


def _rate_limit() -> None:
    qps = float(API.get("RATE_LIMIT_QPS") or 0)
    if qps <= 0:
        return
    global _last_ts
    with _rate_lock:
        delta = time.time() - _last_ts
        if delta < 1.0 / qps:
            time.sleep(1.0 / qps - delta)
        _last_ts = time.time()


def call_llm(system_prompt: str, user_prompt: str) -> str:
    if CFG["DRY_RUN"] or not API.get("API_BASE") or not API.get("MODEL_NAME"):
        return ""
    import requests  # 延迟导入，纯逻辑测试不需要

    headers = {"Content-Type": "application/json"}
    if API.get("API_KEY"):
        headers["Authorization"] = f"Bearer {API['API_KEY']}"
    payload = {
        "model": API["MODEL_NAME"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(CFG["TEMPERATURE"]),
        "max_tokens": int(CFG["MAX_TOKENS"]),
    }
    last_err: Optional[Exception] = None
    for k in range(max(1, int(API["RETRIES"]))):
        try:
            _rate_limit()
            extra = float(API.get("EXTRA_THROTTLE_SEC") or 0)
            if extra > 0:
                time.sleep(extra)
            url = API["API_BASE"].rstrip("/") + API.get("CHAT_COMPLETIONS_PATH", "/chat/completions")
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=float(API["TIMEOUT_SECS"]))
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(f"LLM 调用失败({k + 1}/{API['RETRIES']})：{e}")
            time.sleep(float(API["RETRY_BACKOFF_BASE"]) ** k)
    logger.warning(f"LLM 放弃：{last_err}")
    return ""


def _safe_json(txt: str) -> Dict[str, Any]:
    if not txt:
        return {}
    txt = txt.strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    start, end = txt.find("{"), txt.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(txt[start:end + 1])
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _clean_definition(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip()).strip("\"'` ")
    if not s or s.startswith("{") or len(s) < 8:
        return ""
    limit = int(CFG["MAX_DEFINITION_LEN"])
    if len(s) > limit:
        cut = s[:limit]
        for c in "。；，）】":
            pos = cut.rfind(c)
            if pos >= limit * 0.6:
                return cut[:pos + 1]
        return cut
    return s


def _snippet(section_text: str, forms: List[str], width: int = 160) -> str:
    for form in forms:
        if not form:
            continue
        idx = section_text.find(form)
        if idx >= 0:
            seg = section_text[max(0, idx - width): idx + len(form) + width]
            return re.sub(r"\s+", " ", seg).strip()
    return ""


def _count_mentions(text: str, forms: List[str]) -> int:
    total = 0
    for form in dict.fromkeys(f for f in forms if f):
        total += text.count(form)
    return total


def _has_definitional_cue(text: str, forms: List[str]) -> bool:
    for form in forms:
        if not form:
            continue
        for m in re.finditer(re.escape(form), text):
            window = text[max(0, m.start() - 80): m.end() + 80]
            if any(c in window for c in DEFINITION_CUES):
                return True
    return False


def _walk(nodes: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for n in nodes or []:
        yield n
        yield from _walk(n.get("children") or [])


# ---------------------------------------------------------------------------
# 归并
# ---------------------------------------------------------------------------

class _DSU:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _group_records(records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    dsu = _DSU()
    for rec in records:
        name_key = _key(rec["name"])
        dsu.find(name_key)
        for a in rec.get("aliases") or []:
            dsu.union(name_key, _key(a))
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        groups[dsu.find(_key(rec["name"]))].append(rec)
    return list(groups.values())


def _pick_canonical(group: List[Dict[str, Any]]) -> str:
    counts = Counter(r["name"] for r in group)
    return sorted(counts, key=lambda n: (-counts[n], len(n), n))[0]


def _pick_type(group: List[Dict[str, Any]]) -> str:
    votes = Counter(r["type"] for r in group if r.get("type") and r["type"] != "待定")
    if votes:
        return sorted(votes, key=lambda t: (-votes[t], t))[0]
    return "待定"


def _pick_definition(group: List[Dict[str, Any]], canonical: str) -> str:
    defs = [r.get("definition") or "" for r in group if r.get("definition")]
    if not defs:
        return ""

    def score(d: str) -> Tuple[int, int, int]:
        cue = any(c in d for c in DEFINITION_CUES)
        named = canonical in d
        return (int(cue), int(named), len(d))

    return max(defs, key=score)


def _dedupe_prov(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for p in items:
        k = json.dumps(p, ensure_ascii=False, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def merge_entities(records: List[Dict[str, Any]],
                   section_by_id: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """records: 每条 = 某个小节里的一条实体抽取结果（已带 section 上下文）。"""
    merged: List[Dict[str, Any]] = []
    variant_to_canonical: Dict[str, str] = {}

    for group in _group_records(records):
        canonical = _pick_canonical(group)
        variants: Set[str] = set()
        for r in group:
            variants.add(r["name"])
            variants.update(r.get("aliases") or [])
        aliases = sorted({v for v in variants if _key(v) != _key(canonical)})

        occ_seen: Set[Tuple[str, str]] = set()
        occurrences: List[Dict[str, Any]] = []
        prov: List[Dict[str, Any]] = []
        mention_total = 0
        cue = False
        heading = False

        for r in group:
            sec = r["section"]
            sid = sec.get("section_id") or sec.get("id") or ""
            block_id = ""
            if r.get("provenance"):
                block_id = r["provenance"][0].get("block_id", "")
            okey = (sid, block_id)
            if okey not in occ_seen:
                occ_seen.add(okey)
                forms = [canonical] + aliases
                text = sec.get("content") or ""
                mentions = max(1, _count_mentions(text, forms))
                is_cue = _has_definitional_cue(text, forms)
                is_heading = any(f and f in (sec.get("title") or "") for f in forms)
                mention_total += mentions
                cue = cue or is_cue
                heading = heading or is_heading
                occurrences.append({
                    "section_id": sid,
                    "section_path": sec.get("path") or "",
                    "title": sec.get("title") or "",
                    "section_level": sec.get("section_level") or "unknown",
                    "block_id": block_id,
                    "mention_count": mentions,
                    "definitional_cue": is_cue,
                })
            prov.extend(r.get("provenance") or [])

        definition = _pick_definition(group, canonical)
        entity = {
            "name": canonical,
            "aliases": aliases,
            "type": _pick_type(group),
            "definition": definition,
            "confidence": round(max(float(r.get("confidence", 1.0)) for r in group), 2),
            "occurrences": occurrences,
            "neighbors": [],
            "signals": {
                "cross_section_coverage": len({o["section_id"] for o in occurrences}),
                "occurrence_count": max(mention_total, len(occurrences)),
                "has_definition": bool(definition),
                "definitional_cue": bool(cue),
                "heading_presence": heading,
                "degree": 0,
            },
            "provenance": _dedupe_prov(prov),
        }
        merged.append(entity)
        for v in variants:
            variant_to_canonical[_key(v)] = canonical

    merged.sort(key=lambda e: (-e["signals"]["cross_section_coverage"], e["name"]))
    return merged, variant_to_canonical


# ---------------------------------------------------------------------------
# 关系 / 邻域
# ---------------------------------------------------------------------------

def aggregate_relations(section_docs: List[Dict[str, Any]],
                        variant_to_canonical: Dict[str, str],
                        schema: Schema) -> List[Dict[str, Any]]:
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for sec in section_docs:
        for r in sec.get("relations") or []:
            src = variant_to_canonical.get(_key(r.get("source") or ""))
            tgt = variant_to_canonical.get(_key(r.get("target") or ""))
            rel = schema.canonical_relation(r.get("type") or "")
            if not src or not tgt or src == tgt or not rel:
                continue
            if schema.is_symmetric(rel) and src > tgt:
                src, tgt = tgt, src
            key = (src, tgt, rel)
            if key not in out:
                out[key] = {
                    "source": src, "target": tgt, "type": rel,
                    "description": r.get("description") or "",
                    "keywords": list(dict.fromkeys(r.get("keywords") or [])),
                    "confidence": float(r.get("confidence", 0.5)),
                    "provenance": list(r.get("provenance") or []),
                }
            else:
                cur = out[key]
                cur["keywords"] = list(dict.fromkeys(cur["keywords"] + (r.get("keywords") or [])))
                cur["confidence"] = max(cur["confidence"], float(r.get("confidence", 0.5)))
                cur["provenance"] = _dedupe_prov(cur["provenance"] + (r.get("provenance") or []))
    return sorted(out.values(), key=lambda r: (r["source"], r["target"], r["type"]))


def build_neighbors(entities: List[Dict[str, Any]],
                    relations: List[Dict[str, Any]]) -> None:
    by_name = {e["name"]: e for e in entities}
    section_names: Dict[str, Set[str]] = defaultdict(set)
    section_title: Dict[str, str] = {}
    for e in entities:
        for o in e["occurrences"]:
            section_names[o["section_id"]].add(e["name"])
            section_title.setdefault(o["section_id"], o["title"])

    pair_weight: Dict[Tuple[str, str], int] = defaultdict(int)
    pair_section: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for sid, names in section_names.items():
        ordered = sorted(names)
        for a, b in combinations(ordered, 2):
            pair_weight[(a, b)] += 1
            pair_section.setdefault((a, b), (sid, section_title.get(sid, "")))

    rel_type: Dict[Tuple[str, str], str] = {}
    for r in relations:
        a, b = sorted((r["source"], r["target"]))
        rel_type.setdefault((a, b), r["type"])

    cap = int(CFG["MAX_NEIGHBORS_PER_ENTITY"])
    for e in entities:
        name = e["name"]
        nbs: List[Dict[str, Any]] = []
        for (a, b), w in pair_weight.items():
            if name not in (a, b):
                continue
            other = b if a == name else a
            if other not in by_name:
                continue
            sid, title = pair_section.get((a, b), ("", ""))
            nbs.append({
                "name": other,
                "weight": w,
                "relation": rel_type.get((a, b), ""),
                "section_id": sid,
                "section_title": title,
            })
        nbs.sort(key=lambda x: (-x["weight"], x["name"]))
        e["neighbors"] = nbs[:cap] if cap > 0 else nbs


# ---------------------------------------------------------------------------
# 定义补全
# ---------------------------------------------------------------------------

def _build_user_prompt(entity: Dict[str, Any], snippets: List[str]) -> str:
    tpl = CFG.get("PROMPT_USER_TEMPLATE") or ""
    return Template(tpl).safe_substitute(
        entity_name=entity["name"],
        entity_type=entity["type"],
        snippets="\n".join(f"- {s}" for s in snippets) or "（无）",
        occurrences="; ".join(o["section_path"] for o in entity["occurrences"][:3]),
    )


def enrich_definitions(entities: List[Dict[str, Any]],
                       section_by_id: Dict[str, Dict[str, Any]]) -> int:
    mode = str(CFG["ENRICH_MODE"])
    if mode == "none" or CFG["DRY_RUN"]:
        return 0
    min_cov = int(CFG["ENRICH_MIN_COVERAGE"])
    candidates = [
        e for e in entities
        if (mode == "all" or not e["definition"])
        and e["signals"]["cross_section_coverage"] >= min_cov
    ][: int(CFG["ENRICH_MAX_ENTITIES"])]
    if not candidates:
        return 0

    system = CFG.get("PROMPT_SYSTEM") or ""
    workers = int(API.get("WORKERS") or 8)
    done = 0

    def work(e: Dict[str, Any]) -> bool:
        forms = [e["name"]] + e["aliases"]
        snippets: List[str] = []
        for o in e["occurrences"]:
            sec = section_by_id.get(o["section_id"]) or {}
            s = _snippet(sec.get("content") or "", forms)
            if s and s not in snippets:
                snippets.append(s)
            if len(snippets) >= 3:
                break
        if not snippets:
            return False
        text = call_llm(system, _build_user_prompt(e, snippets))
        obj = _safe_json(text)
        definition = _clean_definition(
            str(obj.get("definition") or obj.get("updated_description") or ""))
        if definition:
            e["definition"] = definition
            return True
        return False

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, e) for e in candidates]
        for fu in tqdm(as_completed(futures), total=len(futures),
                       desc="定义补全", ncols=80):
            if fu.result():
                done += 1
    return done


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(in_path: Optional[Path] = None, out_path: Optional[Path] = None,
        dry_run: bool = False) -> int:
    in_path = in_path or (OUT_DIR / CFG["TOC_ENT_REL_NAME"])
    out_path = out_path or (OUT_DIR / CFG["CONV_RESULT_NAME"])
    if not in_path.exists():
        raise FileNotFoundError(f"未找到输入：{in_path}")
    with in_path.open("r", encoding="utf-8") as f:
        toc_doc = json.load(f)
    if isinstance(toc_doc, list):
        raise ValueError("输入是 v1 裸数组；请先用 v2 流水线重新生成")

    docs = list(_walk(toc_doc.get("sections") or []))
    section_by_id = {s.get("section_id") or s.get("id"): s for s in docs}

    records: List[Dict[str, Any]] = []
    for sec in docs:
        for e in sec.get("entities") or []:
            records.append({"section": sec, **e})

    entities, variant_map = merge_entities(records, section_by_id)
    relations = aggregate_relations(docs, variant_map, SCHEMA)
    build_neighbors(entities, relations)
    enriched = enrich_definitions(entities, section_by_id)

    result = {
        "schema_version": SCHEMA.version,
        "doc_id": toc_doc.get("doc_id") or "doc",
        "entities": entities,
        "relations": relations,
        "stats": {
            "raw_entity_records": len(records),
            "entities": len(entities),
            "merged_groups": len(records) - len(entities),
            "relations": len(relations),
            "enriched_definitions": enriched,
            "with_definition": sum(1 for e in entities if e["definition"]),
            "by_type": dict(Counter(e["type"] for e in entities).most_common()),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    st = result["stats"]
    logger.info(f"✅ Conv 完成：{out_path.resolve()}")
    logger.info(f"  原始记录 {st['raw_entity_records']} → 实体 {st['entities']} "
                f"（合并 {st['merged_groups']}）/ 关系 {st['relations']}")
    logger.info(f"  有定义 {st['with_definition']} / 补全 {enriched}")
    logger.info(f"  类型分布：{st['by_type']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v2 实体归并与定义补全")
    ap.add_argument("--input", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--dry-run", action="store_true", help="不调用 LLM，只做归并")
    ap.add_argument("--loglevel", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logger.setLevel(getattr(logging, args.loglevel))
    if args.dry_run:
        CFG["DRY_RUN"] = True
    return run(Path(args.input) if args.input else None,
               Path(args.output) if args.output else None,
               args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
