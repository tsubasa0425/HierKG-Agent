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


# ---------------------------------------------------------------------------
# 答题溯源：给定证据/节点种子，返回其邻域子图 + 被引用证据的原文详情
# ---------------------------------------------------------------------------

# 证据 snippet 上限：Evidence 节点 snippet 为章节全文，多数数千字；
# 6000 覆盖绝大多数整篇，超限截断并标注（防单次溯源 payload 失控）。
_SNIPPET_CAP = 6000


def _evidence_detail(node) -> Dict[str, Any]:
    """证据详情：直接读 models.Node.snippet 全文（Node.to_dict 会截断到 500）。"""
    snip = node.snippet or ""
    truncated = len(snip) > _SNIPPET_CAP
    if truncated:
        snip = snip[:_SNIPPET_CAP]
    return {
        "id": node.node_id,
        "name": node.name,
        "doc_id": node.doc_id,
        "section_id": node.section_id,
        "section_path": node.section_path,
        "section_level": node.section_level,
        "snippet": snip,
        "truncated": truncated,
    }


def provenance_graph(kg, ids: List[str], depth: int = 1) -> Dict[str, Any]:
    """溯源子图：每个种子取 N 跳邻域并入去重，被引证据附原文详情。

    returns {nodes, edges, used, details}
      nodes/edges —— GraphView 契约（to_graph_node/to_graph_edge 整形）
      used        —— 命中的种子 id（存活证据/节点，前端据此高亮）
      details     —— {evidence_id: _evidence_detail}，只含证据（概念/实体无原文）
    """
    used: List[str] = []
    details: Dict[str, Any] = {}
    node_map: Dict[str, Dict[str, Any]] = {}
    edge_map: Dict[tuple, Dict[str, Any]] = {}
    hop = max(1, min(int(depth or 1), 2))

    for nid in ids:
        node = kg.get_node(nid)
        if node is None:          # 图谱已更新/节点被删 → 跳过
            continue
        used.append(nid)
        sub = kg.get_neighborhood(nid, depth=hop, max_nodes=80)
        for n in sub.get("nodes", []):
            node_map[n["node_id"]] = n
        for e in sub.get("edges", []):
            edge_map[(e["source"], e["target"], e["type"])] = e
        if node.node_type == "evidence":
            details[nid] = _evidence_detail(node)

    return {
        "nodes": [to_graph_node(n) for n in node_map.values()],
        "edges": [to_graph_edge(e) for e in edge_map.values()],
        "used": used,
        "details": details,
    }
