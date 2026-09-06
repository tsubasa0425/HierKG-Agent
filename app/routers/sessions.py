# -*- coding: utf-8 -*-
"""会话管理接口 —— /api/sessions 会话 CRUD。

user_id 字段当前仅预留（无鉴权系统，默认 ''），多用户隔离待接入认证时启用。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import chat_store

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    user_id: str = ""
    title: str = ""


class SessionRename(BaseModel):
    title: str


@router.post("")
async def create_session(body: SessionCreate):
    sid = await asyncio.to_thread(chat_store.create_session, body.user_id, body.title)
    return {"session_id": sid}


@router.get("")
async def list_sessions(user_id: str = ""):
    sessions = await asyncio.to_thread(chat_store.list_sessions, user_id)
    return {"sessions": sessions}


@router.get("/{session_id}/messages")
async def get_session_messages(session_id: str):
    sess = await asyncio.to_thread(chat_store.get_session, session_id)
    if not sess:
        raise HTTPException(404, "会话不存在")
    messages = await asyncio.to_thread(chat_store.get_messages, session_id)
    return {"session_id": session_id, "messages": messages}


@router.patch("/{session_id}")
async def rename_session(session_id: str, body: SessionRename):
    ok = await asyncio.to_thread(chat_store.rename_session, session_id, body.title)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"session_id": session_id, "title": body.title}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    ok = await asyncio.to_thread(chat_store.delete_session, session_id)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"deleted": True}
