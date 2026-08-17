# -*- coding: utf-8 -*-
"""评测入口：python -m eval.run --mode retrieval|agent|all ...

两层评测：
  - retrieval  确定性检索层（不调 LLM、零成本），可加 --baseline 跑朴素 RAG 扁平基线做消融
  - agent      端到端 Agent 层（跑完整 run_agent + LLM-as-judge），需 DeepSeek 网络
  - all        两层都跑

产物写到 eval/runs/<UTC 时间戳>/：config.json + retrieval.jsonl / baseline.jsonl /
traces.jsonl + report.md。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from eval.common import EVAL_ROOT, git_head, json_dumps, make_run_dir, write_jsonl
from eval.datasets.resolver import GoldenResolver
from eval.retrieval import metrics as m
from eval.retrieval.baseline import FlatEvidenceBaseline
from eval.retrieval.runner import RetrievalRunner

_DEFAULT_DATASET = EVAL_ROOT / "datasets" / "treekg_qa.json"


# ---------------------------------------------------------------------------
# 环境组装与探针
# ---------------------------------------------------------------------------

def build_stack():
    """构造 KGDBMemory + ToolRegistry（与 app 共用同一套后端）。"""
    from src.KGRetrieve.db_backend import KGDBMemory
    from src.KGRetrieve.tools import ToolRegistry
    kg = KGDBMemory()
    return kg, ToolRegistry(kg)


def probe_stack(kg) -> Dict[str, Any]:
    """检测 ChromaDB/Ollama 是否降级（向量失败会静默回退到名称匹配，报告需标注）。"""
    flags: Dict[str, Any] = {"chroma_fallback": False}
    try:
        kg._ensure_chroma()
        emb = kg._embed("智能体")   # 打通 Ollama bge-m3 + chroma
        if not emb:
            flags["chroma_fallback"] = True
    except Exception:
        flags["chroma_fallback"] = True
    return flags


# ---------------------------------------------------------------------------
# 检索层
# ---------------------------------------------------------------------------


def run_retrieval(reg, resolver, dataset, limit: Optional[int], baseline: bool) -> List[Dict[str, Any]]:
    """跑检索层：每道题三层排序 + 指标；可选 baseline 扁平证据召回。"""
    rr = RetrievalRunner(reg)
    bl = FlatEvidenceBaseline(reg) if baseline else None
    questions = dataset["questions"]
    if limit:
        questions = questions[:limit]
    rows: List[Dict[str, Any]] = []
    for item in questions:
        item = resolver.resolve_item(item)
        q = item["question"]
        row: Dict[str, Any] = {
            "id": item["id"], "category": item["category"], "question": q,
            "golden_concept_ids": item.get("golden_concept_ids", []),
            "golden_entity_ids": item.get("golden_entity_ids", []),
            "golden_evidence_ids": item.get("golden_evidence_ids", []),
            "_problems": item.get("_problems", []),
        }

        cids = item.get("golden_concept_ids", [])
        eids = item.get("golden_entity_ids", [])
        gids = item.get("golden_evidence_ids", [])

        # 三层排序列表（存 top10 便于逐题复盘）
        ranked_concepts = rr.rank_concepts(q)
        ranked_entities = rr.rank_entities(q)
        ranked_evidence = rr.rank_evidences_full(q)
        row["ranked_concepts"] = ranked_concepts[:10]
        row["ranked_entities"] = ranked_entities[:10]
        row["ranked_evidence"] = ranked_evidence[:10]

        # multi-hop 题：结构化路径探针（出边直达 or 多跳可达）
        if item.get("golden_src_name") and item.get("golden_tgt_name"):
            src = resolver.resolve_concept(item["golden_src_name"])
            tgt = resolver.resolve_concept(item["golden_tgt_name"])
            if src and tgt:
                probe = rr.structural_probe(src, tgt, max_hop=2)
                row["reaches_golden"] = bool(probe.get("reached"))
                row["probe"] = {"src": src, "tgt": tgt, "hops": probe.get("hops")}
            else:
                row["reaches_golden"] = None
                row["probe"] = {"reason": "src/tgt 解析失败"}

        row["concept"] = m.question_metrics(ranked_concepts, cids) if cids else None
        row["entity"] = m.question_metrics(ranked_entities, eids) if eids else None
        row["evidence"] = m.question_metrics(ranked_evidence, gids)

        if bl:
            row["evidence_flat"] = {
                f"recall@{k}": m.recall_at_k(bl.retrieve(q, k), gids, k) for k in m.KS
            }

        rows.append(row)
    return rows


def _agg(rows: List[Dict[str, Any]], metric_keys: List[str]) -> Dict[str, Any]:
    return m.aggregate(rows, metric_keys)


def summarize_retrieval(rows: List[Dict[str, Any]], baseline: bool) -> Dict[str, Any]:
    """跨题聚合：concept / entity / evidence(KG) / flat + Δ 证据召回。"""
    all_rows = [r for r in rows if not r.get("_problems")]
    metric_keys = [f"recall@{k}" for k in m.KS] + ["hit@10", "mrr"]
    sum_ = {
        "n": len(all_rows),
        "concept": _agg([r["concept"] for r in all_rows if r.get("concept")], metric_keys),
        "entity": _agg([r["entity"] for r in all_rows if r.get("entity")], metric_keys),
        "evidence": _agg([r["evidence"] for r in all_rows], metric_keys),
    }
    if baseline:
        ev_rows = [r for r in all_rows if r.get("evidence_flat")]
        flat = _agg([r["evidence_flat"] for r in ev_rows], [f"recall@{k}" for k in m.KS])
        sum_["flat_evidence"] = flat
        kg = sum_["evidence"]
        sum_["delta_evidence_recall"] = {
            f"@k={k}": (round(kg[f"recall@{k}"] - flat[f"recall@{k}"], 4) if kg.get(f"recall@{k}") is not None
                        and flat.get(f"recall@{k}") is not None else None)
            for k in m.KS
        }

    mh = [r for r in all_rows if r.get("reaches_golden") is not None]
    if mh:
        sum_["multi_hop_reached"] = {
            "n": len(mh),
            "reached": sum(1 for r in mh if r["reaches_golden"]),
            "rate": round(sum(1 for r in mh if r["reaches_golden"]) / len(mh), 4),
        }
    return sum_


# ---------------------------------------------------------------------------
# Agent 层
# ---------------------------------------------------------------------------

def run_agent(reg, resolver, dataset, limit, judge, max_rounds, cfg,
              out_dir=None) -> List[Dict[str, Any]]:
    """跑端到端 Agent 层：逐题调 run_agent，算引用/grounding；judge 则 LLM 判分。

    顺序执行（Semaphore 1），尊重 DeepSeek 限流。--limit 用于冒烟省钱。
    每题完成即增量追加到 out_dir/traces.jsonl——中途崩溃不丢已完成题目，也便于看进度。
    """
    import asyncio
    from eval.agent.judge import score as judge_score
    from eval.agent.runner import run_one
    from eval.common import write_jsonl

    questions = dataset["questions"]
    if limit:
        questions = questions[:limit]
    sem = asyncio.Semaphore(1)

    async def _one(item):
        async with sem:
            item = resolver.resolve_item(item)
            jf = judge_score if judge else None
            return await run_one(reg, cfg, item, max_rounds=max_rounds, judge_fn=jf)

    async def _run_all():
        rows: List[Dict[str, Any]] = []
        for i, q in enumerate(questions, 1):
            try:
                row = await _one(q)
            except Exception as exc:
                row = {"id": q.get("id", f"q{i}"), "category": q.get("category"),
                       "question": q.get("question"), "run_error": str(exc),
                       "has_error": True, "no_done": True, "judge": None}
            rows.append(row)
            if out_dir is not None:
                write_jsonl(Path(out_dir) / "traces.jsonl", rows)  # 增量全量覆写，保序
            print(f"  [{i}/{len(questions)}] {row.get('id')} "
                  f"calls={row.get('n_tool_calls', '—')} "
                  f"grounding={row.get('grounding_fraction')} ", flush=True)
        return rows

    return asyncio.run(_run_all())



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="TreeKG 评测：检索层 + Agent 层 + 朴素 RAG 消融")
    ap.add_argument("--mode", choices=["retrieval", "agent", "all"], default="retrieval")
    ap.add_argument("--dataset", default=str(_DEFAULT_DATASET))
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 道题（冒烟用）")
    ap.add_argument("--baseline", action="store_true", help="跑朴素 RAG 扁平证据基线（消融）")
    ap.add_argument("--judge", action="store_true", help="Agent 层用 LLM-as-judge 判分")
    ap.add_argument("--max-rounds", type=int, default=8)
    ap.add_argument("--out", default=None, help="输出目录（默认 eval/runs/<时间戳>）")
    args = ap.parse_args(argv)

    run_dir = Path(args.out) if args.out else make_run_dir()
    resolver = GoldenResolver()
    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    print(f"数据集: {args.dataset}  ({len(data['questions'])} 题)  输出: {run_dir}")

    kg, registry = build_stack()
    stack = probe_stack(kg)
    if stack["chroma_fallback"]:
        print("⚠  ChromaDB/Ollama 向量检索不可用，降级为名称匹配——结果只代表名称匹配上限")

    config = {
        "mode": args.mode, "limit": args.limit, "baseline": args.baseline,
        "judge": args.judge, "max_rounds": args.max_rounds,
        "dataset": str(Path(args.dataset).resolve()),
        "git_head": git_head(), "stack": stack,
    }
    write_jsonl(run_dir / "config.jsonl", [config])

    if args.mode in ("retrieval", "all"):
        print("\n== 检索层 ==")
        rows = run_retrieval(registry, resolver, data, args.limit, args.baseline)
        write_jsonl(run_dir / "retrieval.jsonl", rows)
        summary = summarize_retrieval(rows, args.baseline)
        print(f"n={summary['n']}")
        print("  concept: ", {k: v for k, v in summary["concept"].items() if k in ("recall@5", "recall@10", "mrr")})
        if summary.get("entity") and summary["entity"].get("recall@10") is not None:
            print("  entity:  ", {k: v for k, v in summary["entity"].items() if k in ("recall@5", "recall@10", "mrr")})
        print("  evidence(KG): ", {k: v for k, v in summary["evidence"].items() if k in ("recall@5", "recall@10", "mrr")})
        if args.baseline:
            print("  flat 基线:   ", {k: v for k, v in summary["flat_evidence"].items() if k in ("recall@5", "recall@10")})
            print("  Δ 证据召回:  ", summary["delta_evidence_recall"])
        with open(run_dir / "retrieval_summary.json", "w", encoding="utf-8") as f:
            f.write(json_dumps(summary))

    if args.mode in ("agent", "all"):
        print("\n== Agent 层 ==")
        if not args.judge:
            print("（未加 --judge，只跑轨迹与引用指标，不判分）")
        from app.config import load_llm_config
        cfg = load_llm_config()
        rows = run_agent(registry, resolver, data, args.limit, args.judge, args.max_rounds, cfg,
                         out_dir=run_dir)
        print(f"agent 完成 {len(rows)} 题，traces.jsonl 已增量写入 {run_dir}")

    print(f"\n完成。产物在 {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
