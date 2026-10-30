# -*- coding: utf-8 -*-
"""热问题缓存持久层 —— 单一 SQLite 库（默认 app/data/cache.db）。

只存 `qa_cache` 一张表（问句向量 + 答案 + 证据足迹 + kg_version）。
会话记录不在这里：见 chat_store.py，每个会话一个 JSONL 目录。

为什么缓存仍用 SQLite：q_embed 是 float32 向量 BLOB，查询要全表 cosine 扫描，
命中要 hit_count 原地自增——JSON 文件做这些既慢又容易写坏。

sqlite3 连接以 check_same_thread=False 全局共享，所有写操作经模块级 `lock`
串行化；FastAPI 异步处理器里用 asyncio.to_thread 包一层避免阻塞事件循环。
DB 路径优先级：set_db_path()（config 指定）> 环境变量 HIERKG_CACHE_DB
（兼容旧名 HIERKG_CHAT_DB）> 默认。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

_DEFAULT_DB = str(Path(__file__).resolve().parents[2] / "app" / "data" / "cache.db")

lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[str] = None


def set_db_path(path: str) -> None:
    """启动时由 config 覆盖默认 DB 路径（在首次连接前调用）。"""
    global _db_path
    _db_path = path


def db_path() -> str:
    p = (_db_path or os.environ.get("HIERKG_CACHE_DB")
         or os.environ.get("HIERKG_CHAT_DB") or _DEFAULT_DB)
    return os.path.abspath(p)


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
