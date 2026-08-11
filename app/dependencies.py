# -*- coding: utf-8 -*-
"""懒加载单例依赖：KGDBMemory + ToolRegistry，挂 app.state。

Neo4j 不可用时返回 503 而不是启动崩溃；KGDBMemory 构造会测试连接。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from src.KGRetrieve import KGDBMemory, ToolRegistry


def get_kg(request: Request) -> KGDBMemory:
    if request.app.state.kg is None:
        try:
            request.app.state.kg = KGDBMemory()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Neo4j 不可用: {exc}")
    return request.app.state.kg


def get_registry(request: Request) -> ToolRegistry:
    kg = get_kg(request)
    if request.app.state.registry is None:
        request.app.state.registry = ToolRegistry(kg)
    return request.app.state.registry
