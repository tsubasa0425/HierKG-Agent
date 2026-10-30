#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dedup v2 —— 同层同类型节点去重（别名快速通道 + 向量近邻 + LLM 复核）。

约束：
  * 只在同一 layer（L1/L2）内合并，绝不把 L1 概念并进 L2 实例
  * 默认要求类型一致；别名明确交集时可放宽（类型冲突以代表节点为准）
  * 代表节点保留自己的 id/name，因此 ID 始终满足 concept_/ent_ 前缀与名称哈希规则
  * 合并后重定向 relations / neighbors，并输出 id_map 供下游使用

输入：aggr_entities.json + node_embeddings.pkl（{node_id: vector}）
输出：dedup_result.json
{
  "schema_version": "2.0", "doc_id": "...",
  "entities": [...], "relations": [...],
  "clusters": [...], "merge_map": {...}, "id_map": {...},
  "decisions": [...], "stats": {...}
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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np
import yaml

try:
    from .schema_loader import get_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import get_schema

try:
    from .dedup.knn import knn_topk
    from .dedup.name_similarity import _name_jaccard_mode
except ImportError:
    from KGBuild.dedup.knn import knn_topk
    from KGBuild.dedup.name_similarity import _name_jaccard_mode

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
OUT_DIR = SCRIPT_DIR / "output"
SCHEMA = get_schema()

logger = logging.getLogger("Dedup")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

DEFAULTS: Dict[str, Any] = {
    "ENTITIES_AGGR_NAME": "aggr_entities.json",
    "EMBEDDINGS_NAME": "node_embeddings.pkl",
    "RESULT_NAME": "dedup_result.json",
    "KNN_NEIGHBORS": 15,
    "SIM_THRESHOLD": 0.86,
    "AUTO_MERGE_SIM": 0.95,
    "NO_LLM_MERGE_SIM": 0.92,
    "LLM_MERGE": True,
    "MUTUAL_KNN": True,
    "STRICT_SAME_TYPE": True,
    "USE_ALIAS_FASTPATH": True,
    "USE_NAME_JACCARD": False,
    "NAME_JACCARD_MIN": 0.2,
    "NAME_CONTAINMENT_GUARD": True,
    "NAME_MODE": "auto",
    "PER_NODE_CAP": 20,
    "DRY_RUN": False,
    "TEMPERATURE": 0.0,
    "MAX_TOKENS": 200,
    "PROMPT_VERSION": "dedup.v2",
    "LOG_DECISIONS_MAX": 5000,
    "LLM_SYSTEM_PROMPT": "",
    "LLM_USER_PROMPT": "",
}
API_DEFAULTS: Dict[str, Any] = {
    "API_KEY": "", "API_BASE": "", "MODEL_NAME": "",
    "TIMEOUT_SECS": 120, "RETRIES": 3, "RETRY_BACKOFF_BASE": 2,
    "RATE_LIMIT_QPS": 0, "CHAT_COMPLETIONS_PATH": "/chat/completions",
    "WORKERS": 8,
}
DEFINITION_CUES = ("是指", "定义为", "称为", "是一种", "指的是", "所谓", "即")


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
    if "DedupConfig" not in merged:
        merged.update(_load_yaml(CONFIG_DIR / "dedup.yaml"))
    cfg = dict(DEFAULTS)
    cfg.update(merged.get("DedupConfig") or {})
    cfg["DRY_RUN"] = _as_bool(cfg.get("DRY_RUN"))
    cfg["LLM_MERGE"] = _as_bool(cfg.get("LLM_MERGE"))
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


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    return re.sub(r"\s+", "", s)


def _variants(e: Dict[str, Any]) -> Set[str]:
    return {_norm(x) for x in [e.get("name", "")] + list(e.get("aliases") or []) if _norm(x)}


class _DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


# ---------------------------------------------------------------------------
# LLM 判定
# ---------------------------------------------------------------------------

def _prompt(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    tpl = CFG.get("LLM_USER_PROMPT") or ""
    repl = {
        "{name_a}": a.get("name", ""),
        "{type_a}": a.get("type", ""),
        "{layer_a}": a.get("layer", ""),
        "{alias_a}": "、".join(a.get("aliases") or []) or "（无）",
        "{desc_a}": (a.get("definition") or "（无定义）")[:300],
        "{name_b}": b.get("name", ""),
        "{type_b}": b.get("type", ""),
        "{layer_b}": b.get("layer", ""),
        "{alias_b}": "、".join(b.get("aliases") or []) or "（无）",
        "{desc_b}": (b.get("definition") or "（无定义）")[:300],
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl


def _parse_same(text: str) -> Optional[bool]:
    if not text:
        return None
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(obj, dict) or "is_same" not in obj:
        return None
    return bool(obj["is_same"])


def judge_pairs(pairs: List[Tuple[int, int, float]],
                ents: List[Dict[str, Any]],
                workers: int) -> Dict[Tuple[int, int], Tuple[bool, str]]:
    if not pairs:
        return {}
    if not CFG.get("LLM_MERGE") or CFG["DRY_RUN"] or not API.get("API_BASE"):
        out: Dict[Tuple[int, int], Tuple[bool, str]] = {}
        for i, j, sim in pairs:
            ok = sim >= float(CFG["NO_LLM_MERGE_SIM"])
            out[(i, j)] = (ok, "embedding_threshold" if ok else f"embedding_only:{sim:.3f}")
        return out

    system = CFG.get("LLM_SYSTEM_PROMPT") or ""
    out = {}

    def work(i: int, j: int) -> Tuple[Tuple[int, int], Tuple[bool, str]]:
        same = _parse_same(call_llm(system, _prompt(ents[i], ents[j])))
        if same is None:
            return (i, j), (False, "llm_unparsed")
        return (i, j), (same, "llm")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(work, i, j) for i, j, _ in pairs]
        for fu in as_completed(futs):
            key, val = fu.result()
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def _def_score(e: Dict[str, Any]) -> Tuple[int, int, int]:
    d = e.get("definition") or ""
    return (int(any(c in d for c in DEFINITION_CUES)), len(d), 1 if d else 0)


def _rep_key(e: Dict[str, Any]) -> Tuple[int, int, int, int]:
    sig = e.get("signals") or {}
    return (
        int(sig.get("cross_section_coverage", 0)),
        _def_score(e)[1],
        int(sig.get("occurrence_count", 0)),
        -len(e.get("name") or ""),
    )


def _containment_guard(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """名称存在包含关系时视为上下位，不是同一实体（除非别名明确相交）。"""
    na, nb = _norm(a.get("name")), _norm(b.get("name"))
    if not na or not nb or na == nb:
        return False
    if na not in nb and nb not in na:
        return False
    return not (_variants(a) & _variants(b))


def _cluster_conflict(dsu: "_DSU", ents: List[Dict[str, Any]], i: int, j: int) -> bool:
    """两个待合并簇里只要存在一对上下位名称，就拒绝合并。"""
    ri, rj = dsu.find(i), dsu.find(j)
    if ri == rj:
        return False
    left = [x for x in range(len(ents)) if dsu.find(x) == ri]
    right = [x for x in range(len(ents)) if dsu.find(x) == rj]
    return any(_containment_guard(ents[a], ents[b]) for a in left for b in right)


def _merge_cluster(members: List[Dict[str, Any]], name_map: Dict[str, str]
                   ) -> Dict[str, Any]:
    if len(members) == 1:
        return members[0]
    rep_src = max(members, key=_rep_key)
    rep = dict(rep_src)
    rep_name = rep["name"]

    aliases: Set[str] = set(rep.get("aliases") or [])
    for m in members:
        if m is not rep_src:
            aliases.add(m["name"])
            aliases.update(m.get("aliases") or [])
    aliases.discard(rep_name)
    rep["aliases"] = sorted(a for a in aliases if a)

    best = max(members, key=_def_score)
    if best.get("definition"):
        rep["definition"] = best["definition"]
    rep["confidence"] = round(max(float(m.get("confidence", 0)) for m in members), 2)

    occ: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in members:
        for o in m.get("occurrences") or []:
            k = (str(o.get("section_id", "")), str(o.get("block_id", "")))
            if k not in occ:
                occ[k] = dict(o)
            else:
                occ[k]["mention_count"] = max(
                    int(occ[k].get("mention_count", 1)),
                    int(o.get("mention_count", 1)))
                occ[k]["definitional_cue"] = bool(
                    occ[k].get("definitional_cue") or o.get("definitional_cue"))
    rep["occurrences"] = sorted(occ.values(), key=lambda o: (
        str(o.get("section_id", "")), str(o.get("block_id", ""))))

    prov: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for m in members:
        for p in m.get("provenance") or []:
            k = json.dumps(p, ensure_ascii=False, sort_keys=True)
            if k not in seen:
                seen.add(k)
                prov.append(p)
    rep["provenance"] = prov

    rep["merged_from"] = [
        {"id": m.get("id", ""), "name": m.get("name", ""), "type": m.get("type", "")}
        for m in members if m is not rep_src
    ]

    sig = dict(rep.get("signals") or {})
    sig["cross_section_coverage"] = len({o.get("section_id") for o in rep["occurrences"]})
    sig["occurrence_count"] = sum(int(o.get("mention_count", 1)) for o in rep["occurrences"])
    sig["has_definition"] = bool(rep.get("definition"))
    sig["definitional_cue"] = any(c in (rep.get("definition") or "") for c in DEFINITION_CUES)
    sig["heading_presence"] = any(m.get("signals", {}).get("heading_presence") for m in members)
    rep["signals"] = sig
    return rep


def _merge_neighbors(rep: Dict[str, Any], members: List[Dict[str, Any]],
                     cluster_names: Set[str], name_map: Dict[str, str]) -> None:
    merged: Dict[str, Dict[str, Any]] = {}
    for m in members:
        for nb in m.get("neighbors") or []:
            target = name_map.get(nb.get("name", ""), nb.get("name", ""))
            if not target or target in cluster_names:
                continue
            cur = merged.get(target)
            if not cur:
                merged[target] = {
                    "name": target,
                    "weight": int(nb.get("weight", 0)),
                    "relation": nb.get("relation") or "",
                    "section_id": nb.get("section_id") or "",
                    "section_title": nb.get("section_title") or "",
                }
            else:
                cur["weight"] = max(cur["weight"], int(nb.get("weight", 0)))
                if not cur["relation"] and nb.get("relation"):
                    cur["relation"] = nb["relation"]
    rep["neighbors"] = sorted(merged.values(), key=lambda x: (-x["weight"], x["name"]))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _load_embeddings(path: Path) -> Dict[str, np.ndarray]:
    with path.open("rb") as f:
        raw = pickle.load(f)
    out: Dict[str, np.ndarray] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, np.ndarray) and v.size:
            vec = v.astype(np.float32).reshape(-1)
            out[str(k)] = vec / max(float(np.linalg.norm(vec)), 1e-12)
    return out


def run(in_path: Optional[Path] = None, emb_path: Optional[Path] = None,
        out_path: Optional[Path] = None, dry_run: bool = False,
        workers: int = 8) -> int:
    in_path = in_path or (OUT_DIR / CFG["ENTITIES_AGGR_NAME"])
    emb_path = emb_path or (OUT_DIR / CFG["EMBEDDINGS_NAME"])
    out_path = out_path or (OUT_DIR / CFG["RESULT_NAME"])
    if not in_path.exists():
        raise FileNotFoundError(f"未找到实体文件：{in_path}")
    if not emb_path.exists():
        raise FileNotFoundError(f"未找到嵌入文件：{emb_path}")
    if dry_run:
        CFG["DRY_RUN"] = True

    with in_path.open("r", encoding="utf-8") as f:
        aggr = json.load(f)
    if isinstance(aggr, list) or "entities" not in aggr:
        raise ValueError("输入不是 v2 aggr 产物（需要 {entities:[...]}）")
    entities = [dict(e) for e in aggr["entities"]]
    embeddings = _load_embeddings(emb_path)

    n = len(entities)
    dsu = _DSU(n)
    methods: Dict[Tuple[int, int], str] = {}

    # —— 1. 别名快速合并（同层） —— #
    alias_merges = 0
    if CFG.get("USE_ALIAS_FASTPATH"):
        buckets: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for i, e in enumerate(entities):
            for v in _variants(e):
                buckets[(e.get("layer", ""), v)].append(i)
        for (_, _), idxs in buckets.items():
            if len(idxs) < 2:
                continue
            base = idxs[0]
            for j in idxs[1:]:
                if dsu.union(base, j):
                    alias_merges += 1
                    methods[(min(base, j), max(base, j))] = "alias"

    # —— 2. 向量近邻候选 —— #
    vecs = np.array([embeddings.get(str(e.get("id", ""))) for e in entities], dtype=object)
    candidates: List[Tuple[int, int, float]] = []
    rej = Counter()
    per_node = [0] * n
    auto_merges = 0

    for layer in ("L1", "L2"):
        idxs = [i for i, e in enumerate(entities)
                if e.get("layer") == layer and vecs[i] is not None]
        if len(idxs) < 2:
            continue
        mat = np.vstack([vecs[i] for i in idxs]).astype(np.float32)
        mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12)
        k = min(len(idxs), int(CFG["KNN_NEIGHBORS"]) + 1)
        _, nbr = knn_topk(mat, k)
        nbr_sets = [set(int(x) for x in row) for row in nbr]

        seen_pairs: Set[Tuple[int, int]] = set()
        for a in range(len(idxs)):
            for b in nbr[a]:
                b = int(b)
                if b == a:
                    continue
                ai, bi = idxs[a], idxs[b]
                lo, hi = (ai, bi) if ai < bi else (bi, ai)
                if (lo, hi) in seen_pairs:
                    continue
                seen_pairs.add((lo, hi))

                if CFG.get("MUTUAL_KNN") and b not in nbr_sets[a]:
                    rej["mutual"] += 1
                    continue
                sim = float(np.dot(mat[a], mat[b]))
                if sim < float(CFG["SIM_THRESHOLD"]):
                    rej["sim"] += 1
                    continue
                ea, eb = entities[lo], entities[hi]
                if CFG.get("STRICT_SAME_TYPE") and ea.get("type") != eb.get("type") \
                        and "待定" not in (ea.get("type"), eb.get("type")):
                    rej["type"] += 1
                    continue
                if CFG.get("NAME_CONTAINMENT_GUARD") and _cluster_conflict(dsu, entities, lo, hi):
                    rej["containment"] += 1
                    continue
                if CFG.get("USE_NAME_JACCARD") and sim < float(CFG["AUTO_MERGE_SIM"]):
                    jac = _name_jaccard_mode(ea["name"], eb["name"], str(CFG["NAME_MODE"]))
                    if jac < float(CFG["NAME_JACCARD_MIN"]):
                        rej["name"] += 1
                        continue
                cap = int(CFG["PER_NODE_CAP"])
                if cap > 0 and (per_node[lo] >= cap or per_node[hi] >= cap):
                    rej["cap"] += 1
                    continue
                per_node[lo] += 1
                per_node[hi] += 1

                if sim >= float(CFG["AUTO_MERGE_SIM"]):
                    if dsu.union(lo, hi):
                        auto_merges += 1
                        methods[(lo, hi)] = "auto"
                else:
                    candidates.append((lo, hi, sim))

    # 去掉已被别名/高相似度合并的候选
    candidates = [(i, j, s) for i, j, s in candidates if dsu.find(i) != dsu.find(j)]

    # —— 3. LLM 复核 —— #
    llm_pairs = judge_pairs(candidates, entities, workers)
    llm_merges = 0
    decisions: List[Dict[str, Any]] = []
    for (i, j, sim) in sorted(candidates, key=lambda x: -x[2]):
        same, reason = llm_pairs.get((i, j), (False, "not_judged"))
        decisions.append({
            "a": entities[i]["name"], "b": entities[j]["name"],
            "sim": round(float(sim), 4), "is_same": bool(same), "reason": reason,
        })
        if same and dsu.union(i, j):
            llm_merges += 1
            methods[(i, j)] = "llm"

    # —— 4. 聚类 / 合并 —— #
    clusters_idx: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        clusters_idx[dsu.find(i)].append(i)

    name_map: Dict[str, str] = {}
    id_map: Dict[str, str] = {}
    out_entities: List[Dict[str, Any]] = []
    cluster_records: List[Dict[str, Any]] = []

    # 先算 name_map，邻居重定向需要完整映射
    for members in clusters_idx.values():
        rep = max((entities[i] for i in members), key=_rep_key)
        for i in members:
            name_map[entities[i]["name"]] = rep["name"]
            if entities[i].get("id"):
                id_map[str(entities[i]["id"])] = str(rep.get("id", ""))

    for members in clusters_idx.values():
        group = [entities[i] for i in members]
        rep = _merge_cluster(group, name_map)
        cluster_names = {name_map.get(m["name"], m["name"]) for m in group}
        _merge_neighbors(rep, group, cluster_names, name_map)
        out_entities.append(rep)
        if len(group) > 1:
            cluster_records.append({
                "layer": rep.get("layer", ""),
                "representative": rep["name"],
                "members": [m["name"] for m in group],
            })

    # —— 5. 关系重定向 —— #
    rel_out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in aggr.get("relations") or []:
        src = id_map.get(str(r.get("source", "")), str(r.get("source", "")))
        tgt = id_map.get(str(r.get("target", "")), str(r.get("target", "")))
        if not src or not tgt or src == tgt:
            continue
        key = (src, tgt, r.get("type", ""))
        if key not in rel_out:
            row = dict(r)
            row["source"], row["target"] = src, tgt
            row["source_name"] = name_map.get(r.get("source_name", ""), r.get("source_name", ""))
            row["target_name"] = name_map.get(r.get("target_name", ""), r.get("target_name", ""))
            rel_out[key] = row
        else:
            cur = rel_out[key]
            cur["confidence"] = max(float(cur.get("confidence", 0)),
                                    float(r.get("confidence", 0)))
            cur["provenance"] = list(cur.get("provenance") or []) + list(r.get("provenance") or [])

    # 度数重算
    degree: Dict[str, int] = defaultdict(int)
    for (src, tgt, _) in rel_out:
        degree[src] += 1
        degree[tgt] += 1
    for e in out_entities:
        e["signals"]["degree"] = degree.get(str(e.get("id")), 0)

    out_entities.sort(key=lambda e: (e.get("layer", ""), -int(
        (e.get("signals") or {}).get("cross_section_coverage", 0)), e.get("name", "")))

    stats = {
        "entities_before": n,
        "entities_after": len(out_entities),
        "merged": n - len(out_entities),
        "clusters": len(cluster_records),
        "merge_methods": {
            "alias": alias_merges,
            "auto": auto_merges,
            "llm": llm_merges,
            "no_llm": sum(1 for (_, (ok, reason)) in llm_pairs.items()
                          if ok and reason == "embedding_threshold"),
        },
        "candidate_pairs": len(candidates),
        "rejected": dict(rej),
        "llm_calls": sum(1 for (_, (_, reason)) in llm_pairs.items() if reason == "llm"),
        "relations_before": len(aggr.get("relations") or []),
        "relations_after": len(rel_out),
        "by_layer": dict(Counter(e.get("layer", "") for e in out_entities)),
    }
    result = {
        "schema_version": SCHEMA.version,
        "doc_id": aggr.get("doc_id") or "doc",
        "entities": out_entities,
        "relations": list(rel_out.values()),
        "clusters": cluster_records,
        "merge_map": name_map,
        "id_map": id_map,
        "decisions": decisions[: int(CFG["LOG_DECISIONS_MAX"])],
        "stats": stats,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"✅ Dedup 完成：{out_path.resolve()}")
    logger.info(f"  实体 {n} → {len(out_entities)}（合并 {n - len(out_entities)}，"
                f"簇 {len(cluster_records)}）")
    logger.info(f"  合并方式 {stats['merge_methods']} / 候选 {len(candidates)} / "
                f"LLM {stats['llm_calls']}")
    logger.info(f"  关系 {stats['relations_before']} → {stats['relations_after']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v2 同层实体去重")
    ap.add_argument("--entities", default=None)
    ap.add_argument("--emb", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=int(API.get("WORKERS") or 8))
    ap.add_argument("--loglevel", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logger.setLevel(getattr(logging, args.loglevel))
    return run(Path(args.entities) if args.entities else None,
               Path(args.emb) if args.emb else None,
               Path(args.out) if args.out else None,
               args.dry_run, args.workers)


if __name__ == "__main__":
    sys.exit(main())
