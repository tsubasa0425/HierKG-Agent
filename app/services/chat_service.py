# -*- coding: utf-8 -*-
"""对话编排层 —— 在 run_agent 外面包一层热问题缓存（L1/L2/L3）。

一次对话的完整事件流：
  1. 热缓存判定：qa_cache.lookup()
       L1 答案直出   → status + done（无 LLM、无检索）
       L2 证据回放   → assemble_context 重建上下文 + stream_final_answer
                       （复用 agent 的 DeepSeek 防呆；证据失效自动降级 L3）
  2. L3 全量检索    → run_agent() 原样透传；结束后把答案+证据足迹写入缓存
                     （只缓存真检索过的条目：tool_calls > 0 且出过答案）

result 是调用方传入的 dict，结束时填 answer/tool_rounds/tool_calls/elapsed_ms/cache_hit，
供 chat.py 持久化到 messages.meta。事件字典格式与 run_agent 完全一致（SSE 兼容）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from . import qa_cache
from .kg_version import get_kg_version
from .langgraph_agent import (
    ANSWER_SYSTEM_PROMPT,
    MAX_TOOL_ROUNDS,
    _AgentAbort,
    _build_chat,
    run_agent,
    stream_final_answer,
)

logger = logging.getLogger("hierkg-web.chat_service")

_DEFAULT_THETA_EXACT = 0.96
_DEFAULT_THETA_NEAR = 0.85


def _run_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """构造 stream_final_answer 所需的运行配置（L2 回放用，无工具 LLM）。"""
    return {
        "llm_plain": _build_chat(cfg, None),
        "max_rounds": MAX_TOOL_ROUNDS,
        "t0": time.perf_counter(),
    }


async def _replay_from_cache(
    registry,
    run_cfg: Dict[str, Any],
    question: str,
    entry: Dict[str, Any],
    writer: Callable[[Dict[str, Any]], None],
) -> Optional[str]:
    """L2 证据回放：按缓存证据 id 重建上下文 → 走回答轮流式生成。

    返回答案文本；证据失效/重建失败返回 None（调用方降级到 L3）。
    """
    try:
        node_ids = json.loads(entry.get("node_ids") or "[]")
        evidence_ids = json.loads(entry.get("evidence_ids") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if not node_ids and not evidence_ids:
        return None

    ctx = await asyncio.to_thread(
        registry.call, "assemble_context",
        {"node_ids": node_ids, "evidence_ids": evidence_ids})
    if not ctx.success or not (ctx.data or {}).get("context"):
        return None

    messages = [
        SystemMessage(content=ANSWER_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"用户问题：{question}\n\n"
            f"以下为已预先检索到的证据（来自高频问题缓存）：\n{ctx.data['context']}")),
    ]
    return await stream_final_answer(messages, run_cfg, writer)


async def run_agent_chat(
    registry,
    client: Any,
    cfg: Dict[str, Any],
    message: str,
    history: List[Dict[str, Any]],
    result: Dict[str, Any],
    chat_cfg: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """完整一次对话事件流（含热缓存判定）。yield 的事件可直接序列化为 SSE。"""
    cache_cfg = chat_cfg or {}
    enabled = bool(cache_cfg.get("ENABLED", True))
    theta_exact = float(cache_cfg.get("THETA_EXACT", _DEFAULT_THETA_EXACT))
    theta_near = float(cache_cfg.get("THETA_NEAR", _DEFAULT_THETA_NEAR))
    kg_ver = get_kg_version()
    t0 = time.perf_counter()

    # ---------- 1. 热缓存判定（L1 / L2） ----------
    hit = None
    if enabled:
        hit = await asyncio.to_thread(qa_cache.lookup, message, kg_ver, theta_exact, theta_near)

    if hit is not None:
        qa_cache.mark_hit(hit.entry["id"])
        if hit.level == "answer":
            # L1 答案直出：连 LLM 都不调
            answer = hit.entry.get("answer") or ""
            yield {"event": "status", "data": {
                "status": "answering", "message": "命中高频问题缓存，正在返回缓存答案..."}}
            result.update({
                "answer": answer, "tool_rounds": 0, "tool_calls": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "cache_hit": "answer",
            })
            yield {"event": "done", "data": dict(result)}
            return

        # L2 证据回放：0 轮检索 + 1 次 LLM
        yield {"event": "status", "data": {
            "status": "answering", "message": "命中高频问题缓存，正在基于缓存证据生成回答..."}}
        run_cfg = _run_config(cfg)
        queue: asyncio.Queue = asyncio.Queue()

        def writer(ev: Dict[str, Any]) -> None:
            queue.put_nowait(ev)

        task = asyncio.ensure_future(
            _replay_from_cache(registry, run_cfg, message, hit.entry, writer))
        while not (task.done() and queue.empty()):
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield {"event": ev["event"], "data": ev["data"]}
        try:
            answer = await task
        except _AgentAbort:
            return  # error 已由 stream_final_answer 发出，静默收尾
        except Exception as exc:
            logger.exception("缓存证据回放异常")
            yield {"event": "error", "data": {"message": f"缓存回放失败: {exc}"}}
            return

        if answer is None:
            hit = None  # 证据失效 → 掉到 L3 全量检索
        else:
            result.update({
                "answer": answer, "tool_rounds": 0, "tool_calls": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "cache_hit": "evidence",
            })
            yield {"event": "done", "data": dict(result)}
            return

    # ---------- 2. L3 全量检索 ----------
    full_history = [*history, {"role": "user", "content": message}]
    async for ev in run_agent(registry, client, cfg, full_history):
        if ev["event"] == "done":
            # done 事件本身补上 cache_hit（前端徽章用；data 后续会被 record 复用）
            ev["data"]["cache_hit"] = "none"
        yield ev
        if ev["event"] == "done":
            data = ev["data"]
            result.update({
                "answer": data.get("answer", ""),
                "tool_rounds": data.get("tool_rounds", 0),
                "tool_calls": data.get("tool_calls", 0),
                "elapsed_ms": data.get("elapsed_ms", 0),
                "cache_hit": "none",
            })
            # 只缓存真检索过的条目（闲聊/自我介绍不污染缓存）
            if enabled and data.get("tool_calls", 0) > 0 and data.get("answer"):
                await asyncio.to_thread(
                    qa_cache.record, message, data.get("answer", ""),
                    data.get("evidence_ids") or [], data.get("node_ids") or [],
                    kg_ver, theta_near)
