# -*- coding: utf-8 -*-
"""Embedding.py —— 用 Ollama 嵌入模型生成节点向量（node_embeddings.pkl）。

输入：conv_entities.json（{name: {updated_description / original, ...}}）
输出：pickle {name: np.ndarray[float32]}，供 Dedup.py / Pred.py 做余弦相似度。

模型与地址默认复用 src/KGRetrieve/config/storage.yaml 的 ChromaConfig；
emb.yaml 里的 OLLAMA_HOST / EMBEDDING_MODEL 留空即沿用 storage.yaml，
显式填写则覆盖（一般不需要，保持与检索侧同一模型即可）。
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import ollama
import yaml
from tqdm import tqdm

SCRIPT_DIR = Path(__file__).resolve().parent            # src/KGBuild
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "bge-m3"

# Windows 控制台默认 GBK，含 emoji 的日志会直接崩；统一按 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ====== 配置加载 ======
def _safe_load_yaml(p: Path) -> dict:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(config_path: Path) -> dict:
    """读取 config.yaml，并按 include_files 依次合并（相对 KGBuild/config/）。"""
    base = _safe_load_yaml(config_path)
    merged = dict(base)

    includes = (base.get("include_files") or []) if isinstance(base, dict) else []
    for inc in includes:
        inc_path = (config_path.parent / inc).resolve()
        if not inc_path.exists():
            # 兼容写成 "config/xxx.yaml" 的老习惯
            alt = (config_path.parent / "config" / inc).resolve()
            if alt.exists():
                inc_path = alt
        if not inc_path.exists():
            raise FileNotFoundError(f"include 文件未找到：{inc} -> {inc_path}")
        merged.update(_safe_load_yaml(inc_path))
    return merged


def load_storage_chroma() -> dict:
    """读取检索侧 storage.yaml 的 ChromaConfig（缺失时返回空字典）。"""
    p = SCRIPT_DIR.parent / "KGRetrieve" / "config" / "storage.yaml"
    if not p.exists():
        return {}
    try:
        return (_safe_load_yaml(p).get("ChromaConfig") or {})
    except yaml.YAMLError as exc:
        print(f"⚠️ storage.yaml 解析失败，改用默认 Ollama 配置：{exc}")
        return {}


def resolve_ollama(emb: dict) -> tuple[str, str]:
    chroma = load_storage_chroma()
    host = str(emb.get("OLLAMA_HOST") or chroma.get("OLLAMA_HOST") or DEFAULT_HOST)
    model = str(emb.get("EMBEDDING_MODEL") or chroma.get("EMBEDDING_MODEL") or DEFAULT_MODEL)
    return host.strip().rstrip("/"), model.strip()


# ====== 文本处理 ======
def load_entities(file_path: str) -> Dict[str, dict]:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"📘 已加载实体 {len(data)} 个：{Path(file_path).name}")
    return data


def entity_text(e: dict) -> str:
    return (e.get("updated_description") or e.get("original") or "").strip() or "[EMPTY]"


# ====== 生成嵌入 ======
def generate_embeddings(
    client: ollama.Client,
    model: str,
    entities: Dict[str, dict],
    batch_size: int = 16,
) -> Dict[str, np.ndarray]:
    items = sorted(entities.items(), key=lambda kv: kv[0])
    names: List[str] = [k for k, _ in items]
    texts: List[str] = [entity_text(v) for _, v in items]
    if not texts:
        print("⚠️ 没有可嵌入的文本，输出空字典")
        return {}

    bs = max(1, int(batch_size))
    batches: List[np.ndarray] = []
    for i in tqdm(range(0, len(texts), bs), desc="Ollama 嵌入", ncols=80):
        batch = texts[i:i + bs]
        resp = client.embed(model=model, input=batch, truncate=True)
        embs = np.asarray(resp.embeddings, dtype=np.float32)
        if embs.ndim != 2 or embs.shape[0] != len(batch):
            raise RuntimeError(
                f"Ollama 返回向量形状异常：{embs.shape}（期望 {len(batch)} 条）")
        batches.append(embs)

    mat = np.vstack(batches)
    # 下游 Dedup/Pred 还会再归一化一次；这里先归一是为了 pkl 自身可直接做余弦
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.maximum(norms, 1e-12)
    print(f"✅ 已生成嵌入向量：{mat.shape[0]} × {mat.shape[1]}（{model}）")
    return {n: mat[i] for i, n in enumerate(names)}


def save_embeddings_pkl(name_to_vec: Dict[str, np.ndarray], out_file: str) -> None:
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "wb") as f:
        pickle.dump(name_to_vec, f)
    print(f"💾 已保存嵌入文件：{out_file}")


# ====== 主入口 ======
def main(argv: Optional[List[str]] = None) -> int:
    cfg_path = SCRIPT_DIR / "config" / "config.yaml"
    try:
        config = load_config(cfg_path)
    except FileNotFoundError as exc:
        # 允许 --help / 直接用 CLI 覆盖参数；正式构建前 pipeline 会做配置预检
        print(f"⚠️ 配置加载不完整，改用默认值：{exc}", file=sys.stderr)
        config = {}
    emb = config.get("EmbConfig") or {}

    input_path = SCRIPT_DIR / "output" / emb.get("INPUT_NAME", "conv_entities.json")
    output_path = SCRIPT_DIR / "output" / emb.get("OUTPUT_NAME", "node_embeddings.pkl")
    host, model = resolve_ollama(emb)

    parser = argparse.ArgumentParser(description="用 Ollama 嵌入模型生成实体向量")
    parser.add_argument("--infile", type=str, default=str(input_path))
    parser.add_argument("--outfile", type=str, default=str(output_path))
    parser.add_argument("--batch_size", type=int, default=int(emb.get("BATCH_SIZE", 16)))
    parser.add_argument("--host", type=str, default=host, help="Ollama 地址（默认取配置）")
    parser.add_argument("--model", type=str, default=model, help="嵌入模型（默认取配置）")
    args = parser.parse_args(argv)

    timeout = float(emb.get("TIMEOUT_SECS", 120))
    client = ollama.Client(host=args.host, timeout=timeout)

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
