# -*- coding: utf-8 -*-
"""热问题缓存 —— 三层：L1 答案直出 / L2 证据回放 / L3 全量检索。

与 KGRetrieve 同用 Ollama bge-m3 做问句向量（同一 embedding 空间），
问句相似度用内存 cosine 暴力检索（几千条量级毫秒级）。

层级判定（阈值由 config 传入）：
  sim >= theta_exact 且 kg_version 匹配  → L1 答案直出
  sim >= theta_near                      → L2 证据回放（版本不匹配的 L1 也降级到 L2）
  其余                                   → L3 全量检索（由调用方跑 agent 循环）

失效设计（兼容全量重建与未来增量更新）：
  * kg_version 不匹配 → L1 立即降级 L2（答案文本可能是旧图谱的）
  * L2 回放按 id 重新抓节点/证据，抓不到自然掉回 L3（调用方处理）
Ollama/embedding 失败时静默降级为"缓存未命中"，不阻塞对话。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from .chat_store import get_conn, lock

logger = logging.getLogger("hierkg-web.qa_cache")

_STORAGE_CFG = Path(__file__).resolve().parents[2] / "src" / "KGRetrieve" / "config" / "storage.yaml"

_ollama_client = None
_ollama_model = ""
_embed_ok = False  # 连接过 Ollama 且成功，才允许后续查询


def _load_storage_cfg() -> Dict[str, Any]:
    raw = {}
    if _STORAGE_CFG.exists():
        try:
            raw = yaml.safe_load(_STORAGE_CFG.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            raw = {}
    cc = raw.get("ChromaConfig", {}) or {}
    return {
        "host": cc.get("OLLAMA_HOST", "http://localhost:11434"),
        "model": cc.get("EMBEDDING_MODEL", "bge-m3"),
    }


def embed_text(text: str) -> Optional[List[float]]:
    """问句向量（bge-m3）。失败返回 None，调用方按未命中处理。"""
    global _ollama_client, _ollama_model, _embed_ok
    if not _embed_ok:
        try:
            import ollama
            sc = _load_storage_cfg()
            _ollama_client = ollama.Client(host=sc["host"])
            _ollama_model = sc["model"]
            _embed_ok = True
        except Exception as exc:
            logger.warning("Ollama 连接失败，热缓存禁用: %s", exc)
            return None
    try:
        resp = _ollama_client.embeddings(model=_ollama_model, prompt=text)
        return resp["embedding"]
    except Exception as exc:
        logger.warning("embedding 失败（按缓存未命中处理）: %s", exc)
        return None


def _normalize(q: str) -> str:
    return " ".join(q.strip().split())


# ---------------------------------------------------------------------------
# 内存向量索引
# ---------------------------------------------------------------------------

_index: Optional[List[Dict[str, Any]]] = None
_index_dirty = True


def _load_index() -> List[Dict[str, Any]]:
    global _index, _index_dirty
    if _index is not None and not _index_dirty:
        return _index
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, q_embed FROM qa_cache WHERE q_embed IS NOT NULL").fetchall()
    idx: List[Dict[str, Any]] = []
    for r in rows:
        try:
            emb = np.frombuffer(bytes(r["q_embed"]), dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if emb.size == 0:
            continue
        idx.append({"id": r["id"], "embed": emb})
    _index = idx
    _index_dirty = False
    return idx


def _mark_dirty() -> None:
    global _index_dirty
    _index_dirty = True


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _top_id(emb: np.ndarray, theta: float) -> Optional[int]:
    idx = _load_index()
    best_id, best_sim = None, 0.0
    for it in idx:
        sim = _cosine(emb, it["embed"])
        if sim > best_sim:
            best_sim, best_id = sim, it["id"]
    return best_id if best_id is not None and best_sim >= theta else None


# ---------------------------------------------------------------------------
# 查询 / 写入
# ---------------------------------------------------------------------------

@dataclass
class CacheHit:
    level: str                       # "answer" | "evidence"
    entry: Dict[str, Any]
    similarity: float = 0.0


def lookup(question: str, kg_version: str, theta_exact: float = 0.96,
           theta_near: float = 0.85) -> Optional[CacheHit]:
    q = _normalize(question)
    if not q:
        return None
    emb_raw = embed_text(q)
    if emb_raw is None:
        return None
    emb = np.asarray(emb_raw, dtype=np.float32)
    idx = _load_index()
    if not idx:
        return None
    best_id, best_sim = None, 0.0
    for it in idx:
        sim = _cosine(emb, it["embed"])
        if sim > best_sim:
            best_sim, best_id = sim, it["id"]
    if best_id is None or best_sim < theta_near:
        return None
    row = get_conn().execute("SELECT * FROM qa_cache WHERE id=?", (best_id,)).fetchone()
    if row is None:
        return None
    entry = dict(row)
    # L1：相似度达标 + 版本匹配 + 有缓存答案
    if best_sim >= theta_exact and entry.get("kg_version") == kg_version and entry.get("answer"):
        return CacheHit("answer", entry, best_sim)
    return CacheHit("evidence", entry, best_sim)


def record(question: str, answer: str, evidence_ids: List[str], node_ids: List[str],
           kg_version: str, theta_near: float = 0.85) -> None:
    """全量检索结束后写入缓存；相似问题已存在则就地更新（升级答案/证据）。"""
    q = _normalize(question)
    if not q or not answer.strip():
        return  # 只缓存出过答案的条目
    emb_raw = embed_text(q)
    if emb_raw is None:
        return
    emb = np.asarray(emb_raw, dtype=np.float32)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing_id = _top_id(emb, theta_near)
    with lock:
        conn = get_conn()
        if existing_id is not None:
            conn.execute(
                "UPDATE qa_cache SET answer=?, evidence_ids=?, node_ids=?, kg_version=?, "
                "updated_at=? WHERE id=?",
                (answer, json.dumps(evidence_ids, ensure_ascii=False),
                 json.dumps(node_ids, ensure_ascii=False), kg_version, now, existing_id))
        else:
            conn.execute(
                "INSERT INTO qa_cache(q_norm, q_embed, answer, evidence_ids, node_ids, "
                "kg_version, hit_count, created_at, updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
                (q, emb.tobytes(), answer, json.dumps(evidence_ids, ensure_ascii=False),
                 json.dumps(node_ids, ensure_ascii=False), kg_version, now, now))
        conn.commit()
    _mark_dirty()


def mark_hit(entry_id: int) -> None:
    with lock:
        conn = get_conn()
        conn.execute(
            "UPDATE qa_cache SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), entry_id))
        conn.commit()
