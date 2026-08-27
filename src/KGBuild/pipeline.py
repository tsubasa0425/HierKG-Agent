# -*- coding: utf-8 -*-
"""pipeline.py —— 一键串联 KGBuild 全部构图脚本

按流水线顺序逐个以子进程运行，输出实时透传（tqdm 进度条、print 日志原样显示）：

    textseg → extraction → toc_graph → evidence → conv → aggr
             → embedding → dedup → pred → final

用法（在项目根目录执行）：
    python src/KGBuild/pipeline.py                 # 完整跑一遍
    python src/KGBuild/pipeline.py --check         # 只预检各步骤输入文件，不开跑
    python src/KGBuild/pipeline.py --start aggr    # 断点续跑：从 aggr 开始
    python src/KGBuild/pipeline.py --until conv    # 只跑到 conv 为止
    python src/KGBuild/pipeline.py --skip embedding,dedup   # 跳过指定步骤
    python src/KGBuild/pipeline.py --only pred,final        # 只跑指定步骤
    python src/KGBuild/pipeline.py --keep-going    # 单步失败继续跑，最后汇总

步骤名：textseg / extraction / toc_graph / evidence / conv / aggr /
        embedding / dedup / pred / final

说明：
  - 各步骤的输入/输出文件名从 config.yaml + 各 stage yaml 读取（不 import
    任何 KGBuild 模块，避免 import 副作用）。config.yaml 缺失时报错提示
    cp config.yaml.example。
  - --check 预检在真正开跑（可能烧 API/占 GPU）前先验证输入文件齐全。
  - 任一步骤返回码非 0 即判定失败；默认停止，--keep-going 时继续。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]           # 项目根目录
KGBUILD = ROOT / "src" / "KGBuild"
CFG_DIR = KGBUILD / "config"
OUT_DIR = KGBUILD / "output"
DOC_DIR = KGBUILD / "doc"


# ---------------------------------------------------------------------------
# 配置加载（与各模块相同的合并逻辑：config.yaml + include_files）
# ---------------------------------------------------------------------------

def load_merged_config() -> Dict[str, dict]:
    cfg_path = CFG_DIR / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"未找到配置文件：{cfg_path}\n"
            f"请先复制模板：cp {CFG_DIR / 'config.yaml.example'} {cfg_path}，并填写 API 密钥"
        )
    base = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    merged = dict(base)
    for rel in base.get("include_files", []) or []:
        inc_path = (cfg_path.parent / rel).resolve()
        if not inc_path.exists():
            raise FileNotFoundError(f"未找到 include 文件：{inc_path}")
        inc = yaml.safe_load(inc_path.read_text(encoding="utf-8")) or {}
        merged.update(inc)
    return merged


# ---------------------------------------------------------------------------
# 步骤定义
# ---------------------------------------------------------------------------

class Step:
    """单个构图步骤：子进程方式运行 <script>（相对项目根），可预检输入。"""

    def __init__(
        self,
        name: str,
        label: str,
        script: str,
        desc: str,
        inputs: List[Path],
        outputs: List[Path],
    ):
        self.name = name
        self.label = label
        self.script = ROOT / script
        self.desc = desc
        self.inputs = inputs
        self.outputs = outputs


def build_steps(cfg: Dict[str, dict]) -> List[Step]:
    ts = cfg["TextSegConfig"]
    ex = cfg["ExtractionConfig"]
    cv = cfg["ConvConfig"]
    ag = cfg["AggrConfig"]
    dd = cfg["DedupConfig"]
    pd = cfg["PredConfig"]
    emb = cfg["EmbConfig"]

    toc_ent = OUT_DIR / ex["OUT_NAME"]                  # toc_with_entities_and_relations.json
    evidence = OUT_DIR / "evidence.json"                # evidence_builder 硬编码
    embeddings = OUT_DIR / emb["OUTPUT_NAME"]           # node_embeddings.pkl
    dedup_res = OUT_DIR / dd["RESULT_NAME"]             # dedup_result.json
    pred_res = OUT_DIR / pd["RESULT_NAME"]              # pred_result.json

    return [
        Step("textseg", "文本分割", "src/KGBuild/TextSegmentation.py",
             "docx → TOC 结构 + 原文小节",
             [DOC_DIR / ts["DOCX_NAME"]], [OUT_DIR / ts["TOC_NAME"]]),
        Step("extraction", "实体关系抽取", "src/KGBuild/Extraction.py",
             "小节 → 实体/关系（LLM 并发）",
             [OUT_DIR / ex["IN_NAME"]], [toc_ent]),
        Step("toc_graph", "目录图", "src/KGBuild/toc_graph.py",
             "构建 toc_graph.json",
             [toc_ent], [OUT_DIR / "toc_graph.json"]),
        Step("evidence", "证据层构建", "src/KGBuild/evidence_builder.py",
             "toc → L3 证据 evidence.json",
             [toc_ent], [evidence]),
        Step("conv", "格式转换", "src/KGBuild/Conv.py",
             "实体增强描述（LLM 并发）",
             [toc_ent], [OUT_DIR / cv["CONV_RESULT_NAME"]]),
        Step("aggr", "概念分层", "src/KGBuild/Aggr.py",
             "concept/entity 分层判定（LLM）",
             [OUT_DIR / ag["CONV_IN_NAME"]], [OUT_DIR / ag["OUT_NAME"]]),
        Step("embedding", "BERT 嵌入", "src/KGBuild/Embedding.py",
             "实体向量化（node_embeddings.pkl）",
             [OUT_DIR / emb["INPUT_NAME"]], [embeddings]),
        Step("dedup", "概念去重", "src/KGBuild/Dedup.py",
             "KNN + LLM 去重",
             [OUT_DIR / dd["ENTITIES_AGGR_NAME"], embeddings], [dedup_res]),
        Step("pred", "关系预测", "src/KGBuild/Pred.py",
             "拓扑 + LLM 补边",
             [dedup_res, embeddings], [pred_res]),
        Step("final", "四层装配", "src/KGBuild/FinalKG.py",
             "L0-L3 装配 → final_kg.json",
             [dedup_res, pred_res, evidence], [OUT_DIR / "final_kg.json"]),
    ]


def select_steps(steps: List[Step], args) -> List[Step]:
    names = [s.name for s in steps]

    def _validate(ns: List[str], which: str) -> None:
        bad = [n for n in ns if n not in names]
        if bad:
            raise SystemExit(f"未知步骤名 {bad}（{which}）。可用：{' / '.join(names)}")

    if args.only:
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        _validate(only, "--only")
        return [s for s in steps if s.name in only]

    result = steps
    if args.start:
        _validate([args.start], "--start")
        result = [s for s in result if names.index(s.name) >= names.index(args.start)]
    if args.until:
        _validate([args.until], "--until")
        result = [s for s in result if names.index(s.name) <= names.index(args.until)]
    if args.skip:
        skip = [s.strip() for s in args.skip.split(",") if s.strip()]
        _validate(skip, "--skip")
        result = [s for s in result if s.name not in skip]
    return result


# ---------------------------------------------------------------------------
# 预检 + 运行
# ---------------------------------------------------------------------------

def preflight(steps: List[Step]) -> bool:
    """检查每个步骤的输入文件是否存在。返回是否有缺失。"""
    ok = True
    print("\n== 预检各步骤输入 ==")
    for s in steps:
        for p in s.inputs:
            mark = "✔" if p.exists() else "✘ 缺失"
            if not p.exists():
                ok = False
            print(f"  [{mark}] {s.name:<10} {p}")
    if not ok:
        print("⚠  存在缺失输入，检查后重试（或加 --start 跳过已完成的步骤）。")
    return ok


def run_step(s: Step, keep_going: bool) -> Tuple[bool, float]:
    t0 = time.monotonic()
    print(f"\n── [{s.name}] {s.label} ── {s.desc}")
    print(f"    运行：{s.script}")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程统一 UTF-8 输出，避免 Windows 控制台乱码
    proc = subprocess.run(
        [sys.executable, str(s.script)],
        cwd=str(ROOT),
        env=env,
    )
    cost = time.monotonic() - t0
    ok = proc.returncode == 0
    status = "✅" if ok else f"❌ 失败（exit={proc.returncode}）"
    print(f"  {status}  耗时 {cost:.1f}s")
    return ok, cost


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="HierKG 构图流水线：串联 KGBuild 全部 10 个步骤",
        epilog="步骤名：textseg / extraction / toc_graph / evidence / conv / "
               "aggr / embedding / dedup / pred / final",
    )
    ap.add_argument("--check", action="store_true", help="只预检输入文件，不运行")
    ap.add_argument("--start", help="从该步骤开始（断点续跑）")
    ap.add_argument("--until", help="只跑到该步骤（含）")
    ap.add_argument("--skip", help="逗号分隔的跳过步骤列表")
    ap.add_argument("--only", help="只运行逗号分隔的步骤（覆盖 start/until/skip）")
    ap.add_argument("--keep-going", action="store_true", help="单步失败继续，最后汇总")
    args = ap.parse_args(argv)

    try:
        cfg = load_merged_config()
    except FileNotFoundError as e:
        print(e)
        return 1

    steps = select_steps(build_steps(cfg), args)
    if not steps:
        print("没有选中的步骤（检查 --start/--until/--skip/--only）。")
        return 1

    print(f"流水线共 {len(steps)} 步（全量 10 步）："
          f"{' → '.join(s.name for s in steps)}")

    # ---- 预检 ----
    all_ok = preflight(steps)
    if not all_ok:
        return 1
    if args.check:
        print("\n预检通过 ✔ 未运行任何步骤。")
        return 0

    # ---- 运行 ----
    failed: List[str] = []
    t_all = time.monotonic()
    for s in steps:
        ok, _ = run_step(s, args.keep_going)
        if not ok:
            failed.append(s.name)
            if not args.keep_going:
                print(f"\n⛔ 流水线在 [{s.name}] 失败，已停止。"
                      f"修复后可用 --start {s.name} 断点续跑；"
                      f"或加 --keep-going 继续后续步骤。")
                return 1

    total = time.monotonic() - t_all
    print("\n" + "=" * 40)
    if failed:
        print(f"流水线完成，{len(failed)} 步失败：{', '.join(failed)}")
        return 1
    print(f"🎉 全流程完成，总耗时 {total:.1f}s。最终产物：{OUT_DIR / 'final_kg.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
