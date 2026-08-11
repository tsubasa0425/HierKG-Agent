# -*- coding: utf-8 -*-
"""从 ModelScope 下载 bge-reranker-v2-m3（国内 CDN，绕开被墙的 huggingface.co）
带断点续传：每个文件失败自动重试，直到大小匹配才算完成。
用法：python _download_reranker.py
"""
import os
import sys
import time
from pathlib import Path

import requests

BASE = "https://modelscope.cn/models/BAAI/bge-reranker-v2-m3/resolve/master"
TARGET = Path(__file__).resolve().parent / "src" / "KGRetrieve" / "models" / "bge-reranker-v2-m3"

FILES = [
    ("config.json", 795),
    ("configuration.json", 77),
    ("model.safetensors", 2271071852),
    ("sentencepiece.bpe.model", 5069051),
    ("special_tokens_map.json", 964),
    ("tokenizer.json", 17098273),
    ("tokenizer_config.json", 1173),
]

HEADERS = {"User-Agent": "treekg-downloader/1.0"}


def download_one(name: str, expected: int) -> bool:
    path = TARGET / name
    if path.exists() and path.stat().st_size == expected:
        print(f"[skip] {name} 已存在且完整")
        return True
    pos = path.stat().st_size if path.exists() else 0
    # 已存在但大小不对时，保留已下载部分做续传；若超出预期则重下
    if pos > expected:
        path.unlink()
        pos = 0
    while pos < expected:
        headers = dict(HEADERS)
        if pos > 0:
            headers["Range"] = f"bytes={pos}-"
        try:
            with requests.get(BASE + "/" + name, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                mode = "ab" if pos > 0 else "wb"
                with open(path, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                pos = path.stat().st_size
                pct = 100.0 * pos / expected
                print(f"[{name}] {pos}/{expected} ({pct:.1f}%)", flush=True)
        except Exception as e:
            print(f"[{name}] 中断: {type(e).__name__}: {str(e)[:80]}，3 秒后重试（断点续传）", flush=True)
            time.sleep(3)
    ok = path.stat().st_size == expected
    print(f"[{'OK' if ok else 'FAIL'}] {name} {path.stat().st_size} bytes")
    return ok


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    all_ok = True
    for name, size in FILES:
        try:
            all_ok &= download_one(name, size)
        except KeyboardInterrupt:
            print("手动中断，已下载部分保留，下次运行续传")
            return 1
    if all_ok:
        print(f"\n✅ 全部完成，模型在 {TARGET}")
        return 0
    print("\n⚠️ 部分文件未完成，重新运行本脚本即可续传")
    return 1


if __name__ == "__main__":
    sys.exit(main())