# -*- coding: utf-8 -*-
"""
mcp_server —— 把 KGRetrieve 的检索工具封装成 MCP server
========================================================

Pi Agent / Claude / 任意支持 MCP 的 Agent 通过 stdio 连上来，就能自主调用
KGRetrieve 的全部检索工具（搜索 / 精确查找 / 图遍历 / 重排 / context 组装）。

前提：Neo4j + ChromaDB + Ollama 已就绪，且已执行过 import_kg。

运行：
    python -m src.KGRetrieve.mcp_server              # stdio（本地 Agent 默认）
    python -m src.KGRetrieve.mcp_server --transport streamable-http   # HTTP

Agent 侧接入示例（Claude Code .mcp.json 风格）：
    {
      "mcpServers": {
        "hierkg": {
          "command": "conda",
          "args": ["run", "-n", "TreeKG", "python", "-m", "src.KGRetrieve.mcp_server"],
          "cwd": "d:\\workspace\\HierKG-Agent"
        }
      }
    }

每个工具的参数 schema 直接取自 BaseTool.parameters（JSON Schema），
通过动态生成带 Annotated 注解的函数签名让 MCP SDK 原样透传给 Agent。
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.KGRetrieve import KGDBMemory, ToolRegistry

from mcp.server.mcpserver.server import MCPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON Schema → Python 注解
# ---------------------------------------------------------------------------

def _base_annotation(pschema: Dict[str, Any]) -> Any:
    """JSON Schema 类型 → Python 注解（array 细化为 list[item]）"""
    t = pschema.get("type", "string")
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        items = pschema.get("items") or {}
        return List[_base_annotation(items)]
    if t == "object":
        return Dict[str, Any]
    return str


def _build_tool_fn(tool, registry):
    """按工具的 parameters JSON Schema 生成 MCP tool 函数。

    返回一个 `**kwargs` 分发函数，但通过 __signature__ 声明带类型注解和
    默认值的形参，让 MCP SDK 的 func_metadata 生成与 BaseTool.parameters
    一致的 JSON Schema。
    """
    props = (tool.parameters or {}).get("properties", {})
    required = set((tool.parameters or {}).get("required", []))

    params: List[inspect.Parameter] = []
    for pname, pschema in props.items():
        ann: Any = _base_annotation(pschema)
        desc = pschema.get("description")
        if desc:
            ann = Annotated[ann, Field(description=desc)]
        default = pschema.get("default", inspect.Parameter.empty)
        if pname in required:
            default = inspect.Parameter.empty
        elif default is inspect.Parameter.empty:
            default = None
        params.append(inspect.Parameter(
            pname,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=default,
            annotation=ann,
        ))

    def call(**kwargs):
        result = registry.call(tool.name, kwargs)
        return json.dumps(result.to_dict(), ensure_ascii=False)

    call.__signature__ = inspect.Signature(params)
    call.__name__ = tool.name
    call.__doc__ = tool.description
    return call


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def build_server() -> MCPServer:
    """连接 KGDBMemory，注册全部工具，返回就绪的 MCP server"""
    kg = KGDBMemory()
    registry = ToolRegistry(kg)

    server = MCPServer(
        "hierkg",
        title="HierKG 四层知识图谱检索",
        version="1.0.0",
    )

    tool_names = list(registry.tool_names)
    for name in tool_names:
        tool = registry.get(name)
        if tool is None:
            continue
        server.add_tool(
            _build_tool_fn(tool, registry),
            name=tool.name,
            description=tool.description,
        )
    logger.info("MCP server 就绪：%d 个工具：%s", len(tool_names), ", ".join(tool_names))
    return server


async def _list_tool_count(server: MCPServer) -> int:
    """注册的工具数量（MCP SDK 的 list_tools 是协程）"""
    listing = await server.list_tools()
    return len(listing.tools) if hasattr(listing, "tools") else len(listing)


def _main() -> None:
    ap = argparse.ArgumentParser(description="KGRetrieve MCP server")
    ap.add_argument("--transport", choices=["stdio", "sse", "streamable-http"],
                    default="stdio", help="MCP 传输方式，默认 stdio")
    ap.add_argument("--http-port", type=int, default=8000,
                    help="--transport 为 sse / streamable-http 时的监听端口")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    server = build_server()
    import asyncio
    n_tools = asyncio.run(_list_tool_count(server))

    if args.transport == "stdio":
        print(f"✅ HierKG MCP server 就绪（stdio，{n_tools} 个工具）",
              file=sys.stderr, flush=True)
        server.run(transport="stdio")
    else:
        print(f"✅ HierKG MCP server 就绪（{args.transport}，端口 {args.http_port}）",
              file=sys.stderr, flush=True)
        server.run(transport=args.transport, port=args.http_port)


def main() -> None:
    _main()


if __name__ == "__main__":
    main()