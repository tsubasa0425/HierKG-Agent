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


# 跨层边类型 → 引用强度：described_by 是定义性（证据在"讲"此节点），appears_in 是提及性。
_CROSS_LAYERS = ("L1-L3", "L2-L3")
_REL_WEIGHT = {"described_by": 1.0, "appears_in": 0.5}


def resolve_citation_rels(registry, golden_node_ids, cited_ids):
    """被引证据 → 引用强度。从黄金节点的跨层出边解析：
    described_by=definition(定义/1.0)，appears_in=mention(提及/0.5)。
    同一证据被多个黄金节点以不同边类型挂靠时取强（definition）。
    黄金节点为空或证据不在黄金节点出边上 → 不算强度（中性 1.0，交给 judge 内容判断）。
    """
    cited = set(cited_ids or [])
    rels: Dict[str, str] = {}
    weights: Dict[str, float] = {}
    for nid in (golden_node_ids or []):
        try:
            edges = registry.kg.get_edges(nid, direction="out")
        except Exception:
            continue
        for e in edges:
            if e.layer not in _CROSS_LAYERS or e.target not in cited:
                continue
            w = _REL_WEIGHT.get(e.type, 1.0)
            if e.target not in weights or w > weights[e.target]:
                weights[e.target] = w
                rels[e.target] = "definition" if e.type == "described_by" else "mention"
    return rels, weights


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
    retrieved_set = set(retrieved)
    golden = set(item.get("golden_evidence_ids") or [])
    golden_nodes = list((item.get("golden_concept_ids") or [])
                        + (item.get("golden_entity_ids") or []))
    citation_rels, rel_weights = resolve_citation_rels(registry, golden_nodes, cited)

    # rel 加权引用真实性：定义性引用权重 1.0、提及性 0.5；未挂靠黄金节点的引用中性 1.0。
    # 分母=全部引用的权重和，分子=其中真的被检索到的权重和——定义性引用若没检索到，
    # 比提及性引用没检索到更伤分。
    if cited:
        w_total = sum(rel_weights.get(e, 1.0) for e in cited)
        w_grounded = sum(rel_weights.get(e, 1.0) for e in cited if e in retrieved_set)
        citation_weighted = w_grounded / w_total if w_total else None
    else:
        citation_weighted = None

    row.update({
        "no_done": False,
        "answer": answer,
        "tool_rounds": done.get("tool_rounds", 0),
        "tool_calls": done.get("tool_calls", len(trace["tool_calls"])),
        "elapsed_ms": done.get("elapsed_ms", 0),
        "cited_ids": cited,
        "n_cited": len(cited),
        # grounding：答案引用的证据里，有多大比例是系统真的检索到的
        "grounding_fraction": (len(retrieved_set & set(cited)) / len(cited)
                               if cited else None),
        "ungrounded_citations": sorted(set(cited) - retrieved_set),
        # 引用覆盖率：golden 证据里被答案引用的比例
        "citation_coverage": (len(set(cited) & golden) / len(golden)
                              if golden else None),
        # 引用强度：被引证据相对黄金节点的边类型（definition=定义性 / mention=提及性）
        "citation_rels": citation_rels,
        # rel 加权引用真实性：0–1，定义性引用权重大
        "citation_weighted": citation_weighted,
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
            citation_rels=citation_rels,
            citation_weighted=citation_weighted,
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
