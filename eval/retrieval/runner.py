# -*- coding: utf-8 -*-
"""检索层 runner：确定性工具流水线，把查询转成各层排序列表。

不解析 SSE 事件——直接调 registry.call() 拿完整 ToolResult.data。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from eval.common import call_tool

# 跨层边（canonical）与关系类型权重：
#   described_by = 证据在“讲”这个概念（定义性、强） → 权重 1.0
#   appears_in   = 证据只是“提到”这个实体（提及性、弱）→ 权重 0.5
_CROSS_LAYERS = ("L1-L3", "L2-L3")
_TYPE_WEIGHT = {"described_by": 1.0, "appears_in": 0.5}


def _dedup(ids: List[str]) -> List[str]:
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _dedup_dicts(items: List[dict], key: str) -> List[dict]:
    seen = set()
    out = []
    for d in items:
        k = d.get(key)
        if k and k not in seen:
            seen.add(k)
            out.append(d)
    return out


class RetrievalRunner:
    """对一道题跑 KG 检索流水线，得到 concept / entity / evidence 三层排序列表。

    evidence 排序 = 直接证据命中（semantic_search + search_evidences）
                     + 从检索到的概念/实体 evidence_ids 传播，**按来源排名合并**：
                       直接命中按其在 direct 流的位次打分，传播证据按所属节点的
                       位次打分，取两者 min 排序。传播是 KG 相对扁平检索的结构性
                       优势：即使证据文本本身不是 top 向量命中，只要它挂靠的
                       概念/实体被命中，就能进入召回列表并排在前面。
    """

    def __init__(self, registry):
        self.registry = registry

    # ---------------- 各层排序 ----------------
    def rank_concepts(self, q: str, per_layer: int = 10, kw_limit: int = 10) -> List[str]:
        return [d.get("node_id") for d in self._probe_dicts(q, per_layer, kw_limit)[0] if d.get("node_id")]

    def rank_entities(self, q: str, per_layer: int = 10, kw_limit: int = 10) -> List[str]:
        return [d.get("node_id") for d in self._probe_dicts(q, per_layer, kw_limit)[1] if d.get("node_id")]

    def rank_evidences_direct(self, q: str, per_layer: int = 10, kw_limit: int = 10) -> List[str]:
        return [d.get("node_id") for d in self._probe_dicts(q, per_layer, kw_limit)[2] if d.get("node_id")]

    def _node_evidence_links(self, node_id: str) -> Dict[str, float]:
        """该节点跨层出边 → {证据ID: 关系类型权重}（只用于传播加权，不改成员）。

        described_by=1.0，appears_in=0.5。传播的**成员**仍来自检索结果自带的
        evidence_ids（与 agent 实际看到的一致；投影漂移由 eval/validate_kg.py
        单独报警，修复应在构建/数据层，不在这）。权重不在边里的成员按强(1.0)
        处理。get_edges 异常返回空表，等价不加权。
        """
        w: Dict[str, float] = {}
        try:
            for e in self.registry.kg.get_edges(node_id, direction="out"):
                if e.layer in _CROSS_LAYERS:
                    w[e.target] = _TYPE_WEIGHT.get(e.type, 1.0)
        except Exception:
            return {}
        return w

    def rank_evidences_full(self, q: str, per_layer: int = 10, kw_limit: int = 10,
                            weight_type: bool = True) -> List[str]:
        """合并两路证据并按来源排名排序（直接命中 + 概念/实体传播）。

        成员 = 检索结果的 evidence_ids（agent 实际所见），权重 = 跨层边类型：
          score(e) = min( direct 位次, 节点位次 + 类型惩罚 )
          类型惩罚 = 1 - 权重：described_by(+0) 排在 appears_in(+0.5) 前——
          「定义这段的证据」优先于「只是提到这个实体的证据」。
        weight_type=False 时退化为纯位次排序（旧行为），用于消融。
        tie 时直接命中在前、节点顺序在后，保证确定性。
        """
        concepts, entities, evidences = self._probe_dicts(q, per_layer, kw_limit)
        direct = _dedup([d.get("node_id") for d in evidences if d.get("node_id")])

        best: Dict[str, float] = {}
        for i, eid in enumerate(direct):
            best[eid] = min(best.get(eid, float("inf")), float(i))

        for ni, d in enumerate(concepts + entities):
            nid = d.get("node_id")
            weights = self._node_evidence_links(nid) if (weight_type and nid) else {}
            for eid in d.get("evidence_ids") or []:
                penalty = (1.0 - weights.get(eid, 1.0)) if weight_type else 0.0
                best[eid] = min(best.get(eid, float("inf")), float(ni) + penalty)

        # 稳定排序：score 升序；同 score 直接命中先（它们也先进入 best，天然稳定）
        ordered = sorted(best.keys(), key=lambda eid: best[eid])
        return ordered

    # ---------------- 探测（multi-hop 结构探针） ----------------
    def structural_probe(self, src_id: str, tgt_id: str, max_hop: int = 2) -> Dict[str, Any]:
        """从 src 出发，用出边 + 多跳遍历检查 tgt 是否可达（路径存在性，非从零检索）。"""
        rel = call_tool(self.registry, "get_relations",
                        {"node_id": src_id, "direction": "out", "limit": 50})
        targets = {e.get("target") for e in (rel.get("edges") if rel else []) or []}
        if tgt_id in targets:
            return {"reached": True, "hops": 1}
        mh = call_tool(self.registry, "multi_hop_traverse",
                       {"start_id": src_id, "max_hop": max_hop, "end_id": tgt_id})
        if mh:
            return {"reached": bool(mh.get("reached_end")), "hops": 2,
                    "paths_to_end": mh.get("paths_to_end", [])}
        return {"reached": False, "hops": None}

    # ---------------- 内部 ----------------
    def _probe_dicts(self, q: str, per_layer: int, kw_limit: int):
        s = call_tool(self.registry, "semantic_search", {"query": q, "per_layer_limit": per_layer})
        ckw = call_tool(self.registry, "search_concepts", {"query": q, "limit": kw_limit})
        ekw = call_tool(self.registry, "search_entities", {"query": q, "limit": kw_limit})
        vkw = call_tool(self.registry, "search_evidences",
                        {"query": q, "limit": kw_limit, "include_snippet": False})

        concepts: List[dict] = []
        if s:
            concepts += s.get("L1_concepts") or []
        if ckw:
            concepts += ckw.get("concepts") or []

        entities: List[dict] = []
        if s:
            entities += s.get("L2_entities") or []
        if ekw:
            entities += ekw.get("entities") or []

        evidences: List[dict] = []
        if s:
            evidences += s.get("L3_evidences") or []
        if vkw:
            evidences += vkw.get("evidences") or []

        return (_dedup_dicts(concepts, "node_id"),
                _dedup_dicts(entities, "node_id"),
                _dedup_dicts(evidences, "node_id"))
