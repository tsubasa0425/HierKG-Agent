# -*- coding: utf-8 -*-
"""Agent 对话接口 —— POST /api/chat/stream（SSE 流式，会话式）。

SSE 协议（原生事件直通，前端只按 data.type 分派）：
    event: message
    data:  {type: "REPLY_START" | "TEXT_BLOCK_*" | "THINKING_BLOCK_*" | ...,
            ...}          ← AgentScope 原生事件（reply_id/block_id/tool_call_id 归组）
    data:  {type: "session", session_id}                      ← 回传实际会话 id
    data:  {type: "TURN_DONE", data: {answer, tool_rounds, tool_calls,
            elapsed_ms, cache_hit, evidence_ids, node_ids}}   ← 一次回复权威收尾
    data:  {type: "ERROR", data: {message}}                   ← 模型失败/超时

向后兼容：请求体含 messages 数组且无 message → 走旧格式直跑（不持久化），
事件名沿用旧词表（status/tool_call/...），供无会话历史脚本/测试使用。

会话流程：加载会话历史 → 编排层（热缓存 + AgentScope）流式输出 →
          TURN_DONE/ERROR 收敛后由 finally 按 result 持久化消息与 meta。
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
from app.services.agentscope_agent import run_agent
from app.services.chat_service import run_agent_chat

logger = logging.getLogger("hierkg-web.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_LIMIT = 10  # 喂给 agent 的多轮历史条数（正序，截断最后 N 条）


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


def _sse_message(frame: dict) -> str:
    """主协议：event 名统一 message，前端只按 data.type 分派。"""
    return _sse("message", frame)


def _sse_error(message: str) -> StreamingResponse:
    return StreamingResponse(
        iter([_sse_message({"type": "ERROR", "data": {"message": message}})]),
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
        yield _sse_message({"type": "session", "session_id": session_id})
        # 先落用户消息：流中断也保留问题
        await asyncio.to_thread(chat_store.append_message, session_id, "user", message, {})
        try:
            async for frame in run_agent_chat(registry, cfg, message, history, result, chat_cfg):
                yield _sse_message(frame)
        except Exception as exc:
            logger.exception("chat stream error")
            yield _sse_message({"type": "ERROR", "data": {"message": str(exc)}})
        finally:
            answer = result.get("answer", "")
            if answer:
                meta = {k: result.get(k) for k in
                        ("tool_rounds", "tool_calls", "elapsed_ms", "cache_hit",
                         "usage")}
                await asyncio.to_thread(chat_store.append_message, session_id, "assistant", answer, meta)
            await asyncio.to_thread(chat_store.touch_session, session_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _legacy_stream(request: Request, cfg: dict, messages: List[ChatMessage]) -> StreamingResponse:
    """旧格式（messages 数组）直跑 run_agent：兼容适配器仍出旧事件词表，不持久化、无热缓存。"""
    registry = get_registry(request)
    client = make_client(cfg)   # run_agent 兼容签名保留（已不用）
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
