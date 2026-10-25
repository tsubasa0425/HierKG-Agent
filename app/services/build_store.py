# -*- coding: utf-8 -*-
"""构建/导入任务的 SQLite 持久层（默认 app/data/builds.db）。

SQLite 只存任务与步骤状态，日志写入每个任务的 events.jsonl（见 build_manager）。
连接全局共享（check_same_thread=False + WAL），所有写操作由模块级 lock 串行化。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_DEFAULT_DB = str(Path(__file__).resolve().parents[2] / "app" / "data" / "builds.db")

ACTIVE_STATUSES = ("queued", "running", "cancelling")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[str] = None


def set_db_path(path: str) -> None:
    global _db_path
    _db_path = path


def db_path() -> str:
    p = _db_path or os.environ.get("HIERKG_BUILD_DB") or _DEFAULT_DB
    return os.path.abspath(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


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
    CREATE TABLE IF NOT EXISTS build_jobs (
      id               TEXT PRIMARY KEY,
      kind             TEXT NOT NULL,              -- build | import
      status           TEXT NOT NULL,              -- queued|running|cancelling|succeeded|failed|cancelled|interrupted
      source_kind      TEXT DEFAULT '',            -- existing | upload
      source_name      TEXT DEFAULT '',
      docx_path        TEXT DEFAULT '',
      options_json     TEXT DEFAULT '{}',
      current_step     TEXT DEFAULT '',
      exit_code        INTEGER,
      error            TEXT DEFAULT '',
      pid              INTEGER,
      cancel_requested INTEGER DEFAULT 0,
      result_json      TEXT DEFAULT '{}',
      final_kg_path    TEXT DEFAULT '',
      final_kg_sha1    TEXT DEFAULT '',
      derived_from     TEXT DEFAULT '',            -- import 任务对应的 build id
      imported_at      TEXT,
      created_at       TEXT NOT NULL,
      started_at       TEXT,
      finished_at      TEXT,
      heartbeat_at     TEXT
    );
    CREATE TABLE IF NOT EXISTS build_job_steps (
      job_id       TEXT NOT NULL,
      name         TEXT NOT NULL,
      label        TEXT DEFAULT '',
      ordinal      INTEGER DEFAULT 0,
      selected     INTEGER DEFAULT 1,
      status       TEXT DEFAULT 'pending',         -- pending|running|succeeded|failed|cancelled|skipped
      started_at   TEXT,
      finished_at  TEXT,
      exit_code    INTEGER,
      elapsed_sec  REAL,
      detail       TEXT DEFAULT '',
      PRIMARY KEY (job_id, name)
    );
    CREATE TABLE IF NOT EXISTS build_uploads (
      id            TEXT PRIMARY KEY,
      original_name TEXT NOT NULL,
      path          TEXT NOT NULL,
      size          INTEGER DEFAULT 0,
      sha256        TEXT DEFAULT '',
      created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_build_jobs_created ON build_jobs(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_build_jobs_status ON build_jobs(status);
    """)
    conn.commit()


def init_db() -> None:
    get_conn()


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

_JOB_FIELDS = {
    "status", "source_kind", "source_name", "docx_path", "options_json",
    "current_step", "exit_code", "error", "pid", "cancel_requested",
    "result_json", "final_kg_path", "final_kg_sha1", "derived_from",
    "imported_at", "started_at", "finished_at", "heartbeat_at",
}


def _row_job(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("options_json", "result_json"):
        try:
            d[key[:-5]] = json.loads(d.get(key) or "{}")
        except (json.JSONDecodeError, TypeError):
            d[key[:-5]] = {}
        d.pop(key, None)
    d["cancel_requested"] = bool(d.get("cancel_requested"))
    return d


def create_job(kind: str, steps: List[Dict[str, Any]], *,
               source_kind: str = "", source_name: str = "",
               docx_path: str = "", options: Optional[Dict[str, Any]] = None,
               derived_from: str = "") -> str:
    job_id = uuid.uuid4().hex[:16]
    now = _now()
    options = options or {}
    with lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO build_jobs(id, kind, status, source_kind, source_name, "
            "docx_path, options_json, derived_from, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, kind, "queued", source_kind, source_name, docx_path,
             _json(options), derived_from, now),
        )
        for s in steps:
            conn.execute(
                "INSERT INTO build_job_steps(job_id, name, label, ordinal, selected, status) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, s["name"], s.get("label", ""), int(s.get("ordinal", 0)),
                 1 if s.get("selected", True) else 0,
                 "pending" if s.get("selected", True) else "skipped"),
            )
        conn.commit()
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM build_jobs WHERE id=?", (job_id,)).fetchone()
    return _row_job(row) if row else None


def get_job_steps(job_id: str) -> List[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM build_job_steps WHERE job_id=? ORDER BY ordinal", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_job_with_steps(job_id: str) -> Optional[Dict[str, Any]]:
    job = get_job(job_id)
    if job is None:
        return None
    job["steps"] = get_job_steps(job_id)
    return job


def list_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM build_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(limit),)).fetchall()
    return [_row_job(r) for r in rows]


def get_active_job() -> Optional[Dict[str, Any]]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with lock:
        conn = get_conn()
        row = conn.execute(
            f"SELECT * FROM build_jobs WHERE status IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1", ACTIVE_STATUSES).fetchone()
    return _row_job(row) if row else None


def latest_succeeded_build() -> Optional[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM build_jobs WHERE kind='build' AND status='succeeded' "
            "ORDER BY finished_at DESC, rowid DESC LIMIT 1").fetchone()
    return _row_job(row) if row else None


def update_job(job_id: str, **fields: Any) -> bool:
    clean = {k: v for k, v in fields.items() if k in _JOB_FIELDS}
    if not clean:
        return False
    sets = ", ".join(f"{k}=?" for k in clean)
    with lock:
        conn = get_conn()
        cur = conn.execute(f"UPDATE build_jobs SET {sets} WHERE id=?",
                           (*clean.values(), job_id))
        conn.commit()
    return cur.rowcount > 0


def touch_job(job_id: str) -> None:
    update_job(job_id, heartbeat_at=_now())


def request_cancel(job_id: str) -> bool:
    with lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE build_jobs SET cancel_requested=1, status='cancelling' "
            "WHERE id=? AND status IN ('queued','running')", (job_id,))
        conn.commit()
    return cur.rowcount > 0


def finish_job(job_id: str, status: str, *, error: str = "",
               exit_code: Optional[int] = None,
               result: Optional[Dict[str, Any]] = None) -> None:
    fields: Dict[str, Any] = {
        "status": status,
        "error": error[:4000],
        "finished_at": _now(),
    }
    if exit_code is not None:
        fields["exit_code"] = exit_code
    if result is not None:
        fields["result_json"] = _json(result)
    update_job(job_id, **fields)


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------

_STEP_FIELDS = {"status", "started_at", "finished_at", "exit_code",
                "elapsed_sec", "detail", "selected"}


def update_step(job_id: str, name: str, **fields: Any) -> bool:
    clean = {k: v for k, v in fields.items() if k in _STEP_FIELDS}
    if not clean:
        return False
    sets = ", ".join(f"{k}=?" for k in clean)
    with lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE build_job_steps SET {sets} WHERE job_id=? AND name=?",
            (*clean.values(), job_id, name))
        conn.commit()
    return cur.rowcount > 0


def skip_pending_steps(job_id: str) -> None:
    with lock:
        conn = get_conn()
        conn.execute(
            "UPDATE build_job_steps SET status='skipped' "
            "WHERE job_id=? AND status='pending'", (job_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# 重启恢复 / 清理
# ---------------------------------------------------------------------------

def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001
        return False


def _kill_pid_tree(pid: int) -> None:
    try:
        import psutil
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:  # noqa: BLE001
                pass
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def recover_orphans() -> List[str]:
    """启动时把遗留的 queued/running 任务标记为 interrupted，并杀掉残留进程。"""
    interrupted: List[str] = []
    with lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, pid FROM build_jobs WHERE status IN ('queued','running','cancelling')"
        ).fetchall()
    for row in rows:
        job_id, pid = row["id"], row["pid"]
        if _pid_alive(pid):
            _kill_pid_tree(int(pid))
        with lock:
            conn = get_conn()
            conn.execute(
                "UPDATE build_jobs SET status='interrupted', finished_at=?, "
                "error=CASE WHEN error='' THEN '服务重启，任务中断' ELSE error END "
                "WHERE id=?", (_now(), job_id))
            conn.execute(
                "UPDATE build_job_steps SET status='cancelled', finished_at=? "
                "WHERE job_id=? AND status='running'", (_now(), job_id))
            conn.execute(
                "UPDATE build_job_steps SET status='skipped' "
                "WHERE job_id=? AND status='pending'", (job_id,))
            conn.commit()
        interrupted.append(job_id)
    return interrupted


def prune_jobs(keep: int = 20) -> List[str]:
    """保留最近 keep 个任务；返回被删除任务的 id（用于清理目录）。"""
    with lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id FROM build_jobs ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        keep_ids = {r["id"] for r in rows[:keep]}
        active = {r["id"] for r in conn.execute(
            "SELECT id FROM build_jobs WHERE status IN ('queued','running','cancelling')"
        ).fetchall()}
        removed = [r["id"] for r in rows if r["id"] not in keep_ids and r["id"] not in active]
        for job_id in removed:
            conn.execute("DELETE FROM build_job_steps WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM build_jobs WHERE id=?", (job_id,))
        conn.commit()
    return removed


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

def create_upload(original_name: str, path: str, size: int, sha256: str,
                  upload_id: Optional[str] = None) -> Dict[str, Any]:
    upload_id = upload_id or uuid.uuid4().hex[:16]
    now = _now()
    with lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO build_uploads(id, original_name, path, size, sha256, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (upload_id, original_name, path, int(size), sha256, now))
        conn.commit()
    return {"id": upload_id, "original_name": original_name, "path": path,
            "size": size, "sha256": sha256, "created_at": now}


def get_upload(upload_id: str) -> Optional[Dict[str, Any]]:
    with lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM build_uploads WHERE id=?",
                           (upload_id,)).fetchone()
    return dict(row) if row else None


def delete_orphan_uploads(older_than_sec: int = 24 * 3600) -> List[str]:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - older_than_sec))
    with lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, path FROM build_uploads WHERE created_at < ?", (cutoff,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM build_uploads WHERE id=?", (r["id"],))
        conn.commit()
    return [r["path"] for r in rows]
