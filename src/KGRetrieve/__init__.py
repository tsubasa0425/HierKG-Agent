"""
KGRetrieve —— 面向 Agent 的四层知识图谱检索工具包
====================================================

提供可被 Pi Agent / LangChain / LlamaIndex 等 Agent 框架自主调用的
细粒度原子检索工具。Agent 自己决定调用什么工具、调用几轮、何时停止。

核心抽象：
    KGMemory   : 内存中的四层图索引，零外部数据库依赖
    BaseTool   : 工具基类，统一输入/输出/元数据约定
    ToolRegistry: 工具注册表，枚举可用工具并生成 Agent-friendly 描述
"""

from .memory import KGMemory
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
    GetEntitiesOfConceptTool,
    GetEvidencesOfNodeTool,
    GetSchemaTool,
    GetNodeByIDTool,
)

# 数据库后端（可选依赖，需要 neo4j + chromadb + ollama）
try:
    from .db_backend import KGDBMemory, import_kg
    _DB_AVAILABLE = True
except ImportError:
    KGDBMemory = None
    import_kg = None
    _DB_AVAILABLE = False

__all__ = [
    "KGMemory",
    "KGDBMemory" if _DB_AVAILABLE else None,
    "import_kg" if _DB_AVAILABLE else None,
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
    "GetEntitiesOfConceptTool",
    "GetEvidencesOfNodeTool",
    "GetSchemaTool",
]
__all__ = [x for x in __all__ if x is not None]
