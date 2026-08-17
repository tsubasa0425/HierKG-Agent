# -*- coding: utf-8 -*-
"""检索指标：Recall@k / Hit@k / Precision@k / MRR，纯数学、可单测。"""
from __future__ import annotations

from typing import Dict, List, Sequence, Set, Any

KS = (1, 3, 5, 10)


def _golden_set(golden: Sequence[str]) -> Set[str]:
    return set(golden or [])


def recall_at_k(ranked: Sequence[str], golden: Sequence[str], k: int) -> float:
    g = _golden_set(golden)
    if not g:
        return 0.0
    return len(set(ranked[:k]) & g) / len(g)


def hit_at_k(ranked: Sequence[str], golden: Sequence[str], k: int) -> float:
    g = _golden_set(golden)
    if not g:
        return 0.0
    return 1.0 if set(ranked[:k]) & g else 0.0


def precision_at_k(ranked: Sequence[str], golden: Sequence[str], k: int) -> float:
    g = _golden_set(golden)
    if not g or k <= 0:
        return 0.0
    return len(set(ranked[:k]) & g) / k


def reciprocal_rank(ranked: Sequence[str], golden: Sequence[str]) -> float:
    """第一个 golden 命中所在排名的倒数（排名从 1 起）；没有命中返回 0。"""
    g = _golden_set(golden)
    for i, nid in enumerate(ranked):
        if nid in g:
            return 1.0 / (i + 1)
    return 0.0


def question_metrics(ranked: Sequence[str], golden: Sequence[str]) -> Dict[str, Any]:
    """单题全套指标：{recall@1/3/5/10, hit@1/3/5/10, precision@1/3/5/10, mrr}。"""
    ranked = list(ranked or [])
    golden = list(golden or [])
    return {
        f"recall@{k}": round(recall_at_k(ranked, golden, k), 4) for k in KS
    } | {
        f"hit@{k}": round(hit_at_k(ranked, golden, k), 4) for k in KS
    } | {
        f"precision@{k}": round(precision_at_k(ranked, golden, k), 4) for k in KS
    } | {
        "mrr": round(reciprocal_rank(ranked, golden), 4),
    }


def aggregate(per_question: Sequence[Dict[str, Any]], metric_keys: Sequence[str]) -> Dict[str, Any]:
    """跨题聚合指定指标键的均值。per_question 里缺某个键（如该题无 golden 实体）就跳过。"""
    out: Dict[str, Any] = {}
    for key in metric_keys:
        vals = [q.get(key) for q in per_question if q.get(key) is not None]
        out[key] = round(sum(vals) / len(vals), 4) if vals else None
    return out
