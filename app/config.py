# -*- coding: utf-8 -*-
"""Web 层 LLM 配置 loader。

独立 yaml.safe_load 读取 src/KGBuild/config/config.yaml 的 APIConfig，
不 import KGBuild 各阶段模块（Extraction.py/Conv.py 等在 import 时会构造 client、
mkdir 等副作用）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_DEFAULT_CFG = Path(__file__).resolve().parents[1] / "src" / "KGBuild" / "config" / "config.yaml"


def load_llm_config(path: str | None = None) -> Dict[str, Any]:
    """读取 LLM 配置（APIConfig 字段）。可用环境变量 HIERKG_LLM_CONFIG 覆盖路径。"""
    p = Path(path or os.environ.get("HIERKG_LLM_CONFIG") or _DEFAULT_CFG)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    apic = raw.get("APIConfig") or {}
    missing = [k for k in ("API_BASE", "API_KEY", "MODEL_NAME") if not apic.get(k)]
    if missing:
        raise RuntimeError(f"LLM 配置缺失字段: {missing}（{p}）")
    return apic  # API_BASE / API_KEY / MODEL_NAME / CHAT_COMPLETIONS_PATH / TIMEOUT_SECS / RETRIES ...


def make_client(cfg: Dict[str, Any]):
    """从 APIConfig 构造 AsyncOpenAI 客户端（base_url 自动拼 /chat/completions）。"""
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=cfg["API_KEY"], base_url=cfg["API_BASE"])


_CHAT_DEFAULTS: Dict[str, Any] = {
    "ENABLED": True,       # 热问题缓存总开关
    "DB_PATH": "",         # 空 → chat_store 默认路径（env HIERKG_CHAT_DB > app/data/chat.db）
    "THETA_EXACT": 0.96,   # ≥ 此相似度 + 版本匹配 → L1 答案直出
    "THETA_NEAR": 0.85,    # ≥ 此相似度 → L2 证据回放；否则 L3 全量检索
}


def load_chat_config(path: str | None = None) -> Dict[str, Any]:
    """读取 config.yaml 的 CacheConfig 段（会话/缓存共用配置）。

    与 load_llm_config 同一配置文件；段缺失或字段缺省时回退默认值，不报错。
    返回大写下划线键，与 APIConfig 风格一致。
    """
    merged = dict(_CHAT_DEFAULTS)
    p = Path(path or os.environ.get("HIERKG_LLM_CONFIG") or _DEFAULT_CFG)
    if not p.exists():
        return merged
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = raw.get("CacheConfig") or {}
    for k in _CHAT_DEFAULTS:
        if k in cfg and cfg[k] is not None:
            merged[k] = cfg[k]
    return merged
