# -*- coding: utf-8 -*-
"""对话编排层 —— 在 stream_reply（AgentScope）外面包一层热问题缓存判定。

一次对话的帧流（顶层 type 分派，chat.py 逐帧序列化 SSE）：
  1. 热缓存判定：qa_cache.lookup()
       L1 答案直出 → 直接应用层 TURN_DONE（cache_hit:"answer"，无 LLM、无检索）
       L2 证据命中 → 生成 preload（缓存证据 id），交给 Agent：缓存提示里指导它
                      调 assemble_context 按 id 重建上下文即答，不再关键词检索
  2. 否则走完整 Agent（cache_hit 由编排观察 check_qa_cache / 检索工具用法判定）

帧来源：
  原生事件     —— stream_reply 直通（type=REPLY_START / TOOL_CALL_* / TEXT_* …）
  应用层收尾   —— stream_reply 末尾的 TURN_DONE（answer + 检索统计）
                —— ERROR（模型超时/调用失败）

record 只发生在「真检索过的全量回合」：无 preload 且 tool_calls>0 且出过答案，
闲聊/命中缓存的回合不污染 qa_cache。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from . import qa_cache
from .agentscope_agent import stream_reply
from .kg_version import get_kg_version

logger = logging.getLogger("hierkg-web.chat_service")

_DEFAULT_THETA_EXACT = 0.96
_DEFAULT_THETA_NEAR = 0.85


async def run_agent_chat(
    registry,
    cfg: Dict[str, Any],
    message: str,
    history: List[Dict[str, Any]],
    result: Dict[str, Any],
    chat_cfg: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """完整一次对话帧流（含热缓存判定）。yield 的帧 dict 可直接序列化为 SSE。

    result 是调用方传入的 dict，TURN_DONE 时填入权威终态
    （answer/tool_rounds/tool_calls/elapsed_ms/cache_hit），供 chat.py 持久化。
    """
    cache_cfg = chat_cfg or {}
    enabled = bool(cache_cfg.get("ENABLED", True))
    theta_exact = float(cache_cfg.get("THETA_EXACT", _DEFAULT_THETA_EXACT))
    theta_near = float(cache_cfg.get("THETA_NEAR", _DEFAULT_THETA_NEAR))
    kg_ver = get_kg_version()
    t0 = time.perf_counter()

    # ---------- 1. 热缓存判定（L1 / L2 只定位，不重建 LLM 路径） ----------
    preload: Optional[Dict[str, Any]] = None
    hit = None
    if enabled:
        hit = await asyncio.to_thread(
            qa_cache.lookup, message, kg_ver, theta_exact, theta_near)

    if hit is not None:
        qa_cache.mark_hit(hit.entry["id"])
        if hit.level == "answer":
            # L1 答案直出：连 LLM 都不调，0 轮 0 调用
            answer = hit.entry.get("answer") or ""
            result.update({
                "answer": answer, "tool_rounds": 0, "tool_calls": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "cache_hit": "answer",
            })
            yield {"type": "TURN_DONE", "data": dict(result)}
            return
        # L2 证据命中：把缓存证据 id 作为预热交给 Agent（0 关键词检索）
        try:
            node_ids = json.loads(hit.entry.get("node_ids") or "[]")
            evidence_ids = json.loads(hit.entry.get("evidence_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            node_ids, evidence_ids = [], []
        if node_ids or evidence_ids:
            preload = {"node_ids": node_ids, "evidence_ids": evidence_ids}
        # id 空 → 缓存失效，掉 L3 全量

    # ---------- 2. Agent 全流程（L3，或带 preload 的 L2 证据回放） ----------
    async for frame in stream_reply(
            registry, cfg, message, history,
            preload=preload, theta_exact=theta_exact,
            theta_near=theta_near, kg_ver=kg_ver):
        ftype = frame.get("type")

        if ftype == "TURN_DONE":
            data = frame["data"]
            result.update({k: data.get(k) for k in (
                "answer", "tool_rounds", "tool_calls", "elapsed_ms", "cache_hit")})
            # 只缓存真检索过的全量回合（preload 命中本身已存在于缓存）
            if (enabled and preload is None
                    and data.get("tool_calls", 0) > 0 and data.get("answer")):
                await asyncio.to_thread(
                    qa_cache.record, message, data.get("answer", ""),
                    data.get("evidence_ids") or [], data.get("node_ids") or [],
                    kg_ver, theta_near)
            yield frame
            return

        if ftype == "ERROR":
            result["error"] = (frame.get("data") or {}).get("message", "")
            yield frame
            return

        yield frame
