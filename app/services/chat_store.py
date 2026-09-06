# -*- coding: utf-8 -*-
"""会话 + 热缓存持久层 —— 单一 SQLite 库（默认 app/data/chat.db）。

三张表：
  sessions  —— 会话元信息（id / user_id / title / 时间）
  messages  —— 会话消息（role / content / meta JSON：tool_rounds 等检索统计）
  qa_cache  —— 热问题缓存（问句向量 + 答案 + 证据足迹 + kg_version）

sqlite3 连接以 check_same_thread=False 全局共享，所有写操作经模块级 `lock`
串行化；FastAPI 异步处理器里用 asyncio.to_thread 包一层避免阻塞事件循环。
DB 路径优先级：set_db_path()（config 指定）> 环境变量 HIERKG_CHAT_DB > 默认。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFAULT_DB = str(Path(__file__).resolve().parents[2] / "app" / "data" / "chat.db")

lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[str] = None


def set_db_path(path: str) -> None:
    """启动时由 config 覆盖默认 DB 路径（在首次连接前调用）。"""
    global _db_path
    _db_path = path


def db_path() -> str:
    p = _db_path or os.environ.get("HIERKG_CHAT_DB") or _DEFAULT_DB
    return os.path.abspath(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        p = db_path()
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(p, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
      id         TEXT PRIMARY KEY,
      user_id    TEXT DEFAULT '',
      title      TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      role       TEXT NOT NULL,             -- user | assistant
      content    TEXT NOT NULL,
      meta       TEXT DEFAULT '{}',         -- JSON: tool_rounds/tool_calls/elapsed_ms/cache_hit
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
    CREATE TABLE IF NOT EXISTS qa_cache (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      q_norm       TEXT NOT NULL,
      q_embed      BLOB,                    -- bge-m3 向量 float32 bytes
      answer       TEXT DEFAULT '',
      evidence_ids TEXT DEFAULT '[]',       -- JSON list（[ev_xxx]）
      node_ids     TEXT DEFAULT '[]',       -- JSON list（concept_/ent_）
      kg_version   TEXT DEFAULT '',
      hit_count    INTEGER DEFAULT 0,
      last_hit_at  TEXT,
      created_at   TEXT NOT NULL,
      updated_at   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_qa_cache_q ON qa_cache(q_norm);
    """)
    conn.commit()


def init_db() -> None:
    get_conn()


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def create_session(user_id: str = "", title: str = "") -> str:
    sid = uuid.uuid4().hex
    now = _now()
    with lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO sessions(id, user_id, title, created_at, updated_at) VALUES(?,?,?,?,?)",
            (sid, user_id, title, now, now),
        )
        conn.commit()
    return sid


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(user_id: str = "") -> List[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        if user_id:
            rows = conn.execute(
                "SELECT id, user_id, title, created_at, updated_at FROM sessions "
                "WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_id, title, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def rename_session(session_id: str, title: str) -> bool:
    now = _now()
    with lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (title, now, session_id))
        conn.commit()
    return cur.rowcount > 0


def touch_session(session_id: str) -> None:
    with lock:
        conn = get_conn()
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                     (_now(), session_id))
        conn.commit()


def delete_session(session_id: str) -> bool:
    with lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def append_message(session_id: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> int:
    now = _now()
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    with lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO messages(session_id, role, content, meta, created_at) VALUES(?,?,?,?,?)",
            (session_id, role, content, meta_json, now))
        conn.commit()
    return int(cur.lastrowid)


def get_messages(session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        if limit is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
            rows.reverse()  # 回到时间正序
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["meta"] = {}
        out.append(d)
    return out
