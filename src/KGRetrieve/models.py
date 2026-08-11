# -*- coding: utf-8 -*-
"""
models —— 四层知识图谱的共享数据结构
====================================

Node / Edge 是 KGRetrieve 内部唯一的统一数据模型：
  - KGDBMemory（Neo4j 后端）从查询记录构建它们
  - 14 个工具通过它们把结果序列化成结构化 JSON 返回给 Agent

单独拆成模块，是为了让工具层 / 数据层保持轻量，不依赖数据库驱动。
（历史上有纯内存后端 KGMemory，已删除；数据类保留为共享模型。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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