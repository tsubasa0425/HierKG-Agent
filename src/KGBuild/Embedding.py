# -*- coding: utf-8 -*-
"""Embedding v2 —— 用 Ollama 为 L1/L2 节点生成向量（node_embeddings.pkl）。

输入：aggr_entities.json（v2：{entities:[{id,name,aliases,type,definition,...}]}）
输出：pickle {node_id: np.ndarray[float32]}，向量已 L2 归一化。

嵌入文本 = 规范名 + 别名 + 类型 + 定义。定义来自原文/证据化补全，
不再使用 v1 的“邻居描述扩写”，避免把邻域噪声写进向量。

模型与地址默认复用 src/KGRetrieve/config/storage.yaml 的 ChromaConfig；
emb.yaml 的 OLLAMA_HOST / EMBEDDING_MODEL 留空即沿用，显式填写则覆盖。
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
OUT_DIR = SCRIPT_DIR / "output"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "bge-m3"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEFAULTS: Dict[str, Any] = {
    "INPUT_NAME": "aggr_entities.json",
    "OUTPUT_NAME": "node_embeddings.pkl",
    "OLLAMA_HOST": "",
    "EMBEDDING_MODEL": "",
    "BATCH_SIZE": 16,
    "TIMEOUT_SECS": 120,
    "MAX_DEFINITION_CHARS": 400,
}


def _safe_load_yaml(p: Path) -> dict:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> Dict[str, Any]:
    cfg_path = CONFIG_DIR / "config.yaml"
    merged: Dict[str, Any] = {}
    if cfg_path.exists():
        merged = dict(_safe_load_yaml(cfg_path))
        for rel in merged.get("include_files", []) or []:
            p = (CONFIG_DIR / str(rel)).resolve()
            if p.exists():
                merged.update(_safe_load_yaml(p))
    if "EmbConfig" not in merged:
        merged.update(_safe_load_yaml(CONFIG_DIR / "emb.yaml"))
    cfg = dict(DEFAULTS)
    cfg.update(merged.get("EmbConfig") or {})
    return cfg


def load_storage_chroma() -> dict:
    p = SCRIPT_DIR.parent / "KGRetrieve" / "config" / "storage.yaml"
    if not p.exists():
        return {}
    try:
        return _safe_load_yaml(p).get("ChromaConfig") or {}
    except yaml.YAMLError as exc:
        print(f"⚠️ storage.yaml 解析失败，改用默认 Ollama 配置：{exc}")
        return {}


def resolve_ollama(emb: Dict[str, Any]) -> tuple[str, str]:
    chroma = load_storage_chroma()
    host = str(emb.get("OLLAMA_HOST") or chroma.get("OLLAMA_HOST") or DEFAULT_HOST)
    model = str(emb.get("EMBEDDING_MODEL") or chroma.get("EMBEDDING_MODEL") or DEFAULT_MODEL)
    return host.strip().rstrip("/"), model.strip()


# ---------------------------------------------------------------------------
# 输入 / 文本
# ---------------------------------------------------------------------------

def load_entities(file_path: str) -> List[dict]:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "entities" in data:
        entities = data["entities"]
    elif isinstance(data, list):
        entities = data
    else:
        raise ValueError("输入不是 v2 aggr 产物（需要 {entities:[...]}）")
    print(f"📘 已加载实体 {len(entities)} 个：{Path(file_path).name}")
    return entities


def entity_text(e: dict) -> str:
    name = (e.get("name") or "").strip()
    aliases = [a for a in (e.get("aliases") or []) if a and a != name]
    etype = (e.get("type") or "").strip()
    definition = (e.get("definition") or "").strip()
    parts = [name]
    if aliases:
        parts.append("（" + "、".join(aliases[:4]) + "）")
    if etype:
        parts.append(f"[{etype}]")
    if definition:
        parts.append("：" + definition)
    text = "".join(parts).strip()
    return text or name or "[EMPTY]"


# ---------------------------------------------------------------------------
# 生成嵌入
# ---------------------------------------------------------------------------

def generate_embeddings(client, model: str, entities: List[dict],
                        batch_size: int = 16) -> Dict[str, np.ndarray]:
    items = sorted(
        [e for e in entities if e.get("id")],
        key=lambda e: str(e["id"]),
    )
    if not items:
        print("⚠️ 没有带 id 的实体，输出空字典")
        return {}
    ids: List[str] = [str(e["id"]) for e in items]
    texts: List[str] = [entity_text(e) for e in items]

    bs = max(1, int(batch_size))
    batches: List[np.ndarray] = []
    for i in range(0, len(texts), bs):
        batch = texts[i:i + bs]
        resp = client.embed(model=model, input=batch, truncate=True)
        embs = np.asarray(resp.embeddings, dtype=np.float32)
        if embs.ndim != 2 or embs.shape[0] != len(batch):
            raise RuntimeError(
                f"Ollama 返回向量形状异常：{embs.shape}（期望 {len(batch)} 条）")
        batches.append(embs)
        print(f"  嵌入 {min(i + bs, len(texts))}/{len(texts)}", end="\r")

    mat = np.vstack(batches)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.maximum(norms, 1e-12)
    print(f"\n✅ 已生成嵌入向量：{mat.shape[0]} × {mat.shape[1]}（{model}）")
    return {nid: mat[i] for i, nid in enumerate(ids)}


def save_embeddings_pkl(name_to_vec: Dict[str, np.ndarray], out_file: str) -> None:
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "wb") as f:
        pickle.dump(name_to_vec, f)
    print(f"💾 已保存嵌入文件：{out_file}")


def main(argv: Optional[List[str]] = None) -> int:
    cfg = load_config()
    input_path = OUT_DIR / str(cfg["INPUT_NAME"])
    output_path = OUT_DIR / str(cfg["OUTPUT_NAME"])
    host, model = resolve_ollama(cfg)

    parser = argparse.ArgumentParser(description="用 Ollama 为 v2 节点生成向量")
    parser.add_argument("--infile", type=str, default=str(input_path))
    parser.add_argument("--outfile", type=str, default=str(output_path))
    parser.add_argument("--batch_size", type=int, default=int(cfg.get("BATCH_SIZE", 16)))
    parser.add_argument("--host", type=str, default=host, help="Ollama 地址（默认取配置）")
    parser.add_argument("--model", type=str, default=model, help="嵌入模型（默认取配置）")
    args = parser.parse_args(argv)

    try:
        import ollama
    except ImportError:
        print("❌ 未安装 ollama Python 包：pip install ollama", file=sys.stderr)
        return 1

    client = ollama.Client(host=args.host, timeout=float(cfg.get("TIMEOUT_SECS", 120)))
    print(f"🔗 Ollama: {args.host}  模型: {args.model}")
    entities = load_entities(args.infile)
    try:
        name_to_vec = generate_embeddings(client, args.model, entities, args.batch_size)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 调用 Ollama 嵌入失败：{exc}")
        print(f"   请确认 Ollama 已启动，且模型已拉取：ollama pull {args.model}")
        return 1

    save_embeddings_pkl(name_to_vec, args.outfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
