#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aggr v2 —— 分层（L1/L2）+ 稳定 ID + L2 概念归属。

层级不再由 LLM 拍板，而是 LayerPolicy 规则优先：
  1. 待定类型 → excluded（不入图，保留在待清洗池）
  2. 专有实例（工具/人物/组织/版本号/配置值）→ L2
  3. 定义 + 跨小节复用 → L1；跨小节复用 → L1；标题/定义句 → L1
  4. 规则未命中时才调用 LLM（LLM 不可用时按同一组信号启发式兜底）

L2 的父概念（instance_of）由共现、已有关系和类型偏好共同打分选出，
保证每个 L2 最多一个父节点；没有合适父节点时留空，不硬凑。

输入：conv_entities.json
输出：aggr_entities.json
{
  "schema_version": "2.0",
  "doc_id": "...",
  "entities": [{id,name,aliases,type,layer,parent_id,definition,confidence,
                signals,occurrences,neighbors,provenance,layer_decision}],
  "relations": [{source,target,source_name,target_name,type,layer,
                 description,keywords,confidence,provenance}],
  "excluded": [{name,type,reason}],
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
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import yaml

try:
    from .schema_loader import Schema, get_schema, make_node_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import Schema, get_schema, make_node_id

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(*args, **kwargs):  # type: ignore
        class _Null:
            def __iter__(self): return iter(args[0]) if args else iter(())
            def update(self, n=1): pass
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Null()

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
OUT_DIR = SCRIPT_DIR / "output"
SCHEMA = get_schema()

logger = logging.getLogger("Aggr")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

DEFAULTS: Dict[str, Any] = {
    "CONV_IN_NAME": "conv_entities.json",
    "OUT_NAME": "aggr_entities.json",
    "LIMIT": 0,
    "DRY_RUN": False,
    "WORKERS": 8,
    "TEMPERATURE": 0.0,
    "MAX_TOKENS": 300,
    "PROMPT_VERSION": "aggr.v2",
    "PROPER_INSTANCE_TYPES": ["工具", "人工物", "人物", "组织", "事件"],
    "PARENT_TYPE_PREFERENCE": {
        "工具": ["人工物", "概念", "方法", "算法"],
        "人工物": ["概念", "方法"],
        "人物": ["概念", "组织", "事件"],
        "组织": ["概念", "事件"],
        "事件": ["概念", "方法"],
        "指标": ["概念", "方法"],
        "属性": ["概念", "方法"],
        "任务": ["概念", "方法"],
        "算法": ["数学概念", "方法", "概念"],
        "智能体类型": ["概念", "方法"],
    },
    "PARENT_MIN_SCORE": 1.0,
    "INSTANCE_OF_CONFIDENCE": 0.6,
    "LLM_LAYER": True,
    "PROMPT_SYSTEM": "",
    "PROMPT_USER_TEMPLATE": "",
}
API_DEFAULTS: Dict[str, Any] = {
    "API_KEY": "", "API_BASE": "", "MODEL_NAME": "",
    "TIMEOUT_SECS": 120, "RETRIES": 3, "RETRY_BACKOFF_BASE": 2,
    "RATE_LIMIT_QPS": 0, "CHAT_COMPLETIONS_PATH": "/chat/completions",
}
VERSION_RE = re.compile(r"(?:^|[\s\-_])v?\d+(?:\.\d+)+", re.I)
FILE_RE = re.compile(r"\.(?:py|js|ts|json|ya?ml|md|txt|toml|ini|cfg)$", re.I)
CONFIG_RE = re.compile(r"^[a-z][a-z0-9_\-]*$")
MODEL_RE = re.compile(r"[A-Za-z]+[-\s]?\d+[A-Za-z0-9.\-]*")
PROPER_PROPER_RE = re.compile(r"(?:^[A-Z]{2,}[-_]\d+|^\d+(?:\.\d+)?$)")


# ---------------------------------------------------------------------------
# 配置 / 工具
# ---------------------------------------------------------------------------

def _load_yaml(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _load_config() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    cfg_path = CONFIG_DIR / "config.yaml"
    merged: Dict[str, Any] = {}
    if cfg_path.exists():
        merged = dict(_load_yaml(cfg_path))
        for rel in merged.get("include_files", []) or []:
            p = (CONFIG_DIR / str(rel)).resolve()
            if p.exists():
                merged.update(_load_yaml(p))
    if "AggrConfig" not in merged:
        merged.update(_load_yaml(CONFIG_DIR / "aggr.yaml"))
    cfg = dict(DEFAULTS)
    cfg.update(merged.get("AggrConfig") or {})
    cfg["DRY_RUN"] = _as_bool(cfg.get("DRY_RUN"))
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
    import requests

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
    for k in range(max(1, int(API["RETRIES"]))):
        try:
            _rate_limit()
            url = API["API_BASE"].rstrip("/") + API.get("CHAT_COMPLETIONS_PATH", "/chat/completions")
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=float(API["TIMEOUT_SECS"]))
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM 调用失败({k + 1}/{API['RETRIES']})：{e}")
            time.sleep(float(API["RETRY_BACKOFF_BASE"]) ** k)
    return ""


def _parse_layer(text: str) -> Optional[str]:
    for ln in (text or "").splitlines():
        t = ln.strip().lower().strip(" :：,，.。;；*\"'`")
        if not t:
            continue
        if t in ("l1", "concept", "概念"):
            return "L1"
        if t in ("l2", "entity", "实体"):
            return "L2"
        if t.startswith(("l1", "concept", "概念")):
            return "L1"
        if t.startswith(("l2", "entity", "实体")):
            return "L2"
        break
    m = re.search(r"\b(l1|l2)\b|(概念|实体)", (text or "").lower())
    if not m:
        return None
    tok = (m.group(1) or m.group(2) or "").lower()
    return "L1" if tok in ("l1", "概念") else "L2"


def _prompt(entity: Dict[str, Any]) -> str:
    tpl = CFG.get("PROMPT_USER_TEMPLATE") or ""
    top_neighbors = "、".join(n["name"] for n in (entity.get("neighbors") or [])[:8]) or "（无）"
    return (tpl
            .replace("{entity_name}", entity["name"])
            .replace("{entity_type}", entity["type"])
            .replace("{definition}", (entity.get("definition") or "（无）")[:300])
            .replace("{coverage}", str(entity["signals"].get("cross_section_coverage", 0)))
            .replace("{occurrences}", str(entity["signals"].get("occurrence_count", 0)))
            .replace("{neighbors}", top_neighbors))


# ---------------------------------------------------------------------------
# 分层
# ---------------------------------------------------------------------------

def detect_proper_instance(entity: Dict[str, Any]) -> bool:
    name = entity["name"].strip()
    if entity["type"] in set(CFG["PROPER_INSTANCE_TYPES"]):
        return True
    if VERSION_RE.search(name) or FILE_RE.search(name) or PROPER_PROPER_RE.search(name):
        return True
    if "=" in name or "/" in name or "\\" in name:
        return True
    if CONFIG_RE.match(name) and "_" in name:   # max_tokens / top_k
        return True
    if MODEL_RE.fullmatch(name) and re.search(r"\d", name):  # GPT-4 / Llama3
        return True
    return False


def _heuristic_layer(entity: Dict[str, Any]) -> Tuple[str, str]:
    s = entity["signals"]
    if detect_proper_instance(entity):
        return "L2", "heuristic:proper_instance"
    if s.get("cross_section_coverage", 0) >= 2 and (
            s.get("has_definition") or s.get("definitional_cue")):
        return "L1", "heuristic:defined_and_reused"
    if s.get("cross_section_coverage", 0) >= 2 and s.get("occurrence_count", 0) >= 2:
        return "L1", "heuristic:reused"
    if s.get("heading_presence") or s.get("definitional_cue") or s.get("has_definition"):
        return "L1", "heuristic:heading_or_definition"
    return "L2", "heuristic:default"


def decide_layers(entities: List[Dict[str, Any]], workers: int = 8) -> None:
    """原地写入 layer / layer_decision。"""
    pending: List[Dict[str, Any]] = []
    for e in entities:
        e["signals"]["proper_instance"] = detect_proper_instance(e)
        if e["type"] == "待定":
            e["layer"] = "excluded"
            e["layer_decision"] = {
                "rule": "unresolved", "confidence": 0.0,
                "method": "rule", "reason": "类型待定，LLM 也无法补全类型",
            }
            continue
        decision = SCHEMA.evaluate_layer(e["type"], e["signals"])
        if decision.needs_llm:
            pending.append(e)
            continue
        e["layer"] = decision.layer
        e["layer_decision"] = {
            "rule": decision.rule_id, "confidence": decision.confidence,
            "method": "rule",
        }

    if not pending:
        logger.info("分层：全部由规则命中，未调用 LLM")
        return

    if not CFG.get("LLM_LAYER") or CFG["DRY_RUN"] or not API.get("API_BASE"):
        for e in pending:
            layer, reason = _heuristic_layer(e)
            e["layer"] = layer
            e["layer_decision"] = {
                "rule": "default", "confidence": 0.5,
                "method": "fallback", "reason": reason,
            }
        logger.info(f"分层：{len(pending)} 个实体规则未命中，LLM 不可用，使用启发式兜底")
        return

    system = CFG.get("PROMPT_SYSTEM") or ""
    logger.info(f"分层：{len(pending)} 个实体规则未命中，调用 LLM 判定（并发 {workers}）")

    def work(e: Dict[str, Any]) -> Tuple[str, str, str]:
        layer = _parse_layer(call_llm(system, _prompt(e)))
        if layer:
            return e["name"], layer, "llm"
        layer, reason = _heuristic_layer(e)
        return e["name"], layer, f"fallback:{reason}"

    result: Dict[str, Tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(work, e) for e in pending]
        for fu in as_completed(futs):
            name, layer, method = fu.result()
            result[name] = (layer, method)

    for e in pending:
        layer, method = result.get(e["name"], ("L2", "fallback:missing"))
        e["layer"] = layer
        e["layer_decision"] = {
            "rule": "default",
            "confidence": 0.7 if method == "llm" else 0.5,
            "method": "llm" if method == "llm" else "fallback",
            "reason": "" if method == "llm" else method,
        }


# ---------------------------------------------------------------------------
# 关系 / 父概念
# ---------------------------------------------------------------------------

def _merge_relations(raw: List[Dict[str, Any]], by_name: Dict[str, Dict[str, Any]]
                     ) -> List[Dict[str, Any]]:
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in raw:
        src = by_name.get(r.get("source") or "")
        tgt = by_name.get(r.get("target") or "")
        rel = SCHEMA.canonical_relation(r.get("type") or "")
        if not src or not tgt or not rel or src["name"] == tgt["name"]:
            continue
        if src["layer"] == "excluded" or tgt["layer"] == "excluded":
            continue
        # example_of 规范方向 L2→L1；模型偶尔写反，翻过来
        if rel == "example_of" and src["layer"] == "L1" and tgt["layer"] == "L2":
            src, tgt = tgt, src
        if not SCHEMA.relation_allowed(rel, src["layer"], tgt["layer"]):
            continue
        if SCHEMA.is_symmetric(rel) and src["name"] > tgt["name"]:
            src, tgt = tgt, src
        key = (src["id"], tgt["id"], rel)
        if key not in out:
            out[key] = {
                "source": src["id"], "target": tgt["id"],
                "source_name": src["name"], "target_name": tgt["name"],
                "type": rel, "layer": f"{src['layer']}-{tgt['layer']}",
                "description": r.get("description") or "",
                "keywords": list(dict.fromkeys(r.get("keywords") or [])),
                "confidence": float(r.get("confidence", 0.5)),
                "provenance": list(r.get("provenance") or []),
            }
        else:
            cur = out[key]
            cur["keywords"] = list(dict.fromkeys(cur["keywords"] + (r.get("keywords") or [])))
            cur["confidence"] = max(cur["confidence"], float(r.get("confidence", 0.5)))
            cur["provenance"].extend(r.get("provenance") or [])
    return list(out.values())


def _parent_score(child: Dict[str, Any], parent: Dict[str, Any],
                  rel_pairs: Set[Tuple[str, str]],
                  sections: Dict[str, Set[str]]) -> float:
    score = 0.0
    if (child["name"], parent["name"]) in rel_pairs or (parent["name"], child["name"]) in rel_pairs:
        score += 8.0
    for nb in child.get("neighbors") or []:
        if nb["name"] == parent["name"]:
            score += 2.0 + float(nb.get("weight", 0))
            break
    c_secs, p_secs = sections.get(child["name"], set()), sections.get(parent["name"], set())
    score += 2.0 * len(c_secs & p_secs)
    prefs = CFG.get("PARENT_TYPE_PREFERENCE") or {}
    pref = list(prefs.get(child["type"]) or ["概念", "方法"])
    if parent["type"] in pref:
        score += 5.0 - 2.0 * pref.index(parent["type"])
    if parent.get("definition"):
        score += 0.5
    return score


def assign_parents(entities: List[Dict[str, Any]],
                   relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_name = {e["name"]: e for e in entities if e["layer"] != "excluded"}
    l1 = [e for e in by_name.values() if e["layer"] == "L1"]
    l2 = [e for e in by_name.values() if e["layer"] == "L2"]
    rel_pairs = {(r["source_name"], r["target_name"]) for r in relations}
    sections: Dict[str, Set[str]] = {
        e["name"]: {o["section_id"] for o in e.get("occurrences") or []}
        for e in by_name.values()
    }

    added: List[Dict[str, Any]] = []
    min_score = float(CFG["PARENT_MIN_SCORE"])
    for child in l2:
        best, best_score = None, min_score
        for parent in l1:
            if parent["name"] == child["name"]:
                continue
            s = _parent_score(child, parent, rel_pairs, sections)
            if s > best_score:
                best, best_score = parent, s
        if not best:
            continue
        child["parent_id"] = best["id"]
        conf = min(0.9, float(CFG["INSTANCE_OF_CONFIDENCE"]) + 0.02 * best_score)
        added.append({
            "source": child["id"], "target": best["id"],
            "source_name": child["name"], "target_name": best["name"],
            "type": "instance_of", "layer": "L2-L1",
            "description": f"{child['name']} 归属于 {best['name']}",
            "keywords": ["归属"],
            "confidence": round(conf, 2),
            "provenance": [{"section_id": o["section_id"], "extractor": "aggr.v2"}
                           for o in child.get("occurrences") or []][:5],
            "derived": True,
        })
    return added


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(in_path: Optional[Path] = None, out_path: Optional[Path] = None,
        dry_run: bool = False, limit: int = 0, workers: int = 8) -> int:
    in_path = in_path or (OUT_DIR / CFG["CONV_IN_NAME"])
    out_path = out_path or (OUT_DIR / CFG["OUT_NAME"])
    if not in_path.exists():
        raise FileNotFoundError(f"未找到输入：{in_path}")
    with in_path.open("r", encoding="utf-8") as f:
        conv = json.load(f)
    if isinstance(conv, list) or "entities" not in conv:
        raise ValueError("输入不是 v2 conv 产物（需要 {entities:[...]}）")

    if dry_run:
        CFG["DRY_RUN"] = True
    limit = limit or int(CFG.get("LIMIT") or 0)
    entities = [dict(e) for e in conv["entities"]]
    if limit > 0:
        entities = entities[:limit]

    for e in entities:
        e.setdefault("signals", {})
        e.setdefault("occurrences", [])
        e.setdefault("neighbors", [])
        e.setdefault("aliases", [])
        e.setdefault("provenance", [])
        e.setdefault("parent_id", "")

    decide_layers(entities, workers=workers)

    kept = [e for e in entities if e["layer"] in ("L1", "L2")]
    excluded = [e for e in entities if e["layer"] == "excluded"]
    for e in kept:
        e["id"] = make_node_id(e["name"], e["layer"])

    by_name = {e["name"]: e for e in kept}
    relations = _merge_relations(conv.get("relations") or [], by_name)
    relations.extend(assign_parents(kept, relations))

    # 度数信号
    degree: Dict[str, int] = defaultdict(int)
    for r in relations:
        degree[r["source"]] += 1
        degree[r["target"]] += 1
    for e in kept:
        e["signals"]["degree"] = degree.get(e["id"], 0)

    result = {
        "schema_version": SCHEMA.version,
        "doc_id": conv.get("doc_id") or "doc",
        "entities": kept,
        "relations": relations,
        "excluded": [{
            "name": e["name"], "type": e["type"],
            "reason": (e.get("layer_decision") or {}).get("reason", "待定类型"),
        } for e in excluded],
        "stats": {
            "input_entities": len(entities),
            "concepts_L1": sum(1 for e in kept if e["layer"] == "L1"),
            "entities_L2": sum(1 for e in kept if e["layer"] == "L2"),
            "excluded": len(excluded),
            "with_parent": sum(1 for e in kept if e["layer"] == "L2" and e.get("parent_id")),
            "relations": len(relations),
            "relations_by_type": dict(Counter(r["type"] for r in relations).most_common()),
            "layer_method": dict(Counter(
                (e.get("layer_decision") or {}).get("method", "?") for e in kept).most_common()),
            "by_type": dict(Counter(e["type"] for e in kept).most_common()),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    st = result["stats"]
    logger.info(f"✅ Aggr 完成：{out_path.resolve()}")
    logger.info(f"  L1 {st['concepts_L1']} / L2 {st['entities_L2']} / excluded {st['excluded']}")
    logger.info(f"  L2 有父概念 {st['with_parent']} / 关系 {st['relations']} {st['relations_by_type']}")
    logger.info(f"  分层方式：{st['layer_method']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v2 分层与概念归属")
    ap.add_argument("--input", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=int(CFG.get("WORKERS") or 8))
    ap.add_argument("--loglevel", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logger.setLevel(getattr(logging, args.loglevel))
    if args.dry_run:
        CFG["DRY_RUN"] = True
    return run(Path(args.input) if args.input else None,
               Path(args.output) if args.output else None,
               args.dry_run, args.limit, args.workers)


if __name__ == "__main__":
    sys.exit(main())
