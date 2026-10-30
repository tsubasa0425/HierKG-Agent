# -*- coding: utf-8 -*-
"""会话记录持久层 —— 每会话一个目录，JSONL 追加写。

目录结构（默认 app/data/sessions/）：
  <session_id>/
    meta.json         会话元信息（id / user_id / title / 时间）
    messages.jsonl    一行一条消息（id / role / content / meta / created_at）

写入策略：
  * meta.json 用「临时文件 + os.replace」原子替换，避免写一半崩溃留下坏文件；
  * messages.jsonl 只做追加，崩溃最多留下最后一行半截，读取时跳过坏行即可；
  * 所有读写经模块级 `lock` 串行化（单进程、单写者假设）；FastAPI 异步处理器
    里用 asyncio.to_thread 包一层避免阻塞事件循环。

session_id 会拼进文件路径，因此必须是 uuid4().hex 格式（32 位小写十六进制），
否则一律拒绝——这是 SQLite 时代不需要、文件系统时代必须防的路径穿越。

热问题缓存（向量）不在这里：见 cache_db.py。
数据目录优先级：set_data_dir()（config 指定）> 环境变量 HIERKG_SESSION_DIR > 默认。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFAULT_DIR = str(Path(__file__).resolve().parents[2] / "app" / "data" / "sessions")

_META_FILE = "meta.json"
_MESSAGES_FILE = "messages.jsonl"
_SID_RE = re.compile(r"^[0-9a-f]{32}$")

lock = threading.Lock()
_data_dir: Optional[str] = None


def set_data_dir(path: str) -> None:
    """启动时由 config 覆盖默认数据目录（在首次使用前调用）。"""
    global _data_dir
    _data_dir = path


def data_dir() -> str:
    p = _data_dir or os.environ.get("HIERKG_SESSION_DIR") or _DEFAULT_DIR
    return os.path.abspath(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _session_dir(session_id: str) -> Path:
    """校验并返回会话目录；ID 非法时抛 ValueError。"""
    if not _SID_RE.fullmatch(session_id or ""):
        raise ValueError(f"非法 session_id: {session_id!r}")
    return Path(data_dir()) / session_id


def _safe_session_dir(session_id: str) -> Optional[Path]:
    """URL 传来的 ID 可能任意，非法时返回 None 而不是抛错。"""
    try:
        return _session_dir(session_id)
    except ValueError:
        return None


def _read_meta(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _write_meta(path: Path, meta: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return sum(1 for _ in f)


def init_db() -> None:
    Path(data_dir()).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def create_session(user_id: str = "", title: str = "") -> str:
    sid = uuid.uuid4().hex
    now = _now()
    d = _session_dir(sid)
    meta = {"id": sid, "user_id": user_id, "title": title,
            "created_at": now, "updated_at": now}
    with lock:
        d.mkdir(parents=True, exist_ok=True)
        _write_meta(d / _META_FILE, meta)
    return sid


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    d = _safe_session_dir(session_id)
    if d is None:
        return None
    return _read_meta(d / _META_FILE)


def list_sessions(user_id: str = "") -> List[Dict[str, Any]]:
    root = Path(data_dir())
    if not root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    with lock:
        for d in root.iterdir():
            if not d.is_dir() or not _SID_RE.fullmatch(d.name):
                continue
            meta = _read_meta(d / _META_FILE)
            if meta is None:
                continue
            if user_id and meta.get("user_id") != user_id:
                continue
            out.append({k: meta.get(k, "") for k in
                        ("id", "user_id", "title", "created_at", "updated_at")})
    out.sort(key=lambda m: (m.get("updated_at") or "", m.get("created_at") or ""),
             reverse=True)
    return out


def rename_session(session_id: str, title: str) -> bool:
    d = _safe_session_dir(session_id)
    if d is None:
        return False
    with lock:
        meta = _read_meta(d / _META_FILE)
        if meta is None:
            return False
        meta["title"] = title
        meta["updated_at"] = _now()
        _write_meta(d / _META_FILE, meta)
    return True


def touch_session(session_id: str) -> None:
    d = _safe_session_dir(session_id)
    if d is None:
        return
    with lock:
        meta = _read_meta(d / _META_FILE)
        if meta is None:
            return
        meta["updated_at"] = _now()
        _write_meta(d / _META_FILE, meta)


def delete_session(session_id: str) -> bool:
    d = _safe_session_dir(session_id)
    if d is None or not d.is_dir():
        return False
    with lock:
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            return False
    return True


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def append_message(session_id: str, role: str, content: str,
                   meta: Optional[Dict[str, Any]] = None) -> int:
    d = _session_dir(session_id)   # 非法 ID 直接抛 ValueError
    path = d / _MESSAGES_FILE
    with lock:
        d.mkdir(parents=True, exist_ok=True)
        # 流式回答期间会话被删时，补一个最小 meta，避免留下孤儿目录
        if not (d / _META_FILE).exists():
            now = _now()
            _write_meta(d / _META_FILE, {"id": session_id, "user_id": "", "title": "",
                                         "created_at": now, "updated_at": now})
        next_id = _count_lines(path) + 1
        record = {"id": next_id, "session_id": session_id, "role": role,
                  "content": content, "meta": meta or {}, "created_at": _now()}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return next_id


def get_messages(session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    d = _safe_session_dir(session_id)
    if d is None:
        return []
    path = d / _MESSAGES_FILE
    out: List[Dict[str, Any]] = []
    try:
        with lock:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 崩溃留下的半行，跳过
                    if not isinstance(rec, dict):
                        continue
                    if not isinstance(rec.get("meta"), dict):
                        rec["meta"] = {}
                    out.append(rec)
    except FileNotFoundError:
        return []   # 读取期间会话被删除
    return out[-limit:] if limit is not None else out
