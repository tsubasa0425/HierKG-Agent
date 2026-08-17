# -*- coding: utf-8 -*-
"""Agent 对话循环 —— 兼容层（shim）。

原手写 ReAct 循环（while + yield SSE 事件）已等价迁移到
LangGraph StateGraph：app/services/langgraph_agent.py。

本模块仅 re-export，保证 app/routers/chat.py 的
`from app.services.agent_loop import run_agent` 继续可用，
前端与 chat.py 均零改动。
"""
from app.services.langgraph_agent import (
    HISTORY_LIMIT,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOOL_ROUNDS,
    _strip_think,
    build_system_prompt,
    compact_summary,
    run_agent,
    truncate_tool_result,
)

__all__ = [
    "run_agent",
    "build_system_prompt",
    "truncate_tool_result",
    "compact_summary",
    "_strip_think",
    "MAX_TOOL_ROUNDS",
    "MAX_TOOL_RESULT_CHARS",
    "HISTORY_LIMIT",
]