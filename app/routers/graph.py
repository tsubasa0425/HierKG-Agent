# -*- coding: utf-8 -*-
"""图谱可视化接口：全图采样 / 统计 / 邻域子图 / 名称搜索。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.dependencies import get_kg
from app.services.graph_service import build_graph_data, search_results

router = APIRouter(prefix="/api/graph", tags=["graph"])

_VALID_LAYERS = ("concept", "entity", "evidence")


@router.get("")
async def graph(request: Request, types: str | None = None, limit: int = 200):
    """全图分层采样。types 逗号分隔 concept/entity/evidence 过滤层级。"""
    layers = [t for t in (types or "").split(",") if t in _VALID_LAYERS] or None
    kg = get_kg(request)
    return build_graph_data(kg.sample_graph(limit=min(limit, 600), layers=layers))


@router.get("/stats")
async def graph_stats(request: Request):
    return get_kg(request).graph_stats()


@router.get("/neighborhood")
async def neighborhood(request: Request, entity: str, depth: int = 2):
    """N 跳邻域子图（双击展开用）。entity 为 node_id。"""
    kg = get_kg(request)
    return build_graph_data(kg.get_neighborhood(entity, depth=min(depth, 4), max_nodes=150))


@router.get("/search")
async def graph_search(request: Request, q: str, limit: int = 20):
    """图谱页搜索框：名称/别名模糊搜节点。"""
    return {"results": search_results(get_kg(request), q, limit=min(limit, 50))}
