# -*- coding: utf-8 -*-
"""
检索工具集合 —— 细粒度原子工具，供 Agent 自主编排调用
======================================================

设计原则：
  1. 每个工具只干一件事，输入输出严格结构化
  2. 不做答案生成，只返回检索到的结构化知识
  3. 有统一的 name / description / parameters / output_schema
  4. 每个工具都支持 dry_run 模式（给 Agent 先看描述再决定）

典型使用（给 Agent 做 tool-use）：

    from KGRetrieve import KGDBMemory, ToolRegistry

    kg = KGDBMemory()
    registry = ToolRegistry(kg)

    # 1. 给 Agent 看所有工具描述（function calling 的 JSON）
    tool_schemas = registry.get_tool_schemas()  # → 可以直接喂给 LLM
    # 2. Agent 选择工具，调用
    result = registry.call("search_concepts", {"query": "强化学习", "limit": 5})
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import Node

logger = logging.getLogger(__name__)


# =====================================================================
# 工具基类
# =====================================================================

@dataclass
class ToolResult:
    """统一的工具输出结构"""
    tool_name: str
    success: bool
    data: Any = None
    message: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "data": self.data,
            "message": self.message,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)


class BaseTool(ABC):
    """
    工具基类。子类只需实现：
      - name / description / parameters / output_schema
      - _run(params) -> ToolResult
    """

    name: str = ""
    description: str = ""

    # JSON Schema 格式的参数声明（给 LLM function calling 用）
    # 例: {"type": "object", "properties": {"query": {...}}, "required": ["query"]}
    parameters: Dict[str, Any] = field(default_factory=dict)

    # 返回值的文字说明（给 Agent 读）
    output_schema: Dict[str, Any] = field(default_factory=dict)

    def __init__(self, kg: "KGDBMemory"):  # noqa: F821 —— 字符串注解避免 tools 层反向依赖数据库驱动
        self.kg = kg

    # ------------------------------------------------------------------
    # 元数据（给 Agent 看）
    # ------------------------------------------------------------------

    def get_function_schema(self) -> Dict[str, Any]:
        """返回 OpenAI function calling 兼容的格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def short_doc(self) -> str:
        """一行文档（给 Agent 做 tool list 快速浏览）"""
        return f"- {self.name}: {self.description[:100]}"

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def __call__(self, params: Optional[Dict[str, Any]] = None, **kwargs) -> ToolResult:
        import time
        params = params or {}
        merged = {**params, **kwargs}
        # 过滤无效参数
        valid_keys = set((self.parameters or {}).get("properties", {}).keys())
        if valid_keys:
            merged = {k: v for k, v in merged.items() if k in valid_keys}
        t0 = time.perf_counter()
        try:
            result = self._run(merged)
            result.elapsed_ms = (time.perf_counter() - t0) * 1000
            return result
        except Exception as exc:  # pragma: no cover
            logger.exception("工具 %s 执行失败", self.name)
            return ToolResult(
                tool_name=self.name,
                success=False,
                message=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

    @abstractmethod
    def _run(self, params: Dict[str, Any]) -> ToolResult: ...


# =====================================================================
# 具体工具：精确 ID 查找
# =====================================================================

class GetSchemaTool(BaseTool):
    name = "get_schema"
    description = (
        "获取知识图谱的本体层（L0 Schema）定义，包括所有实体类型（概念/方法/实例/工具/角色等）"
        "、关系类型（prerequisite/part_of/synonym等）、跨层关系说明。"
        "当你不确定某个类型或关系的语义时调用此工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": ["all", "entity_types", "relation_types"],
                "default": "all",
                "description": "返回 schema 的哪个部分：all=全部，entity_types=仅实体类型，relation_types=仅关系类型",
            },
        },
    }
    output_schema = {
        "description": "L0 本体定义（与 final_kg.json L0_schema 结构一致）",
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        section = params.get("section", "all")
        schema = self.kg.schema
        if section == "entity_types":
            data = {"entity_types": schema.get("entity_types", [])}
        elif section == "relation_types":
            data = {"relation_types": schema.get("relation_types", {})}
        else:
            data = schema
        return ToolResult(self.name, True, data=data)


class GetNodeByIDTool(BaseTool):
    name = "get_node_by_id"
    description = "通过 node_id（concept_id / entity_id / evidence_id 三者通用）精确查找节点详情。"
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "节点ID，如 concept_7446050b / ent_db974238 / ev_1.1.1",
            },
        },
        "required": ["node_id"],
    }
    output_schema = {
        "description": "单个节点的完整字段（名称/类型/描述/别名/证据链接等）",
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        node_id = params.get("node_id", "")
        node = self.kg.get_node(node_id)
        if not node:
            return ToolResult(self.name, False, message=f"未找到节点: {node_id}")
        return ToolResult(self.name, True, data=node.to_dict())


class GetConceptByIDTool(BaseTool):
    name = "get_concept_by_id"
    description = "通过 concept_id 精确获取 L1 概念详情（包含归属它的所有 L2 实体数量）。"
    parameters = {
        "type": "object",
        "properties": {
            "concept_id": {"type": "string", "description": "L1 概念ID，如 concept_7446050b"},
            "include_entities": {"type": "boolean", "default": False,
                                 "description": "是否同时返回挂在此概念下的所有 L2 实体列表"},
            "entity_limit": {"type": "integer", "default": 20, "description": "include_entities=true 时最多返回的实体数"},
        },
        "required": ["concept_id"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        cid = params["concept_id"]
        node = self.kg.get_concept(cid)
        if not node:
            return ToolResult(self.name, False, message=f"未找到概念: {cid}")
        data = node.to_dict()
        if params.get("include_entities"):
            ents = self.kg.get_entities_of_concept(cid)
            limit = int(params.get("entity_limit", 20))
            data["entities"] = [e.to_dict() for e in ents[:limit]]
            data["entity_count"] = len(ents)
        return ToolResult(self.name, True, data=data)


class GetEntityByIDTool(BaseTool):
    name = "get_entity_by_id"
    description = "通过 entity_id 精确获取 L2 实体详情，会自动带出它归属的 L1 概念节点。"
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "L2 实体ID，如 ent_db974238"},
            "include_concept": {"type": "boolean", "default": True,
                                "description": "是否同时返回该实体归属的 L1 概念信息"},
        },
        "required": ["entity_id"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        eid = params["entity_id"]
        node = self.kg.get_entity(eid)
        if not node:
            return ToolResult(self.name, False, message=f"未找到实体: {eid}")
        data = {"entity": node.to_dict()}
        if params.get("include_concept", True) and node.concept_id:
            concept = self.kg.get_concept(node.concept_id)
            if concept:
                data["concept"] = concept.to_dict()
        return ToolResult(self.name, True, data=data)


class GetEvidenceByIDTool(BaseTool):
    name = "get_evidence_by_id"
    description = "通过 evidence_id 获取 L3 证据详情（原文片段 / 章节路径 / 文档位置，用于溯源）。"
    parameters = {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "string", "description": "L3 证据ID，如 ev_1.1.1"},
            "include_snippet": {"type": "boolean", "default": True,
                                "description": "是否返回原文片段（默认返回；当 snippet 太长时可设置为 false 只看元数据）"},
            "snippet_chars": {"type": "integer", "default": 2000,
                              "description": "原文片段最多返回的字符数"},
        },
        "required": ["evidence_id"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        eid = params["evidence_id"]
        ev = self.kg.get_evidence(eid)
        if not ev:
            return ToolResult(self.name, False, message=f"未找到证据: {eid}")
        d = ev.to_dict()
        if not params.get("include_snippet", True):
            d.pop("snippet", None)
        elif "snippet" in d:
            limit = int(params.get("snippet_chars", 2000))
            sn = d["snippet"]
            if len(sn) > limit:
                d["snippet"] = sn[:limit] + f"... (truncated, total {len(sn)} chars)"
        return ToolResult(self.name, True, data=d)


# =====================================================================
# 具体工具：关联 / 关系查询
# =====================================================================

class GetRelationsTool(BaseTool):
    name = "get_relations"
    description = (
        "查询某个节点的所有入边/出边/双向边。可以按 edge_type（如 prerequisite/synonym/part_of）"
        "或跨层标注（layer 如 L1-L1 / L2-L1 / L1-L3）过滤。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "起点或终点的 node_id"},
            "direction": {
                "type": "string",
                "enum": ["in", "out", "both"],
                "default": "both",
                "description": "边的方向：in=入边，out=出边，both=双向",
            },
            "edge_type": {
                "type": "string",
                "description": "按关系类型精确过滤，例 prerequisite / synonym / instance_of / appears_in",
            },
            "layer": {
                "type": "string",
                "description": "按层标注过滤，例 L1-L1 / L1-L2 / L2-L1 / L1-L3 / L2-L3",
            },
            "limit": {"type": "integer", "default": 50, "description": "最多返回的边数"},
        },
        "required": ["node_id"],
    }
    output_schema = {"description": "边列表（含 source / target / type / layer / description）"}

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        nid = params["node_id"]
        if not self.kg.get_node(nid):
            return ToolResult(self.name, False, message=f"节点不存在: {nid}")
        edges = self.kg.get_edges(
            nid,
            direction=params.get("direction", "both"),
            edge_type=params.get("edge_type"),
            layer=params.get("layer"),
        )
        limit = int(params.get("limit", 50))
        data = {
            "total": len(edges),
            "edges": [e.to_dict() for e in edges[:limit]],
        }
        return ToolResult(self.name, True, data=data)


class GetEntitiesOfConceptTool(BaseTool):
    name = "get_entities_of_concept"
    description = "获取归属于某个 L1 概念下的所有 L2 实体（instance_of 关系）。"
    parameters = {
        "type": "object",
        "properties": {
            "concept_id": {"type": "string", "description": "L1 概念ID"},
            "limit": {"type": "integer", "default": 50, "description": "最多返回的实体数"},
        },
        "required": ["concept_id"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        cid = params["concept_id"]
        if not self.kg.get_concept(cid):
            return ToolResult(self.name, False, message=f"概念不存在: {cid}")
        ents = self.kg.get_entities_of_concept(cid)
        limit = int(params.get("limit", 50))
        return ToolResult(self.name, True, data={
            "concept_id": cid,
            "total": len(ents),
            "entities": [e.to_dict() for e in ents[:limit]],
        })


class GetEvidencesOfNodeTool(BaseTool):
    name = "get_evidences_of_node"
    description = (
        "获取某个 L1 概念或 L2 实体关联的所有 L3 证据（跨层 described_by / appears_in 边）。"
        "每条证据带 rel 字段：described_by=该证据在“讲”此节点（强/定义性），"
        "appears_in=该证据只是“提到”此节点（弱/提及性）。直接返回原文片段和章节定位，用于溯源和判断引用强度。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "L1/L2 节点ID"},
            "include_snippet": {"type": "boolean", "default": True},
            "snippet_chars": {"type": "integer", "default": 1000},
            "limit": {"type": "integer", "default": 20},
        },
        "required": ["node_id"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        nid = params["node_id"]
        node = self.kg.get_node(nid)
        if not node:
            return ToolResult(self.name, False, message=f"节点不存在: {nid}")
        # 以跨层边为权威来源（canonical），保留关系类型语义：
        #   described_by = 该证据在“讲”这个概念（强/定义性）
        #   appears_in   = 该证据只是“提到”这个实体（弱/提及性）
        # evidence_ids 只是边的投影，不再双写合并；若出现投影漂移，兜底保留并
        # 标注 rel=None（数据不一致由 eval/validate_kg.py 报警）。
        pairs: List[Tuple[str, Optional[str]]] = []
        seen = set()
        for e in self.kg.get_edges(nid, direction="out"):
            if e.layer in ("L1-L3", "L2-L3") and e.target not in seen:
                seen.add(e.target)
                pairs.append((e.target, e.type))
        for eid in node.evidence_ids:  # 投影兜底（数据一致时此处应为空）
            if eid not in seen:
                seen.add(eid)
                pairs.append((eid, None))
        limit = int(params.get("limit", 20))
        snippet_chars = int(params.get("snippet_chars", 1000))
        include_snippet = bool(params.get("include_snippet", True))
        evidences: List[Dict] = []
        for eid, rel in pairs[:limit]:
            ev = self.kg.get_evidence(eid)
            if not ev:
                continue
            d = ev.to_dict()
            d["rel"] = rel
            if not include_snippet:
                d.pop("snippet", None)
            elif "snippet" in d and len(d["snippet"]) > snippet_chars:
                d["snippet"] = d["snippet"][:snippet_chars] + "..."
            evidences.append(d)
        return ToolResult(self.name, True, data={
            "node_id": nid,
            "node_name": node.name,
            "total": len(pairs),
            "evidences": evidences,
        })


class MultiHopTraverseTool(BaseTool):
    name = "multi_hop_traverse"
    description = (
        "从某个节点出发进行 BFS 多跳遍历，用于跨概念查询（如「A 和 B 之间有什么路径」"
        "或「某个概念的二跳邻居有哪些」）。可以按允许的层集合、允许的边类型过滤。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "start_id": {"type": "string", "description": "起点 node_id"},
            "max_hop": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5},
            "allowed_layers": {
                "type": "array",
                "items": {"type": "string", "enum": ["concept", "entity", "evidence"]},
                "description": "允许经过的节点类型；默认空表示全部允许",
            },
            "allowed_edge_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许经过的边类型（例 [\"prerequisite\",\"part_of\",\"synonym\"]）；空表示全部允许",
            },
            "max_nodes": {"type": "integer", "default": 100},
            "end_id": {
                "type": "string",
                "description": "可选。如果指定，一旦 BFS 到达 end_id 就提前停止，并在 paths 中标记命中路径",
            },
        },
        "required": ["start_id"],
    }
    output_schema = {
        "description": "子图：{nodes, edges, paths, reached_end?}",
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        start_id = params["start_id"]
        if not self.kg.get_node(start_id):
            return ToolResult(self.name, False, message=f"起点不存在: {start_id}")
        allowed_layers = set(params.get("allowed_layers") or []) or None
        allowed_edge_types = set(params.get("allowed_edge_types") or []) or None
        result = self.kg.multi_hop(
            start_id=start_id,
            max_hop=int(params.get("max_hop", 2)),
            allowed_layers=allowed_layers,
            allowed_edge_types=allowed_edge_types,
            max_nodes=int(params.get("max_nodes", 100)),
        )
        end_id = params.get("end_id")
        if end_id:
            reached_paths = [p for p in result["paths"] if p[-1] == end_id]
            result["reached_end"] = len(reached_paths) > 0
            result["paths_to_end"] = reached_paths
        return ToolResult(self.name, True, data=result)


# =====================================================================
# 具体工具：模糊搜索 / 语义搜索
# =====================================================================

class SearchConceptsTool(BaseTool):
    name = "search_concepts"
    description = "按关键词搜索 L1 概念层（名称 / 别名 / 描述模糊匹配）。当你想知道「有没有讲过 X 这个概念」时调用。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，如 \"强化学习\" \"智能体类型\""},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "min_score": {"type": "number", "default": 0.5,
                          "description": "最低匹配分数，调高可降低噪声"},
        },
        "required": ["query"],
    }
    output_schema = {
        "description": "命中的概念列表（按相关度降序，含匹配分数）",
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        hits = self.kg.search_by_name(params["query"], node_type="concept",
                                      limit=int(params.get("limit", 10)))
        min_score = float(params.get("min_score", 0.5))
        data = [
            {**node.to_dict(), "match_score": round(score, 3)}
            for node, score in hits if score >= min_score
        ]
        return ToolResult(self.name, True, data={
            "query": params["query"],
            "hits": len(data),
            "concepts": data,
        })


class SearchEntitiesTool(BaseTool):
    name = "search_entities"
    description = "按关键词搜索 L2 实体层（具体实例 / 工具 / 角色 / 参数）。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，如 \"AlphaGo\" \"LangChain\""},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "entity_type": {
                "type": "string",
                "description": "按 schema 定义的实体类型过滤：实例 / 工具 / 角色 / 参数",
            },
        },
        "required": ["query"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        hits = self.kg.search_by_name(params["query"], node_type="entity",
                                      limit=int(params.get("limit", 10)) + 30)
        filtered = []
        etype = params.get("entity_type")
        for node, score in hits:
            if etype and node.type != etype:
                continue
            filtered.append({**node.to_dict(), "match_score": round(score, 3)})
        limit = int(params.get("limit", 10))
        return ToolResult(self.name, True, data={
            "query": params["query"],
            "hits": len(filtered[:limit]),
            "entities": filtered[:limit],
        })


class SearchEvidencesTool(BaseTool):
    name = "search_evidences"
    description = (
        "按关键词搜索 L3 证据层（章节标题 / 章节路径 / 原文片段 / 章节内出现的实体名）。"
        "当你想找「某段原文」或「XX 这个词出现在哪几节」时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，如 \"传统视角\" \"规划旅行\""},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "include_snippet": {"type": "boolean", "default": True},
            "snippet_chars": {"type": "integer", "default": 800},
        },
        "required": ["query"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        hits = self.kg.search_by_name(params["query"], node_type="evidence",
                                      limit=int(params.get("limit", 10)) + 30)
        include_snippet = bool(params.get("include_snippet", True))
        snippet_chars = int(params.get("snippet_chars", 800))
        results = []
        for node, score in hits:
            d = node.to_dict()
            if not include_snippet:
                d.pop("snippet", None)
            elif "snippet" in d and len(d["snippet"]) > snippet_chars:
                d["snippet"] = d["snippet"][:snippet_chars] + "..."
            d["match_score"] = round(score, 3)
            results.append(d)
        limit = int(params.get("limit", 10))
        return ToolResult(self.name, True, data={
            "query": params["query"],
            "hits": len(results[:limit]),
            "evidences": results[:limit],
        })


class SemanticSearchTool(BaseTool):
    name = "semantic_search"
    description = (
        "混合语义搜索：一次性跨 L1+L2+L3 三层搜索，把命中结果按层分类返回。"
        "当你不确定要查的东西在概念层、实体层还是证据层时，用这个工具做第一步探索。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词或自然语言描述"},
            "per_layer_limit": {"type": "integer", "default": 8, "description": "每层最多返回多少条"},
        },
        "required": ["query"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        q = params["query"]
        lim = int(params.get("per_layer_limit", 8))
        c = [(n, s) for n, s in self.kg.search_by_name(q, "concept", lim + 5)]
        e = [(n, s) for n, s in self.kg.search_by_name(q, "entity", lim + 5)]
        v = [(n, s) for n, s in self.kg.search_by_name(q, "evidence", lim + 5)]

        def convert(items, extra_fn=None):
            out = []
            for node, score in items:
                d = node.to_dict()
                if extra_fn:
                    extra_fn(d)
                d["match_score"] = round(score, 3)
                out.append(d)
            return out[:lim]

        def trim_snippet(d):
            sn = d.get("snippet", "")
            if len(sn) > 600:
                d["snippet"] = sn[:600] + "..."

        return ToolResult(self.name, True, data={
            "query": q,
            "L1_concepts": convert(c),
            "L2_entities": convert(e),
            "L3_evidences": convert(v, trim_snippet),
        })


# =====================================================================
# 具体工具：结果重排序
# =====================================================================

# —— cross-encoder 重排器（懒加载）——
# 用 bge-reranker-v2-m3 做精排：把 query 和每个候选拼成 pair 一起过 transformer，
# 模型能看见两者的逐词交互，比 bi-encoder（bge-m3）的向量相似度准得多。
# 只在候选集（top 20~50）上跑，CPU 也可接受。
# 权重默认放本地 models/bge-reranker-v2-m3（从 ModelScope 下载，见 _download_reranker.py），
# 本地缺失时才走 HF 镜像（huggingface.co 在国内常被墙，HF_ENDPOINT 指向 hf-mirror.com）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_RERANKER: Optional[Any] = None
_RERANKER_LOCK = threading.Lock()
_RERANKER_MODEL = str(Path(__file__).resolve().parent / "models" / "bge-reranker-v2-m3")


def _get_reranker():
    """懒加载 cross-encoder，线程安全；失败时抛异常由调用方降级"""
    global _RERANKER
    if _RERANKER is None:
        with _RERANKER_LOCK:
            if _RERANKER is None:
                from sentence_transformers import CrossEncoder
                model_ref = _RERANKER_MODEL
                if not (Path(model_ref) / "model.safetensors").exists():
                    model_ref = "BAAI/bge-reranker-v2-m3"
                    logger.info("本地重排模型缺失，将尝试从 HF 镜像下载 %s ...", model_ref)
                logger.info("加载重排模型 %s ...", model_ref)
                _RERANKER = CrossEncoder(model_ref)
    return _RERANKER


def _node_search_text(node: Node) -> str:
    """把节点拼成给重排器/关键词打分用的检索文本"""
    parts = [node.name, node.description, " ".join(node.aliases)]
    if node.node_type == "evidence":
        parts.append(node.snippet)
        parts.append(node.section_path)
    return " ".join(p for p in parts if p).strip()


def _sigmoid(x: float) -> float:
    """cross-encoder 原始 logit → 0~1 相关度，便于 Agent 阅读"""
    return 1.0 / (1.0 + math.exp(-x))


class RankResultsTool(BaseTool):
    name = "rank_results"
    description = (
        "对前几步检索返回的候选节点/边/证据列表做二次重排序。当前面的工具返回结果太多、"
        "你希望按跟某个意图的相关度重新排序时调用。"
        "支持按：node_ids 列表手动挑选（top_n 指定留多少）、或按 match_score 直接重排。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "待排序的 node_id 列表。必填。",
            },
            "intent_query": {
                "type": "string",
                "description": "排序意图（如 \"找智能体的先修概念\"）。用于对每个节点做关键词打分。",
            },
            "top_n": {"type": "integer", "default": 10, "description": "返回前 N 个"},
            "include_evidences": {
                "type": "boolean", "default": False,
                "description": "是否把每个节点关联的 L3 证据一并带上",
            },
        },
        "required": ["node_ids"],
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        ids = params.get("node_ids") or []
        intent = (params.get("intent_query") or "").strip()
        tokens = [t for t in intent.replace(",", " ").split() if t] if intent else []

        # 收集候选 + 检索文本
        candidates: List[Tuple[str, Node, str]] = []
        for nid in ids:
            node = self.kg.get_node(nid)
            if not node:
                continue
            candidates.append((nid, node, _node_search_text(node)))

        rerank_method = "original_order"  # 无 intent 时保持 Agent 传入的顺序
        scores: List[float] = []
        if tokens:
            # —— 首选：cross-encoder 精排（bge-reranker-v2-m3）——
            try:
                reranker = _get_reranker()
                raw = reranker.predict([(intent, text) for _, _, text in candidates])
                scores = [_sigmoid(float(s)) for s in raw]
                rerank_method = "cross_encoder"
            except Exception as exc:
                logger.warning("cross-encoder 重排失败，降级为关键词重叠: %s", exc)
                # —— 降级：关键词重叠打分 ——
                for _, _, text in candidates:
                    low = text.lower()
                    scores.append(sum(1 for tk in tokens if tk in low))
                rerank_method = "keyword_overlap"

        # 有 intent 时按分数降序，否则保持原顺序
        ordered = list(zip(candidates, scores))
        if tokens:
            ordered.sort(key=lambda x: x[1], reverse=True)
        top_n = int(params.get("top_n", 10))
        picked = ordered[:top_n]

        items: List[Dict] = []
        for (nid, node, _text), score in picked:
            d = node.to_dict()
            d["rank_score"] = round(score, 3)
            if params.get("include_evidences"):
                evs = self.kg.get_edges(nid, direction="out")
                rel = [e.target for e in evs if e.layer in ("L1-L3", "L2-L3")][:5]
                d["related_evidence_ids"] = rel
            items.append(d)

        return ToolResult(self.name, True, data={
            "input_count": len(ids),
            "returned": len(items),
            "intent_query": params.get("intent_query", ""),
            "rerank_method": rerank_method,
            "items": items,
        })


# =====================================================================
# 具体工具：context 组装（纯格式化，不做取舍决策）
# =====================================================================

def _evidence_ids_of_node(kg, node: Node) -> List[str]:
    """节点的关联证据 ID：evidence_ids 字段 + 跨层 L1-L3/L2-L3 出边，去重保序"""
    ev_ids = list(node.evidence_ids)
    for e in kg.get_edges(node.node_id, direction="out"):
        if e.layer in ("L1-L3", "L2-L3"):
            ev_ids.append(e.target)
    seen: set = set()
    out: List[str] = []
    for x in ev_ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class AssembleContextTool(BaseTool):
    name = "assemble_context"
    description = (
        "把已经收集到的节点/证据 ID 组装成一段紧凑、去重、带溯源标注的 context 文本，"
        "可直接粘进回答 prompt。它只做格式化（拼块/去重/截断/贴引用 ID），"
        "不做任何取舍：查什么、信什么由你（Agent）自己决定。"
        "当检索结果较多、想控制 token 预算、或需要统一引用格式（[ev_xxx]）时调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "已收集的 concept/entity/evidence node_id 列表",
            },
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "额外要带的证据 ID；不传则自动取各节点关联的证据",
            },
            "max_nodes": {"type": "integer", "default": 20, "description": "最多组装多少个节点"},
            "max_evidence_per_node": {"type": "integer", "default": 5,
                                      "description": "每个节点下列出多少条相关证据引用"},
            "snippet_chars": {"type": "integer", "default": 400,
                              "description": "每条证据原文片段最多多少字符"},
        },
        "required": ["node_ids"],
    }
    output_schema = {
        "description": "context 文本块 + 结构化的节点/证据清单",
    }

    def _run(self, params: Dict[str, Any]) -> ToolResult:
        nids = params.get("node_ids") or []
        max_nodes = int(params.get("max_nodes", 20))
        max_ev_per_node = int(params.get("max_evidence_per_node", 5))
        snippet_chars = int(params.get("snippet_chars", 400))
        extra_ev = params.get("evidence_ids") or []

        nodes = []
        for nid in nids:
            node = self.kg.get_node(nid)
            if node:
                nodes.append(node)
        nodes = nodes[:max_nodes]

        # 收集证据 ID（显式指定优先，再按节点关联补），去重保序
        ev_order: List[str] = []
        seen_ev: set = set()

        def add_ev(eid: str) -> None:
            if eid and eid not in seen_ev:
                seen_ev.add(eid)
                ev_order.append(eid)

        for evid in extra_ev:
            add_ev(evid)
        for node in nodes:
            for eid in _evidence_ids_of_node(self.kg, node):
                add_ev(eid)
            if node.node_type == "evidence":  # 证据节点本身也作为证据入块
                add_ev(node.node_id)

        evidences = []
        for eid in ev_order:
            ev = self.kg.get_evidence(eid)
            if ev:
                evidences.append(ev)

        # —— 组装文本块 ——
        type_label = {"concept": "概念", "entity": "实体", "evidence": "证据"}
        lines: List[str] = []
        for node in nodes:
            label = type_label.get(node.node_type, node.node_type)
            lines.append(f"### [{label}] {node.name} ({node.node_id})")
            if node.description:
                lines.append(f"- 描述: {node.description[:200]}")
            # 该节点的证据引用（证据节点不引用自己）
            refs = []
            for eid in _evidence_ids_of_node(self.kg, node)[:max_ev_per_node]:
                if eid == node.node_id:
                    continue
                ev = self.kg.get_evidence(eid)
                if ev:
                    refs.append(f"[{eid}] ({ev.section_id}) {ev.name}")
            if refs:
                lines.append(f"- 相关证据: {' | '.join(refs)}")
            lines.append("")

        if evidences:
            lines.append("### 相关证据原文")
            for ev in evidences:
                sn = ev.snippet
                if len(sn) > snippet_chars:
                    sn = sn[:snippet_chars] + "..."
                lines.append(f"[{ev.node_id}] ({ev.section_path}) {ev.name}")
                lines.append(f"  {sn}")
                lines.append("")

        context = "\n".join(lines).strip()

        return ToolResult(self.name, True, data={
            "node_count": len(nodes),
            "evidence_count": len(evidences),
            "context": context,
            "node_ids": [n.node_id for n in nodes],
            "evidence_ids": [e.node_id for e in evidences],
        })


# =====================================================================
# 工具注册表
# =====================================================================

_ALL_TOOLS: List[type] = [
    GetSchemaTool,
    GetNodeByIDTool,
    GetConceptByIDTool,
    GetEntityByIDTool,
    GetEvidenceByIDTool,
    GetRelationsTool,
    GetEntitiesOfConceptTool,
    GetEvidencesOfNodeTool,
    MultiHopTraverseTool,
    SearchConceptsTool,
    SearchEntitiesTool,
    SearchEvidencesTool,
    SemanticSearchTool,
    RankResultsTool,
    AssembleContextTool,
]


class ToolRegistry:
    """
    统一管理所有工具：注册、枚举、调用。

    只接一个后端：KGDBMemory（Neo4j + ChromaDB）—— ToolRegistry(KGDBMemory(...))
    """

    def __init__(self, kg: "KGDBMemory"):  # noqa: F821
        """kg 是 KGDBMemory 实例（Neo4j + ChromaDB 后端）"""
        self.kg = kg
        self._tools: Dict[str, BaseTool] = {}
        for cls in _ALL_TOOLS:
            self._tools[cls.name] = cls(kg)

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def __getitem__(self, name: str) -> BaseTool:
        return self._tools[name]

    # ------------------------------------------------------------------
    # 元数据导出（直接喂给 LLM function calling）
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """返回所有工具的 function schema（OpenAI 格式）"""
        return [t.get_function_schema() for t in self._tools.values()]

    def get_tool_docs(self) -> str:
        """返回一行一个的文本版工具列表（给非 function calling 的 Agent 读）"""
        lines = ["### KGRetrieve 工具列表"]
        for t in self._tools.values():
            lines.append(t.short_doc())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 调用入口
    # ------------------------------------------------------------------

    def call(self, tool_name: str, params: Optional[Dict[str, Any]] = None) -> ToolResult:
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                message=f"未知工具: {tool_name}，可用工具: {self.tool_names}",
            )
        return tool(params or {})
