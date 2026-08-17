# -*- coding: utf-8 -*-
"""评测公用：ID 正则、深遍历提取、JSONL 写出、运行目录、git head。"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

# 证据 ID 允许 `.`、中文、`#`（如 ev_1.1.1 / ev_1章 / ev_1.4#2），`\w` 覆盖不了这些字符。
_EVIDENCE_CHARS = r"0-9A-Za-z_.#一-鿿"
EVIDENCE_ID_RE = re.compile(rf"^ev_[{_EVIDENCE_CHARS}]+$")
CITATION_RE = re.compile(rf"\[(ev_[{_EVIDENCE_CHARS}]+)\]")
CONCEPT_ID_RE = re.compile(r"^concept_[0-9a-z]+$")
ENTITY_ID_RE = re.compile(r"^ent_[0-9a-z]+$")

EVAL_ROOT = Path(__file__).resolve().parent


def is_evidence_id(value: Any) -> bool:
    return isinstance(value, str) and EVIDENCE_ID_RE.match(value) is not None


def extract_evidence_ids(obj: Any) -> Set[str]:
    """深遍历 dict/list，收集所有匹配证据 ID 的字符串值。

    覆盖工具结果里的 node_id、evidence_ids 列表、边 source/target 等。
    """
    found: Set[str] = set()
    stack: List[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple, set)):
            stack.extend(cur)
        elif is_evidence_id(cur):
            found.add(cur)
    return found


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def call_tool(registry, tool_name: str, params: dict) -> Any:
    """调工具并安全返回完整 ToolResult.data（失败/异常返回 None）。

    检索层与引用 grounding 一律走这里拿完整数据，绝不解析 SSE 事件（被 compact_summary 压缩）。
    """
    try:
        res = registry.call(tool_name, params)
    except Exception:
        return None
    if not res or not getattr(res, "success", False) or not res.data:
        return None
    return res.data


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json_dumps(row) + "\n")


def read_jsonl(path: Path) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=EVAL_ROOT.parent,
        ).stdout.strip()
        return out or ""
    except Exception:
        return ""


def make_run_dir(base: Optional[Path] = None) -> Path:
    """新建 runs/<UTC 时间戳> 目录并返回。"""
    base = base or (EVAL_ROOT / "runs")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    d = base / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d
