# -*- coding: utf-8 -*-
"""Agent 对话循环 —— LLM 自主调用 15 个检索工具 + 流式回答。

SSE 事件协议（yield dict，由 router 序列化为 event/data）：
    status      {"status","message","round"?}      # connecting / thinking / answering
    tool_call   {"id","name","arguments","thinking_ms"}   # thinking_ms=本轮 LLM 推理耗时
    tool_result {"id","name","success","message","elapsed_ms","data"}
    chunk       {"text"}
    done        {"answer","tool_rounds","tool_calls","elapsed_ms"}
    error       {"message"}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List

from src.KGRetrieve import ToolRegistry
from src.KGRetrieve.tools import ToolResult

logger = logging.getLogger("treekg-web.agent")

MAX_TOOL_ROUNDS = 8            # 最大工具迭代轮数（防死循环）
MAX_TOOL_RESULT_CHARS = 4000   # 单条工具结果喂给 LLM 的字符上限
HISTORY_LIMIT = 10             # 多轮历史最多保留条数（防上下文爆炸）

_THINK_RE = re.compile(r"</?think>", re.IGNORECASE)
# DeepSeek 在非工具轮偶发把工具调用决策以 <tool_calls> XML 文本输出，
# 而不是给出真实回答；这类块应从最终答案里剥掉。
_TOOL_CALLS_RE = re.compile(r"</?tool_calls?\b[^>]*>", re.IGNORECASE)


def build_system_prompt(registry: ToolRegistry) -> str:
    return f"""你是 TreeKG 四层知识图谱（L1 概念 / L2 实体 / L3 证据）的检索 Agent。
可用工具：
{registry.get_tool_docs()}

规则：
1. 先用 search_concepts / search_entities / semantic_search 定位，再用 get_relations /
   multi_hop_traverse / get_entities_of_concept / get_evidences_of_node 深入。
2. 回答必须基于检索到的知识，用 [ev_xxx] 标注证据来源；查不到就明说，不要编造。
3. 检索到足够信息立即停止调用工具，给出结构化中文回答（Markdown）。
4. 控制工具参数 limit，避免一次拉取过大。"""


# ---------------------------------------------------------------------------
# 工具结果处理
# ---------------------------------------------------------------------------

def _compact(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """递归压缩工具结果：长字符串截断、列表/字典截前几项，保住结构便于 LLM 理解。"""
    if depth > max_depth:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "…"
    if isinstance(value, list):
        if len(value) > 8:
            return [_compact(x, depth + 1) for x in value[:8]] + [f"...({len(value)}项)"]
        return [_compact(x, depth + 1) for x in value]
    if isinstance(value, dict):
        return {k: _compact(v, depth + 1) for k, v in list(value.items())[:12]}
    return value


def truncate_tool_result(result: ToolResult, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """喂给 LLM 的工具结果文本（压缩 + 截断）。"""
    text = json.dumps(_compact(result.data), ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def compact_summary(result: ToolResult) -> Dict[str, Any]:
    """发前端 UI 的短摘要：只保留计数/首项，不带大段 data。"""
    d = result.to_dict()
    data = d.get("data")
    if isinstance(data, dict):
        d["data"] = {k: (len(v) if isinstance(v, list) else v) for k, v in list(data.items())[:6]}
    elif isinstance(data, list):
        d["data"] = f"[{len(data)} 项]"
    return d


def _strip_think(text: str) -> str:
    """去掉 deepseek 类模型的 <think>...</think> 推理块和 <tool_calls> 残留。"""
    text = _THINK_RE.sub("", text)
    text = _TOOL_CALLS_RE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Agent 循环
# ---------------------------------------------------------------------------

async def run_agent(
    registry: ToolRegistry,
    client: Any,            # AsyncOpenAI
    cfg: Dict[str, Any],
    messages: List[Dict[str, Any]],   # 多轮历史 [{role, content}]，工具轮消息由本循环内部追加
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> AsyncIterator[Dict[str, Any]]:
    model = cfg["MODEL_NAME"]
    timeout = cfg.get("TIMEOUT_SECS", 120)
    llm_msgs: List[Dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(registry)},
    ] + messages[-HISTORY_LIMIT:]
    tools = registry.get_tool_schemas()

    yield {"event": "status", "data": {"status": "connecting", "message": "正在连接模型..."}}

    rounds = 0
    tool_calls_count = 0
    t0 = time.perf_counter()
    forced = False

    while True:
        # —— 工具轮：非流式拿 tool_calls ——
        # 在阻塞调用前就发 thinking，让前端立刻有视觉反馈。
        # LLM 推理常需 5~15s，若等返回后再发 status，用户会看到整段静默。
        next_round = rounds + 1
        t_llm = time.perf_counter()
        yield {"event": "status", "data": {
            "status": "thinking", "round": next_round,
            "message": f"第 {next_round} 轮 · Agent 正在推理下一步检索...",
        }}
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=llm_msgs,
                tools=tools,
                temperature=0,
                timeout=timeout,
            )
        except Exception as exc:
            logger.exception("LLM 工具轮调用失败")
            yield {"event": "error", "data": {"message": f"模型调用失败: {exc}"}}
            return
        thinking_ms = round((time.perf_counter() - t_llm) * 1000)

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls

        if not tool_calls or rounds >= max_rounds:
            # 模型决定回答（无 tool_calls），或已耗尽检索轮数。
            # 后者直接带着已获取的上下文进入回答轮，保证用户始终拿到回答，
            # 而不是看到一整条 ReAct 时间线后收到"超上限"的错误。
            forced = bool(tool_calls) and rounds >= max_rounds
            break

        rounds += 1
        # assistant 消息（含 tool_calls）原样回追，供后续轮引用
        llm_msgs.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {"_raw": args}
            tool_calls_count += 1
            yield {"event": "tool_call", "data": {
                "id": tc.id, "name": name, "arguments": args, "thinking_ms": thinking_ms,
            }}

            try:
                result = await asyncio.to_thread(registry.call, name, args)
            except Exception as exc:
                logger.exception("工具 %s 执行异常", name)
                result = ToolResult(tool_name=name, success=False, message=f"{type(exc).__name__}: {exc}")

            yield {"event": "tool_result", "data": {**compact_summary(result), "id": tc.id, "name": name}}
            llm_msgs.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": truncate_tool_result(result),
            })

    # —— 最终回答轮：stream=True 转发 token（不带 tools，保证纯文本）——
    if forced:
        # 模型还想要继续检索，但轮数已到上限：明确要求它基于已有结果作答，
        # 避免它把工具调用决策以 <tool_calls> 文本形式输出而非真正回答。
        llm_msgs.append({"role": "user", "content":
            f"检索轮数已到上限（{max_rounds} 轮）。请立即基于以上已检索到的信息给出最终中文回答，不要再调用任何工具。"})
        yield {"event": "status", "data": {
            "status": "answering",
            "message": f"已进行 {max_rounds} 轮检索，基于已有信息生成回答...",
        }}
    else:
        yield {"event": "status", "data": {"status": "answering", "message": "检索完成，正在生成回答..."}}
    final = ""
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=llm_msgs,
            temperature=0,
            stream=True,
            timeout=timeout,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                final += text
                yield {"event": "chunk", "data": {"text": text}}
    except Exception as exc:
        logger.exception("LLM 回答轮失败")
        yield {"event": "error", "data": {"message": f"回答生成失败: {exc}"}}
        return

    yield {"event": "done", "data": {
        "answer": _strip_think(final),
        "tool_rounds": rounds,
        "tool_calls": tool_calls_count,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
    }}
    return
