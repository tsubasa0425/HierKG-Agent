# -*- coding: utf-8 -*-
"""构图 / 导入子进程编排。

同一时刻只允许一个任务。子进程 stdout 按行（\\r 或 \\n）解析：
  * `@@HIERKG_EVENT@@ {...}` → 结构化事件，写入环形缓冲与 events.jsonl；
  * 其余行 → 普通 log 事件。
SSE 订阅者各自持有 cursor，支持断线重连（after / Last-Event-ID）与回放。
"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Set

from app.services import build_store

logger = logging.getLogger("hierkg-web.build")

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "app" / "data"
BUILD_DIR = DATA_DIR / "builds"
OUTPUT_DIR = ROOT / "src" / "KGBuild" / "output"
FINAL_KG = OUTPUT_DIR / "final_kg.json"

EVENT_PREFIX = "@@HIERKG_EVENT@@"
RING_SIZE = 5000
MAX_LINE_CHARS = 4096
MAX_EVENTS_BYTES = 20 * 1024 * 1024
HEARTBEAT_SEC = 15.0


class JobBusyError(RuntimeError):
    def __init__(self, active_job_id: str):
        super().__init__("已有构建或导入任务在运行")
        self.active_job_id = active_job_id


def build_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    parts = [str(ROOT / "src"), str(ROOT)]
    old = env.get("PYTHONPATH", "")
    parts.extend(p for p in old.split(os.pathsep) if p and p not in parts)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Runtime:
    """一次任务的运行时状态。"""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.dir = BUILD_DIR / job_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.events: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)
        self.lock = threading.RLock()
        self.subscribers: Set[asyncio.Queue] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.seq = 0
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.bytes_written = 0
        self.truncated = False
        self.last_run_end: Optional[Dict[str, Any]] = None
        self.import_started = False
        self.import_finished = False
        self._fh = open(self.events_path, "a", encoding="utf-8", newline="\n")

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


class BuildManager:
    def __init__(self) -> None:
        self._start_lock = threading.Lock()
        self._runtimes: Dict[str, Runtime] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.on_import_terminal: Optional[Callable[[str, str, Dict[str, Any]], None]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def startup(self) -> None:
        interrupted = build_store.recover_orphans()
        if interrupted:
            logger.warning("标记 %d 个中断任务: %s", len(interrupted), interrupted)
        removed = build_store.prune_jobs(keep=20)
        for job_id in removed:
            shutil.rmtree(BUILD_DIR / job_id, ignore_errors=True)
        for path in build_store.delete_orphan_uploads():
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def shutdown(self) -> None:
        with self._start_lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            if not rt.done.is_set():
                rt.cancel_event.set()
                self._kill_tree(rt)
                rt.done.wait(timeout=5)
            rt.close()

    # ------------------------------------------------------------------
    # 启动任务
    # ------------------------------------------------------------------

    def plan_steps(self, options: Dict[str, Any]) -> List[Dict[str, Any]]:
        from src.KGBuild.pipeline import STEP_META, resolve_selection

        names = [str(m["name"]) for m in STEP_META]
        selected = set(resolve_selection(
            names,
            only=options.get("only"),
            start=options.get("start"),
            until=options.get("until"),
            skip=options.get("skip"),
        ))
        return [
            {"name": m["name"], "label": m["label"],
             "ordinal": m["ordinal"], "selected": m["name"] in selected}
            for m in STEP_META
        ]

    def start_build(self, *, source_kind: str, source_name: str, docx_path: str,
                    options: Dict[str, Any]) -> Dict[str, Any]:
        steps = self.plan_steps(options)
        with self._start_lock:
            active = build_store.get_active_job()
            if active:
                raise JobBusyError(active["id"])
            job_id = build_store.create_job(
                "build", steps, source_kind=source_kind, source_name=source_name,
                docx_path=docx_path, options=options)
            rt = self._new_runtime(job_id)
            cmd = [sys.executable, "-u", str(ROOT / "src" / "KGBuild" / "pipeline.py"),
                   "--json-events", "--heartbeat-sec", "10"]
            if options.get("check_only"):
                cmd.append("--check")
            for key, flag in (("start", "--start"), ("until", "--until"),
                              ("skip", "--skip"), ("only", "--only")):
                if options.get(key):
                    cmd.extend([flag, str(options[key])])
            if docx_path:
                cmd.extend(["--docx", docx_path])
            self._launch(rt, cmd, "build")
        return {"job_id": job_id, "job": build_store.get_job_with_steps(job_id)}

    def start_import(self, *, build_job: Dict[str, Any]) -> Dict[str, Any]:
        with self._start_lock:
            active = build_store.get_active_job()
            if active:
                raise JobBusyError(active["id"])
            job_id = build_store.create_job(
                "import", [], source_kind="build", source_name=build_job["id"],
                options={"kg_path": build_job.get("final_kg_path", "")},
                derived_from=build_job["id"])
            rt = self._new_runtime(job_id)
            cmd = [sys.executable, "-u", "-m", "src.KGRetrieve.db_backend",
                   "import", "--json-events",
                   "--kg", build_job.get("final_kg_path") or str(FINAL_KG)]
            self._launch(rt, cmd, "import")
        return {"job_id": job_id, "job": build_store.get_job_with_steps(job_id)}

    def _new_runtime(self, job_id: str) -> Runtime:
        old = self._runtimes.pop(job_id, None)
        if old is not None:
            old.close()
        rt = Runtime(job_id)
        rt.loop = self._loop
        self._runtimes[job_id] = rt
        return rt

    def _launch(self, rt: Runtime, cmd: List[str], kind: str) -> None:
        build_store.update_job(rt.job_id, status="running",
                               started_at=_now_iso(), heartbeat_at=_now_iso())
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=build_env(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0, creationflags=flags)
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动子进程失败")
            build_store.finish_job(rt.job_id, "failed", error=f"启动失败: {exc}", exit_code=-1)
            self._publish(rt, {"type": "job_failed", "error": str(exc)})
            rt.done.set()
            return
        rt.proc = proc
        build_store.update_job(rt.job_id, pid=proc.pid)
        self._publish(rt, {"type": "job_status", "status": "running", "pid": proc.pid})
        rt.thread = threading.Thread(target=self._run, args=(rt, kind),
                                     daemon=True, name=f"build-{rt.job_id}")
        rt.thread.start()

    # ------------------------------------------------------------------
    # 子进程读取与终态处理
    # ------------------------------------------------------------------

    def _run(self, rt: Runtime, kind: str) -> None:
        proc = rt.proc
        assert proc is not None and proc.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        try:
            while True:
                try:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += decoder.decode(chunk)
                parts = re.split(r"\r\n|\r|\n", buf)
                buf = parts.pop()
                for line in parts:
                    self._handle_line(rt, line)
                build_store.touch_job(rt.job_id)
            buf += decoder.decode(b"", final=True)
            if buf:
                self._handle_line(rt, buf)
            exit_code = proc.wait()
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取子进程输出失败")
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            exit_code = -1
            self._publish(rt, {"type": "log", "text": f"读取进程输出失败: {exc}",
                               "truncated": False})

        try:
            self._finalize(rt, kind, exit_code)
        finally:
            rt.done.set()

    def _handle_line(self, rt: Runtime, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        truncated = len(line) > MAX_LINE_CHARS
        text = line[:MAX_LINE_CHARS]
        if text.startswith(EVENT_PREFIX):
            raw = text[len(EVENT_PREFIX):].strip()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict):
                self._apply_job_event(rt, event)
                self._publish(rt, event)
                return
        self._publish(rt, {"type": "log", "text": text, "truncated": truncated})

    def _apply_job_event(self, rt: Runtime, event: Dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "step_start":
            build_store.update_job(rt.job_id, current_step=event.get("step", ""))
            build_store.update_step(
                rt.job_id, event.get("step", ""),
                status="running", started_at=_now_iso(),
                detail=f"第 {event.get('index', '?')}/{event.get('total', '?')} 步")
        elif etype == "step_end":
            ok = bool(event.get("ok"))
            build_store.update_step(
                rt.job_id, event.get("step", ""),
                status="succeeded" if ok else "failed",
                finished_at=_now_iso(), exit_code=event.get("exit_code"),
                elapsed_sec=event.get("elapsed_sec"))
            build_store.update_job(rt.job_id, current_step="")
        elif etype == "run_end":
            rt.last_run_end = event
        elif etype == "import_start":
            rt.import_started = True
        elif etype in ("import_done", "import_error"):
            rt.import_finished = True

    def _publish(self, rt: Runtime, event: Dict[str, Any]) -> None:
        with rt.lock:
            rt.seq += 1
            event = dict(event)
            event["seq"] = rt.seq
            event.setdefault("ts", _now_iso())
            rt.events.append(event)
            if not rt.truncated:
                try:
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    line = payload + "\n"
                    size = len(line.encode("utf-8"))
                    if rt.bytes_written + size > MAX_EVENTS_BYTES:
                        rt.truncated = True
                    else:
                        rt._fh.write(line)
                        rt._fh.flush()
                        rt.bytes_written += size
                except OSError:
                    rt.truncated = True
            queues = list(rt.subscribers)
        if rt.loop is not None and not rt.loop.is_closed():
            for q in queues:
                try:
                    rt.loop.call_soon_threadsafe(q.put_nowait, event)
                except RuntimeError:
                    pass

    def _finalize(self, rt: Runtime, kind: str, exit_code: int) -> None:
        job = build_store.get_job(rt.job_id)
        if job is None:
            return
        cancelled = bool(job.get("cancel_requested")) or rt.cancel_event.is_set()

        if kind == "build":
            run_end = rt.last_run_end or {}
            if cancelled:
                build_store.skip_pending_steps(rt.job_id)
                build_store.finish_job(rt.job_id, "cancelled", exit_code=exit_code,
                                       error="用户取消")
                self._publish(rt, {"type": "job_cancelled"})
                return
            if exit_code != 0 or run_end.get("ok") is False or not run_end:
                build_store.skip_pending_steps(rt.job_id)
                error = run_end.get("error") or f"流水线退出码 {exit_code}"
                build_store.finish_job(rt.job_id, "failed", exit_code=exit_code,
                                       error=str(error))
                self._publish(rt, {"type": "job_failed", "error": str(error)})
                return
            if (job.get("options") or {}).get("check_only"):
                result = {"check_only": True, "counts": {}}
                build_store.finish_job(rt.job_id, "succeeded", exit_code=exit_code,
                                       result=result)
                self._publish(rt, {"type": "job_done", "result": result})
                return
            try:
                result = self._collect_build_result(rt)
            except Exception as exc:  # noqa: BLE001
                logger.exception("收集构建产物失败")
                build_store.finish_job(rt.job_id, "failed", exit_code=exit_code,
                                       error=f"构建产物校验失败: {exc}")
                self._publish(rt, {"type": "job_failed", "error": str(exc)})
                return
            build_store.update_job(
                rt.job_id, final_kg_path=result["final_kg_path"],
                final_kg_sha1=result["final_kg_sha1"])
            build_store.finish_job(rt.job_id, "succeeded", exit_code=exit_code,
                                   result=result)
            self._publish(rt, {"type": "job_done", "result": result})
            return

        # —— import ——
        partial = rt.import_started and not rt.import_finished
        result = {"partial": partial}
        if cancelled:
            build_store.finish_job(rt.job_id, "cancelled", exit_code=exit_code,
                                   error="用户取消", result=result)
            self._notify_import_terminal(rt.job_id, "cancelled", result)
            self._publish(rt, {"type": "job_cancelled", "result": result})
            return
        if exit_code == 0 and rt.import_finished:
            build_store.update_job(rt.job_id, imported_at=_now_iso())
            build_store.finish_job(rt.job_id, "succeeded", exit_code=exit_code,
                                   result=result)
            self._notify_import_terminal(rt.job_id, "succeeded", result)
            self._publish(rt, {"type": "job_done", "result": result})
        else:
            error = "导入失败" + ("（可能已留下半成品图）" if partial else "")
            build_store.finish_job(rt.job_id, "failed", exit_code=exit_code,
                                   error=error, result=result)
            self._notify_import_terminal(rt.job_id, "failed", result)
            self._publish(rt, {"type": "job_failed", "error": error,
                               "result": result})

    def _collect_build_result(self, rt: Runtime) -> Dict[str, Any]:
        if not FINAL_KG.exists():
            raise FileNotFoundError("流水线结束但没有产出 final_kg.json")
        dest = rt.dir / "final_kg.json"
        tmp = dest.with_suffix(".tmp")
        shutil.copy2(FINAL_KG, tmp)
        os.replace(tmp, dest)
        sha = sha1_file(dest)
        with open(dest, "r", encoding="utf-8") as f:
            kg = json.load(f)
        counts = {
            "concepts": len(kg.get("L1_concepts", []) or []),
            "entities": len(kg.get("L2_entities", []) or []),
            "evidences": len(kg.get("L3_evidences", []) or []),
            "edges": len(kg.get("edges", []) or []),
        }
        return {
            "final_kg_path": str(dest),
            "final_kg_sha1": sha,
            "counts": counts,
            "check_only": bool((build_store.get_job(rt.job_id) or {})
                               .get("options", {}).get("check_only")),
        }

    def _notify_import_terminal(self, job_id: str, status: str,
                                result: Dict[str, Any]) -> None:
        cb = self.on_import_terminal
        if cb is None:
            return
        try:
            cb(job_id, status, result)
        except Exception:  # noqa: BLE001
            logger.exception("import 终态回调失败")

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        job = build_store.get_job(job_id)
        if job is None or job["status"] not in build_store.ACTIVE_STATUSES:
            return False
        build_store.request_cancel(job_id)
        rt = self._runtimes.get(job_id)
        if rt is None:
            return True
        rt.cancel_event.set()
        self._kill_tree(rt)
        return True

    def _kill_tree(self, rt: Runtime) -> None:
        proc = rt.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                import psutil
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    child.terminate()
                parent.terminate()
                try:
                    parent.wait(timeout=3)
                except psutil.TimeoutExpired:
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
        except Exception as exc:  # noqa: BLE001
            logger.warning("终止进程树失败: %s", exc)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    @staticmethod
    def _read_events_file(job_id: str, after: Optional[int]) -> List[Dict[str, Any]]:
        path = BUILD_DIR / job_id / "events.jsonl"
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if after is None or int(event.get("seq", 0)) > after:
                        out.append(event)
        except OSError:
            pass
        return out

    async def event_stream(self, job_id: str, after: Optional[int] = None
                           ) -> AsyncIterator[Dict[str, Any]]:
        rt = self._runtimes.get(job_id)
        if rt is None or rt.done.is_set():
            for event in self._read_events_file(job_id, after):
                yield event
            return

        queue: asyncio.Queue = asyncio.Queue()
        with rt.lock:
            backlog = [e for e in rt.events
                       if after is None or int(e.get("seq", 0)) > after]
            oldest = rt.events[0]["seq"] if rt.events else rt.seq + 1
            gapped = after is not None and after + 1 < oldest
            rt.subscribers.add(queue)
            last_seq = rt.seq

        def _snapshot() -> Dict[str, Any]:
            recent = [e for e in rt.events if e.get("type") == "log"][-200:]
            return {
                "type": "snapshot",
                "job": build_store.get_job_with_steps(job_id),
                "last_seq": last_seq,
                "events": recent,
            }

        try:
            if after is None:
                yield _snapshot()
            elif gapped:
                yield {"type": "gap", "from": after, "to": oldest - 1,
                       "last_seq": last_seq}
                yield _snapshot()
            else:
                for event in backlog:
                    yield event

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat", "ts": _now_iso()}
                    continue
                yield event
                if rt.done.is_set() and queue.empty():
                    break
        finally:
            with rt.lock:
                rt.subscribers.discard(queue)

    # ------------------------------------------------------------------
    # 导入资格
    # ------------------------------------------------------------------

    def validate_import(self, build_job_id: str,
                        expected_sha1: Optional[str] = None) -> Dict[str, Any]:
        job = build_store.get_job(build_job_id)
        if job is None:
            raise LookupError("构建任务不存在")
        if job["kind"] != "build" or job["status"] != "succeeded":
            raise ValueError("只有成功完成的构建任务才能导入")
        if (job.get("options") or {}).get("check_only"):
            raise ValueError("仅预检任务没有构建产物")
        latest = build_store.latest_succeeded_build()
        if latest is None or latest["id"] != job["id"]:
            raise ValueError("只能导入最新一次成功构建")

        path = Path(job.get("final_kg_path") or "")
        if not path.is_file():
            raise FileNotFoundError("构建产物已丢失，请重新构建")
        sha = sha1_file(path)
        if job.get("final_kg_sha1") and sha != job["final_kg_sha1"]:
            raise ValueError("构建产物校验失败（文件被修改）")
        if not FINAL_KG.exists() or sha1_file(FINAL_KG) != sha:
            raise ValueError("当前 final_kg.json 与该次构建不一致，请重新构建后再导入")
        if expected_sha1 and expected_sha1 != sha:
            raise ValueError("expected_sha1 与构建产物不一致")

        return {
            "build_job_id": job["id"],
            "final_kg_path": str(path),
            "final_kg_sha1": sha,
            "counts": (job.get("result") or {}).get("counts", {}),
        }


build_manager = BuildManager()
