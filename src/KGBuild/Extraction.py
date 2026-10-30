#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extraction v2 —— 小节正文 → 实体 / 语义关系（LLM，分块并发）。

输入 toc_structure.json（v2 对象），逐小节处理其 blocks：
  * 按 MAX_CHARS 分块，代码 / 公式块默认跳过
  * 实体类型必须落在 schema.v2 的闭集内；无法归类返回「待定」，不再无脑塞「概念」
  * 实体名必须能在原文中找到（名称或别名），否则丢弃（DROP_UNGROUNDED）
  * 定义优先用原文定义句；LLM 给的 definition 仅在含实体名时采用
  * 关系只抽同层语义关系（part_of / prerequisite / applies_to / example_of /
    synonym / contrasts_with / related）；described_by / appears_in / instance_of
    由后续阶段按证据与分层构造，不在这里抽
  * 每条实体/关系带 confidence 与 provenance

输出：toc_with_entities_and_relations.json（保留原结构，逐节加 entities / relations /
extraction 元数据）。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

try:
    from .schema_loader import Schema, get_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.schema_loader import Schema, get_schema

try:
    from tqdm import tqdm
except ImportError:  # 允许无 tqdm 环境跑 dry-run / 测试
    def tqdm(*args, **kwargs):  # type: ignore
        class _Null:
            def update(self, n=1): pass
            def close(self): pass
            def __iter__(self):
                return iter(args[0] if args else [])
        return _Null()

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
SCHEMA = get_schema()

logger = logging.getLogger("Extraction")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

EXTRACTION_DEFAULTS: Dict[str, Any] = {
    "IN_NAME": "toc_structure.json",
    "OUT_NAME": "toc_with_entities_and_relations.json",
    "TEMPERATURE": 0.3,
    "MAX_CHARS": 6000,
    "MAX_NAME_LEN": 40,
    "MAX_DEFINITION_LEN": 300,
    "DROP_UNGROUNDED": True,
    "SKIP_BLOCK_TYPES": ["code", "formula"],
    "SKIP_UNKNOWN_SECTIONS": True,
    "RELATION_CONFIDENCE": 0.5,
    "PROMPT_VERSION": "extraction.v2",
    "FAIL_FAST": False,
    "ENTITY_PROMPT": "",
    "RELATION_PROMPT": "",
}

API_DEFAULTS: Dict[str, Any] = {
    "API_KEY": "",
    "API_BASE": "",
    "MODEL_NAME": "",
    "TIMEOUT_SECS": 120,
    "RETRIES": 3,
    "RETRY_BACKOFF_BASE": 2,
    "RATE_LIMIT_QPS": 0,
    "EXTRA_THROTTLE_SEC": 0,
    "WORKERS": 4,
}

CROSS_LAYER_RELATIONS = {"described_by", "appears_in", "instance_of"}
DEFINITION_CUES = ("是指", "定义为", "称为", "是一种", "指的是", "所谓", "即")
STOP_NAMES = {"本文", "本章", "本节", "我们", "你们", "他们", "它们", "其中",
              "因此", "例如", "如图", "如表", "上述", "下面", "以上", "如下",
              "注意", "总结", "介绍"}
LIST_ITEM_RE = re.compile(r"^\s*(?:[（(]\s*\d+\s*[)）]|\d+\s*[、.)）]|[①-⑳]|[•·▪◦])")
TYPE_HINTS: Sequence[Tuple[str, str]] = (
    ("框架", "工具"), ("平台", "工具"), ("软件", "工具"), ("库", "工具"),
    ("工具", "工具"), ("模型", "人工物"), ("系统", "人工物"), ("产品", "人工物"),
    ("文档", "人工物"), ("公司", "组织"), ("机构", "组织"), ("团队", "组织"),
    ("组织", "组织"), ("研究者", "人物"), ("学者", "人物"), ("人物", "人物"),
    ("度量", "指标"), ("指标", "指标"), ("评估", "指标"),
    ("配置", "属性"), ("参数", "属性"), ("属性", "属性"),
    ("算法", "算法"), ("技术", "方法"), ("方法", "方法"), ("流程", "方法"),
    ("场景", "任务"), ("任务", "任务"), ("应用", "任务"),
    ("事件", "事件"), ("会议", "事件"),
    ("理论", "概念"), ("术语", "概念"), ("概念", "概念"),
)


# ---------------------------------------------------------------------------
# 配置 / LLM
# ---------------------------------------------------------------------------

def _load_yaml(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _merge_includes(base: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for rel in base.get("include_files", []) or []:
        p = Path(str(rel))
        if not p.is_absolute():
            p = (CONFIG_DIR / p).resolve()
        if not p.exists() and str(rel).startswith("config/"):
            p = (CONFIG_DIR / str(rel).split("/", 1)[1]).resolve()
        if not p.exists():
            raise FileNotFoundError(f"找不到包含文件：{p}")
        merged.update(_load_yaml(p))
    return merged


def _load_config() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        merged = _merge_includes(_load_yaml(CONFIG_FILE))
    if "ExtractionConfig" not in merged:
        fallback = CONFIG_DIR / "extraction.yaml"
        if not fallback.exists():
            raise FileNotFoundError(f"找不到抽取配置：{CONFIG_FILE} / {fallback}")
        merged.update(_load_yaml(fallback))
    cfg = dict(EXTRACTION_DEFAULTS)
    cfg.update(merged.get("ExtractionConfig") or {})
    api = dict(API_DEFAULTS)
    api.update(merged.get("APIConfig") or {})
    return cfg, api


CFG, API = _load_config()

_rate_lock = threading.Lock()
_last_ts = 0.0
_client_obj = None


def _client():
    global _client_obj
    if _client_obj is None:
        from openai import OpenAI
        if not API.get("API_KEY"):
            raise RuntimeError("缺少 APIConfig.API_KEY，无法调用 LLM（可用 --dry-run 只验证流程）")
        _client_obj = OpenAI(api_key=API["API_KEY"], base_url=API.get("API_BASE") or None)
    return _client_obj


def _rate_limit() -> None:
    qps = float(API.get("RATE_LIMIT_QPS") or 0)
    if qps <= 0:
        return
    interval = 1.0 / qps
    global _last_ts
    with _rate_lock:
        delta = time.time() - _last_ts
        if delta < interval:
            time.sleep(interval - delta)
        _last_ts = time.time()


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _safe_json(content: str) -> Any:
    try:
        return json.loads(_strip_code_fences(content))
    except Exception:
        return {}


def _chat_once(prompt: str) -> str:
    _rate_limit()
    if float(API.get("EXTRA_THROTTLE_SEC") or 0) > 0:
        time.sleep(float(API["EXTRA_THROTTLE_SEC"]))
    resp = _client().chat.completions.create(
        model=API["MODEL_NAME"],
        messages=[{"role": "user", "content": prompt}],
        temperature=float(CFG["TEMPERATURE"]),
        timeout=float(API["TIMEOUT_SECS"]),
    )
    text = (resp.choices[0].message.content or "").strip()
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def chat_with_retry(prompt: str) -> str:
    last_err: Optional[Exception] = None
    retries = int(API["RETRIES"])
    for k in range(max(1, retries)):
        try:
            return _chat_once(prompt)
        except Exception as e:  # noqa: BLE001 - 需要兜住所有 API 异常
            last_err = e
            time.sleep(float(API["RETRY_BACKOFF_BASE"]) ** k)
    raise RuntimeError(f"LLM 请求失败（重试 {retries} 次）：{last_err}")


# ---------------------------------------------------------------------------
# Prompt 渲染（从 schema v2）
# ---------------------------------------------------------------------------

def render_entity_types(schema: Schema) -> str:
    lines: List[str] = []
    for nt in schema.node_types.values():
        if not nt.resolved:
            lines.append(f"- {nt.name}：{nt.description}（无法归类时选它，会进入待清洗池）")
            continue
        parent = f"（属于「{nt.parent}」）" if nt.parent else ""
        ex = "，如：" + "、".join(nt.examples[:4]) if nt.examples else ""
        lines.append(f"- {nt.name}{parent}：{nt.description}{ex}")
    return "\n".join(lines)


def render_relation_types(schema: Schema) -> str:
    lines: List[str] = []
    for rt in schema.relation_types.values():
        if rt.name in CROSS_LAYER_RELATIONS:
            continue
        direction = "对称" if rt.direction == "symmetric" else "有向"
        lines.append(f"- {rt.name}（{direction}）：{rt.description}")
    return "\n".join(lines)


def render_prompts(schema: Schema) -> Tuple[str, str]:
    ent_tpl = Template(CFG["ENTITY_PROMPT"] or "")
    rel_tpl = Template(CFG["RELATION_PROMPT"] or "")
    ent = ent_tpl.safe_substitute(
        entity_types_with_desc=render_entity_types(schema),
        relation_types_with_desc="",
        section_text="${section_text}",
        entity_list="${entity_list}",
    )
    rel = rel_tpl.safe_substitute(
        entity_types_with_desc="",
        relation_types_with_desc=render_relation_types(schema),
        section_text="${section_text}",
        entity_list="${entity_list}",
    )
    return ent, rel


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

def _name_key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _norm_for_match(s: str) -> str:
    return re.sub(r"[\s　]+", "", (s or "")).lower()


def _valid_name(name: str) -> bool:
    name = (name or "").strip()
    if not name or len(name) > int(CFG["MAX_NAME_LEN"]):
        return False
    if name in STOP_NAMES or re.fullmatch(r"[\d\W_]+", name):
        return False
    if "\n" in name or name.endswith(("。", "；", "，", ",")):
        return False
    if re.search(r'(?:"""|\'\'\'|=|;|\{|\}|\[|\]|<>|^\s*#)', name):
        return False
    if LIST_ITEM_RE.match(name):
        return False
    return True


def normalize_entity_type(raw: str, schema: Schema) -> str:
    t = re.sub(r"\s+", "", (raw or "").strip().strip("：:"))
    if not t:
        return "待定"
    if schema.is_valid_node_type(t):
        return t
    for name in schema.node_types:
        if _name_key(name) == _name_key(t):
            return name
    for kw, target in TYPE_HINTS:
        if kw in t:
            return target
    return "待定"


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[。；;！？!?])", text or "") if s.strip()]


def find_definition(text: str, name: str) -> str:
    """原文中优先返回含定义性措辞的句子，其次返回第一句提及。"""
    max_len = int(CFG["MAX_DEFINITION_LEN"])
    sentences = _split_sentences(text)
    for cue in DEFINITION_CUES:
        for s in sentences:
            if name in s and cue in s:
                return s[:max_len]
    for s in sentences:
        if name in s:
            return s[:max_len]
    return ""


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------

def _split_long_block(block: Dict[str, Any], max_chars: int) -> List[Dict[str, Any]]:
    pieces: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_start = block["char_start"]
    buf_len = 0
    cursor = block["char_start"]
    for line in block["text"].split("\n"):
        if buf and buf_len + len(line) + 1 > max_chars:
            txt = "\n".join(buf)
            pieces.append({**block, "text": txt, "char_start": buf_start,
                           "char_end": buf_start + len(txt)})
            buf_start = cursor
            buf, buf_len = [], 0
        buf.append(line)
        buf_len += len(line) + (1 if len(buf) > 1 else 0)
        cursor += len(line) + 1
    if buf:
        txt = "\n".join(buf)
        pieces.append({**block, "text": txt, "char_start": buf_start,
                       "char_end": buf_start + len(txt)})
    return pieces


def build_chunks(section: Dict[str, Any], max_chars: int,
                 skip_types: Iterable[str]) -> List[Dict[str, Any]]:
    skip = set(skip_types)
    blocks: List[Dict[str, Any]] = []
    for b in section.get("blocks") or []:
        if b.get("type") in skip or not (b.get("text") or "").strip():
            continue
        if len(b["text"]) > max_chars:
            blocks.extend(_split_long_block(b, max_chars))
        else:
            blocks.append(b)
    if not blocks:
        return []

    chunks: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []
    cur_len = 0
    for b in blocks:
        extra = len(b["text"]) + (1 if cur else 0)
        if cur and cur_len + extra > max_chars:
            chunks.append(_make_chunk(cur))
            cur, cur_len = [], 0
            extra = len(b["text"])
        cur.append(b)
        cur_len += extra
    if cur:
        chunks.append(_make_chunk(cur))
    return chunks


def _make_chunk(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "text": "\n".join(b["text"] for b in blocks),
        "blocks": blocks,
        "block_id": blocks[0]["block_id"],
        "char_start": blocks[0]["char_start"],
        "char_end": blocks[-1]["char_end"],
    }


# ---------------------------------------------------------------------------
# 解析 LLM 结果
# ---------------------------------------------------------------------------

def _prov(doc_id: str, section: Dict[str, Any], chunk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "doc_id": doc_id,
        "section_id": section.get("section_id") or section.get("id") or "",
        "block_id": (chunk or {}).get("block_id", ""),
        "extractor": "llm",
        "model": API.get("MODEL_NAME", ""),
        "prompt_version": CFG["PROMPT_VERSION"],
    }


def parse_entities(data: Any, chunk_text: str, section: Dict[str, Any],
                   doc_id: str, chunk: Dict[str, Any],
                   schema: Schema) -> Tuple[List[Dict[str, Any]], int, int]:
    raw_list = data.get("entities") if isinstance(data, dict) else data
    out: List[Dict[str, Any]] = []
    dropped_ungrounded = 0
    unknown_types = 0
    text_norm = _norm_for_match(chunk_text)

    for raw in raw_list if isinstance(raw_list, list) else []:
        if not isinstance(raw, dict):
            continue
        name = (raw.get("name") or "").strip()
        if not _valid_name(name):
            continue
        aliases = raw.get("aliases") or raw.get("alias") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [a.strip() for a in aliases
                   if isinstance(a, str) and a.strip() and _name_key(a) != _name_key(name)]

        name_hit = _name_key(name) in text_norm
        alias_hit = any(_name_key(a) in text_norm for a in aliases)
        if not name_hit and not alias_hit:
            dropped_ungrounded += 1
            if CFG["DROP_UNGROUNDED"]:
                continue

        etype = normalize_entity_type(str(raw.get("type") or ""), schema)
        if etype == "待定" and str(raw.get("type") or "").strip():
            unknown_types += 1

        llm_def = (raw.get("definition") or raw.get("raw_content") or "").strip()
        if llm_def and name not in llm_def:
            llm_def = ""
        definition = llm_def[: int(CFG["MAX_DEFINITION_LEN"])] or find_definition(chunk_text, name)

        confidence = 1.0 if name_hit else 0.9 if alias_hit else 0.3
        out.append({
            "name": name,
            "aliases": aliases,
            "type": etype,
            "definition": definition,
            "confidence": round(confidence, 2),
            "provenance": [_prov(doc_id, section, chunk)],
        })
    return out, dropped_ungrounded, unknown_types


def _resolve_name(name: str, entities: List[Dict[str, Any]]) -> Optional[str]:
    key = _name_key(name)
    for e in entities:
        if _name_key(e["name"]) == key:
            return e["name"]
        if any(_name_key(a) == key for a in e["aliases"]):
            return e["name"]
    return None


def parse_relations(data: Any, entities: List[Dict[str, Any]], section: Dict[str, Any],
                    doc_id: str, chunk: Dict[str, Any],
                    schema: Schema) -> Tuple[List[Dict[str, Any]], int]:
    raw_list = data.get("relations") if isinstance(data, dict) else data
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    dropped = 0

    for raw in raw_list if isinstance(raw_list, list) else []:
        if not isinstance(raw, dict):
            continue
        src_raw = (raw.get("source") or "").strip()
        tgt_raw = (raw.get("target") or "").strip()
        rel_raw = (raw.get("type") or "").strip()
        if not src_raw or not tgt_raw or not rel_raw:
            dropped += 1
            continue
        rel = schema.canonical_relation(rel_raw)
        if rel is None or rel in CROSS_LAYER_RELATIONS:
            dropped += 1
            continue
        src = _resolve_name(src_raw, entities)
        tgt = _resolve_name(tgt_raw, entities)
        if not src or not tgt or src == tgt:
            dropped += 1
            continue
        if schema.is_symmetric(rel) and src > tgt:
            src, tgt = tgt, src

        keywords = raw.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
        key = (src, tgt, rel)
        rec = {
            "source": src,
            "target": tgt,
            "type": rel,
            "description": str(raw.get("description") or "").strip(),
            "keywords": keywords,
            "confidence": float(CFG["RELATION_CONFIDENCE"]),
            "provenance": [_prov(doc_id, section, chunk)],
        }
        if key in out:
            old = out[key]
            old["keywords"] = list(dict.fromkeys(old["keywords"] + keywords))
            if not old["description"] and rec["description"]:
                old["description"] = rec["description"]
        else:
            out[key] = rec
    return list(out.values()), dropped


def merge_entities(entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for e in entities:
        key = _name_key(e["name"])
        if key not in merged:
            merged[key] = {**e, "aliases": list(e["aliases"]),
                           "provenance": list(e["provenance"])}
            continue
        cur = merged[key]
        cur["aliases"] = list(dict.fromkeys(cur["aliases"] + e["aliases"]))
        cur["provenance"].extend(e["provenance"])
        cur["confidence"] = max(cur["confidence"], e["confidence"])
        if len(e["definition"]) > len(cur["definition"]):
            cur["definition"] = e["definition"]
        if cur["type"] == "待定" and e["type"] != "待定":
            cur["type"] = e["type"]
    return list(merged.values())


def merge_relations(relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in relations:
        key = (r["source"], r["target"], r["type"])
        if key not in merged:
            merged[key] = {**r, "keywords": list(r["keywords"]),
                           "provenance": list(r["provenance"])}
        else:
            cur = merged[key]
            cur["keywords"] = list(dict.fromkeys(cur["keywords"] + r["keywords"]))
            cur["provenance"].extend(r["provenance"])
    return list(merged.values())


def canonicalize_relations(entities: List[Dict[str, Any]],
                           relations: List[Dict[str, Any]],
                           schema: Schema) -> List[Dict[str, Any]]:
    """跨块合并后，用全局别名表把关系端点折算到规范实体名，并再次去重。"""
    alias_map: Dict[str, str] = {}
    for e in entities:
        alias_map[_name_key(e["name"])] = e["name"]
        for a in e["aliases"]:
            alias_map.setdefault(_name_key(a), e["name"])
    names = {e["name"] for e in entities}

    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in relations:
        src = alias_map.get(_name_key(r["source"]), r["source"])
        tgt = alias_map.get(_name_key(r["target"]), r["target"])
        if src not in names or tgt not in names or src == tgt:
            continue
        rel = r["type"]
        if schema.is_symmetric(rel) and src > tgt:
            src, tgt = tgt, src
        key = (src, tgt, rel)
        if key in out:
            cur = out[key]
            cur["keywords"] = list(dict.fromkeys(cur["keywords"] + r["keywords"]))
            cur["provenance"].extend(r["provenance"])
        else:
            out[key] = {**r, "source": src, "target": tgt,
                        "keywords": list(r["keywords"]),
                        "provenance": list(r["provenance"])}
    return list(out.values())


# ---------------------------------------------------------------------------
# 单节抽取
# ---------------------------------------------------------------------------

def process_section(section: Dict[str, Any], doc_id: str, schema: Schema,
                    ent_prompt: str, rel_prompt: str) -> Dict[str, Any]:
    chunks = build_chunks(section, int(CFG["MAX_CHARS"]), CFG["SKIP_BLOCK_TYPES"])
    stats = {"chunks": len(chunks), "entities": 0, "relations": 0,
             "dropped_ungrounded": 0, "unknown_types": 0, "dropped_relations": 0,
             "failed_chunks": 0}
    if not chunks:
        section["entities"] = []
        section["relations"] = []
        section["extraction"] = stats
        return stats

    all_entities: List[Dict[str, Any]] = []
    all_relations: List[Dict[str, Any]] = []

    for chunk in chunks:
        try:
            ent_data = _safe_json(chat_with_retry(
                Template(ent_prompt).safe_substitute(section_text=chunk["text"])))
            ents, dropped_ground, unknown = parse_entities(
                ent_data, chunk["text"], section, doc_id, chunk, schema)
            stats["dropped_ungrounded"] += dropped_ground
            stats["unknown_types"] += unknown
            if not ents:
                continue
            names = "、".join(e["name"] for e in ents)
            rel_data = _safe_json(chat_with_retry(
                Template(rel_prompt).safe_substitute(
                    section_text=chunk["text"], entity_list=names)))
            rels, dropped_rel = parse_relations(
                rel_data, ents, section, doc_id, chunk, schema)
            stats["dropped_relations"] += dropped_rel
            all_entities.extend(ents)
            all_relations.extend(rels)
        except Exception as exc:  # noqa: BLE001
            stats["failed_chunks"] += 1
            logger.warning(f"[抽取失败] {section.get('section_id')} "
                           f"block={chunk['block_id']}: {exc}")
            if CFG["FAIL_FAST"]:
                raise

    entities = merge_entities(all_entities)
    relations = merge_relations(all_relations)
    relations = canonicalize_relations(entities, relations, schema)
    section["entities"] = entities
    section["relations"] = relations
    stats["entities"] = len(entities)
    stats["relations"] = len(relations)
    section["extraction"] = stats
    return stats


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def walk_sections(nodes: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for n in nodes or []:
        yield n
        yield from walk_sections(n.get("children") or [])


def should_extract(section: Dict[str, Any]) -> bool:
    if not (section.get("content") or "").strip():
        return False
    if not section.get("blocks"):
        return False
    if CFG["SKIP_UNKNOWN_SECTIONS"] and section.get("section_level") == "unknown":
        return False
    skip = set(CFG["SKIP_BLOCK_TYPES"])
    return any((b.get("text") or "").strip() and b.get("type") not in skip
               for b in section["blocks"])


def load_toc(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        raise ValueError("输入是 v1 的裸数组；请先用 v2 TextSegmentation 重新生成 toc_structure.json")
    if "sections" not in data:
        raise ValueError("toc_structure.json 缺少 sections 字段")
    return data


def run(workers: int, limit: Optional[int] = None,
        only_section: Optional[str] = None, out_path: Optional[Path] = None,
        dry_run: bool = False, in_path: Optional[Path] = None) -> int:
    in_json = in_path or (SCRIPT_DIR / "output" / CFG["IN_NAME"])
    out_json = out_path or (SCRIPT_DIR / "output" / CFG["OUT_NAME"])
    if not in_json.exists():
        raise FileNotFoundError(f"未找到输入：{in_json}")

    toc_doc = load_toc(in_json)
    doc_id = toc_doc.get("doc_id") or "doc"
    targets = [s for s in walk_sections(toc_doc["sections"]) if should_extract(s)]
    if only_section:
        targets = [s for s in targets
                   if (s.get("section_id") or s.get("id")) == only_section]
    if limit:
        targets = targets[:limit]

    ent_prompt, rel_prompt = render_prompts(SCHEMA)
    total_chunks = sum(len(build_chunks(s, int(CFG["MAX_CHARS"]), CFG["SKIP_BLOCK_TYPES"]))
                       for s in targets)
    logger.info(f"待抽取小节 {len(targets)} 个，分块 {total_chunks} 个，"
                f"模型 {API.get('MODEL_NAME') or '(未配置)'}")

    if dry_run:
        for s in targets[:3]:
            chunks = build_chunks(s, int(CFG["MAX_CHARS"]), CFG["SKIP_BLOCK_TYPES"])
            logger.info(f"  dry-run {s.get('section_id')} {s.get('title')}: "
                        f"{len(chunks)} 块 / {sum(len(c['text']) for c in chunks)} 字")
        logger.info("dry-run 完成，未调用 LLM。")
        return 0

    if not API.get("API_KEY"):
        raise RuntimeError("缺少 APIConfig.API_KEY，无法调用 LLM（可用 --dry-run 只验证流程）")

    pbar = tqdm(total=len(targets), desc="实体与关系抽取", ncols=90)
    total_stats = CounterLike()

    def worker(sec: Dict[str, Any]) -> Dict[str, Any]:
        st = process_section(sec, doc_id, SCHEMA, ent_prompt, rel_prompt)
        pbar.update(1)
        return st

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(worker, s) for s in targets]
        for fu in as_completed(futures):
            st = fu.result()
            for k, v in st.items():
                total_stats.add(k, v)
    pbar.close()

    toc_doc["extraction_meta"] = {
        "schema_version": SCHEMA.version,
        "model": API.get("MODEL_NAME", ""),
        "prompt_version": CFG["PROMPT_VERSION"],
        "temperature": CFG["TEMPERATURE"],
        "sections": len(targets),
        "counts": total_stats.as_dict(),
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(toc_doc, f, ensure_ascii=False, indent=2)

    logger.info(f"✅ 抽取完成：{out_json.resolve()}")
    logger.info(f"  实体 {total_stats.get('entities')} / 关系 {total_stats.get('relations')} / "
                f"未落地实体 {total_stats.get('dropped_ungrounded')} / "
                f"失败块 {total_stats.get('failed_chunks')}")
    return 0


class CounterLike:
    def __init__(self) -> None:
        self._d: Dict[str, int] = {}

    def add(self, k: str, v: Any) -> None:
        if isinstance(v, int):
            self._d[k] = self._d.get(k, 0) + v

    def get(self, k: str) -> int:
        return self._d.get(k, 0)

    def as_dict(self) -> Dict[str, int]:
        return dict(sorted(self._d.items()))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v2 实体与关系抽取（分块并发）")
    ap.add_argument("--workers", type=int, default=int(API.get("WORKERS") or 4))
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个小节（调试）")
    ap.add_argument("--section-id", default=None, help="只处理指定 section_id")
    ap.add_argument("--in", dest="in_path", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只渲染 prompt / 统计分块，不调 LLM")
    ap.add_argument("--loglevel", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logger.setLevel(getattr(logging, args.loglevel))
    return run(args.workers, args.limit, args.section_id,
               Path(args.out) if args.out else None, args.dry_run,
               Path(args.in_path) if args.in_path else None)


if __name__ == "__main__":
    sys.exit(main())
