# -*- coding: utf-8 -*-
"""AgentScope 2.0.7 驱动 —— HierKG 检索 Agent 的编排实现（替代 LangGraph 自研循环）。

一次 reply 的职责：
  1. 装配 Toolkit：15 个 KG 检索工具（registry 原样 schema）+ check_qa_cache，
     全部本地工具自动放行（覆盖 FunctionTool 默认 ASK 权限）；
  2. 单一 XML 结构化 system prompt 驱动 Agent（可选缓存预热提示）；
  3. 编排层逐事件消费 reply_stream —— 原生事件与「应用层帧」统一为
     顶层带 type 的 JSON dict 帧，router 直接序列化成 SSE；
  4. 同循环统计 tool_calls / rounds / elapsed_ms / 证据足迹 / cache_hit；
  5. 末尾发应用层 TURN_DONE 帧（权威 answer + meta），供持久化与前端收敛。

帧类型（顶层 type 分派）：
  原生事件 —— AgentScope event.model_dump(mode="json")（type=REPLY_START / …）
  应用层   —— {"type":"TURN_DONE","data":{answer,tool_rounds,tool_calls,
             elapsed_ms,cache_hit,evidence_ids,node_ids}}
              {"type":"ERROR","data":{"message":…}}
（SESSION 帧由 router 发，不属本模块。）

run_agent() 是给 eval 子系统与旧入口的兼容适配：把上面的帧流翻译回旧的事件词表
（status/tool_call/tool_result/chunk/done/error），eval 的轨迹解析/指标代码不改。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from agentscope.agent import Agent, InjectionConfig, ReActConfig
from agentscope.message import AssistantMsg, Msg, UserMsg
from agentscope.model import DeepSeekChatModel, OpenAIChatModel
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

logger = logging.getLogger("hierkg-web.agentscope_agent")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 16        # AgentScope ReAct 最大迭代（防失控；正常由提示词提前停）
STREAM_IDLE_TIMEOUT = 50   # 相邻事件间隔超时（防静默挂死，< 前端 60s stall）
STREAM_TOTAL_TIMEOUT = 240 # 整条 reply 总预算（保险丝）
MAX_TOOL_RESULT_CHARS = 4000  # 工具结果喂模型/前端 JSON 的字符上限

_THINK_RE = re.compile(r"</?think>", re.IGNORECASE)
_DSML_DELIM = r"(?:｜{1,2}|\|{1,2})DSML(?:｜{1,2}|\|{1,2})"
_TOOL_CALLS_RE = re.compile(
    r"<tool_calls\b[^>]*>.*?(?:</tool_calls\s*>|$)"
    r"|<" + _DSML_DELIM + r"(?:tool_calls|function_calls)\b[^>]*>.*?"
    + r"(?:</" + _DSML_DELIM + r"(?:tool_calls|function_calls)\s*>|$)"
    r"|</?tool_calls?\b[^>]*>?"
    r"|</?invoke\b[^>]*>?"
    r"|</?parameter\b[^>]*>?"
    r"|</?" + _DSML_DELIM + r"(?:tool_calls?|function_calls?|invoke|parameter)\b[^>]*>?",
    re.IGNORECASE | re.DOTALL,
)

_CACHE_TOOL_NAME = "check_qa_cache"
# 缓存判定时不视为「真实检索」的工具：check 本身 + 按 id 组装上下文的 assemble_context
_CACHE_NEUTRAL_TOOLS = {_CACHE_TOOL_NAME, "assemble_context"}


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

def build_system_prompt(registry) -> str:
    """单一 XML 结构化 system prompt（Anthropic 风格标签）。

    缓存预热时由调用方在 <cache_hint> 里告知已命中的证据 id，模型直接
    assemble_context 取回回答，不必重新检索（保住 L2 的 0 检索轮特性）。
    """
    return f"""<role>你是 HierKG 四层知识图谱（L1 概念 / L2 实体 / L3 证据）的检索 Agent。</role>
<available_tools>
{registry.get_tool_docs()}
另有 {_CACHE_TOOL_NAME}：查询高频问题缓存（返回 answer / evidence / miss）。
</available_tools>
<workflow>
1. 若用户问题可能与历史高频问题重复，先调 {_CACHE_TOOL_NAME}：answer → 原样采用；
   evidence → 用 assemble_context 按 id 重建上下文后回答；miss → 走正常检索链。
2. 正常检索先用 search_concepts / search_entities / semantic_search 定位，
   再用 get_relations / multi_hop_traverse / get_entities_of_concept /
   get_evidences_of_node 深入。
3. 检索到足够证据立即停止调用工具，给出最终中文 Markdown 回答。
</workflow>
<tool_policy>
- 各检索工具带 limit 参数，控制单次拉取量。
- assemble_context 取不到节点（图谱已更新）时回到检索工具补取。
- 若用户问题与图谱检索无关（闲聊 / 自我介绍），不要调用任何检索工具，直接作答；
  介绍自己时基于上方可用工具说明身份、检索能力与作答方式，不必强行引用证据。
</tool_policy>
<output_format>中文 Markdown，用 [ev_xxx] 标注来源章节；证据不足明说，不编造。</output_format>
<termination>证据足够即停止调用工具，给出最终答案；严禁输出任何工具调用标记（XML / DSML 一律禁止）。</termination>"""


def build_cache_hint(question: str, node_ids: List[str], evidence_ids: List[str]) -> str:
    """缓存预热提示：编排层已命中 evidence 级缓存，直接按 id 组装证据再作答。"""
    ids = sorted(set(node_ids) | set(evidence_ids))
    return (
        f"\n<cache_hint>用户问题命中高频问题缓存（evidence 级）。请直接调用 "
        f"assemble_context(node_ids=…, evidence_ids=…) 用以下 id 重建上下文后作答，"
        f"不要再执行关键词检索：\n证据/节点 id：{json.dumps(ids, ensure_ascii=False)}</cache_hint>"
    )


# ---------------------------------------------------------------------------
# 工具结果压缩 / 清洗（行为对齐 LangGraph 版，纯函数搬入）
# ---------------------------------------------------------------------------

def _compact(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """递归压缩：长字符串截断、列表/字典截前几项，保住结构便于 LLM 理解。"""
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


def _collect_ids(value: Any, acc: Set[str]) -> None:
    """递归收集工具结果里的 node/evidence id（热缓存证据足迹）。"""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("node_id", "evidence_id", "concept_id", "entity_id"):
                if isinstance(v, str) and v:
                    acc.add(v)
            elif k in ("evidence_ids", "node_ids"):
                if isinstance(v, (list, tuple)):
                    for x in v:
                        if isinstance(x, str) and x:
                            acc.add(x)
            else:
                _collect_ids(v, acc)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_ids(item, acc)


def _evidence_ids_from_answer(text: str) -> List[str]:
    """从最终答案里解析 [ev_xxx] 引用（足迹兜底）。"""
    return sorted(set(re.findall(r"\[(ev_[^\]]+)\]", text or "")))


def _tool_payload(result) -> Dict[str, Any]:
    """工具结果 payload：data 压缩（保留内容给模型）+ 截断，供 ToolChunk 文本。

    同时是前端 TOOL_RESULT 文本与模型上下文的唯一载体（对齐旧的 compact_summary
    + truncate_tool_result 的合并语义：结构 + success + 计数 + 截断）。
    """
    d = result.to_dict() if hasattr(result, "to_dict") else {
        "tool_name": getattr(result, "tool_name", ""),
        "success": bool(getattr(result, "success", False)),
        "data": getattr(result, "data", None),
        "message": getattr(result, "message", ""),
        "elapsed_ms": round(getattr(result, "elapsed_ms", 0.0), 2),
    }
    data = d.get("data")
    if isinstance(data, (dict, list)):
        compacted = _compact(data)
        try:
            s = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            s = ""
        if len(s) > MAX_TOOL_RESULT_CHARS:
            # 压缩后仍超长：保住结构头部 + 计数，正文头部原样塞给模型/前端
            d["data"] = {"$head": s[:MAX_TOOL_RESULT_CHARS],
                         "$len": len(s)}
        else:
            d["data"] = compacted
    elif isinstance(data, str) and len(data) > MAX_TOOL_RESULT_CHARS:
        d["data"] = data[:MAX_TOOL_RESULT_CHARS] + "…"
    return d


def _msg_text(msg: Msg) -> str:
    """取 Msg 的纯文本内容（text 块拼接；无 text 返回空串）。"""
    parts: List[str] = []
    for b in getattr(msg, "content", []) or []:
        t = getattr(b, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "".join(parts)


def _history_to_msgs(items: List[Dict[str, Any]]) -> List[Msg]:
    out: List[Msg] = []
    for m in items:
        c = (m.get("content") or "").strip()
        if not c:
            continue
        if m.get("role") == "assistant":
            out.append(AssistantMsg(name="assistant", content=c))
        else:
            out.append(UserMsg(name="user", content=c))
    return out


def _classify_cache(ctx: Dict[str, Any]) -> str:
    """cache_hit 三值判定（宁低估 evidence 不高估）。

    evidence/answer 只在「没调任何真实检索工具」时成立：check_qa_cache 与
    assemble_context 视为中性；预热(preload)命中按 evidence 计；否则 none。
    """
    extra = set(ctx["tools_called"]) - _CACHE_NEUTRAL_TOOLS
    if extra:
        return "none"
    if ctx.get("preloaded"):
        return "evidence"
    lvl = ctx.get("check_level")
    if lvl == "answer":
        return "answer"
    if lvl == "evidence":
        return "evidence"
    return "none"


# ---------------------------------------------------------------------------
# 工具装配
# ---------------------------------------------------------------------------

class _AutoAllowTool(FunctionTool):
    """本地检索工具：覆盖默认 ASK 权限，一律自动放行。"""

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="HierKG 本地检索工具自动放行",
        )


def _make_registry_tool(registry, name: str, ctx: Dict[str, Any]) -> _AutoAllowTool:
    """把一个 registry 工具包成 FunctionTool（schema 原样，async 内线程执行）。"""
    async def _run(**kw: Any) -> Dict[str, Any]:
        t0 = time.perf_counter()
        try:
            result = await asyncio.to_thread(registry.call, name, kw)
        except Exception as exc:  # 与 LangGraph 版同款兜底
            logger.exception("工具 %s 执行异常", name)
            from src.KGRetrieve.tools import ToolResult
            result = ToolResult(tool_name=name, success=False,
                                message=f"{type(exc).__name__}: {exc}")
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        if result.success:
            _collect_ids(result.data, ctx["touched"])   # 证据足迹（全量 data）
        payload = _tool_payload(result)
        ctx["tools_called"].add(name)
        return payload

    schema = registry.get(name).get_function_schema()["function"]
    return _AutoAllowTool(
        func=_run,
        name=schema["name"],
        description=schema["description"],
        input_schema=schema["parameters"],
        is_concurrency_safe=False,   # 顺序执行，保证 ctx.results 与事件流一一对应
    )


def _make_cache_tool(ctx: Dict[str, Any], theta_exact: float, theta_near: float,
                     kg_ver: str) -> _AutoAllowTool:
    """check_qa_cache 工具：包装 qa_cache.lookup（embedding 放线程）。"""
    from . import qa_cache

    async def _check(question: str) -> Dict[str, Any]:
        hit = await asyncio.to_thread(
            qa_cache.lookup, question, kg_ver, theta_exact, theta_near)
        if hit is None:
            ctx["check_level"] = "miss"
            return {"level": "miss", "similarity": None}
        ctx["check_level"] = hit.level
        sim = float(hit.entry.get("similarity") or 0.0)
        if hit.level == "answer":
            return {"level": "answer", "answer": hit.entry.get("answer") or "",
                    "similarity": sim}
        try:
            node_ids = json.loads(hit.entry.get("node_ids") or "[]")
            evidence_ids = json.loads(hit.entry.get("evidence_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            node_ids, evidence_ids = [], []
        return {"level": "evidence", "node_ids": node_ids,
                "evidence_ids": evidence_ids, "similarity": sim}

    return _AutoAllowTool(
        func=_check,
        name=_CACHE_TOOL_NAME,
        description=(
            "查询高频问题缓存：返回 {level: 'answer'|'evidence'|'miss'}。"
            "answer → 原样采用缓存答案；evidence → 用返回的 node_ids/evidence_ids "
            "调 assemble_context 重建上下文后回答；miss → 走正常检索链。"),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "用户问题原文（或其关键子问题）"},
            },
            "required": ["question"],
        },
        is_concurrency_safe=False,
    )


def build_toolkit(registry, ctx: Dict[str, Any], theta_exact: float,
                  theta_near: float, kg_ver: str,
                  include_cache: bool = True) -> Toolkit:
    """装配 15 个 KG 检索工具 + 可选 check_qa_cache。

    include_cache=False（eval 兼容路径）：不给 Agent check 工具，与迁移前
    的纯全量检索测量口径一致（不被 qa_cache 命中污染）。"""
    tools: List[FunctionTool] = [
        _make_registry_tool(registry, name, ctx) for name in registry.tool_names
    ]
    if include_cache:
        tools.append(_make_cache_tool(ctx, theta_exact, theta_near, kg_ver))
    return Toolkit(tools=tools)


# ---------------------------------------------------------------------------
# 模型工厂（懒缓存，进程内复用）
# ---------------------------------------------------------------------------

_MODEL_CACHE: Dict[tuple, Any] = {}


def _build_model(cfg: Dict[str, Any]):
    key = (cfg.get("API_BASE"), cfg.get("MODEL_NAME"),
           cfg.get("TIMEOUT_SECS"), cfg.get("RETRIES"))
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    base = cfg.get("API_BASE") or ""
    api_key = cfg.get("API_KEY") or ""
    model = cfg.get("MODEL_NAME") or ""
    timeout = float(cfg.get("TIMEOUT_SECS") or 60)
    client_kwargs = {"timeout": timeout}
    if "deepseek" in base.lower():
        from agentscope.credential import DeepSeekCredential
        inst = DeepSeekChatModel(
            credential=DeepSeekCredential(api_key=api_key, base_url=base),
            model=model, client_kwargs=client_kwargs)
    else:
        from agentscope.credential import OpenAICredential
        inst = OpenAIChatModel(
            credential=OpenAICredential(api_key=api_key, base_url=base),
            model=model, client_kwargs=client_kwargs)
    _MODEL_CACHE[key] = inst
    return inst


# ---------------------------------------------------------------------------
# 编排：stream_reply —— 一次回复的原生事件帧流
# ---------------------------------------------------------------------------

async def stream_reply(
    registry,
    cfg: Dict[str, Any],
    message: str,
    history: List[Dict[str, Any]],
    *,
    preload: Optional[Dict[str, Any]] = None,   # 编排层 evidence 命中 → 预热
    theta_exact: float = 0.96,
    theta_near: float = 0.85,
    kg_ver: str = "",
    enable_cache_tool: bool = True,             # eval 兼容路径置 False
) -> AsyncIterator[Dict[str, Any]]:
    """一次完整回复。yield 帧：原生 AgentScope 事件（model_dump JSON）或
    TURN_DONE / ERROR。帧顶层 type 供 router/前端分派。
    """
    ctx: Dict[str, Any] = {
        "touched": set(),
        "tools_called": set(),
        "check_level": None,
        "preloaded": bool(preload),
    }
    toolkit = build_toolkit(registry, ctx, theta_exact, theta_near, kg_ver,
                            include_cache=enable_cache_tool)
    system_prompt = build_system_prompt(registry)
    if preload:
        system_prompt += build_cache_hint(
            message, preload.get("node_ids") or [], preload.get("evidence_ids") or [])

    inputs: List[Msg] = _history_to_msgs(history)
    inputs.append(UserMsg(name="user", content=message))

    model = _build_model(cfg)
    agent = Agent(
        name="hierkg-agent",
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=MAX_ITERATIONS),
        injection_config=InjectionConfig(
            inject_runtime_state=False, emit_hint_event=False),
    )

    t0 = time.perf_counter()
    tool_rounds, tool_calls = 0, 0
    in_tool_phase = False
    final_msg: Optional[Msg] = None
    answer = ""
    gen = agent.reply_stream(inputs, yield_final_msg=True)
    deadline = time.perf_counter() + STREAM_TOTAL_TIMEOUT

    def _expired() -> bool:
        return time.perf_counter() > deadline

    try:
        while True:
            if _expired():
                raise asyncio.TimeoutError("整条回复超过总预算")
            try:
                chunk = await asyncio.wait_for(gen.__anext__(), timeout=STREAM_IDLE_TIMEOUT)
            except StopAsyncIteration:
                break

            if isinstance(chunk, Msg):            # yield_final_msg=True 的收尾 Msg
                final_msg = chunk
                continue

            etype = getattr(chunk, "type", None)
            if etype is None:                     # 理论不达：非事件也非 Msg
                continue

            if etype == "TOOL_CALL_START":
                tool_calls += 1
                if not in_tool_phase:
                    tool_rounds += 1              # 一批工具调用 ≈ 一轮 acting
                    in_tool_phase = True
            elif etype == "TOOL_RESULT_END":
                in_tool_phase = False

            frame = chunk.model_dump(mode="json")
            yield frame                            # 原生事件直通

            if etype == "REPLY_END":
                reason = frame.get("finished_reason")
                if reason not in (None, "completed"):
                    if reason == "error":
                        err = frame.get("error") or {}
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                    else:
                        msg = f"回复中断（{reason}）"
                    yield {"type": "ERROR", "data": {"message": msg or "模型调用失败"}}
                    return
    except asyncio.TimeoutError:
        logger.exception("AgentScope reply 超时")
        yield {"type": "ERROR", "data": {"message": "模型响应超时，请重试"}}
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("AgentScope reply 异常")
        yield {"type": "ERROR", "data": {"message": f"模型调用失败: {exc}"}}
        return

    # ---- 正常结束：收敛权威 answer + meta ----
    if final_msg is not None:
        answer = _msg_text(final_msg)
    touched = set(ctx["touched"])
    touched |= set(_evidence_ids_from_answer(answer))
    evidence_ids = sorted({x for x in touched if x.startswith("ev_")})
    node_ids = sorted({x for x in touched if not x.startswith("ev_")})
    yield {"type": "TURN_DONE", "data": {
        "answer": answer,
        "tool_rounds": tool_rounds,
        "tool_calls": tool_calls,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        "cache_hit": _classify_cache(ctx),
        "evidence_ids": evidence_ids,
        "node_ids": node_ids,
    }}


# ---------------------------------------------------------------------------
# 兼容适配：run_agent —— 旧事件词表（status/tool_call/tool_result/done/error）
# ---------------------------------------------------------------------------

async def run_agent(
    registry,
    client: Any,                 # 旧签名保留（已不用）
    cfg: Dict[str, Any],
    messages: List[Dict[str, Any]],   # 最后一条为当前问题
    max_rounds: int = 8,
) -> AsyncIterator[Dict[str, Any]]:
    """eval 子系统 / 旧入口用：把 stream_reply 的帧翻译回旧 {event,data} 事件流。"""
    del max_rounds                # 迭代预算由 MAX_ITERATIONS 统一约束
    history = messages[:-1]
    question = (messages[-1].get("content") or "") if messages else ""

    yield {"event": "status", "data": {"status": "connecting", "message": "正在连接模型..."}}

    # tool_call_id -> 累积的 arguments JSON 片段 / 名字（重放引用 coverage 需要完整 arguments）
    args_buf: Dict[str, str] = {}
    names: Dict[str, str] = {}

    async for frame in stream_reply(
            registry, cfg, question, history,
            theta_exact=float(cfg.get("CACHE_THETA_EXACT", 0.96)),
            theta_near=float(cfg.get("CACHE_THETA_NEAR", 0.85)),
            enable_cache_tool=False):   # eval：纯全量检索口径，与迁移前可比
        etype = frame.get("type")

        if etype == "TOOL_CALL_START":
            tid = frame.get("tool_call_id", "")
            names[tid] = frame.get("tool_call_name", "")
            args_buf[tid] = ""
            continue
        if etype == "TOOL_CALL_DELTA":
            tid = frame.get("tool_call_id", "")
            if tid in args_buf:
                args_buf[tid] += frame.get("delta") or ""
            continue
        if etype == "TOOL_CALL_END":
            tid = frame.get("tool_call_id", "")
            raw = (args_buf.pop(tid, "") or "").strip()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"_raw": raw}
            if not isinstance(parsed, dict):
                parsed = {"_raw": parsed}
            yield {"event": "tool_call", "data": {
                "id": tid, "name": names.get(tid, ""), "arguments": parsed}}
            continue
        if etype == "TOOL_RESULT_END":
            tid = frame.get("tool_call_id", "")
            # eval 只用 tool_result 计数对齐；成功态无模型调用，结果内容无关紧要
            yield {"event": "tool_result", "data": {
                "tool_name": names.get(tid, ""),
                "state": frame.get("state") or "success"}}
            continue
        if etype == "ERROR":
            yield {"event": "error", "data": {"message": frame["data"]["message"]}}
            return
        if etype == "TURN_DONE":
            d = frame["data"]
            yield {"event": "done", "data": d}
            return
        # 其余原生事件（REPLY_* / TEXT_* / THINKING_* / HINT / TOOL_RESULT_START 等）不译
        _ = etype
