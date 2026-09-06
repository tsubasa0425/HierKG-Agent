# -*- coding: utf-8 -*-
"""KG 构建版本号 —— 热缓存失效的依据之一。

优先读增量构建将来写的 stamp 文件 src/KGBuild/output/kg_version.txt；
未引入版本标记前，兜底用 final_kg.json 内容 sha1 前 12 位（mtime 变化即失效）。
缓存层只调 get_kg_version()，不关心构建方式（全量/增量），将来构建侧换机制
只需把版本写进 stamp 文件。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_OUT_DIR = ROOT / "src" / "KGBuild" / "output"
_STAMP = _OUT_DIR / "kg_version.txt"
_FINAL_KG = _OUT_DIR / "final_kg.json"

_cache: str | None = None


def get_kg_version() -> str:
    """当前 KG 版本号；无构建产物时返回空串（缓存视为无版本约束）。"""
    global _cache
    if _cache is not None:
        return _cache
    ver = _read_version()
    _cache = ver
    return ver


def _read_version() -> str:
    if _STAMP.exists():
        try:
            return _STAMP.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if _FINAL_KG.exists():
        try:
            return hashlib.sha1(_FINAL_KG.read_bytes()).hexdigest()[:12]
        except OSError:
            pass
    return ""


def invalidate_cache() -> None:
    """测试用：清空内存缓存，强制重新计算。"""
    global _cache
    _cache = None
