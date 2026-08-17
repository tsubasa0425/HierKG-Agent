# -*- coding: utf-8 -*-
"""Agent 层 runner：驱动完整 run_agent，收集 SSE 轨迹，算引用覆盖率 + grounding。

关键纪律：SSE 的 tool_result data 被 compact_summary 压成计数，不含 node_id，
所以引用 grounding 用「重放」——对每条 tool_call 事件（SSE 带完整 arguments）
重新 registry.call()，用 extract_evidence_ids 汇总系统真实检索到的证据集合。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Set

from eval.common import CITATION_RE, call_tool, extract_evidence_ids


async def run_agent_once(registry, cfg: Dict[str, Any], question: str, max_rounds: int = 8):
    """跑一次 run_agent，返回 (events, wall_ms)。"""
    from app.services.langgraph_agent import run_agent
    events: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    async for ev in run_agent(registry, None, cfg,
                              [{"role": "user", "content": question}], max_rounds=max_rounds):
        events.append(ev)
    return events, round((time.perf_counter() - t0) * 1000)


def _strip_think(text: str) -> str:
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<tool_calls>.*?</tool_calls>", "", text, flags=re.S)
    # 畸形块：有 <invoke>/<parameter> 但没有闭合 </tool_calls>（deepseek 偶发截断）
    text = re.sub(r"<tool_calls>", "", text)
    text = re.sub(r"</?invoke\b[^>]*>", "", text)
    text = re.sub(r"<parameter\b[^>]*>.*?</parameter>", "", text, flags=re.S)
    return text.strip()


def parse_trace(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    tool_calls = [e["data"] for e in events if e["event"] == "tool_call"]
    tool_results = [e["data"] for e in events if e["event"] == "tool_result"]
    errors = [e["data"] for e in events if e["event"] == "error"]
    dones = [e["data"] for e in events if e["event"] == "done"]
    return {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "errors": errors,
        "done": dones[-1] if dones else None,
    }


def replay_retrieved(registry, tool_calls: List[Dict[str, Any]]) -> List[str]:
    """重放每条 tool_call 拿完整 ToolResult.data，汇总系统真正检索到的证据 ID（保序去重）。"""
    seen: Set[str] = set()
    ordered: List[str] = []
    for tc in tool_calls:
        data = call_tool(registry, tc.get("name", ""), tc.get("arguments") or {})
        for eid in extract_evidence_ids(data):
            if eid not in seen:
                seen.add(eid)
                ordered.append(eid)
    return ordered


async def run_one(registry, cfg, item: Dict[str, Any], max_rounds: int = 8,
                  judge_fn=None) -> Dict[str, Any]:
    """跑一道题并产出轨迹 + 引用/grounding 指标；judge_fn 传入则判分。"""
    q = item["question"]
    events, wall_ms = await run_agent_once(registry, cfg, q, max_rounds=max_rounds)
    trace = parse_trace(events)
    done = trace["done"]

    row: Dict[str, Any] = {
        "id": item["id"], "category": item["category"], "question": q,
        "reference_answer": item.get("reference_answer", ""),
        "golden_concept_ids": item.get("golden_concept_ids", []),
        "golden_entity_ids": item.get("golden_entity_ids", []),
        "golden_evidence_ids": item.get("golden_evidence_ids", []),
        "n_tool_calls": len(trace["tool_calls"]),
        "n_tool_results": len(trace["tool_results"]),
        "tool_calls_log": [{"name": t["name"], "args": t.get("arguments")} for t in trace["tool_calls"]],
        "errors": [e.get("message") for e in trace["errors"]],
        "wall_ms": wall_ms,
        "has_error": bool(trace["errors"]),
    }

    if done is None:
        row["no_done"] = True
        row["answer"] = ""
        row["cited_ids"] = []
        row["grounding_fraction"] = None
        row["citation_coverage"] = None
        row["judge"] = None
        return row

    answer = _strip_think(done.get("answer", ""))
    cited = sorted({m for m in CITATION_RE.findall(answer)})
    retrieved = replay_retrieved(registry, trace["tool_calls"])
    golden = set(item.get("golden_evidence_ids") or [])

    row.update({
        "no_done": False,
        "answer": answer,
        "tool_rounds": done.get("tool_rounds", 0),
        "tool_calls": done.get("tool_calls", len(trace["tool_calls"])),
        "elapsed_ms": done.get("elapsed_ms", 0),
        "cited_ids": cited,
        "n_cited": len(cited),
        # grounding：答案引用的证据里，有多大比例是系统真的检索到的
        "grounding_fraction": (len(set(cited) & set(retrieved)) / len(cited)
                               if cited else None),
        "ungrounded_citations": sorted(set(cited) - set(retrieved)),
        # 引用覆盖率：golden 证据里被答案引用的比例
        "citation_coverage": (len(set(cited) & golden) / len(golden)
                              if golden else None),
        "retrieved_evidence_count": len(retrieved),
    })

    if judge_fn is not None:
        # 片段优先取答案引用的证据（judge 能核实引用内容），再补检索到的其余证据；
        # 全量 retrieved IDs 也传给 judge，避免「引用真实存在却因截断被判不存在」。
        snippet_ids = list(dict.fromkeys(cited + retrieved))
        snippets = _snippets_for_ids(registry, snippet_ids)
        row["judge"] = await judge_fn(
            cfg, question=q, reference_answer=item.get("reference_answer", ""),
            golden_evidence_ids=item.get("golden_evidence_ids", []),
            snippets=snippets, answer=answer, cited_ids=cited,
            retrieved_ids=retrieved,
            grounding_fraction=row["grounding_fraction"],
            citation_coverage=row["citation_coverage"],
        )
    return row


def _snippets_for_ids(registry, evidence_ids: List[str], limit: int = 5) -> List[Dict[str, Any]]:
    """从 agent 实际检索到的证据 ID 取片段（≤400 字符），按检索顺序，最多 limit 条。

    与「重新 search_evidences 的 top-5」不同——judge 看到的是系统真检索到的证据，
    避免 judge 把实际已检索到的引用误判为「不存在的证据」。
    """
    out: List[Dict[str, Any]] = []
    for eid in evidence_ids:
        if len(out) >= limit:
            break
        try:
            node = registry.kg.get_node(eid)
        except Exception:
            node = None
        if node is None:
            continue
        d = node.to_dict()
        out.append({"id": eid, "snippet": (d.get("snippet") or "")[:400]})
    return out
