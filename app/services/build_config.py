# -*- coding: utf-8 -*-
"""图谱构建配置的读取、校验与写入。

两类文件：
  * form 模式 —— config.yaml（APIConfig）/ storage.yaml（Neo4j + Ollama），
    结构化字段，密钥只写不回显；
  * yaml 模式 —— text/extraction/conv/aggr/dedup/pred/emb 与 schema.v2.yaml，
    保存原始文本（保留注释与顺序），服务端做结构校验后原子写入。

所有路径都按注册表白名单解析，绝不接受任意 file_id 之外的路径。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]
CFG_DIR = ROOT / "src" / "KGBuild" / "config"
STG_DIR = ROOT / "src" / "KGRetrieve" / "config"
DOC_DIR = ROOT / "src" / "KGBuild" / "doc"
DATA_DIR = ROOT / "app" / "data"
BACKUP_DIR = DATA_DIR / "config_backups"

MAX_YAML_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

write_lock = threading.Lock()

# 可编辑文件注册表：rebuild_from 表示改完后建议从哪一步重跑。
CONFIG_FILES: Dict[str, Dict[str, Any]] = {
    "app": {
        "label": "主配置", "path": CFG_DIR / "config.yaml", "mode": "form",
        "secrets": ["API_KEY"], "rebuild_from": None,
    },
    "storage": {
        "label": "存储与嵌入", "path": STG_DIR / "storage.yaml", "mode": "form",
        "secrets": ["PASSWORD"], "rebuild_from": None,
    },
    "text": {
        "label": "文本分割", "path": CFG_DIR / "text.yaml", "mode": "yaml",
        "top": "TextSegConfig", "rebuild_from": "textseg",
    },
    "extraction": {
        "label": "实体关系抽取", "path": CFG_DIR / "extraction.yaml", "mode": "yaml",
        "top": "ExtractionConfig", "rebuild_from": "extraction",
    },
    "conv": {
        "label": "格式转换", "path": CFG_DIR / "conv.yaml", "mode": "yaml",
        "top": "ConvConfig", "rebuild_from": "conv",
    },
    "aggr": {
        "label": "概念分层", "path": CFG_DIR / "aggr.yaml", "mode": "yaml",
        "top": "AggrConfig", "rebuild_from": "aggr",
    },
    "dedup": {
        "label": "概念去重", "path": CFG_DIR / "dedup.yaml", "mode": "yaml",
        "top": "DedupConfig", "rebuild_from": "dedup",
    },
    "pred": {
        "label": "关系预测", "path": CFG_DIR / "pred.yaml", "mode": "yaml",
        "top": "PredConfig", "rebuild_from": "pred",
    },
    "emb": {
        "label": "向量嵌入", "path": CFG_DIR / "emb.yaml", "mode": "yaml",
        "top": "EmbConfig", "rebuild_from": "embedding",
    },
    "schema": {
        "label": "L0 本体", "path": CFG_DIR / "schema.v2.yaml", "mode": "yaml",
        "top": None, "rebuild_from": "textseg", "critical": True,
    },
}

FORM_KEYS = {"app", "storage"}
STAGE_IO_KEYS: Dict[str, List[str]] = {
    "text": ["DOCX_NAME", "TOC_NAME", "WARN_NAME"],
    "extraction": ["IN_NAME", "OUT_NAME"],
    "conv": ["TOC_ENT_REL_NAME", "CONV_RESULT_NAME"],
    "aggr": ["CONV_IN_NAME", "OUT_NAME"],
    "dedup": ["ENTITIES_AGGR_NAME", "EMBEDDINGS_NAME", "RESULT_NAME"],
    "pred": ["DEDUP_NAME", "EMBEDDINGS_NAME", "RESULT_NAME"],
    "emb": ["INPUT_NAME", "OUTPUT_NAME"],
}

_LLM_FIELDS = [
    "api_base", "model_name", "api_key", "workers", "timeout_secs",
    "retries", "rate_limit_qps", "extra_throttle_sec",
]
_STORAGE_FIELDS = [
    "neo4j_uri", "neo4j_username", "neo4j_password", "neo4j_database",
    "ollama_host", "embedding_model", "persist_directory", "kg_file",
]

_DEFAULT_INCLUDES = ["text.yaml", "extraction.yaml", "conv.yaml", "aggr.yaml",
                     "dedup.yaml", "pred.yaml", "emb.yaml"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    """原样读取（不做换行归一化），保证 content/sha1/size 与磁盘一致。"""
    with path.open("r", encoding="utf-8", newline="") as f:
        return f.read()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _backup(path: Path, file_id: str) -> Optional[Path]:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{stamp}-{file_id}{path.suffix}"
    shutil.copy2(path, dest)
    return dest


def _mask(value: str) -> Dict[str, Any]:
    value = value or ""
    return {
        "set": bool(value) and not _is_placeholder(value),
        "hint": f"****{value[-4:]}" if len(value) >= 4 else ("****" if value else ""),
    }


def _is_placeholder(value: Any) -> bool:
    s = str(value or "").strip()
    return (not s) or s.startswith("<") or "YOUR_" in s.upper()


def _resolve_config_path(file_id: str) -> Path:
    meta = CONFIG_FILES.get(file_id)
    if meta is None:
        raise KeyError(file_id)
    return meta["path"]


def _yaml_error(exc: yaml.YAMLError) -> List[Dict[str, Any]]:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    return [{
        "line": (mark.line + 1) if mark else None,
        "column": (mark.column + 1) if mark else None,
        "message": problem,
    }]


def _err(errors: List[Dict[str, Any]], message: str,
         line: Optional[int] = None, column: Optional[int] = None) -> None:
    errors.append({"line": line, "column": column, "message": message})


def _warn(warnings: List[Dict[str, Any]], message: str,
          line: Optional[int] = None, column: Optional[int] = None) -> None:
    warnings.append({"line": line, "column": column, "message": message})


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# 文件列表 / 读取
# ---------------------------------------------------------------------------

def list_config_files() -> List[Dict[str, Any]]:
    out = []
    for file_id, meta in CONFIG_FILES.items():
        p: Path = meta["path"]
        try:
            stat = p.stat()
            size, mtime = stat.st_size, time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime))
        except OSError:
            size, mtime = 0, None
        out.append({
            "id": file_id,
            "label": meta["label"],
            "path": str(p.relative_to(ROOT)),
            "exists": p.exists(),
            "size": size,
            "mtime": mtime,
            "mode": meta["mode"],
            "rebuild_from": meta.get("rebuild_from"),
            "critical": bool(meta.get("critical")),
        })
    return out


def read_config_file(file_id: str) -> Dict[str, Any]:
    if file_id not in CONFIG_FILES:
        raise KeyError(file_id)
    meta = CONFIG_FILES[file_id]
    p: Path = meta["path"]
    base = {
        "id": file_id,
        "label": meta["label"],
        "path": str(p.relative_to(ROOT)),
        "mode": meta["mode"],
        "exists": p.exists(),
        "critical": bool(meta.get("critical")),
        "rebuild_from": meta.get("rebuild_from"),
    }
    if meta["mode"] == "form":
        if file_id == "app":
            base["values"] = read_llm_config()
        else:
            base["values"] = read_storage_config()
        return base

    content = _read_text(p) if p.exists() else ""
    parsed: Any = None
    if content.strip():
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            parsed = None
    base.update({
        "content": content,
        "sha1": _sha1(content),
        "size": len(content.encode("utf-8")),
        "parsed": parsed,
    })
    if file_id == "schema" and isinstance(parsed, dict):
        base["summary"] = schema_summary(parsed)
    return base


# ---------------------------------------------------------------------------
# 结构化表单：LLM / storage
# ---------------------------------------------------------------------------

def _load_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        example = path.with_name(path.name + ".example")
        if example.exists():
            path = example
        else:
            return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def read_llm_config() -> Dict[str, Any]:
    raw = _load_yaml_file(CONFIG_FILES["app"]["path"])
    api = raw.get("APIConfig") or {}
    if not isinstance(api, dict):
        api = {}
    key = str(api.get("API_KEY") or "")
    return {
        "api_base": api.get("API_BASE", ""),
        "model_name": api.get("MODEL_NAME", ""),
        "api_key_set": _mask(key)["set"],
        "api_key_hint": _mask(key)["hint"],
        "workers": api.get("WORKERS", 10),
        "timeout_secs": api.get("TIMEOUT_SECS", 120),
        "retries": api.get("RETRIES", 3),
        "rate_limit_qps": api.get("RATE_LIMIT_QPS", 0),
        "extra_throttle_sec": api.get("EXTRA_THROTTLE_SEC", 0.0),
        "include_files": list(raw.get("include_files") or []),
    }


def read_storage_config() -> Dict[str, Any]:
    raw = _load_yaml_file(CONFIG_FILES["storage"]["path"])
    neo = raw.get("Neo4jConfig") or {}
    chroma = raw.get("ChromaConfig") or {}
    if not isinstance(neo, dict):
        neo = {}
    if not isinstance(chroma, dict):
        chroma = {}
    pwd = str(neo.get("PASSWORD") or "")
    return {
        "neo4j_uri": neo.get("URI", "bolt://localhost:7687"),
        "neo4j_username": neo.get("USERNAME", "neo4j"),
        "neo4j_password_set": _mask(pwd)["set"],
        "neo4j_password_hint": _mask(pwd)["hint"],
        "neo4j_database": neo.get("DATABASE", "neo4j"),
        "ollama_host": chroma.get("OLLAMA_HOST", "http://localhost:11434"),
        "embedding_model": chroma.get("EMBEDDING_MODEL", "bge-m3"),
        "persist_directory": chroma.get("PERSIST_DIRECTORY", "./chroma_db"),
        "kg_file": raw.get("KG_FILE", "src/KGBuild/output/final_kg.json"),
    }


def _validate_llm_patch(patch: Dict[str, Any], existing: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    clean: Dict[str, Any] = {}

    def _str_field(name: str) -> Optional[str]:
        v = patch.get(name)
        if v is None:
            return None
        if not isinstance(v, str):
            errors.append(f"{name} 必须是字符串")
            return None
        return v.strip()

    api_base = _str_field("api_base")
    if api_base is not None:
        u = urlparse(api_base)
        if u.scheme not in ("http", "https") or not u.netloc:
            errors.append("api_base 必须以 http:// 或 https:// 开头")
        else:
            clean["API_BASE"] = api_base.rstrip("/")

    model_name = _str_field("model_name")
    if model_name is not None:
        if not model_name:
            errors.append("model_name 不能为空")
        else:
            clean["MODEL_NAME"] = model_name

    api_key = _str_field("api_key")
    if api_key is not None:
        if api_key and "*" not in api_key:
            clean["API_KEY"] = api_key

    for name, key, lo, hi in (
        ("workers", "WORKERS", 1, 256),
        ("timeout_secs", "TIMEOUT_SECS", 1, 3600),
        ("retries", "RETRIES", 0, 10),
    ):
        if name not in patch:
            continue
        n = _as_number(patch[name])
        if n is None or int(n) != n or not (lo <= int(n) <= hi):
            errors.append(f"{name} 必须是 {lo}~{hi} 的整数")
        else:
            clean[key] = int(n)

    for name, key in (("rate_limit_qps", "RATE_LIMIT_QPS"),
                      ("extra_throttle_sec", "EXTRA_THROTTLE_SEC")):
        if name not in patch:
            continue
        n = _as_number(patch[name])
        if n is None or n < 0:
            errors.append(f"{name} 必须是 ≥0 的数字")
        else:
            clean[key] = n

    return clean, errors


def write_llm_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    path = CONFIG_FILES["app"]["path"]
    raw = _load_yaml_file(path)
    api = raw.get("APIConfig") or {}
    if not isinstance(api, dict):
        api = {}

    clean, errors = _validate_llm_patch(patch or {}, api)
    if errors:
        raise ValueError("；".join(errors))

    api.update(clean)
    raw["APIConfig"] = api

    includes = list(raw.get("include_files") or [])
    for name in _DEFAULT_INCLUDES:
        if name not in includes:
            includes.append(name)
    raw["include_files"] = includes

    text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    with write_lock:
        backup = _backup(path, "app")
        _atomic_write(path, text)
    return {"ok": True, "backup": str(backup) if backup else None,
            "values": read_llm_config()}


def _validate_storage_patch(patch: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    clean: Dict[str, Any] = {}

    def _text(name: str) -> Optional[str]:
        v = patch.get(name)
        if v is None:
            return None
        if not isinstance(v, str):
            errors.append(f"{name} 必须是字符串")
            return None
        return v.strip()

    uri = _text("neo4j_uri")
    if uri is not None:
        u = urlparse(uri)
        if u.scheme not in ("bolt", "neo4j", "bolt+s", "neo4j+s") or not u.netloc:
            errors.append("neo4j_uri 必须是 bolt:// 或 neo4j:// 地址")
        else:
            clean["URI"] = uri
    username = _text("neo4j_username")
    if username is not None:
        if not username:
            errors.append("neo4j_username 不能为空")
        else:
            clean["USERNAME"] = username
    pwd = patch.get("neo4j_password")
    if isinstance(pwd, str) and pwd and "*" not in pwd:
        clean["PASSWORD"] = pwd
    elif pwd is not None and not isinstance(pwd, str):
        errors.append("neo4j_password 必须是字符串")
    database = _text("neo4j_database")
    if database is not None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", database):
            errors.append("neo4j_database 名称不合法")
        else:
            clean["DATABASE"] = database

    host = _text("ollama_host")
    if host is not None:
        u = urlparse(host)
        if u.scheme not in ("http", "https") or not u.netloc:
            errors.append("ollama_host 必须以 http:// 或 https:// 开头")
        else:
            clean["OLLAMA_HOST"] = host.rstrip("/")
    model = _text("embedding_model")
    if model is not None:
        if not model:
            errors.append("embedding_model 不能为空")
        else:
            clean["EMBEDDING_MODEL"] = model
    persist = _text("persist_directory")
    if persist is not None:
        if not persist:
            errors.append("persist_directory 不能为空")
        else:
            clean["PERSIST_DIRECTORY"] = persist

    kg_file = _text("kg_file")
    if kg_file is not None:
        if not kg_file:
            errors.append("kg_file 不能为空")
        else:
            clean["KG_FILE"] = kg_file

    return clean, errors


def write_storage_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    path = CONFIG_FILES["storage"]["path"]
    raw = _load_yaml_file(path)
    neo = raw.get("Neo4jConfig") or {}
    chroma = raw.get("ChromaConfig") or {}
    if not isinstance(neo, dict):
        neo = {}
    if not isinstance(chroma, dict):
        chroma = {}

    clean, errors = _validate_storage_patch(patch or {})
    if errors:
        raise ValueError("；".join(errors))

    for k in ("URI", "USERNAME", "PASSWORD", "DATABASE"):
        if k in clean:
            neo[k] = clean[k]
    for k in ("OLLAMA_HOST", "EMBEDDING_MODEL", "PERSIST_DIRECTORY"):
        if k in clean:
            chroma[k] = clean[k]
    raw["Neo4jConfig"] = neo
    raw["ChromaConfig"] = chroma
    if "KG_FILE" in clean:
        raw["KG_FILE"] = clean["KG_FILE"]
    else:
        raw.setdefault("KG_FILE", "src/KGBuild/output/final_kg.json")

    text = "# HierKG 知识图谱存储配置（由 Web 端保存）\n" + yaml.safe_dump(
        raw, allow_unicode=True, sort_keys=False, default_flow_style=False)
    with write_lock:
        backup = _backup(path, "storage")
        _atomic_write(path, text)
    return {"ok": True, "backup": str(backup) if backup else None,
            "values": read_storage_config()}


# ---------------------------------------------------------------------------
# YAML 文本校验
# ---------------------------------------------------------------------------

def validate_yaml_content(file_id: str, content: str) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    if file_id not in CONFIG_FILES:
        raise KeyError(file_id)
    meta = CONFIG_FILES[file_id]
    if meta["mode"] != "yaml":
        _err(errors, f"{file_id} 是表单配置，请使用专用接口保存")
        return {"ok": False, "errors": errors, "warnings": warnings}

    if not isinstance(content, str):
        _err(errors, "content 必须是字符串")
        return {"ok": False, "errors": errors, "warnings": warnings}
    if len(content.encode("utf-8")) > MAX_YAML_BYTES:
        _err(errors, f"文件超过 {MAX_YAML_BYTES // 1024}KB 上限")
        return {"ok": False, "errors": errors, "warnings": warnings}

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        errors.extend(_yaml_error(exc))
        return {"ok": False, "errors": errors, "warnings": warnings}

    if not isinstance(parsed, dict):
        _err(errors, "顶层必须是 mapping（键值对）")
        return {"ok": False, "errors": errors, "warnings": warnings}

    top = meta.get("top")
    if top:
        if top not in parsed:
            _err(errors, f"缺少顶层配置段 {top}")
        elif not isinstance(parsed.get(top), dict):
            _err(errors, f"{top} 必须是 mapping")
        else:
            if file_id == "schema":
                _validate_schema(parsed, errors, warnings)
            else:
                _validate_stage(file_id, parsed[top], errors, warnings)
    elif file_id == "schema":
        _validate_schema(parsed, errors, warnings)

    result: Dict[str, Any] = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }
    if file_id == "schema" and not errors:
        result["summary"] = schema_summary(parsed)
    return result


def _validate_stage(file_id: str, section: Dict[str, Any],
                    errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]]) -> None:
    for key in STAGE_IO_KEYS.get(file_id, []):
        value = section.get(key)
        if value is None:
            _err(errors, f"{key} 缺失")
            continue
        if not isinstance(value, str) or not value.strip():
            _err(errors, f"{key} 必须是非空字符串")
            continue
        if "/" in value or "\\" in value or ".." in value:
            _err(errors, f"{key} 只能是文件名，不能包含路径分隔符或 ..")

    def num(key: str, lo: float, hi: float, integer: bool = False) -> None:
        if key not in section:
            return
        n = _as_number(section[key])
        if n is None or (integer and int(n) != n) or not (lo <= n <= hi):
            kind = "整数" if integer else "数字"
            _err(errors, f"{key} 必须是 {lo}~{hi} 的{kind}")

    if file_id == "text":
        num("MAX_LEVEL", 1, 6, True)
        num("MAX_PARAGRAPH", 0, 1_000_000, True)
        if section.get("MAX_PARAGRAPH") == 0:
            _warn(warnings, "MAX_PARAGRAPH=0 可能表示不限制，请确认 TextSegmentation 的行为")
        docx = section.get("DOCX_NAME")
        if isinstance(docx, str) and docx and not (DOC_DIR / docx).exists():
            _warn(warnings, f"doc/ 下暂时没有 {docx}，构建前需要上传或改名")
    elif file_id == "extraction":
        num("TEMPERATURE", 0, 2)
        num("MAX_CHARS", 500, 100_000, True)
        num("MAX_NAME_LEN", 1, 200, True)
        num("MAX_DEFINITION_LEN", 10, 5000, True)
        for key in ("ENTITY_PROMPT", "RELATION_PROMPT"):
            if not isinstance(section.get(key), str) or not section.get(key, "").strip():
                _warn(warnings, f"{key} 缺失，将使用代码内默认提示词")
    elif file_id == "conv":
        num("TEMPERATURE", 0, 2)
        num("MAX_TOKENS", 1, 100_000, True)
        num("MAX_NEIGHBORS_PER_ENTITY", 1, 10_000, True)
        num("MAX_DEFINITION_LEN", 10, 5000, True)
        num("ENRICH_MAX_ENTITIES", 0, 1_000_000, True)
        num("ENRICH_MIN_COVERAGE", 0, 100, True)
        if section.get("ENRICH_MODE") not in (None, "missing", "all", "none"):
            _err(errors, "ENRICH_MODE 必须是 missing / all / none")
    elif file_id == "aggr":
        num("TEMPERATURE", 0, 2)
        num("MAX_TOKENS", 1, 100_000, True)
        num("LIMIT", 0, 1_000_000, True)
        num("WORKERS", 1, 256, True)
        num("PARENT_MIN_SCORE", 0, 100)
        num("INSTANCE_OF_CONFIDENCE", 0, 1)
        num("FALLBACK_RATE_WARN", 0, 1)
        num("FALLBACK_RATE_FAIL", 0, 1)
        warn_rate = _as_number(section.get("FALLBACK_RATE_WARN"))
        fail_rate = _as_number(section.get("FALLBACK_RATE_FAIL"))
        if warn_rate is not None and fail_rate is not None and warn_rate > fail_rate:
            _warn(warnings, "FALLBACK_RATE_WARN 高于 FALLBACK_RATE_FAIL，告警阈值应不高于失败阈值")
        if not isinstance(section.get("PROMPT_USER_TEMPLATE"), str):
            _warn(warnings, "PROMPT_USER_TEMPLATE 缺失，Aggr 将使用代码内默认提示词")
    elif file_id == "dedup":
        num("KNN_NEIGHBORS", 1, 10_000, True)
        num("SIM_THRESHOLD", 0, 1)
        num("AUTO_MERGE_SIM", 0, 1)
        num("NO_LLM_MERGE_SIM", 0, 1)
        num("NAME_JACCARD_MIN", 0, 1)
        num("PER_NODE_CAP", 0, 10_000, True)
        if section.get("NAME_MODE") not in (None, "token", "char", "bigram", "auto"):
            _warn(warnings, f"NAME_MODE={section.get('NAME_MODE')} 不是常见取值")
        lo = _as_number(section.get("SIM_THRESHOLD"))
        hi = _as_number(section.get("AUTO_MERGE_SIM"))
        if lo is not None and hi is not None and lo > hi:
            _warn(warnings, "SIM_THRESHOLD 高于 AUTO_MERGE_SIM，灰区将为空")
        if not isinstance(section.get("LLM_USER_PROMPT"), str):
            _warn(warnings, "LLM_USER_PROMPT 缺失，Dedup 的 LLM 复核将不可用")
    elif file_id == "pred":
        num("MIN_SIM", 0, 1)
        num("MIN_WEIGHT", 0, 100, True)
        num("MIN_SCORE", 0, 1)
        num("MAX_PREDICTIONS_PER_NODE", 0, 10_000, True)
        num("MAX_TOTAL", 1, 1_000_000, True)
        num("LLM_MAX_PAIRS", 0, 1_000_000, True)
        num("LLM_MIN_CONFIDENCE", 0, 1)
        if not isinstance(section.get("PROMPT_USER_TEMPLATE"), str):
            _warn(warnings, "PROMPT_USER_TEMPLATE 缺失，Pred 的 LLM 复核将不可用")
    elif file_id == "emb":
        host = section.get("OLLAMA_HOST")
        if host not in (None, "") and not (
                isinstance(host, str) and host.startswith(("http://", "https://"))):
            _err(errors, "OLLAMA_HOST 必须是 http(s) 地址，留空则复用 storage.yaml")
        model = section.get("EMBEDDING_MODEL")
        if model not in (None, "") and (not isinstance(model, str) or not model.strip()):
            _err(errors, "EMBEDDING_MODEL 必须是非空字符串，留空则复用 storage.yaml")
        num("BATCH_SIZE", 1, 10_000, True)
        num("TIMEOUT_SECS", 1, 3600, True)


def _validate_schema(parsed: Dict[str, Any], errors: List[Dict[str, Any]],
                     warnings: List[Dict[str, Any]]) -> None:
    node_types = parsed.get("NodeTypes")
    if not isinstance(node_types, dict) or not node_types:
        _err(errors, "NodeTypes 必须是非空 mapping")
        node_types = {}

    all_type_names: set[str] = set()
    core = node_types.get("core")
    if not isinstance(core, dict) or not core:
        _err(errors, "NodeTypes.core 必须是非空 mapping")
        core = {}
    all_type_names.update(str(k) for k in core)

    packs = node_types.get("domain_packs") or {}
    if not isinstance(packs, dict):
        _err(errors, "NodeTypes.domain_packs 必须是 mapping")
        packs = {}
    for pack_name, pack in packs.items():
        if not isinstance(pack, dict):
            _err(errors, f"NodeTypes.domain_packs.{pack_name} 必须是 mapping")
            continue
        types = pack.get("types")
        if not isinstance(types, dict) or not types:
            _err(errors, f"NodeTypes.domain_packs.{pack_name}.types 必须是非空 mapping")
            continue
        all_type_names.update(str(k) for k in types)

    def _check_types(items: Dict[str, Any], prefix: str) -> None:
        for name, spec in items.items():
            if not isinstance(spec, dict):
                _err(errors, f"{prefix}.{name} 必须是 mapping")
                continue
            if not spec.get("description"):
                _warn(warnings, f"{prefix}.{name} 缺少 description")
            parent = spec.get("parent")
            if parent and parent not in all_type_names:
                _warn(warnings, f"{prefix}.{name}.parent={parent} 不是已定义的节点类型")

    _check_types(core, "NodeTypes.core")
    for pack_name, pack in packs.items():
        if isinstance(pack, dict) and isinstance(pack.get("types"), dict):
            _check_types(pack["types"], f"NodeTypes.domain_packs.{pack_name}.types")

    relations = parsed.get("RelationTypes")
    if not isinstance(relations, dict):
        _err(errors, "RelationTypes 必须是 mapping")
    else:
        if "core" not in relations:
            _err(errors, "RelationTypes.core 缺失")
        for group, block in relations.items():
            if group == "derived_inverses":
                continue
            if not isinstance(block, dict):
                _err(errors, f"RelationTypes.{group} 必须是 mapping")
                continue
            for rname, rspec in block.items():
                if not isinstance(rspec, dict):
                    _err(errors, f"RelationTypes.{group}.{rname} 必须是 mapping")
                    continue
                if rspec.get("direction") not in ("directed", "symmetric"):
                    _warn(warnings, f"RelationTypes.{group}.{rname}.direction "
                                    "建议为 directed / symmetric")
                if rspec.get("endpoint_policy") not in (None, "same_layer",
                                                        "layer_constraint", "any"):
                    _warn(warnings, f"RelationTypes.{group}.{rname}.endpoint_policy 取值少见")
                layers = rspec.get("layers")
                if not isinstance(layers, list) or not layers:
                    _err(errors, f"RelationTypes.{group}.{rname}.layers 必须是非空列表")
                if rspec.get("store_inverse") and not rspec.get("inverse"):
                    _err(errors, f"RelationTypes.{group}.{rname} 声明 store_inverse 但没有 inverse")

    evidence = parsed.get("EvidenceSchema")
    if not isinstance(evidence, dict):
        _err(errors, "EvidenceSchema 必须是 mapping")
    else:
        for key in ("required", "optional"):
            if not isinstance(evidence.get(key), list):
                _err(errors, f"EvidenceSchema.{key} 必须是 list")
        if not isinstance(evidence.get("id_format"), str):
            _err(errors, "EvidenceSchema.id_format 缺失")

    policy = parsed.get("LayerPolicy")
    if not isinstance(policy, dict):
        _err(errors, "LayerPolicy 必须是 mapping")
    else:
        rules = policy.get("rules")
        if not isinstance(rules, list) or not rules:
            _err(errors, "LayerPolicy.rules 必须是非空 list")
        else:
            for i, rule in enumerate(rules, 1):
                if not isinstance(rule, dict):
                    _err(errors, f"LayerPolicy.rules[{i}] 必须是 mapping")
                    continue
                if not rule.get("id"):
                    _err(errors, f"LayerPolicy.rules[{i}].id 缺失")
                if not isinstance(rule.get("when"), dict):
                    _err(errors, f"LayerPolicy.rules[{i}].when 必须是 mapping")
                if rule.get("layer") not in ("L1", "L2", "llm", "excluded"):
                    _err(errors, f"LayerPolicy.rules[{i}].layer 必须是 L1/L2/llm/excluded")
                conf = _as_number(rule.get("confidence"))
                if conf is None or not (0 <= conf <= 1):
                    _err(errors, f"LayerPolicy.rules[{i}].confidence 必须是 0~1")
        if not isinstance(policy.get("signals"), dict) or not policy.get("signals"):
            _err(errors, "LayerPolicy.signals 必须是非空 mapping")


# ---------------------------------------------------------------------------
# schema 只读概览
# ---------------------------------------------------------------------------

def schema_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    node_types = parsed.get("NodeTypes") or {}
    relations = parsed.get("RelationTypes") or {}
    policy = parsed.get("LayerPolicy") or {}

    nodes: List[Dict[str, Any]] = []

    def _add_nodes(items: Any, pack: str) -> None:
        if not isinstance(items, dict):
            return
        for name, spec in items.items():
            spec = spec if isinstance(spec, dict) else {}
            nodes.append({
                "name": name,
                "pack": pack,
                "parent": spec.get("parent", ""),
                "description": spec.get("description", ""),
                "examples": spec.get("examples", []) or [],
            })

    if isinstance(node_types, dict):
        _add_nodes(node_types.get("core"), "core")
        packs = node_types.get("domain_packs") or {}
        if isinstance(packs, dict):
            for pack_name, pack in packs.items():
                if isinstance(pack, dict):
                    _add_nodes(pack.get("types"), str(pack_name))

    rels: List[Dict[str, Any]] = []
    if isinstance(relations, dict):
        for group, block in relations.items():
            if group == "derived_inverses" or not isinstance(block, dict):
                continue
            for name, spec in block.items():
                spec = spec if isinstance(spec, dict) else {}
                rels.append({
                    "name": name,
                    "group": group,
                    "direction": spec.get("direction", ""),
                    "description": spec.get("description", ""),
                    "layers": spec.get("layers", []),
                })

    rules = policy.get("rules") if isinstance(policy, dict) else []
    rules = rules if isinstance(rules, list) else []
    pack_counts: Dict[str, int] = {}
    for n in nodes:
        pack_counts[n["pack"]] = pack_counts.get(n["pack"], 0) + 1

    return {
        "ontology_name": parsed.get("ontology_name", ""),
        "schema_version": parsed.get("schema_version", ""),
        "node_types": nodes,
        "relation_types": rels,
        "layer_rules": [
            {"id": r.get("id", ""), "layer": r.get("layer", ""),
             "confidence": r.get("confidence", 0)}
            for r in rules if isinstance(r, dict)
        ],
        "counts": {
            "node_types": len(nodes),
            "core_types": pack_counts.get("core", 0),
            "domain_types": len(nodes) - pack_counts.get("core", 0),
            "relation_types": len(rels),
            "layer_rules": len(rules),
            "packs": pack_counts,
        },
    }


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def write_config_file(file_id: str, content: str) -> Dict[str, Any]:
    if file_id not in CONFIG_FILES:
        raise KeyError(file_id)
    meta = CONFIG_FILES[file_id]
    if meta["mode"] != "yaml":
        raise ValueError(f"{file_id} 是表单配置，请使用专用接口保存")

    check = validate_yaml_content(file_id, content)
    if not check["ok"]:
        return check  # 调用方按 400 处理

    # schema.v2.yaml 不参与 include 合并，其余 stage 写完再校验整条流水线配置。
    path = meta["path"]
    with write_lock:
        backup = _backup(path, file_id)
        _atomic_write(path, content)
        try:
            if file_id != "schema":
                _cross_validate_pipeline()
        except Exception as exc:  # noqa: BLE001
            if backup is not None and backup.exists():
                shutil.copy2(backup, path)
            else:
                path.unlink(missing_ok=True)
            check["ok"] = False
            check["errors"].append({"line": None, "column": None,
                                    "message": f"写入后整条配置校验失败，已回滚：{exc}"})
            return check

    return {
        "ok": True,
        "errors": [],
        "warnings": check.get("warnings", []),
        "backup": str(backup) if backup else None,
        "requires_rebuild": bool(meta.get("rebuild_from")),
        "rebuild_from": meta.get("rebuild_from"),
        "critical": bool(meta.get("critical")),
        **({"summary": check["summary"]} if "summary" in check else {}),
    }


def _cross_validate_pipeline() -> None:
    """用 pipeline 的合并逻辑验证 config.yaml + 所有 include 文件。"""
    from src.KGBuild import pipeline  # 只读 yaml，无重量级 import

    cfg = pipeline.load_merged_config()
    required = ["TextSegConfig", "ExtractionConfig", "ConvConfig", "AggrConfig",
                "DedupConfig", "PredConfig"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"合并后的配置缺少 {missing}")
    # 触发一次 step 组装，提前暴露文件名键缺失
    pipeline.build_steps(cfg)


# ---------------------------------------------------------------------------
# 就绪检查
# ---------------------------------------------------------------------------

def _tcp_check(host: str, port: int, timeout: float = 0.8) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可连接"
    except OSError as exc:
        return False, f"{host}:{port} 不可连接：{exc}"


def _check_config() -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    path = CONFIG_FILES["app"]["path"]
    if not path.exists():
        checks.append({
            "id": "llm_config", "label": "LLM 配置", "severity": "error",
            "detail": f"{path.relative_to(ROOT)} 不存在",
            "fix": "在「编辑配置 → 接口与存储」中填写 API 配置并保存",
        })
        return checks

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        checks.append({
            "id": "llm_config", "label": "LLM 配置", "severity": "error",
            "detail": f"YAML 解析失败：{exc}", "fix": "修正 config.yaml 语法",
        })
        return checks
    api = raw.get("APIConfig") or {}
    missing = [k for k in ("API_BASE", "API_KEY", "MODEL_NAME") if not api.get(k)]
    placeholder = [k for k in ("API_KEY",) if _is_placeholder(api.get(k))]
    if missing:
        checks.append({
            "id": "llm_config", "label": "LLM 配置", "severity": "error",
            "detail": f"APIConfig 缺少 {missing}", "fix": "补齐后保存",
        })
    elif placeholder:
        checks.append({
            "id": "llm_config", "label": "LLM 配置", "severity": "error",
            "detail": "API_KEY 仍是占位符", "fix": "填入真实 API Key",
        })
    else:
        checks.append({
            "id": "llm_config", "label": "LLM 配置", "severity": "ok",
            "detail": f"{api.get('MODEL_NAME')} @ {api.get('API_BASE')}", "fix": "",
        })

    includes = raw.get("include_files") or []
    missing_inc = [name for name in includes if not (CFG_DIR / name).exists()]
    if missing_inc:
        checks.append({
            "id": "includes", "label": "阶段配置", "severity": "error",
            "detail": f"include 文件缺失：{missing_inc}", "fix": "恢复缺失的 yaml",
        })
    elif "emb.yaml" not in includes:
        checks.append({
            "id": "includes", "label": "阶段配置", "severity": "warn",
            "detail": "include_files 未包含 emb.yaml，嵌入参数将使用默认值",
            "fix": "在「编辑配置」保存一次主配置以自动补齐",
        })
    else:
        checks.append({
            "id": "includes", "label": "阶段配置", "severity": "ok",
            "detail": f"{len(includes)} 个 include 文件齐全", "fix": "",
        })

    try:
        from src.KGBuild import pipeline
        cfg = pipeline.load_merged_config()
        pipeline.build_steps(cfg)
        checks.append({
            "id": "pipeline_config", "label": "流水线配置", "severity": "ok",
            "detail": "配置可合并、步骤可组装", "fix": "",
        })
    except Exception as exc:  # noqa: BLE001
        checks.append({
            "id": "pipeline_config", "label": "流水线配置", "severity": "error",
            "detail": str(exc), "fix": "检查各阶段 yaml 的文件名与顶层段",
        })
    return checks


def _check_storage() -> List[Dict[str, Any]]:
    path = CONFIG_FILES["storage"]["path"]
    if not path.exists():
        return [{
            "id": "storage_config", "label": "存储配置", "severity": "error",
            "detail": "storage.yaml 不存在",
            "fix": "在「编辑配置 → 接口与存储」中填写 Neo4j / Ollama 并保存",
        }]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [{
            "id": "storage_config", "label": "存储配置", "severity": "error",
            "detail": f"YAML 解析失败：{exc}", "fix": "修正 storage.yaml 语法",
        }]
    neo = raw.get("Neo4jConfig") or {}
    pwd = str(neo.get("PASSWORD") or "")
    if _is_placeholder(pwd):
        return [{
            "id": "storage_config", "label": "存储配置", "severity": "error",
            "detail": "Neo4j 密码为空或仍是占位符", "fix": "填入真实密码",
        }]
    return [{
        "id": "storage_config", "label": "存储配置", "severity": "ok",
        "detail": f"{neo.get('URI', '')} / {neo.get('DATABASE', 'neo4j')}", "fix": "",
    }]


def _deep_services() -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    st = read_storage_config()
    uri = str(st["neo4j_uri"])
    u = urlparse(uri)
    host, port = u.hostname or "localhost", u.port or 7687
    try:
        from neo4j import GraphDatabase
        # 读取真实凭据（read_storage_config 不回显明文）
        from src.KGRetrieve.db_backend import _load_config
        real = _load_config()["neo4j"]
        driver = GraphDatabase.driver(
            uri, auth=(real["username"], real["password"]), connection_timeout=3)
        try:
            with driver.session(database=real["database"]) as sess:
                sess.run("RETURN 1").consume()
        finally:
            driver.close()
        checks.append({"id": "neo4j", "label": "Neo4j", "severity": "ok",
                       "detail": f"{host}:{port} 可连接", "fix": ""})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "neo4j", "label": "Neo4j", "severity": "error",
                       "detail": f"{host}:{port} 连接失败：{exc}",
                       "fix": "启动 Neo4j 并检查 storage.yaml"})

    host = str(st["ollama_host"])
    u = urlparse(host)
    try:
        import httpx
        resp = httpx.post(f"{host.rstrip('/')}/api/embeddings",
                          json={"model": st["embedding_model"], "prompt": "ping"},
                          timeout=10.0)
        if resp.status_code != 200 or not resp.json().get("embedding"):
            raise RuntimeError(f"HTTP {resp.status_code}")
        checks.append({"id": "ollama", "label": "Ollama 嵌入", "severity": "ok",
                       "detail": f"{st['embedding_model']} @ {host}", "fix": ""})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "ollama", "label": "Ollama 嵌入", "severity": "error",
                       "detail": f"{host} / {st['embedding_model']} 不可用：{exc}",
                       "fix": f"启动 Ollama 并拉取 {st['embedding_model']}"})
    return checks


def readiness(deep: bool = False) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    checks.extend(_check_config())
    checks.extend(_check_storage())

    st = read_storage_config()
    neo_u = urlparse(str(st["neo4j_uri"]))
    tcp_ok, tcp_detail = _tcp_check(neo_u.hostname or "localhost", neo_u.port or 7687)
    checks.append({"id": "neo4j_tcp", "label": "Neo4j 端口",
                   "severity": "ok" if tcp_ok else "error",
                   "detail": tcp_detail, "fix": "" if tcp_ok else "启动 Neo4j"})
    ol_u = urlparse(str(st["ollama_host"]))
    ollama_ok, ollama_detail = _tcp_check(ol_u.hostname or "localhost", ol_u.port or 11434)
    checks.append({"id": "ollama_tcp", "label": "Ollama 端口",
                   "severity": "ok" if ollama_ok else "error",
                   "detail": ollama_detail, "fix": "" if ollama_ok else "启动 Ollama"})

    if deep:
        for c in _deep_services():
            checks = [x for x in checks if x["id"] != c["id"]]
            checks.append(c)

    final_kg = ROOT / str(st.get("kg_file") or "src/KGBuild/output/final_kg.json")
    checks.append({
        "id": "final_kg", "label": "构建产物", "severity": "warn" if not final_kg.exists() else "ok",
        "detail": str(final_kg.relative_to(ROOT)) if final_kg.exists() else "尚无 final_kg.json",
        "fix": "" if final_kg.exists() else "先完成一次构建",
    })

    def _ok(ids: List[str]) -> bool:
        return all(c["severity"] != "error" for c in checks if c["id"] in ids)

    embedding_ready = _ok(["llm_config", "includes", "pipeline_config", "ollama_tcp"])
    if deep:
        embedding_ready = embedding_ready and _ok(["ollama"])
    return {
        "build_ready": _ok(["llm_config", "includes", "pipeline_config"]),
        "embedding_ready": embedding_ready,
        "import_ready": _ok(["storage_config", "neo4j_tcp", "ollama_tcp"]) and final_kg.exists(),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# docx 来源
# ---------------------------------------------------------------------------

def list_docx() -> List[Dict[str, Any]]:
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(DOC_DIR.glob("*.docx")):
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "size": stat.st_size,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return out


def resolve_existing_docx(name: str) -> Path:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("docx 文件名不合法")
    path = (DOC_DIR / name).resolve()
    if path.parent != DOC_DIR.resolve():
        raise ValueError("docx 路径越界")
    if path.suffix.lower() != ".docx":
        raise ValueError("只支持 .docx 文件")
    if not path.is_file():
        raise FileNotFoundError(f"doc/ 下不存在 {name}")
    return path


def validate_upload(filename: str, size: int, head: bytes) -> None:
    if not filename or not filename.lower().endswith(".docx"):
        raise ValueError("只支持 .docx 文件")
    if size <= 0:
        raise ValueError("文件为空")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    if not head.startswith(b"PK\x03\x04"):
        raise ValueError("文件不是有效的 docx（ZIP 头不符）")


# ---------------------------------------------------------------------------
# 步骤选择
# ---------------------------------------------------------------------------

def step_names() -> List[str]:
    from src.KGBuild.pipeline import STEP_NAMES
    return list(STEP_NAMES)


def validate_selection(only: Optional[str] = None, start: Optional[str] = None,
                       until: Optional[str] = None, skip: Optional[str] = None) -> List[str]:
    from src.KGBuild.pipeline import resolve_selection
    return resolve_selection(step_names(), only=only, start=start,
                             until=until, skip=skip)
