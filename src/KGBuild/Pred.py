#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pred v2 —— 在去重后的图上补边（拓扑候选 + 向量相似 + LLM 复核）。

原则：
  * 不重复已有边（同一对节点只保留最强的一条语义关系）
  * L1-L1 / L2-L2 只能使用 same_layer 关系；L2-L1 只补 instance_of
  * L2 已经有父概念时不再补 instance_of
  * LLM 不可用时只用高置信启发式，宁可少补
  * 所有新增边都写 confidence / method / section_ids，Final 阶段据此接证据

输入：dedup_result.json + node_embeddings.pkl
输出：pred_result.json
{
  "schema_version": "2.0", "doc_id": "...",
  "predictions": [
    {"source","target","source_name","target_name","type","layer",
     "confidence","method","description","keywords","section_ids","provenance"}
  ],
  "stats": {...}
}
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import yaml

try:
    from .schema_loader import Schema, get_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import Schema, get_schema

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
OUT_DIR = SCRIPT_DIR / "output"
SCHEMA = get_schema()

logger = logging.getLogger("Pred")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

DEFAULTS: Dict[str, Any] = {
    "DEDUP_NAME": "dedup_result.json",
    "EMBEDDINGS_NAME": "node_embeddings.pkl",
    "RESULT_NAME": "pred_result.json",
    "MIN_SIM": 0.62,
    "MIN_WEIGHT": 2,
    "MIN_SCORE": 0.55,
    "MAX_PREDICTIONS_PER_NODE": 4,
    "MAX_TOTAL": 500,
    "LLM_PREDICT": True,
    "LLM_MAX_PAIRS": 80,
    "LLM_MIN_CONFIDENCE": 0.5,
    "DRY_RUN": False,
    "TEMPERATURE": 0.2,
    "MAX_TOKENS": 300,
    "PROMPT_VERSION": "pred.v2",
    "PROMPT_SYSTEM": "",
    "PROMPT_USER_TEMPLATE": "",
}
API_DEFAULTS: Dict[str, Any] = {
    "API_KEY": "", "API_BASE": "", "MODEL_NAME": "",
    "TIMEOUT_SECS": 120, "RETRIES": 3, "RETRY_BACKOFF_BASE": 2,
    "RATE_LIMIT_QPS": 0, "CHAT_COMPLETIONS_PATH": "/chat/completions",
    "WORKERS": 8,
}
SEMANTIC_TYPES = ["part_of", "prerequisite", "applies_to",
                  "synonym", "contrasts_with", "related"]

# ---------------------------------------------------------------------------
# 配置 / LLM
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
    if "PredConfig" not in merged:
        merged.update(_load_yaml(CONFIG_DIR / "pred.yaml"))
    cfg = dict(DEFAULTS)
    cfg.update(merged.get("PredConfig") or {})
    cfg["DRY_RUN"] = _as_bool(cfg.get("DRY_RUN"))
    cfg["LLM_PREDICT"] = _as_bool(cfg.get("LLM_PREDICT"))
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


def _json_obj(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}


def _prompt(a: Dict[str, Any], b: Dict[str, Any], sim: float, shared: int,
            weight: int, allowed: List[str]) -> str:
    tpl = CFG.get("PROMPT_USER_TEMPLATE") or ""
    repl = {
        "{name_a}": a["name"], "{type_a}": a.get("type", ""),
        "{layer_a}": a.get("layer", ""), "{desc_a}": (a.get("definition") or "（无）")[:300],
        "{name_b}": b["name"], "{type_b}": b.get("type", ""),
        "{layer_b}": b.get("layer", ""), "{desc_b}": (b.get("definition") or "（无）")[:300],
        "{sim}": f"{sim:.3f}", "{shared}": str(shared), "{weight}": str(weight),
        "{allowed}": " / ".join(allowed),
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl


# ---------------------------------------------------------------------------
# 候选
# ---------------------------------------------------------------------------

def _load_embeddings(path: Path) -> Dict[str, np.ndarray]:
    with path.open("rb") as f:
        raw = pickle.load(f) or {}
    out: Dict[str, np.ndarray] = {}
    for k, v in raw.items():
        if isinstance(v, np.ndarray) and v.size:
            vec = v.astype(np.float32).reshape(-1)
            out[str(k)] = vec / max(float(np.linalg.norm(vec)), 1e-12)
    return out


def _sections(e: Dict[str, Any]) -> Set[str]:
    return {str(o.get("section_id")) for o in e.get("occurrences") or []
            if o.get("section_id") is not None}


def _allowed_types(layer_a: str, layer_b: str) -> List[str]:
    if {layer_a, layer_b} == {"L1", "L2"}:
        return ["instance_of"]
    if layer_a == layer_b and layer_a in ("L1", "L2"):
        return list(SEMANTIC_TYPES)
    return []


def _candidate_pairs(entities: List[Dict[str, Any]], embeddings: Dict[str, np.ndarray]
                     ) -> List[Dict[str, Any]]:
    by_name = {e["name"]: e for e in entities}
    by_id = {str(e["id"]): e for e in entities}
    sections = {e["name"]: _sections(e) for e in entities}
    seen: Set[Tuple[str, str]] = set()
    rows: List[Dict[str, Any]] = []

    for e in entities:
        for nb in e.get("neighbors") or []:
            other = by_name.get(nb.get("name") or "")
            if not other or other["id"] == e["id"]:
                continue
            key = tuple(sorted((str(e["id"]), str(other["id"]))))
            if key in seen:
                continue
            seen.add(key)
            a, b = (e, other) if str(e["id"]) < str(other["id"]) else (other, e)
            allowed = _allowed_types(a.get("layer", ""), b.get("layer", ""))
            if not allowed:
                continue

            va, vb = embeddings.get(str(a["id"])), embeddings.get(str(b["id"]))
            sim = float(np.dot(va, vb)) if va is not None and vb is not None else 0.0
            shared = len(sections[a["name"]] & sections[b["name"]])
            weight = int(nb.get("weight", 0))

            if weight < int(CFG["MIN_WEIGHT"]) and sim < float(CFG["MIN_SIM"]):
                continue
            if sim < float(CFG["MIN_SIM"]) and weight < int(CFG["MIN_WEIGHT"]) + 2:
                continue

            score = (0.6 * sim
                     + 0.25 * min(weight, 5) / 5.0
                     + 0.15 * min(shared, 3) / 3.0)
            if score < float(CFG["MIN_SCORE"]):
                continue
            rows.append({
                "a": a, "b": b, "sim": sim, "shared": shared,
                "weight": weight, "score": score,
                "allowed": allowed, "neighbor_relation": nb.get("relation") or "",
            })
    return rows


def _heuristic(a: Dict[str, Any], b: Dict[str, Any], sim: float,
               weight: int, shared: int) -> Optional[Dict[str, Any]]:
    if {a.get("layer"), b.get("layer")} == {"L1", "L2"}:
        child, parent = (a, b) if a.get("layer") == "L2" else (b, a)
        return {
            "source": child["id"], "target": parent["id"],
            "type": "instance_of", "confidence": 0.55,
            "description": f"{child['name']} 归属于 {parent['name']}",
            "keywords": ["归属"],
        }
    if a.get("layer") != b.get("layer"):
        return None
    methods = {"方法", "算法", "工具", "提示词", "智能体类型"}
    ta, tb = a.get("type", ""), b.get("type", "")
    src, tgt = (a, b) if ta in methods and tb not in methods else (b, a)
    if (ta in methods) != (tb in methods):
        rtype = "applies_to"
    else:
        rtype = "related"
    conf = min(0.75, 0.45 + 0.15 * sim + 0.02 * weight + 0.03 * shared)
    if conf < float(SCHEMA.get_relation(rtype).min_confidence if SCHEMA.get_relation(rtype) else 0.5):
        return None
    return {
        "source": src["id"], "target": tgt["id"],
        "type": rtype, "confidence": round(conf, 2),
        "description": (f"{src['name']} 与 {tgt['name']} 在原文中共同出现"
                        if rtype == "related" else f"{src['name']} 用于 {tgt['name']}"),
        "keywords": ["共现"] if rtype == "related" else ["应用"],
    }


def _llm_judge(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    a, b = row["a"], row["b"]
    allowed = row["allowed"]
    system = CFG.get("PROMPT_SYSTEM") or ""
    text = call_llm(system, _prompt(a, b, row["sim"], row["shared"],
                                   row["weight"], allowed))
    obj = _json_obj(text)
    if not obj:
        return None
    rel = SCHEMA.canonical_relation(str(obj.get("type") or ""))
    if rel not in allowed:
        return None
    try:
        conf = float(obj.get("confidence", 0.6))
    except (TypeError, ValueError):
        conf = 0.6
    conf = max(0.0, min(1.0, conf))
    if conf < float(CFG["LLM_MIN_CONFIDENCE"]):
        return None
    # 方向：instance_of 必须是 L2→L1；其它同层关系按 LLM 的 source 语义
    if rel == "instance_of":
        src, tgt = (a, b) if a.get("layer") == "L2" else (b, a)
    elif rel == "applies_to" and obj.get("direction") == "ba":
        src, tgt = b, a
    else:
        src, tgt = a, b
    return {
        "source": src["id"], "target": tgt["id"],
        "type": rel, "confidence": round(conf, 2),
        "description": str(obj.get("description") or "")[:120],
        "keywords": [str(x)[:20] for x in (obj.get("keywords") or [])][:5],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(dedup_path: Optional[Path] = None, emb_path: Optional[Path] = None,
        out_path: Optional[Path] = None, dry_run: bool = False,
        workers: int = 8) -> int:
    dedup_path = dedup_path or (OUT_DIR / CFG["DEDUP_NAME"])
    emb_path = emb_path or (OUT_DIR / CFG["EMBEDDINGS_NAME"])
    out_path = out_path or (OUT_DIR / CFG["RESULT_NAME"])
    if not dedup_path.exists():
        raise FileNotFoundError(f"未找到去重结果：{dedup_path}")
    if not emb_path.exists():
        raise FileNotFoundError(f"未找到嵌入文件：{emb_path}")
    if dry_run:
        CFG["DRY_RUN"] = True

    with dedup_path.open("r", encoding="utf-8") as f:
        dedup = json.load(f)
    if isinstance(dedup, list) or "entities" not in dedup:
        raise ValueError("输入不是 v2 dedup 产物（需要 {entities:[...]}）")
    entities = dedup["entities"]
    embeddings = _load_embeddings(emb_path)
    by_id = {str(e["id"]): e for e in entities}

    existing: Set[Tuple[str, str]] = set()
    parent_of: Set[str] = set()
    for r in dedup.get("relations") or []:
        s, t = str(r.get("source", "")), str(r.get("target", ""))
        existing.add(tuple(sorted((s, t))))
        if r.get("type") == "instance_of":
            parent_of.add(s)
        if r.get("type") == "example_of":
            parent_of.add(s)

    rows = _candidate_pairs(entities, embeddings)
    rows = [r for r in rows
            if tuple(sorted((str(r["a"]["id"]), str(r["b"]["id"])))) not in existing]
    rows.sort(key=lambda r: (-r["score"], -r["sim"]))

    # L2 已有父概念 → 不再补 instance_of
    def needs_parent(row: Dict[str, Any]) -> bool:
        if row["allowed"] != ["instance_of"]:
            return True
        child = row["a"] if row["a"].get("layer") == "L2" else row["b"]
        return str(child["id"]) not in parent_of

    rows = [r for r in rows if needs_parent(r)]

    use_llm = bool(CFG.get("LLM_PREDICT")) and not CFG["DRY_RUN"] and bool(API.get("API_BASE"))
    llm_rows = rows[: int(CFG["LLM_MAX_PAIRS"])] if use_llm else []
    llm_keys = {(str(r["a"]["id"]), str(r["b"]["id"])) for r in llm_rows}

    # 先跑 LLM 候选（并发），其余用启发式
    judged: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
    if llm_rows:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(_llm_judge, r): r for r in llm_rows}
            for fu in as_completed(futs):
                r = futs[fu]
                try:
                    judged[(str(r["a"]["id"]), str(r["b"]["id"]))] = fu.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"LLM 判定失败：{e}")
                    judged[(str(r["a"]["id"]), str(r["b"]["id"]))] = None

    predictions: List[Dict[str, Any]] = []
    per_node: Dict[str, int] = defaultdict(int)
    cap = int(CFG["MAX_PREDICTIONS_PER_NODE"])
    for r in rows:
        key = (str(r["a"]["id"]), str(r["b"]["id"]))
        if key in llm_keys:
            pred = judged.get(key)
            method = "llm"
        else:
            pred = _heuristic(r["a"], r["b"], r["sim"], r["weight"], r["shared"])
            method = "heuristic"
        if not pred:
            continue
        src, tgt = str(pred["source"]), str(pred["target"])
        if src not in by_id or tgt not in by_id:
            continue
        layer = f"{by_id[src]['layer']}-{by_id[tgt]['layer']}"
        rel = pred["type"]
        if not SCHEMA.relation_allowed(rel, by_id[src]["layer"], by_id[tgt]["layer"]):
            continue
        if SCHEMA.is_symmetric(rel) and src > tgt:
            src, tgt = tgt, src
        if cap > 0 and (per_node[src] >= cap or per_node[tgt] >= cap):
            continue
        # 同层兜底关系 confidence 不得低于 schema 阈值
        rt = SCHEMA.get_relation(rel)
        if rt and rt.is_fallback and pred["confidence"] < rt.min_confidence:
            continue

        per_node[src] += 1
        per_node[tgt] += 1
        secs = sorted(_sections(by_id[src]) & _sections(by_id[tgt]))
        predictions.append({
            "source": src, "target": tgt,
            "source_name": by_id[src]["name"], "target_name": by_id[tgt]["name"],
            "type": rel, "layer": layer,
            "confidence": pred["confidence"],
            "method": method,
            "description": pred.get("description") or "",
            "keywords": pred.get("keywords") or [],
            "section_ids": secs,
            "provenance": [{
                "section_id": sid, "extractor": f"pred.v2:{method}",
                "prompt_version": CFG.get("PROMPT_VERSION", "pred.v2"),
            } for sid in secs],
        })
        if len(predictions) >= int(CFG["MAX_TOTAL"]):
            break

    result = {
        "schema_version": SCHEMA.version,
        "doc_id": dedup.get("doc_id") or "doc",
        "predictions": predictions,
        "stats": {
            "candidates": len(rows),
            "llm_judged": len(llm_rows),
            "predictions": len(predictions),
            "by_method": dict(Counter(p["method"] for p in predictions).most_common()),
            "by_type": dict(Counter(p["type"] for p in predictions).most_common()),
            "by_layer": dict(Counter(p["layer"] for p in predictions).most_common()),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    st = result["stats"]
    logger.info(f"✅ Pred 完成：{out_path.resolve()}")
    logger.info(f"  候选 {st['candidates']} / LLM {st['llm_judged']} / "
                f"新增边 {st['predictions']}")
    logger.info(f"  方式 {st['by_method']} / 类型 {st['by_type']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v2 关系补全")
    ap.add_argument("--dedup", default=None)
    ap.add_argument("--emb", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=int(API.get("WORKERS") or 8))
    ap.add_argument("--loglevel", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logger.setLevel(getattr(logging, args.loglevel))
    return run(Path(args.dedup) if args.dedup else None,
               Path(args.emb) if args.emb else None,
               Path(args.out) if args.out else None,
               args.dry_run, args.workers)


if __name__ == "__main__":
    sys.exit(main())
