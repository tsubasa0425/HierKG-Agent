# -*- coding: utf-8 -*-
"""TreeKG Web UI —— FastAPI 入口。

启动：python -m uvicorn app.main:app --port 8777
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 保证仓库根在 sys.path，使 `src.*` 可导入（与 mcp_server.py 同法）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import load_llm_config
from app.routers import chat, graph  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("treekg-web")

app = FastAPI(title="TreeKG Agent Web UI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5777", "http://127.0.0.1:5777"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(graph.router)
app.include_router(chat.router)

# 懒加载单例（构造时会连 Neo4j，故不在启动时建）
app.state.kg = None
app.state.registry = None

# LLM 配置启动时读一次（缺配置启动即报错，便于快速暴露）
try:
    app.state.llm_cfg = load_llm_config()
    logger.info("LLM 配置加载成功: %s", app.state.llm_cfg.get("MODEL_NAME"))
except Exception as exc:
    logger.error("LLM 配置加载失败: %s", exc)
    app.state.llm_cfg = None


@app.on_event("shutdown")
async def _shutdown() -> None:
    kg = getattr(app.state, "kg", None)
    if kg is not None:
        try:
            kg.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭 Neo4j 连接异常: %s", exc)
