# -*- coding: utf-8 -*-
"""Agent 对话接口 —— POST /api/chat/stream（SSE 流式，会话式）。

请求体（新格式）：
    { "message": str, "session_id": str | null, "user_id": str }
  - session_id 为空 → 服务端创建新会话，回复 SSE 中不显式带 id（前端可用
    /api/sessions 查询最新会话），会话消息落 SQLite。
  - 向后兼容：请求体仍含 messages 数组且无 message 时走旧逻辑（不持久化）。

流程：加载会话历史 → 编排层（热缓存 L1/L2/L3 + run_agent）流式输出 →
      完成后持久化 user/assistant 消息与检索统计 meta。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import make_client
from app.dependencies import get_registry
from app.services import chat_store
from app.services.chat_service import run_agent_chat
from app.services.langgraph_agent import run_agent

logger = logging.getLogger("hierkg-web.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_LIMIT = 10  # 喂给 agent 的多轮历史条数（与 langgraph_agent.HISTORY_LIMIT 对齐）


class ChatMessage(BaseModel):
    role: str  # user | assistant
    content: str


class ChatRequest(BaseModel):
    message: str = ""
    session_id: Optional[str] = None
    user_id: str = ""
    # 向后兼容：旧格式直接传全量历史（无会话持久化）
    messages: Optional[List[ChatMessage]] = None


def _sse(event: str, data: dict) -> str:
    """序列化一条 SSE 事件。json.dumps 会把换行转义为 \\n，不破坏 \\n\\n 分帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_error(message: str) -> StreamingResponse:
    return StreamingResponse(
        iter([_sse("error", {"message": message})]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request):
    cfg = request.app.state.llm_cfg
    if cfg is None:
        return _sse_error("LLM 配置加载失败，请检查 config.yaml")

    # 旧格式兼容：messages 数组 + 无 message → 无会话直跑（不持久化）
    if not req.message and req.messages is not None:
        return _legacy_stream(request, cfg, req.messages)

    message = (req.message or "").strip()
    if not message:
        return _sse_error("消息不能为空")

    registry = get_registry(request)
    client = make_client(cfg)
    chat_cfg = getattr(request.app.state, "chat_cfg", {}) or {}

    # 会话归属：session_id 为空或不存在 → 创建新会话
    session_id = req.session_id or None
    if not session_id or await asyncio.to_thread(chat_store.get_session, session_id) is None:
        session_id = await asyncio.to_thread(chat_store.create_session, req.user_id, message[:30])

    # 服务端历史（时间正序，截断最后 N 条），喂给编排层
    rows = await asyncio.to_thread(chat_store.get_messages, session_id, limit=HISTORY_LIMIT * 2)
    history = [{"role": r["role"], "content": r["content"]} for r in rows]

    result: dict = {}

    async def gen():
        # 回传实际使用的 session_id（前端陈旧 id 被重建时更新本地）
        yield _sse("session", {"session_id": session_id})
        # 先落用户消息：流中断也保留问题
        await asyncio.to_thread(chat_store.append_message, session_id, "user", message, {})
        try:
            async for ev in run_agent_chat(registry, client, cfg, message, history, result, chat_cfg):
                yield _sse(ev["event"], ev["data"])
        except Exception as exc:
            logger.exception("chat stream error")
            yield _sse("error", {"message": str(exc)})
        finally:
            answer = result.get("answer", "")
            if answer:
                meta = {k: result.get(k) for k in
                        ("tool_rounds", "tool_calls", "elapsed_ms", "cache_hit")}
                await asyncio.to_thread(chat_store.append_message, session_id, "assistant", answer, meta)
            await asyncio.to_thread(chat_store.touch_session, session_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _legacy_stream(request: Request, cfg: dict, messages: List[ChatMessage]) -> StreamingResponse:
    """旧格式（messages 数组）直跑 run_agent，不持久化、无热缓存。"""
    registry = get_registry(request)
    client = make_client(cfg)
    history = [m.model_dump() for m in messages]

    async def gen():
        try:
            async for ev in run_agent(registry, client, cfg, history):
                yield _sse(ev["event"], ev["data"])
        except Exception as exc:
            logger.exception("chat stream error (legacy)")
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
