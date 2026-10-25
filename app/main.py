# -*- coding: utf-8 -*-
"""HierKG Web UI —— FastAPI 入口。

启动：python -m uvicorn app.main:app    # 默认 127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 保证仓库根在 sys.path，使 `src.*` 可导入（与 mcp_server.py 同法）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import load_chat_config, load_llm_config
from app.routers import build, chat, graph, sessions
from app.services import build_manager, build_store, chat_store, kg_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("hierkg-web")

app = FastAPI(title="HierKG Agent Web UI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5777", "http://127.0.0.1:5777"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(graph.router)
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(build.router)

# 懒加载单例（构造时会连 Neo4j，故不在启动时建）
app.state.kg = None
app.state.registry = None
# 导入期间图不可用（Neo4j 会被清空重建）
app.state.kg_maintenance = False
app.state.loop = None

# 会话/热缓存配置：启动读一次，DB_PATH 非空时覆盖 chat_store 默认路径
app.state.chat_cfg = load_chat_config()
if app.state.chat_cfg.get("DB_PATH"):
    chat_store.set_db_path(app.state.chat_cfg["DB_PATH"])

# LLM 配置启动时读一次（缺配置启动即报错，便于快速暴露）
try:
    app.state.llm_cfg = load_llm_config()
    logger.info("LLM 配置加载成功: %s", app.state.llm_cfg.get("MODEL_NAME"))
except Exception as exc:
    logger.error("LLM 配置加载失败: %s", exc)
    app.state.llm_cfg = None


def _apply_import_terminal(job_id: str, status: str, result: dict) -> None:
    """导入结束：释放陈旧 Chroma/Neo4j 句柄，刷新 KG 版本戳。"""
    app.state.kg_maintenance = False
    kg = getattr(app.state, "kg", None)
    if kg is not None:
        try:
            kg.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭旧 KG 连接异常: %s", exc)
    app.state.kg = None
    app.state.registry = None

    if status == "succeeded":
        try:
            job = build_store.get_job(job_id) or {}
            build_job = build_store.get_job(job.get("derived_from") or "") or {}
            sha = build_job.get("final_kg_sha1") or ""
            if sha:
                stamp = _REPO_ROOT / "src" / "KGBuild" / "output" / "kg_version.txt"
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text(sha, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 kg_version 失败: %s", exc)
    kg_version.invalidate_cache()


def _dispatch_import_terminal(job_id: str, status: str, result: dict) -> None:
    """从任务线程回到事件循环线程执行（关闭连接的副作用不阻塞读取线程）。"""
    loop = getattr(app.state, "loop", None)
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(_apply_import_terminal, job_id, status, result)
    else:
        _apply_import_terminal(job_id, status, result)


@app.on_event("startup")
async def _startup() -> None:
    app.state.loop = asyncio.get_running_loop()
    manager = build_manager.build_manager
    manager.bind_loop(app.state.loop)
    manager.on_import_terminal = _dispatch_import_terminal
    manager.startup()


@app.on_event("shutdown")
async def _shutdown() -> None:
    build_manager.build_manager.shutdown()
    kg = getattr(app.state, "kg", None)
    if kg is not None:
        try:
            kg.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭 Neo4j 连接异常: %s", exc)
