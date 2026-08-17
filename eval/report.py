# -*- coding: utf-8 -*-
"""评测报告生成：python -m eval.report <run_dir> → report.md

输入：eval/runs/<timestamp>/ 下的 retrieval_summary.json、retrieval.jsonl、
     traces.jsonl（agent 层可选）、config.jsonl。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from eval.common import read_jsonl


def _fmt(v: Any, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.1%}{suffix}" if v <= 1.01 else f"{v:.2f}{suffix}"
    return f"{v}{suffix}"


def _pct(v: Any) -> str:
    return _fmt(v)  # `:.1%` 已自带 %，不要再加 suffix


def _maybe_jsonl(p: Path) -> List[Dict[str, Any]]:
    if not p.exists():
        return []
    try:
        return read_jsonl(p)
    except Exception:
        return []


def load_run(run_dir: Path) -> Dict[str, Any]:
    rows = _maybe_jsonl(run_dir / "retrieval.jsonl")
    cfg_rows = _maybe_jsonl(run_dir / "config.jsonl")
    cfg = cfg_rows[0] if cfg_rows else {}
    summary = {}
    sp = run_dir / "retrieval_summary.json"
    if sp.exists():
        try:
            summary = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            summary = {}
    traces = None
    if (run_dir / "traces.jsonl").exists():
        traces = _maybe_jsonl(run_dir / "traces.jsonl")
    return {"dir": run_dir, "retrieval": rows, "summary": summary,
            "config": cfg, "traces": traces}


def markdown(run: Dict[str, Any]) -> str:
    L: List[str] = []
    cfg, summary = run["config"], run["summary"]

    L.append("# TreeKG 评测报告\n")
    L.append(f"- 生成时间: {run['dir'].name}  (UTC 时间戳目录)")
    if cfg.get("git_head"):
        L.append(f"- git HEAD: `{cfg['git_head']}`")
    if cfg.get("dataset"):
        L.append(f"- 数据集: `{cfg['dataset']}`")
    if cfg.get("stack", {}).get("chroma_fallback"):
        L.append("- ⚠ **ChromaDB/Ollama 向量检索不可用**，检索层降级为名称匹配，本报告指标不代表向量检索上限")
    if cfg.get("judge"):
        L.append(f"- Agent 层: LLM-as-judge 判分（max_rounds={cfg.get('max_rounds', 8)}）")
    L.append("")

    # —— ② 头号消融结论 ——
    L.append("## 1. 核心结论：知识图谱检索优于扁平 RAG\n")
    if summary.get("evidence") and summary.get("flat_evidence"):
        kg10 = summary["evidence"].get("recall@10")
        flat10 = summary["flat_evidence"].get("recall@10")
        n = summary.get("n", 0)
        delta = summary.get("delta_evidence_recall", {}).get("@k=10")
        if kg10 is not None and flat10 is not None:
            L.append(
                f"KG 分层检索证据 **Recall@10 = {_pct(kg10)}** "
                f"vs 扁平向量基线 **{_pct(flat10)}**（Δ **+{_pct(delta)}** pp），N={n}。"
            )
        else:
            L.append("检索层未跑基线（缺 --baseline），无消融结论。")
    L.append("")

    # —— ③ 检索总表 ——
    L.append("## 2. 检索层指标\n")
    if summary:
        L.append("| 指标 | recall@1 | recall@3 | recall@5 | recall@10 | hit@10 | MRR |")
        L.append("|---|---|---|---|---|---|---|")
        for label, key in (("概念 (L1)", "concept"), ("实体 (L2)", "entity"),
                           ("证据 (L3, KG)", "evidence"), ("证据 (扁平基线)", "flat_evidence")):
            d = summary.get(key)
            if not d:
                continue
            L.append(f"| {label} | {_pct(d.get('recall@1'))} | {_pct(d.get('recall@3'))} | "
                     f"{_pct(d.get('recall@5'))} | {_pct(d.get('recall@10'))} | "
                     f"{_pct(d.get('hit@10'))} | {_fmt(d.get('mrr'))} |")
        if summary.get("delta_evidence_recall"):
            L.append("| **Δ 证据召回 (KG−flat)** | "
                     + " | ".join(f"{_pct(summary['delta_evidence_recall'].get(k))}"
                                  for k in ("@k=1", "@k=3", "@k=5", "@k=10")) + " | — | — |")
        L.append("")
        mh = summary.get("multi_hop_reached")
        if mh:
            L.append(f"- multi-hop 结构化路径探针：{mh['reached']}/{mh['n']} 题从起点可达黄金目标（{_pct(mh['rate'])}）")
            L.append("")

    # —— ④ 按类别 ——
    L.append("## 3. 按类别（证据 Recall@10 / @5）\n")
    cats = ["definition", "single_hop", "multi_hop", "instance", "comparison"]
    L.append("| 类别 | n | KG@5 | KG@10 | flat@5 | flat@10 | Δ@10 |")
    L.append("|---|---|---|---|---|---|---|")
    for cat in cats:
        sub = [r for r in run["retrieval"] if r.get("category") == cat]
        if not sub:
            continue
        kg5 = sum(r["evidence"]["recall@5"] for r in sub) / len(sub)
        kg10 = sum(r["evidence"]["recall@10"] for r in sub) / len(sub)
        flat5 = flat10 = None
        if all("evidence_flat" in r for r in sub):
            flat5 = sum(r["evidence_flat"]["recall@5"] for r in sub) / len(sub)
            flat10 = sum(r["evidence_flat"]["recall@10"] for r in sub) / len(sub)
        d10 = (kg10 - flat10) if flat10 is not None else None
        L.append(f"| {cat} | {len(sub)} | {_pct(kg5)} | {_pct(kg10)} | "
                 f"{_pct(flat5) if flat5 is not None else '—'} | {_pct(flat10) if flat10 is not None else '—'} | "
                 f"{'+' + _pct(d10) if d10 is not None else '—'} |")
    L.append("")

    # —— ⑤ Agent 层 ——
    traces = run["traces"]
    if traces:
        L.append("## 4. 端到端 Agent 层\n")
        ok = [r for r in traces if not r.get("has_error") and not r.get("no_done")]
        err = [r for r in traces if r.get("has_error") or r.get("no_done")]
        L.append(f"- 运行: {len(traces)} 题，成功 {len(ok)}，错误/无 done {len(err)}")
        if ok:
            import statistics
            def mean(k):
                vals = [r.get(k) for r in ok if r.get(k) is not None]
                return sum(vals) / len(vals) if vals else None
            g = [r.get("grounding_fraction") for r in ok if r.get("grounding_fraction") is not None]
            cc = [r.get("citation_coverage") for r in ok if r.get("citation_coverage") is not None]
            L.append(f"- 平均工具轮数: {mean('tool_rounds'):.1f}  平均工具调用: {mean('tool_calls'):.1f}  "
                     f"平均耗时: {mean('elapsed_ms') / 1000:.1f}s")
            if g:
                L.append(f"- 引用 grounding：{_pct(sum(g) / len(g))}  （答案引用中确实被检索到的比例）")
            if cc:
                L.append(f"- 引用覆盖率：{_pct(sum(cc) / len(cc))}  （golden 证据中被答案引用的比例）")
            def _degenerate(r):
                return bool(r.get("no_done")) or r.get("n_cited") == 0 \
                    or (bool(r.get("answer")) and "<invoke" in r["answer"])
            deg = [r for r in traces if _degenerate(r)]
            if deg:
                L.append(f"- ⚠ **{len(deg)}/{len(traces)} 题为退化答案**（未给出文字回答，输出工具调用 XML 或零引用），"
                         f"judge 记 1 分；正常作答题的平均分见下）")
            judges = [r.get("judge") for r in ok if r.get("judge")]
            llm_judges = [j for j in judges if j and j.get("judge_source") == "llm"]
            if llm_judges:
                L.append("\n| 维度 | 平均分 (全部) | 平均分 (正常作答) |")
                L.append("|---|---|---|")
                for dim in ("faithfulness", "completeness", "relevance", "citation"):
                    all_v = [j.get(dim) for j in llm_judges if j.get(dim) is not None]
                    norm_v = [j.get(dim) for j, r in zip(llm_judges, ok)
                              if j.get(dim) is not None and not _degenerate(r)]
                    L.append(f"| {dim} | {sum(all_v) / len(all_v):.2f} | "
                             f"{sum(norm_v) / len(norm_v):.2f} |")
                L.append(f"\n- LLM judge 判分行: {len(llm_judges)}/{len(judges)}（其余为启发式回退）")
            else:
                L.append(f"- 本次未产出 LLM judge 分（judge_source 全部 fallback，共 {len(judges)} 行）")
        L.append("")

    # —— ⑥ 逐题明细 ——
    traces_by_id = {r.get("id"): r for r in (traces or [])}
    L.append("## 5. 逐题明细\n")
    L.append("| 题ID | 类别 | 概念@10 | 实体@10 | 证据KG@10 | flat@10 | 引用数 | grounding | 引用覆盖率 | agent退化 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in run["retrieval"]:
        c = r.get("concept", {}) or {}
        e = r.get("entity", {}) or {}
        ev = r.get("evidence", {}) or {}
        flat = r.get("evidence_flat", {}) or {}
        t = traces_by_id.get(r["id"]) or {}
        n_cited = t.get("n_cited")
        g = t.get("grounding_fraction")
        cc = t.get("citation_coverage")
        deg = (t.get("no_done") or t.get("n_cited") == 0
               or (bool(t.get("answer")) and "<invoke" in t["answer"]))
        L.append(f"| {r['id']} | {r['category']} | {_pct(c.get('recall@10'))} | "
                 f"{_pct(e.get('recall@10')) if e else '—'} | {_pct(ev.get('recall@10'))} | "
                 f"{_pct(flat.get('recall@10')) if flat else '—'} | {n_cited if n_cited is not None else '—'} | "
                 f"{_pct(g) if g is not None else '—'} | {_pct(cc) if cc is not None else '—'} | "
                 f"{'⚠ XML' if deg else ''} |")
    L.append("")

    # —— ⑦ 复现 ——
    L.append("## 6. 复现\n")
    cmd = f"python -m eval.run --mode all --baseline --judge --max-rounds {cfg.get('max_rounds', 8)}"
    L.append(f"```bash\n{cmd}\n```")
    L.append("检索层不调 LLM、零成本；Agent 层调 DeepSeek flash，25 题约 10–40 分钟。")
    L.append("")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="从 run 目录生成 report.md")
    ap.add_argument("run_dir", help="eval/runs/<timestamp> 目录")
    args = ap.parse_args(argv)

    run = load_run(Path(args.run_dir))
    md = markdown(run)
    out = Path(args.run_dir) / "report.md"
    out.write_text(md, encoding="utf-8")
    print(f"报告已生成: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
