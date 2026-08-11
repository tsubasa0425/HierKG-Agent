"""
KGRetrieve —— 面向 Agent 的四层知识图谱检索工具包
====================================================

提供可被 Pi Agent / LangChain / LlamaIndex 等 Agent 框架自主调用的
细粒度原子检索工具。Agent 自己决定调用什么工具、调用几轮、何时停止。

核心抽象：
    KGDBMemory    : Neo4j + ChromaDB 数据库后端（唯一后端）
    BaseTool      : 工具基类，统一输入/输出/元数据约定
    ToolRegistry  : 工具注册表，枚举可用工具并生成 Agent-friendly 描述
"""

from .db_backend import KGDBMemory, import_kg
from .models import Node, Edge
from .tools import (
    BaseTool,
    ToolRegistry,
    SearchConceptsTool,
    SearchEntitiesTool,
    SearchEvidencesTool,
    GetConceptByIDTool,
    GetEntityByIDTool,
    GetEvidenceByIDTool,
    GetRelationsTool,
    MultiHopTraverseTool,
    SemanticSearchTool,
    RankResultsTool,
    AssembleContextTool,
    GetEntitiesOfConceptTool,
    GetEvidencesOfNodeTool,
    GetSchemaTool,
    GetNodeByIDTool,
)

__all__ = [
    "KGDBMemory",
    "import_kg",
    "Node",
    "Edge",
    "BaseTool",
    "ToolRegistry",
    "SearchConceptsTool",
    "SearchEntitiesTool",
    "SearchEvidencesTool",
    "GetConceptByIDTool",
    "GetEntityByIDTool",
    "GetEvidenceByIDTool",
    "GetNodeByIDTool",
    "GetRelationsTool",
    "MultiHopTraverseTool",
    "SemanticSearchTool",
    "RankResultsTool",
    "AssembleContextTool",
    "GetEntitiesOfConceptTool",
    "GetEvidencesOfNodeTool",
    "GetSchemaTool",
]