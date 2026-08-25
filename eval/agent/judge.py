# -*- coding: utf-8 -*-
"""LLM-as-judge：DeepSeek 四维判分（faithfulness / completeness / relevance / citation），
1–5 分 + 一句理由，严格 JSON 输出。失败退避重试 3 次，仍失败回退确定性启发式并标记
judge_source。报告只在 judge_source == "llm" 的行上断言硬指标。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Set

from app.config import make_client

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_NUM_RE = re.compile(r"[1-5]")


def _bigrams(s: str) -> Set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _dice(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return round(2.0 * len(A & B) / (len(A) + len(B)), 4)


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    """容错解析：直接 loads → 剥代码围栏 → brace 扫描。"""
    if not content:
        return None
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(content)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    start, end = content.find("{"), content.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(content[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _norm_scores(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 judge 返回里挑出四维分数（数值或含数字的字符串），缺任一维则判无效。"""
    def pick(k: str) -> Optional[int]:
        v = obj.get(k)
        if isinstance(v, (int, float)) and 1 <= int(v) <= 5:
            return int(v)
        if isinstance(v, str):
            m = _NUM_RE.search(v)
            if m:
                return int(m.group(0))
        return None
    keys = ["faithfulness", "completeness", "relevance", "citation"]
    scores = {k: pick(k) for k in keys}
    if any(s is None for s in scores.values()):
        return None
    return {**scores, "reason": str(obj.get("reason", ""))[:500]}


async def _llm_score(cfg: Dict[str, Any], prompt: str) -> Optional[Dict[str, Any]]:
    client = make_client(cfg)
    for attempt, delay in enumerate((0.8, 1.6, 3.2)):
        try:
            resp = await client.chat.completions.create(
                model=cfg["MODEL_NAME"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = (resp.choices[0].message.content or "") if resp.choices else ""
            obj = _extract_json(content)
            if obj:
                return _norm_scores(obj)
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(delay)
    return None


def _fallback(question: str, reference_answer: str, answer: str, golden_evidence_ids: List[str],
              cited_ids: List[str], grounding_fraction: Optional[float],
              citation_coverage: Optional[float],
              citation_weighted: Optional[float] = None) -> Dict[str, Any]:
    """确定性回退：grounding 分 → faithfulness/citation，bigram Dice → 其余。

    citation 维度优先用 rel 加权指标（定义性引用权重大），没算出来再回退到 golden 覆盖率。
    """
    cited = set(cited_ids or [])
    golden = set(golden_evidence_ids or [])
    if cited:
        faithfulness = grounding_fraction if grounding_fraction is not None else 0.0
        citation = (citation_weighted if citation_weighted is not None
                    else (len(cited & golden) / len(golden) if golden
                          else (grounding_fraction if grounding_fraction is not None else 0.0)))
    else:
        faithfulness = 0.0
        citation = 0.0
    completeness = _dice(answer, reference_answer)
    relevance = _dice(answer, question)

    def to5(v: float) -> int:
        return max(1, min(5, round(v * 4 + 1)))

    return {
        "faithfulness": to5(faithfulness),
        "completeness": to5(completeness),
        "relevance": to5(relevance),
        "citation": to5(citation),
        "reason": "fallback: LLM judge 不可用，基于引用 grounding/rel 加权与 bigram Dice 的启发式评分",
    }


def _rels_text(citation_rels: Optional[Dict[str, str]]) -> str:
    """把被引证据的引用强度分类渲染成 judge 的输入块。"""
    if not citation_rels:
        return ""
    lines = []
    for eid, rel in sorted(citation_rels.items()):
        label = "定义性（described_by，该证据在定义/讲解相关概念，强）" if rel == "definition" \
            else "提及性（appears_in，该证据只是顺带提到，弱）"
        lines.append(f"- [{eid}]: {label}")
    return "\n".join(lines)


async def score(cfg: Dict[str, Any], *, question: str, reference_answer: str,
                golden_evidence_ids: List[str], snippets: List[Dict[str, Any]],
                answer: str, cited_ids: List[str], retrieved_ids: Optional[List[str]] = None,
                grounding_fraction: Optional[float],
                citation_coverage: Optional[float],
                citation_rels: Optional[Dict[str, str]] = None,
                citation_weighted: Optional[float] = None) -> Dict[str, Any]:
    """评一道题。返回 {faithfulness, completeness, relevance, citation, reason, judge_source}。"""
    if not answer:
        return {
            "faithfulness": 1, "completeness": 1, "relevance": 1, "citation": 1,
            "reason": "答案为空", "judge_source": "fallback",
        }

    golden_text = "、".join(golden_evidence_ids or ["（无）"])
    snip_text = "\n".join(f"- [{s['id']}] {s['snippet']}" for s in snippets) or "（无检索证据片段）"
    retrieved_text = "、".join(retrieved_ids or []) or "（无）"
    rels_block = _rels_text(citation_rels)
    rels_instruction = (
        "\n- citation（引用质量）评分时，**定义性引用比提及性引用权重更高**：定义性引用是"
        "该证据在定义/讲解问题涉及的概念（强），提及性引用只是顺带提到（弱）。\n"
        f"【答案引用的引用强度】（judge 已按图谱边类型解析；未列出的引用视为中性）\n{rels_block}\n"
        if rels_block else ""
    )
    prompt = f"""你是严格的 RAG 系统评测员。请对「系统给出的答案」按四个维度各打 1–5 分（整数），并给出一句理由。

【用户问题】{question}
【参考答案】{reference_answer}
【黄金证据 ID】{golden_text}
【系统实际检索到的证据 ID】（答案若引用，引用必须出现在此列表中才有效）
{retrieved_text}
【检索证据片段】（供核对引用内容是否支撑答案）
{snip_text}

【系统答案】
{answer}

评分标准：
- faithfulness（忠实度）：答案内容是否都能由检索到的证据支撑，有没有编造。
- completeness（完整性）：相对参考答案，答案覆盖了哪些关键点，有没有漏答。
- relevance（相关性）：答案是否紧扣问题本身，有没有跑题。
- citation（引用质量）：答案是否用 [ev_...] 引用「系统实际检索到的证据 ID」列表中的证据，引用是否准确、充分（引用列表之外的 ID 视为无效引用）。{rels_instruction}
只输出一个 JSON 对象，格式：
{{"faithfulness": 5, "completeness": 4, "relevance": 5, "citation": 3, "reason": "一句话理由"}}"""

    scores = await _llm_score(cfg, prompt)
    if scores:
        return {**scores, "judge_source": "llm"}
    return {
        **{k: v for k, v in _fallback(question, reference_answer, answer, golden_evidence_ids,
                                      cited_ids, grounding_fraction, citation_coverage,
                                      citation_weighted).items()
           if k != "reason"},
        "reason": "LLM judge 3 次重试失败，使用启发式回退评分",
        "judge_source": "fallback",
    }
