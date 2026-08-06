# -*- coding: utf-8 -*-
"""
KGMemory —— 纯内存四层知识图谱索引
==================================

直接从 final_kg.json 加载，构建以下索引，支持 O(1) 查找和 BFS 多跳：

  1. id → node          （概念 / 实体 / 证据统一索引）
  2. name → [node_ids]   （名称 → 同名下的所有节点，含别名）
  3. concept_id → [entity_ids] （概念下挂的所有实体）
  4. node_id → [in_edges, out_edges] （邻接表，用于多跳）
  5. layer → [node_ids]  （按层枚举）

零数据库依赖，对当前 453 L1 + 141 L2 + 42 L3 的规模完全够用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    """边对象（与 final_kg.json edges 数组格式一致，但字段类型化）"""
    source: str
    target: str
    type: str
    layer: str
    description: str = ""
    keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "layer": self.layer,
            "description": self.description,
            "keywords": self.keywords,
        }


@dataclass
class Node:
    """统一的节点对象，概念/实体/证据都用它表示，node_type 区分"""

    # 通用字段
    node_id: str                 # concept_id / entity_id / evidence_id
    node_type: str               # "concept" | "entity" | "evidence"
    name: str
    type: str                    # schema 中的实体类型 / section_level（证据）
    description: str = ""
    aliases: List[str] = field(default_factory=list)

    # L1 / L2 专属
    concept_id: Optional[str] = None      # L2 指向 L1 的外键；L1 为自身
    evidence_ids: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)

    # L3 专属
    doc_id: str = ""
    section_id: str = ""
    section_path: str = ""
    section_level: str = ""
    snippet: str = ""
    entities_in_section: List[str] = field(default_factory=list)

    # 原始数据（兜底）
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        base = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "name": self.name,
            "type": self.type,
            "aliases": self.aliases,
        }
        if self.description:
            base["description"] = self.description
        if self.node_type == "concept" or self.node_type == "entity":
            base["concept_id"] = self.concept_id
            base["evidence_ids"] = list(self.evidence_ids)
            if self.attributes:
                base["attributes"] = dict(self.attributes)
        if self.node_type == "evidence":
            base.update({
                "doc_id": self.doc_id,
                "section_id": self.section_id,
                "section_path": self.section_path,
                "section_level": self.section_level,
                "snippet": self.snippet[:500] + "..." if len(self.snippet) > 500 else self.snippet,
                "entities_in_section": list(self.entities_in_section),
            })
        return base


# ---------------------------------------------------------------------------
# 核心内存索引
# ---------------------------------------------------------------------------

class KGMemory:
    """
    内存中的四层知识图谱。典型用法：

        kg = KGMemory.load("src/KGBuild/output/final_kg.json")
        node = kg.get_node("concept_7446050b")
        neighbors = kg.get_neighbors(node.node_id, direction="out")
    """

    def __init__(self, raw_kg: Dict[str, Any]):
        self.raw = raw_kg

        # —— 索引 ——
        self._nodes: Dict[str, Node] = {}              # node_id → Node
        self._name_index: Dict[str, List[str]] = {}    # name/lowercase → [node_id]
        self._alias_index: Dict[str, List[str]] = {}   # alias/lowercase → [node_id]
        self._concept_to_entities: Dict[str, List[str]] = {}  # concept_id → [entity_id]
        self._out_edges: Dict[str, List[Edge]] = {}    # node_id → 出边
        self._in_edges: Dict[str, List[Edge]] = {}     # node_id → 入边
        self._by_layer: Dict[str, List[str]] = {       # layer → [node_id]
            "concept": [], "entity": [], "evidence": [],
        }

        self._build_indices()

    # ------------------------------------------------------------------
    # 加载入口
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, kg_path: str | Path) -> "KGMemory":
        """从 final_kg.json 文件加载"""
        path = Path(kg_path)
        if not path.exists():
            raise FileNotFoundError(f"KG 文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        logger.info("KGMemory 加载成功: %s", path)
        return cls(raw)

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def _build_indices(self) -> None:
        # 1. L1 概念
        for c in self.raw.get("L1_concepts", []):
            node = Node(
                node_id=c["concept_id"],
                node_type="concept",
                name=c.get("name", ""),
                type=c.get("type", "概念"),
                description=c.get("description", ""),
                aliases=c.get("aliases", []),
                concept_id=c["concept_id"],
                evidence_ids=c.get("evidence_ids", []),
                raw=c,
            )
            self._register_node(node)

        # 2. L2 实体
        for e in self.raw.get("L2_entities", []):
            node = Node(
                node_id=e["entity_id"],
                node_type="entity",
                name=e.get("name", ""),
                type=e.get("type", "实例"),
                description=e.get("description", ""),
                aliases=e.get("aliases", []),
                concept_id=e.get("concept_id"),
                evidence_ids=e.get("evidence_ids", []),
                attributes=e.get("attributes", {}),
                raw=e,
            )
            self._register_node(node)
            if node.concept_id:
                self._concept_to_entities.setdefault(node.concept_id, []).append(node.node_id)

        # 3. L3 证据
        for ev in self.raw.get("L3_evidences", []):
            node = Node(
                node_id=ev["evidence_id"],
                node_type="evidence",
                name=ev.get("title", ev.get("section_id", "")),
                type=ev.get("section_level", "evidence"),
                description=ev.get("summary", ""),
                doc_id=ev.get("doc_id", ""),
                section_id=ev.get("section_id", ""),
                section_path=ev.get("section_path", ""),
                section_level=ev.get("section_level", ""),
                snippet=ev.get("snippet", ev.get("content", "")),
                entities_in_section=ev.get("entities_in_section", []),
                evidence_ids=[],
                raw=ev,
            )
            self._register_node(node)

        # 4. 边
        for e in self.raw.get("edges", []):
            edge = Edge(
                source=e["source"],
                target=e["target"],
                type=e.get("type", "related"),
                layer=e.get("layer", ""),
                description=e.get("description", ""),
                keywords=e.get("keywords", []),
            )
            self._out_edges.setdefault(edge.source, []).append(edge)
            self._in_edges.setdefault(edge.target, []).append(edge)

        logger.info(
            "索引构建完成: L1=%d, L2=%d, L3=%d, 边=%d",
            len(self._by_layer["concept"]),
            len(self._by_layer["entity"]),
            len(self._by_layer["evidence"]),
            sum(len(v) for v in self._out_edges.values()),
        )

    def _register_node(self, node: Node) -> None:
        self._nodes[node.node_id] = node
        self._by_layer[node.node_type].append(node.node_id)
        self._add_name(node.name, node.node_id)
        for alias in node.aliases:
            self._add_alias(alias, node.node_id)

    def _add_name(self, name: str, node_id: str) -> None:
        if not name:
            return
        key = name.strip().lower()
        self._name_index.setdefault(key, []).append(node_id)
        # 原始大小写也索引一份
        self._name_index.setdefault(name.strip(), []).append(node_id)

    def _add_alias(self, alias: str, node_id: str) -> None:
        if not alias:
            return
        key = alias.strip().lower()
        self._alias_index.setdefault(key, []).append(node_id)
        self._alias_index.setdefault(alias.strip(), []).append(node_id)

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    @property
    def schema(self) -> Dict[str, Any]:
        return self.raw.get("L0_schema", {})

    def list_layers(self) -> Dict[str, int]:
        return {k: len(v) for k, v in self._by_layer.items()}

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_concept(self, concept_id: str) -> Optional[Node]:
        n = self._nodes.get(concept_id)
        return n if n and n.node_type == "concept" else None

    def get_entity(self, entity_id: str) -> Optional[Node]:
        n = self._nodes.get(entity_id)
        return n if n and n.node_type == "entity" else None

    def get_evidence(self, evidence_id: str) -> Optional[Node]:
        n = self._nodes.get(evidence_id)
        return n if n and n.node_type == "evidence" else None

    def get_entities_of_concept(self, concept_id: str) -> List[Node]:
        ids = self._concept_to_entities.get(concept_id, [])
        return [self._nodes[i] for i in ids if i in self._nodes]

    def get_edges(
        self,
        node_id: str,
        direction: str = "both",  # "in" | "out" | "both"
        edge_type: Optional[str] = None,
        layer: Optional[str] = None,
    ) -> List[Edge]:
        edges: List[Edge] = []
        if direction in ("out", "both"):
            edges.extend(self._out_edges.get(node_id, []))
        if direction in ("in", "both"):
            edges.extend(self._in_edges.get(node_id, []))
        if edge_type:
            edges = [e for e in edges if e.type == edge_type]
        if layer:
            edges = [e for e in edges if e.layer == layer]
        return edges

    def multi_hop(
        self,
        start_id: str,
        max_hop: int = 2,
        allowed_layers: Optional[Set[str]] = None,
        allowed_edge_types: Optional[Set[str]] = None,
        max_nodes: int = 100,
    ) -> Dict[str, Any]:
        """
        BFS 多跳遍历。

        Returns:
            {
                "nodes": [Node.to_dict()],   # 去重后的所有节点
                "edges": [Edge.to_dict()],   # 路径上的所有边
                "paths": [[node_id, ...]],   # 每条从起点出发的路径
            }
        """
        if start_id not in self._nodes:
            return {"nodes": [], "edges": [], "paths": []}

        visited: Set[str] = {start_id}
        visited_edges: Set[Tuple[str, str, str]] = set()
        result_nodes: List[str] = [start_id]
        result_edges: List[Edge] = []
        paths: List[List[str]] = [[start_id]]

        queue: List[Tuple[str, int, List[str]]] = [(start_id, 0, [start_id])]

        while queue:
            cur_id, hop, path = queue.pop(0)
            if hop >= max_hop:
                continue
            for edge in self._out_edges.get(cur_id, []):
                if allowed_edge_types and edge.type not in allowed_edge_types:
                    continue
                nxt = edge.target
                target_node = self._nodes.get(nxt)
                if not target_node:
                    continue
                if allowed_layers and target_node.node_type not in allowed_layers:
                    continue
                edge_key = (edge.source, edge.target, edge.type)
                if edge_key not in visited_edges:
                    visited_edges.add(edge_key)
                    result_edges.append(edge)
                if nxt not in visited:
                    visited.add(nxt)
                    result_nodes.append(nxt)
                    new_path = path + [nxt]
                    paths.append(new_path)
                    if len(result_nodes) < max_nodes:
                        queue.append((nxt, hop + 1, new_path))

        return {
            "nodes": [self._nodes[i].to_dict() for i in result_nodes if i in self._nodes],
            "edges": [e.to_dict() for e in result_edges],
            "paths": paths,
        }

    # ------------------------------------------------------------------
    # 模糊搜索（基于名称 / 别名的关键词匹配，纯字符串，零依赖）
    # ------------------------------------------------------------------

    def search_by_name(
        self,
        query: str,
        node_type: Optional[str] = None,  # concept / entity / evidence
        limit: int = 20,
    ) -> List[Tuple[Node, int]]:
        """
        名称 / 别名模糊匹配，返回 (node, score) 列表。
        score 是命中关键词的字符覆盖比 + 精确匹配加分，不依赖嵌入模型。
        """
        q = query.strip().lower()
        if not q:
            return []

        scores: Dict[str, float] = {}

        # 1. 精确名称匹配
        if q in self._name_index:
            for nid in self._name_index[q]:
                scores[nid] = scores.get(nid, 0) + 10.0

        # 2. 精确别名匹配
        if q in self._alias_index:
            for nid in self._alias_index[q]:
                scores[nid] = scores.get(nid, 0) + 9.0

        # 3. 包含匹配（名称）
        for key, ids in self._name_index.items():
            if q in key:
                overlap = len(q) / max(len(key), 1)
                for nid in ids:
                    scores[nid] = scores.get(nid, 0) + 3.0 * overlap

        # 4. 包含匹配（别名）
        for key, ids in self._alias_index.items():
            if q in key:
                overlap = len(q) / max(len(key), 1)
                for nid in ids:
                    scores[nid] = scores.get(nid, 0) + 2.5 * overlap

        # 5. description / snippet 里的关键词匹配（权重低一些）
        q_tokens = [t for t in q.split() if t]
        for nid, node in self._nodes.items():
            hay = (node.description + " " + node.name + " " + " ".join(node.aliases)).lower()
            if node.node_type == "evidence":
                hay += " " + node.snippet.lower() + " " + node.section_path.lower()
            token_hits = sum(1 for t in q_tokens if t in hay)
            if token_hits:
                scores[nid] = scores.get(nid, 0) + 0.5 * token_hits

        # 过滤 node_type
        results = []
        for nid, score in scores.items():
            node = self._nodes[nid]
            if node_type and node.node_type != node_type:
                continue
            results.append((node, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]
