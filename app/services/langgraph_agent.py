# -*- coding: utf-8 -*-
"""Agent 对话循环（LangGraph StateGraph 版）—— LLM 自主调用 15 个检索工具 + 流式回答。

原实现为 app/services/agent_loop.py 的手写 ReAct 循环（while + yield SSE 事件），
本文件将其等价迁移为 LangGraph StateGraph：

    START → agent ─┬─(有 tool_calls 且 rounds < max)──→ tools ──→ agent（回环）
                    ├─(有 tool_calls 但 rounds ≥ max)──→ answer（forced nudge）──→ END
                    └─(无 tool_calls)──────────────────→ answer ──→ END

SSE 事件协议（run_agent 逐条 yield dict，由 router 序列化为 event/data）：
    status      {"status","message","round"?}      # connecting / thinking / answering
    tool_call   {"id","name","arguments","thinking_ms"}   # thinking_ms=本轮 LLM 推理耗时
    tool_result {"id","name","success","message","elapsed_ms","data"}
    chunk       {"text"}
    done        {"answer","tool_rounds","tool_calls","elapsed_ms"}
    error       {"message"}

事件均经 LangGraph 的 StreamWriter 以 stream_mode="custom" 发出；
节点内已发 error 时抛 _AgentAbort 让外层静默收尾（无 done）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables.config import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph

from src.KGRetrieve.tools import ToolResult

logger = logging.getLogger("treekg-web.agent")

MAX_TOOL_ROUNDS = 8            # 最大工具迭代轮数（防死循环）
MAX_TOOL_RESULT_CHARS = 4000   # 单条工具结果喂给 LLM 的字符上限
HISTORY_LIMIT = 10             # 多轮历史最多保留条数（防上下文爆炸）

_THINK_RE = re.compile(r"</?think>", re.IGNORECASE)
# DeepSeek 在非工具轮偶发把工具调用决策以 <tool_calls> XML 文本输出，
# 而不是给出真实回答；这类块应从最终答案里剥掉。
# 注意只剥 <tool_calls> 包装不够 —— invoke/parameter 内层标签也要剥，
# 且要优先按 <tool_calls>...</tool_calls> 整块（DOTALL）剥，避免残留空壳。
_TOOL_CALLS_RE = re.compile(
    r"<tool_calls\b[^>]*>.*?</tool_calls\s*>"  # 完整 <tool_calls>...</tool_calls> 块
    r"|</?tool_calls?\b[^>]*>"                  # 孤立 tool_calls 标签（防截断）
    r"|</?invoke\b[^>]*>"
    r"|</?parameter\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
LLM_CALL_TIMEOUT = 55   # 工具轮单次 LLM 调用硬上限；< 前端 60s 停摆检测，后端先报错
LLM_CHUNK_TIMEOUT = 45  # 回答轮相邻 token 间隔上限（断流判定）


def build_system_prompt(registry) -> str:
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
# 工具结果处理（从 agent_loop.py 原样搬入，行为逐字对齐）
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
# LangGraph State 与节点
# ---------------------------------------------------------------------------

class AgentState(MessagesState, total=False):
    """MessagesState 自带 messages: Annotated[list[AnyMessage], add_messages]。

    自定义字段走 last-write-wins（LangGraph 对无 reducer 字段的默认语义）。
    """
    rounds: int            # 已完成工具轮数（agent 决定"继续工具"时才 +1，与现码 tool_rounds 语义一致）
    tool_calls_count: int  # 累计工具调用次数（发 done 用）
    last_thinking_ms: int  # 上一轮 LLM 推理耗时（agent → tools 传递）
    forced: bool           # 到轮数上限被迫进入回答轮


class _AgentAbort(Exception):
    """节点内部已发过 error 事件，信号让外层静默收尾（避免双 error / 无 done 语义丢失）。"""


async def agent(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """工具轮：非流式拿 tool_calls，三态出口决定路由。"""
    writer = get_stream_writer()
    cfg = config["configurable"]
    llm_tools, max_rounds = cfg["llm_tools"], cfg["max_rounds"]

    rounds = state.get("rounds", 0)
    next_round = rounds + 1
    # 阻塞前的 thinking 事件，前端立刻有反馈（与现码一致）
    writer({"event": "status", "data": {
        "status": "thinking", "round": next_round,
        "message": f"第 {next_round} 轮 · Agent 正在推理下一步检索...",
    }})

    t_llm = time.perf_counter()
    try:
        # wait_for 提供硬超时：deepseek 偶发静默挂死（httpx 超时不触发），
        # 55s 后报错收尾，而不是让请求无限卡住。
        msg = await asyncio.wait_for(
            llm_tools.ainvoke(state["messages"]), timeout=LLM_CALL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.exception("LLM 工具轮调用超时")
        writer({"event": "error", "data": {"message": "模型推理超时，请重试"}})
        raise _AgentAbort()
    except Exception as exc:
        logger.exception("LLM 工具轮调用失败")
        writer({"event": "error", "data": {"message": f"模型调用失败: {exc}"}})
        raise _AgentAbort()
    thinking_ms = round((time.perf_counter() - t_llm) * 1000)

    # 三态出口，与现码 if/break 逐字等价：
    if msg.tool_calls and rounds < max_rounds:             # 继续检索
        return {"rounds": next_round, "last_thinking_ms": thinking_ms, "messages": [msg]}
    if msg.tool_calls:                                     # 想检索但已到上限 → forced
        nudge = HumanMessage(content=(
            f"检索轮数已到上限（{max_rounds} 轮）。请立即基于以上已检索到的信息给出最终中文回答，不要再调用任何工具。"))
        return {"forced": True, "messages": [nudge]}
    return {"last_thinking_ms": thinking_ms}               # 决定作答：不追加任何消息（现码同样不追加）


async def tools(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """逐个执行本轮 tool_calls，SSE 双轨：tool_call/tool_result 发前端 + ToolMessage 喂 LLM。"""
    writer = get_stream_writer()
    registry = config["configurable"]["registry"]
    last = state["messages"][-1]                 # 本轮 AIMessage（带 tool_calls）
    thinking_ms = state.get("last_thinking_ms", 0)

    tool_msgs: List[AnyMessage] = []
    count = 0
    for tc in last.tool_calls:
        name = tc["name"]
        args = tc.get("args") or {}
        if isinstance(args, str):                # 防御：个别供应商给 JSON 字符串
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {"_raw": args}
        count += 1

        writer({"event": "tool_call", "data": {
            "id": tc["id"], "name": name, "arguments": args, "thinking_ms": thinking_ms,
        }})

        try:
            result = await asyncio.to_thread(registry.call, name, args)   # 不阻塞事件循环
        except Exception as exc:                   # 与现码同款兜底
            logger.exception("工具 %s 执行异常", name)
            result = ToolResult(tool_name=name, success=False, message=f"{type(exc).__name__}: {exc}")

        writer({"event": "tool_result", "data": {**compact_summary(result), "id": tc["id"], "name": name}})
        tool_msgs.append(ToolMessage(
            content=truncate_tool_result(result),  # 喂 LLM 的截断文本
            tool_call_id=tc["id"], name=name,
        ))
    return {"messages": tool_msgs, "tool_calls_count": state.get("tool_calls_count", 0) + count}


async def answer(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """回答轮：stream=True 转发 token（不带 tools，保证纯文本），结尾发 done。

    防呆三件事：
    1. deepseek 偶尔在回答轮仍把工具调用以 <tool_calls> XML 文本输出 ——
       转发前逐段清洗，前端看不到原始 XML；done 里再整体剥一次兜底。
    2. 若整段只有工具调用 XML 没有文字 → 追加 nudge 重试一轮，仍无则给兜底文案。
    3. 相邻 token 间隔超时（LLM_CHUNK_TIMEOUT）→ 判死流并报错，避免无限挂起。
    """
    writer = get_stream_writer()
    cfg = config["configurable"]
    llm_plain, max_rounds, t0 = cfg["llm_plain"], cfg["max_rounds"], cfg["t0"]

    if state.get("forced"):
        writer({"event": "status", "data": {"status": "answering",
            "message": f"已进行 {max_rounds} 轮检索，基于已有信息生成回答..."}})
    else:
        writer({"event": "status", "data": {"status": "answering", "message": "检索完成，正在生成回答..."}})

    messages = state["messages"]
    stripped = ""
    for attempt in range(2):
        final = ""
        emitted = 0                                # 已转发（清洗后）的字符数
        try:
            stream = llm_plain.astream(messages)   # 无 tools → 纯文本流
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream.__anext__(), timeout=LLM_CHUNK_TIMEOUT)
                except StopAsyncIteration:
                    break
                text = chunk.content if isinstance(chunk.content, str) else ""
                if not text:
                    continue
                final += text
                clean = _strip_think(final)        # 逐段清洗，原始 XML 不透传前端
                if len(clean) > emitted:
                    writer({"event": "chunk", "data": {"text": clean[emitted:]}})
                    emitted = len(clean)
        except asyncio.TimeoutError:
            logger.exception("LLM 回答流超时")
            writer({"event": "error", "data": {"message": "回答生成超时（模型长时间无输出），请重试"}})
            raise _AgentAbort()
        except Exception as exc:
            logger.exception("LLM 回答轮失败")
            writer({"event": "error", "data": {"message": f"回答生成失败: {exc}"}})
            raise _AgentAbort()

        stripped = _strip_think(final)
        if stripped:
            break                                 # 有实质文字，直接用
        # 整段都是工具调用 XML → nudge 让它给文字回答，最多重试一轮
        nudge = HumanMessage(content=(
            "注意：上面你输出了工具调用格式，但本轮检索已结束。请直接基于已检索到的信息给出最终中文回答，"
            "不要输出任何 XML 标签（<tool_calls>、<invoke>、<parameter> 等）。"))
        messages = [*messages, nudge]
    else:
        stripped = "模型未能给出文字回答，请换个问法重试。"

    writer({"event": "done", "data": {
        "answer": stripped,
        "tool_rounds": state.get("rounds", 0),
        "tool_calls": state.get("tool_calls_count", 0),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
    }})
    return {"messages": [AIMessage(content=stripped)], "final_answer": stripped}


def router(state: AgentState) -> str:
    if state.get("forced"):
        return "answer"
    msgs = state.get("messages") or []
    if not msgs:
        return "answer"                        # 防御：离线/空输入直接作答
    last = msgs[-1]
    if getattr(last, "tool_calls", None):      # ToolMessage/UserMessage 无此属性 → None
        return "tools"
    return "answer"


def build_agent_graph() -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("answer", answer)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", router, {"tools": "tools", "answer": "answer"})
    g.add_edge("tools", "agent")               # 回环
    g.add_edge("answer", END)
    return g.compile()


AGENT_GRAPH = build_agent_graph()              # 一次性编译（图无每请求状态，全部走 configurable）


# ---------------------------------------------------------------------------
# run_agent wrapper（SSE 事件映射层）
# ---------------------------------------------------------------------------

def _history_to_messages(items: List[Dict[str, Any]]) -> List[AnyMessage]:
    out: List[AnyMessage] = []
    for m in items:
        c = m.get("content") or ""
        out.append(AIMessage(content=c) if m.get("role") == "assistant" else HumanMessage(content=c))
    return out


def _build_chat(cfg: Dict[str, Any], registry):
    """从同一 cfg 构造 ChatOpenAI；registry 非空时 bind_tools（工具轮）。"""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=cfg["MODEL_NAME"],
        api_key=cfg["API_KEY"],
        base_url=cfg["API_BASE"],            # https://api.deepseek.com → 自动拼 /chat/completions
        temperature=0,
        timeout=cfg.get("TIMEOUT_SECS", 120),
    )
    return llm.bind_tools(registry.get_tool_schemas()) if registry is not None else llm


async def run_agent(
    registry,
    client: Any,            # AsyncOpenAI —— 兼容旧签名保留，LangGraph 版不再使用
    cfg: Dict[str, Any],
    messages: List[Dict[str, Any]],   # 多轮历史 [{role, content}]，工具轮消息由图内部追加
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> AsyncIterator[Dict[str, Any]]:
    yield {"event": "status", "data": {"status": "connecting", "message": "正在连接模型..."}}

    initial = {"messages": (
        [SystemMessage(content=build_system_prompt(registry))]
        + _history_to_messages(messages[-HISTORY_LIMIT:])
    )}
    config = {"configurable": {
        "registry": registry,
        "llm_tools": _build_chat(cfg, registry),    # 带 tools
        "llm_plain": _build_chat(cfg, None),        # 不带 tools
        "max_rounds": max_rounds,
        "t0": time.perf_counter(),
    }}

    try:
        async for ev in AGENT_GRAPH.astream(initial, config, stream_mode="custom"):
            # 单 custom 模式：ev 直接就是我们 writer(ev) 传的那个 dict
            yield {"event": ev["event"], "data": ev["data"]}
    except _AgentAbort:
        return                                       # 节点内已发 error，静默收尾（无 done）
    except Exception as exc:
        logger.exception("LangGraph agent 异常")
        yield {"event": "error", "data": {"message": f"模型调用失败: {exc}"}}