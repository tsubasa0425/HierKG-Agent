# -*- coding: utf-8 -*-
"""朴素 RAG 基线：扁平证据向量检索（无知识图谱结构），用于消融对比。

与 KG 方法共用同一 search_by_name 后端（ChromaDB hierkg_evidences + Neo4j 名称匹配），
只走 search_evidences 一个工具，不做概念/实体传播——这就是「图不图」的唯一差异，
保证 KG 召回优势可归因于图谱结构。
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from eval.common import call_tool


class FlatEvidenceBaseline:
    def __init__(self, registry):
        self.registry = registry

    def retrieve(self, query: str, k: int) -> List[str]:
        data = call_tool(self.registry, "search_evidences",
                         {"query": query, "limit": k, "include_snippet": False})
        if not data:
            return []
        return [d.get("node_id") for d in data.get("evidences") or [] if d.get("node_id")]

    def recall_at_k(self, query: str, golden: Sequence[str], k: int) -> float:
        g = set(golden or [])
        if not g:
            return 0.0
        return len(set(self.retrieve(query, k)) & g) / len(g)
