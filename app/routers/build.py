# -*- coding: utf-8 -*-
"""图谱构建 Web 入口 —— 配置编辑、docx 来源、构建/导入任务与 SSE。

路由前缀 /api/build。同一时刻只允许一个构建或导入任务，配置写入在任务
运行期间返回 409。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import build_config, build_manager, build_store

logger = logging.getLogger("hierkg-web.build")

router = APIRouter(prefix="/api/build", tags=["build"])

DATA_DIR = Path(__file__).resolve().parents[2] / "app" / "data"
UPLOAD_DIR = DATA_DIR / "uploads"


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class LLMConfigPatch(BaseModel):
    api_base: Optional[str] = None
    model_name: Optional[str] = None
    api_key: Optional[str] = None
    workers: Optional[int] = None
    timeout_secs: Optional[int] = None
    retries: Optional[int] = None
    rate_limit_qps: Optional[float] = None
    extra_throttle_sec: Optional[float] = None


class StorageConfigPatch(BaseModel):
    neo4j_uri: Optional[str] = None
    neo4j_username: Optional[str] = None
    neo4j_password: Optional[str] = None
    neo4j_database: Optional[str] = None
    ollama_host: Optional[str] = None
    embedding_model: Optional[str] = None
    persist_directory: Optional[str] = None
    kg_file: Optional[str] = None


class YAMLContent(BaseModel):
    content: str


class ValidateRequest(BaseModel):
    file_id: str
    content: str


class SourceSpec(BaseModel):
    kind: str = "existing"          # existing | upload
    name: Optional[str] = None
    upload_id: Optional[str] = None


class BuildJobRequest(BaseModel):
    source: Optional[SourceSpec] = None
    check_only: bool = False
    start: Optional[str] = None
    until: Optional[str] = None
    skip: Optional[str] = None
    only: Optional[str] = None


class ImportRequest(BaseModel):
    confirm: bool = False
    expected_sha1: Optional[str] = None


class RetryRequest(BaseModel):
    start: Optional[str] = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _busy() -> None:
    active = build_store.get_active_job()
    if active:
        raise HTTPException(status_code=409, detail={
            "message": "已有构建或导入任务在运行",
            "active_job_id": active["id"],
        })


def _not_found(job_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"任务不存在: {job_id}")


def _sse(event: Dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False)
    seq = event.get("seq")
    if seq is not None:
        return f"id: {seq}\nevent: message\ndata: {data}\n\n"
    return f"event: message\ndata: {data}\n\n"


def _apply_llm_config(request: Request) -> None:
    from app.config import load_llm_config
    from app.services.agentscope_agent import clear_model_cache

    request.app.state.llm_cfg = load_llm_config()
    clear_model_cache()


def _apply_storage_config(request: Request) -> None:
    from app.services.qa_cache import reset_ollama_client

    kg = getattr(request.app.state, "kg", None)
    if kg is not None:
        try:
            kg.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭旧 KG 连接失败: %s", exc)
    request.app.state.kg = None
    request.app.state.registry = None
    reset_ollama_client()


def _source_spec(req: BuildJobRequest) -> Dict[str, str]:
    if req.source is None:
        # 未指定则用 text.yaml 的 DOCX_NAME（必须存在于 doc/）
        raw = build_config._load_yaml_file(build_config.CONFIG_FILES["text"]["path"])
        name = ((raw.get("TextSegConfig") or {}).get("DOCX_NAME") or "")
        if not name:
            raise HTTPException(status_code=400, detail="未指定 docx 来源，且 text.yaml 没有 DOCX_NAME")
        path = build_config.resolve_existing_docx(name)
        return {"kind": "existing", "name": name, "path": str(path)}

    if req.source.kind == "existing":
        if not req.source.name:
            raise HTTPException(status_code=400, detail="existing 来源需要 name")
        try:
            path = build_config.resolve_existing_docx(req.source.name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"kind": "existing", "name": req.source.name, "path": str(path)}

    if req.source.kind == "upload":
        if not req.source.upload_id:
            raise HTTPException(status_code=400, detail="upload 来源需要 upload_id")
        upload = build_store.get_upload(req.source.upload_id)
        if upload is None:
            raise HTTPException(status_code=404, detail="上传文件不存在")
        path = Path(upload["path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="上传文件已被清理，请重新上传")
        return {"kind": "upload", "name": upload["original_name"], "path": str(path)}

    raise HTTPException(status_code=400, detail=f"未知来源类型: {req.source.kind}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_config(request: Request, deep: int = 0):
    return {
        "readiness": build_config.readiness(deep=bool(deep)),
        "llm": build_config.read_llm_config(),
        "storage": build_config.read_storage_config(),
        "active_job": build_store.get_active_job(),
    }


@router.put("/config/llm")
async def put_llm_config(patch: LLMConfigPatch, request: Request):
    _busy()
    try:
        result = build_config.write_llm_config(patch.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        _apply_llm_config(request)
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM 配置已保存，但热加载失败: %s", exc)
        result["hot_reload_error"] = str(exc)
    return result


@router.put("/config/storage")
async def put_storage_config(patch: StorageConfigPatch, request: Request):
    _busy()
    try:
        result = build_config.write_storage_config(patch.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _apply_storage_config(request)
    return result


@router.get("/config/files")
async def list_config_files():
    return {"files": build_config.list_config_files()}


@router.get("/config/files/{file_id}")
async def get_config_file(file_id: str):
    try:
        return build_config.read_config_file(file_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知配置文件: {file_id}")


@router.put("/config/files/{file_id}")
async def put_config_file(file_id: str, body: YAMLContent):
    if file_id not in build_config.CONFIG_FILES:
        raise HTTPException(status_code=404, detail=f"未知配置文件: {file_id}")
    if build_config.CONFIG_FILES[file_id]["mode"] != "yaml":
        raise HTTPException(status_code=400, detail="该文件请使用专用表单接口保存")
    _busy()
    try:
        result = build_config.write_config_file(file_id, body.content)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知配置文件: {file_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/config/validate")
async def validate_config(body: ValidateRequest):
    try:
        return build_config.validate_yaml_content(body.file_id, body.content)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知配置文件: {body.file_id}")


# ---------------------------------------------------------------------------
# docx / 上传
# ---------------------------------------------------------------------------

@router.get("/docx")
async def list_docx():
    return {"items": build_config.list_docx()}


@router.post("/uploads", status_code=201)
async def upload_docx(file: UploadFile = File(...)):
    filename = file.filename or ""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    head = await file.read(4)
    size = 0
    digest = hashlib.sha256()
    upload_id = uuid.uuid4().hex[:16]
    tmp: Optional[Path] = None
    try:
        # 先按扩展名/大小做快速校验，再落盘
        if not filename.lower().endswith(".docx"):
            raise HTTPException(status_code=415, detail="只支持 .docx 文件")
        if not head.startswith(b"PK\x03\x04"):
            raise HTTPException(status_code=415, detail="文件不是有效的 docx（ZIP 头不符）")

        tmp = UPLOAD_DIR / f".upload-{upload_id}.tmp"
        with open(tmp, "wb") as out:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > build_config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {build_config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
                digest.update(chunk)
                out.write(chunk)
                chunk = await file.read(1024 * 1024)
        if size == 0:
            raise HTTPException(status_code=400, detail="文件为空")

        dest = UPLOAD_DIR / f"{upload_id}.docx"
        os.replace(tmp, dest)
        build_store.create_upload(filename, str(dest), size, digest.hexdigest(),
                                  upload_id=upload_id)
    except HTTPException:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        await file.close()

    return {
        "id": upload_id,
        "original_name": filename,
        "size": size,
        "sha256": digest.hexdigest(),
    }


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

@router.post("/jobs", status_code=201)
async def create_build_job(req: BuildJobRequest):
    source = _source_spec(req)
    try:
        build_config.validate_selection(only=req.only, start=req.start,
                                        until=req.until, skip=req.skip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ready = build_config.readiness()
    if not ready["build_ready"]:
        errors = [c for c in ready["checks"] if c["severity"] == "error"]
        raise HTTPException(status_code=400, detail={
            "message": "构建配置未就绪", "checks": errors})

    options = {
        "check_only": req.check_only,
        "start": req.start, "until": req.until,
        "skip": req.skip, "only": req.only,
    }
    try:
        result = build_manager.build_manager.start_build(
            source_kind=source["kind"], source_name=source["name"],
            docx_path=source["path"], options=options)
    except build_manager.JobBusyError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "active_job_id": exc.active_job_id})
    return result


@router.get("/jobs/active")
async def active_job():
    return {"job": build_store.get_active_job()}


@router.get("/jobs")
async def list_jobs(limit: int = 20):
    limit = max(1, min(int(limit), 100))
    return {"items": build_store.list_jobs(limit)}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = build_store.get_job_with_steps(job_id)
    if job is None:
        raise _not_found(job_id)
    return job


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request, after: Optional[int] = None):
    job = build_store.get_job(job_id)
    if job is None:
        raise _not_found(job_id)
    if after is None:
        header = request.headers.get("last-event-id")
        if header:
            try:
                after = int(header)
            except ValueError:
                after = None

    async def gen():
        async for event in build_manager.build_manager.event_stream(job_id, after):
            yield _sse(event)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: str):
    job = build_store.get_job(job_id)
    if job is None:
        raise _not_found(job_id)
    if not build_manager.build_manager.cancel(job_id):
        raise HTTPException(status_code=409, detail="任务已经结束，无法取消")
    return {"ok": True, "job_id": job_id}


@router.post("/jobs/{job_id}/retry", status_code=201)
async def retry_job(job_id: str, body: RetryRequest):
    job = build_store.get_job_with_steps(job_id)
    if job is None:
        raise _not_found(job_id)
    if job["status"] not in ("failed", "cancelled", "interrupted"):
        raise HTTPException(status_code=409, detail="只有失败/取消/中断的任务可以重试")

    if job["kind"] == "build":
        options = dict(job.get("options") or {})
        failed = next((s["name"] for s in job["steps"] if s["status"] == "failed"), None)
        start = body.start or failed or job.get("current_step") or options.get("start")
        if not start:
            start = next((s["name"] for s in job["steps"] if s["selected"]), None)
        options["start"] = start
        options["check_only"] = bool(options.get("check_only"))
        try:
            build_config.validate_selection(
                only=options.get("only"), start=start,
                until=options.get("until"), skip=options.get("skip"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        docx_path = job.get("docx_path") or ""
        if not docx_path or not Path(docx_path).is_file():
            raise HTTPException(status_code=410, detail="源 docx 已被清理，请重新选择")
        try:
            return build_manager.build_manager.start_build(
                source_kind=job.get("source_kind", ""),
                source_name=job.get("source_name", ""),
                docx_path=docx_path, options=options)
        except build_manager.JobBusyError as exc:
            raise HTTPException(status_code=409, detail={
                "message": str(exc), "active_job_id": exc.active_job_id})

    # import 重试
    derived = job.get("derived_from")
    if not derived:
        raise HTTPException(status_code=409, detail="导入任务缺少来源构建")
    try:
        build_manager.build_manager.validate_import(derived)
    except (LookupError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    build_job = build_store.get_job(derived)
    try:
        return build_manager.build_manager.start_import(build_job=build_job)
    except build_manager.JobBusyError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "active_job_id": exc.active_job_id})


@router.post("/jobs/{job_id}/import", status_code=201)
async def import_job(job_id: str, req: ImportRequest, request: Request):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="导入会清空 Neo4j，必须显式确认")
    try:
        info = build_manager.build_manager.validate_import(
            job_id, expected_sha1=req.expected_sha1)
    except LookupError:
        raise _not_found(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        msg = str(exc)
        code = 412 if msg.startswith("expected_sha1") else 409
        raise HTTPException(status_code=code, detail=msg)

    build_job = build_store.get_job(job_id)
    request.app.state.kg_maintenance = True
    try:
        result = build_manager.build_manager.start_import(build_job=build_job)
    except build_manager.JobBusyError as exc:
        request.app.state.kg_maintenance = False
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "active_job_id": exc.active_job_id})
    except Exception:
        request.app.state.kg_maintenance = False
        raise
    return {"job_id": result["job_id"], "job": result["job"], "import": info}
