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
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]           # 项目根目录
KGBUILD = ROOT / "src" / "KGBuild"
CFG_DIR = KGBUILD / "config"
OUT_DIR = KGBUILD / "output"
DOC_DIR = KGBUILD / "doc"

# Web 端消费的结构化事件协议：单行 stdout，前缀 + 紧凑 JSON。
EVENT_PREFIX = "@@HIERKG_EVENT@@"
EVENT_VERSION = 1

# 静态步骤元数据：配置缺失时 Web 仍可渲染完整时间线。
STEP_META: List[Dict[str, object]] = [
    {"name": "textseg", "label": "文本分割", "desc": "docx → TOC 结构 + 原文小节", "ordinal": 1},
    {"name": "extraction", "label": "实体关系抽取", "desc": "小节 → 实体/关系（LLM 并发）", "ordinal": 2},
    {"name": "toc_graph", "label": "目录图", "desc": "构建 toc_graph.json", "ordinal": 3},
    {"name": "evidence", "label": "证据层构建", "desc": "toc → L3 证据 evidence.json", "ordinal": 4},
    {"name": "conv", "label": "实体归并", "desc": "同义实体合并 + 信号/邻域 + 定义补全", "ordinal": 5},
    {"name": "aggr", "label": "分层归属", "desc": "LayerPolicy 规则分层 + L2 概念归属", "ordinal": 6},
    {"name": "embedding", "label": "向量嵌入", "desc": "L1/L2 节点向量化（Ollama → node_embeddings.pkl）", "ordinal": 7},
    {"name": "dedup", "label": "同层去重", "desc": "别名 + 向量近邻 + LLM 复核", "ordinal": 8},
    {"name": "pred", "label": "关系补全", "desc": "拓扑候选 + 向量相似 + LLM 复核", "ordinal": 9},
    {"name": "final", "label": "四层装配", "desc": "L0-L3 装配 → final_kg.json", "ordinal": 10},
]
STEP_NAMES: List[str] = [str(m["name"]) for m in STEP_META]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def emit(enabled: bool, type_: str, **data) -> None:
    """向 stdout 写一条结构化事件；未启用时是空操作。

    用一次 os.write 保证与子进程 stdout 交错时按整行写入。
    """
    if not enabled:
        return
    payload = {"v": EVENT_VERSION, "ts": _now_iso(), "type": type_, **data}
    line = f"{EVENT_PREFIX} {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
    try:
        os.write(sys.stdout.fileno(), line.encode("utf-8", errors="replace"))
    except (AttributeError, OSError, ValueError):
        sys.stdout.write(line)
        sys.stdout.flush()


def build_env() -> Dict[str, str]:
    """子进程统一 UTF-8 / 无缓冲，并补上 `src/` 以修复 KGBuild 包导入。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    src_dir = str(ROOT / "src")
    old = env.get("PYTHONPATH", "")
    parts = [src_dir] + [p for p in old.split(os.pathsep) if p and p != src_dir]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


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
        args: Optional[List[str]] = None,
    ):
        self.name = name
        self.label = label
        self.script = ROOT / script
        self.desc = desc
        self.inputs = inputs
        self.outputs = outputs
        self.args = list(args or [])


def build_steps(cfg: Dict[str, dict], docx_override: Optional[Path] = None) -> List[Step]:
    ts = cfg["TextSegConfig"]
    ex = cfg["ExtractionConfig"]
    cv = cfg["ConvConfig"]
    ag = cfg["AggrConfig"]
    dd = cfg["DedupConfig"]
    pd = cfg["PredConfig"]
    emb = cfg.get("EmbConfig", {}) or {}

    toc_ent = OUT_DIR / ex["OUT_NAME"]                  # toc_with_entities_and_relations.json
    evidence = OUT_DIR / "evidence.json"                # evidence_builder 硬编码
    emb_input = OUT_DIR / emb.get("INPUT_NAME", "conv_entities.json")
    embeddings = OUT_DIR / emb.get("OUTPUT_NAME", "node_embeddings.pkl")
    dedup_embeddings = OUT_DIR / dd.get("EMBEDDINGS_NAME", "node_embeddings.pkl")
    pred_embeddings = OUT_DIR / pd.get("EMBEDDINGS_NAME", "node_embeddings.pkl")
    dedup_res = OUT_DIR / dd["RESULT_NAME"]             # dedup_result.json
    pred_res = OUT_DIR / pd["RESULT_NAME"]              # pred_result.json

    docx_path = Path(docx_override) if docx_override else DOC_DIR / ts["DOCX_NAME"]
    textseg_args = ["--docx", str(docx_path)] if docx_override else []

    return [
        Step("textseg", "文本分割", "src/KGBuild/TextSegmentation.py",
             "docx → TOC 结构 + 原文小节",
             [docx_path], [OUT_DIR / ts["TOC_NAME"]], args=textseg_args),
        Step("extraction", "实体关系抽取", "src/KGBuild/Extraction.py",
             "小节 → 实体/关系（LLM 并发）",
             [OUT_DIR / ex["IN_NAME"]], [toc_ent]),
        Step("toc_graph", "目录图", "src/KGBuild/toc_graph.py",
             "构建 toc_graph.json",
             [toc_ent], [OUT_DIR / "toc_graph.json"]),
        Step("evidence", "证据层构建", "src/KGBuild/evidence_builder.py",
             "toc → L3 证据 evidence.json",
             [toc_ent], [evidence]),
        Step("conv", "实体归并", "src/KGBuild/Conv.py",
             "同义实体合并 + 信号/邻域 + 定义补全",
             [toc_ent], [OUT_DIR / cv["CONV_RESULT_NAME"]]),
        Step("aggr", "分层归属", "src/KGBuild/Aggr.py",
             "LayerPolicy 规则分层 + L2 概念归属",
             [OUT_DIR / ag["CONV_IN_NAME"]], [OUT_DIR / ag["OUT_NAME"]]),
        Step("embedding", "向量嵌入", "src/KGBuild/Embedding.py",
             "L1/L2 节点向量化（Ollama → node_embeddings.pkl）",
             [emb_input], [embeddings]),
        Step("dedup", "同层去重", "src/KGBuild/Dedup.py",
             "别名 + 向量近邻 + LLM 复核",
             [OUT_DIR / dd.get("ENTITIES_AGGR_NAME", "aggr_entities.json"), dedup_embeddings], [dedup_res]),
        Step("pred", "关系补全", "src/KGBuild/Pred.py",
             "拓扑候选 + 向量相似 + LLM 复核",
             [dedup_res, pred_embeddings], [pred_res]),
        Step("final", "四层装配", "src/KGBuild/FinalKG.py",
             "L0-L3 装配 → final_kg.json",
             [dedup_res, pred_res, evidence], [OUT_DIR / "final_kg.json"]),
    ]


def resolve_selection(
    names: List[str],
    only: Optional[str] = None,
    start: Optional[str] = None,
    until: Optional[str] = None,
    skip: Optional[str] = None,
) -> List[str]:
    """纯步骤选择逻辑；CLI 与 Web 共用。非法步骤名抛 ValueError。"""
    def _split(raw: Optional[str]) -> List[str]:
        return [x.strip() for x in (raw or "").split(",") if x.strip()]

    def _validate(ns: List[str], which: str) -> None:
        bad = [n for n in ns if n not in names]
        if bad:
            raise ValueError(f"未知步骤名 {bad}（{which}）。可用：{' / '.join(names)}")

    only_list = _split(only)
    if only_list:
        _validate(only_list, "--only")
        return [n for n in names if n in only_list]

    result = list(names)
    if start:
        _validate([start], "--start")
        result = [n for n in result if names.index(n) >= names.index(start)]
    if until:
        _validate([until], "--until")
        result = [n for n in result if names.index(n) <= names.index(until)]
    skip_list = _split(skip)
    if skip_list:
        _validate(skip_list, "--skip")
        result = [n for n in result if n not in skip_list]
    return result


def select_steps(steps: List[Step], args) -> List[Step]:
    names = [s.name for s in steps]
    try:
        selected = resolve_selection(
            names,
            only=getattr(args, "only", None),
            start=getattr(args, "start", None),
            until=getattr(args, "until", None),
            skip=getattr(args, "skip", None),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return [s for s in steps if s.name in selected]


# ---------------------------------------------------------------------------
# 预检 + 运行
# ---------------------------------------------------------------------------

def preflight(steps: List[Step], events_enabled: bool = False) -> bool:
    """检查每个步骤的输入文件是否存在。返回是否有缺失。"""
    ok = True
    checked: List[dict] = []
    missing: List[dict] = []
    print("\n== 预检各步骤输入 ==")
    for s in steps:
        for p in s.inputs:
            exists = p.exists()
            checked.append({"step": s.name, "path": str(p), "exists": exists})
            if not exists:
                ok = False
                missing.append({"step": s.name, "path": str(p)})
            mark = "✔" if exists else "✘ 缺失"
            print(f"  [{mark}] {s.name:<10} {p}")
    if not ok:
        print("⚠  存在缺失输入，检查后重试（或加 --start 跳过已完成的步骤）。")
    emit(events_enabled, "preflight", ok=ok, checked=checked, missing=missing)
    return ok


def run_step(
    s: Step,
    keep_going: bool = False,
    *,
    events_enabled: bool = False,
    heartbeat_sec: float = 10.0,
    ordinal: int = 0,
    index: int = 0,
    total: int = 0,
) -> Tuple[bool, float]:
    t0 = time.monotonic()
    print(f"\n── [{s.name}] {s.label} ── {s.desc}")
    print(f"    运行：{s.script}")
    emit(events_enabled, "step_start", step=s.name, label=s.label,
         ordinal=ordinal, index=index, total=total)

    stop = threading.Event()

    def _beat() -> None:
        interval = max(1.0, float(heartbeat_sec))
        while not stop.wait(interval):
            emit(events_enabled, "step_heartbeat", step=s.name,
                 elapsed_sec=round(time.monotonic() - t0, 1))

    beat = threading.Thread(target=_beat, name=f"heartbeat-{s.name}", daemon=True)
    beat.start()

    rc = -1
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(s.script), *s.args],
            cwd=str(ROOT),
            env=build_env(),
        )
        rc = proc.returncode
    except OSError as exc:
        print(f"  ❌ 启动失败：{exc}")
        rc = -1
    finally:
        stop.set()
        beat.join(timeout=2.0)

    cost = time.monotonic() - t0
    ok = rc == 0
    outputs = [
        {
            "path": str(p),
            "exists": p.exists(),
            "size": p.stat().st_size if p.exists() else 0,
        }
        for p in s.outputs
    ]
    emit(events_enabled, "step_end", step=s.name, ok=ok, exit_code=rc,
         elapsed_sec=round(cost, 1), outputs=outputs)
    status = "✅" if ok else f"❌ 失败（exit={rc}）"
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
    ap.add_argument("--json-events", action="store_true",
                    help="向 stdout 输出 @@HIERKG_EVENT@@ 结构化事件（供 Web 消费）")
    ap.add_argument("--heartbeat-sec", type=float, default=10.0,
                    help="运行中步骤的心跳间隔秒数（默认 10）")
    ap.add_argument("--docx", help="覆盖 TextSegConfig.DOCX_NAME 的 docx 路径")
    args = ap.parse_args(argv)
    events = bool(args.json_events)

    try:
        cfg = load_merged_config()
    except FileNotFoundError as e:
        print(e)
        emit(events, "run_error", code="config_missing", message=str(e))
        return 1

    docx_override = Path(args.docx).resolve() if args.docx else None

    try:
        steps = select_steps(build_steps(cfg, docx_override), args)
    except SystemExit as exc:
        msg = str(exc)
        print(msg)
        emit(events, "run_error", code="invalid_selection", message=msg)
        return 1

    if not steps:
        msg = "没有选中的步骤（检查 --start/--until/--skip/--only）。"
        print(msg)
        emit(events, "run_error", code="invalid_selection", message=msg)
        return 1

    selected = {s.name for s in steps}
    emit(
        events, "run_start",
        steps=[
            {**meta, "selected": meta["name"] in selected}
            for meta in STEP_META
        ],
        total_selected=len(steps),
        docx=str(docx_override) if docx_override else
            str(DOC_DIR / cfg["TextSegConfig"]["DOCX_NAME"]),
        check_only=bool(args.check),
        selection={
            "start": args.start, "until": args.until,
            "skip": args.skip, "only": args.only,
        },
    )

    print(f"流水线共 {len(steps)} 步（全量 10 步）："
          f"{' → '.join(s.name for s in steps)}")

    # ---- 预检 ----
    all_ok = preflight(steps, events)
    if not all_ok:
        emit(events, "run_end", ok=False, failed_steps=[], elapsed_sec=0.0,
             cancelled=False, final_kg={"path": str(OUT_DIR / "final_kg.json"),
                                        "exists": (OUT_DIR / "final_kg.json").exists()})
        return 1
    if args.check:
        print("\n预检通过 ✔ 未运行任何步骤。")
        emit(events, "check_result", ok=True, missing=[])
        final = OUT_DIR / "final_kg.json"
        emit(events, "run_end", ok=True, failed_steps=[], elapsed_sec=0.0,
             cancelled=False,
             final_kg={"path": str(final), "exists": final.exists(),
                       "size": final.stat().st_size if final.exists() else 0})
        return 0

    # ---- 运行 ----
    ordinal_by_name = {str(m["name"]): int(m["ordinal"]) for m in STEP_META}  # type: ignore[arg-type]
    failed: List[str] = []
    t_all = time.monotonic()
    for idx, s in enumerate(steps, start=1):
        ok, _ = run_step(
            s, args.keep_going,
            events_enabled=events,
            heartbeat_sec=args.heartbeat_sec,
            ordinal=ordinal_by_name.get(s.name, idx),
            index=idx,
            total=len(steps),
        )
        if not ok:
            failed.append(s.name)
            if not args.keep_going:
                print(f"\n⛔ 流水线在 [{s.name}] 失败，已停止。"
                      f"修复后可用 --start {s.name} 断点续跑；"
                      f"或加 --keep-going 继续后续步骤。")
                emit(events, "run_end", ok=False, failed_steps=failed,
                     elapsed_sec=round(time.monotonic() - t_all, 1),
                     cancelled=False,
                     final_kg={"path": str(OUT_DIR / "final_kg.json"),
                               "exists": (OUT_DIR / "final_kg.json").exists()})
                return 1

    total = time.monotonic() - t_all
    final = OUT_DIR / "final_kg.json"
    print("\n" + "=" * 40)
    if failed:
        print(f"流水线完成，{len(failed)} 步失败：{', '.join(failed)}")
        emit(events, "run_end", ok=False, failed_steps=failed,
             elapsed_sec=round(total, 1), cancelled=False,
             final_kg={"path": str(final), "exists": final.exists(),
                       "size": final.stat().st_size if final.exists() else 0})
        return 1
    print(f"🎉 全流程完成，总耗时 {total:.1f}s。最终产物：{final}")
    emit(events, "run_end", ok=True, failed_steps=[],
         elapsed_sec=round(total, 1), cancelled=False,
         final_kg={"path": str(final), "exists": final.exists(),
                   "size": final.stat().st_size if final.exists() else 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
