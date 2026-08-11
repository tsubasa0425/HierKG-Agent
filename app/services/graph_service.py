# -*- coding: utf-8 -*-
"""图谱可视化数据整形 —— 把 KGDBMemory 返回的 Node/Edge dict 映射成 GraphView 契约。

只做展示层整形（加 layer/color/size），不碰数据库连接。
GraphView 契约：
    nodes: [{id, label, type, layer, description, color, size}]
    edges: [{from, to, label, weight}]
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 分层配色：L1 概念蓝 / L2 实体绿 / L3 证据橙
LAYER_META = {
    "concept": {"layer": "L1", "label": "L1 概念", "color": "#2f6fed", "size": 18},
    "entity": {"layer": "L2", "label": "L2 实体", "color": "#00b578", "size": 12},
    "evidence": {"layer": "L3", "label": "L3 证据", "color": "#fa8c16", "size": 7},
}
_DEFAULT_META = LAYER_META["concept"]


def _meta(node_type: str) -> Dict[str, Any]:
    return LAYER_META.get(str(node_type or "").lower(), _DEFAULT_META)


def to_graph_node(n: Dict[str, Any]) -> Dict[str, Any]:
    meta = _meta(n.get("node_type", "concept"))
    return {
        "id": n.get("node_id", ""),
        "label": n.get("name", "") or n.get("node_id", ""),
        "type": str(n.get("node_type", "concept")),      # concept / entity / evidence
        "layer": meta["layer"],                          # L1 / L2 / L3（前端分层着色/筛选）
        "description": n.get("description", ""),
        "color": meta["color"],
        "size": meta["size"],
    }


def to_graph_edge(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "from": e.get("source", ""),
        "to": e.get("target", ""),
        "label": e.get("description") or e.get("type", ""),
        "weight": 1.0,
    }


def build_graph_data(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "nodes": [to_graph_node(x) for x in result.get("nodes", [])],
        "edges": [to_graph_edge(x) for x in result.get("edges", [])],
    }


def search_results(kg, q: str, limit: int = 20) -> List[Dict[str, Any]]:
    """图谱页搜索框结果（轻量名称搜索，不带向量）。"""
    nodes = kg.search_nodes_by_name(q, limit=limit)
    out = []
    for n in nodes:
        meta = _meta(n.get("node_type", "concept"))
        out.append({
            "node_id": n.get("node_id", ""),
            "name": n.get("name", ""),
            "layer": meta["layer"],
            "description": n.get("description", ""),
        })
    return out
