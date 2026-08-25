# -*- coding: utf-8 -*-
"""Agent 对话接口 —— POST /api/chat/stream（SSE 流式）。"""
from __future__ import annotations

import json
import logging
from typing import List

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import make_client
from app.dependencies import get_registry
from app.services.agent_loop import run_agent

logger = logging.getLogger("hierkg-web.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatMessage(BaseModel):
    role: str  # user | assistant
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


def _sse(event: str, data: dict) -> str:
    """序列化一条 SSE 事件。json.dumps 会把换行转义为 \\n，不破坏 \\n\\n 分帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request):
    cfg = request.app.state.llm_cfg
    if cfg is None:
        return StreamingResponse(
            iter([_sse("error", {"message": "LLM 配置加载失败，请检查 explicit_config.yaml"})]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    registry = get_registry(request)
    client = make_client(cfg)
    history = [m.model_dump() for m in req.messages]

    async def gen():
        try:
            async for ev in run_agent(registry, client, cfg, history):
                yield _sse(ev["event"], ev["data"])
        except Exception as exc:
            logger.exception("chat stream error")
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
