from __future__ import annotations
import json
import time
import hashlib
import logging
import requests
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import yaml
from tqdm import tqdm
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========================= 配置加载（YAML）
def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# KGBuild 目录
SCRIPT_DIR = Path(__file__).resolve().parent
CFG_DIR = SCRIPT_DIR / "config"

# 主配置：src/KGBuild/config/config.yaml
MAIN_CFG_PATH = CFG_DIR / "config.yaml"
config = load_yaml(MAIN_CFG_PATH)

# 递归加载 include_files（相对 KGBuild/config/ 拼接；绝对路径原样用）
includes = config.get("include_files", []) or []
for inc in includes:
    inc_path = Path(inc)
    if not inc_path.is_absolute():
        inc_path = (CFG_DIR / inc).resolve()
    if inc_path.exists():
        cfg_add = load_yaml(inc_path)
        if cfg_add:
            config.update(cfg_add)

# 取出 API 与 Aggr 配置（字典）
APIConfig = config.get("APIConfig", {})
AggrCfg   = config.get("AggrConfig", {})
# 关键修复①：PROMPTS 同时兼容顶层和 AggrConfig 下两种放法
PROMPTS   = (config.get("PROMPTS") or AggrCfg.get("PROMPTS") or {})

# ========================= 路径拼接
OUTPUT_DIR = SCRIPT_DIR / "output"
LOGS_DIR   = SCRIPT_DIR / "logs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 名字到实际路径
CONV_IN_PATH  = OUTPUT_DIR / AggrCfg.get("CONV_IN_NAME", "conv_entities.json")
AGGR_OUT_PATH = OUTPUT_DIR / AggrCfg.get("OUT_NAME", "aggr_entities.json")
LOG_PATH      = LOGS_DIR   / AggrCfg.get("LOG_NAME", "aggr.log")

# ========================= 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ],
)
logger = logging.getLogger("Aggr")

# ========================= 运行参数
TEMPERATURE = float(AggrCfg.get("TEMPERATURE", 0.0))
MAX_TOKENS  = int(AggrCfg.get("MAX_TOKENS", 1000))
API_TIMEOUT = int(APIConfig.get("TIMEOUT_SECS", 120))
RETRIES     = int(APIConfig.get("RETRIES", 3))
CHAT_COMPLETIONS_PATH = APIConfig.get("CHAT_COMPLETIONS_PATH", "/chat/completions")
DRY_RUN     = bool(int(AggrCfg.get("DRY_RUN", 0)))

LIMIT = int(AggrCfg.get("LIMIT", 0))
TREE_ENFORCE = bool(int(AggrCfg.get("TREE_ENFORCE", 1)))
PROGRESS_NCOLS = int(AggrCfg.get("PROGRESS_NCOLS", 100))
WORKERS = int(APIConfig.get("WORKERS", os.cpu_count() or 6))

API_BASE = APIConfig.get("API_BASE", "")
API_KEY  = APIConfig.get("API_KEY", "")
MODEL    = APIConfig.get("MODEL_NAME", "")

# ========================= L0 本体加载（schema.yaml）
SCHEMA_PATH = SCRIPT_DIR / "config" / "schema.yaml"  # src/KGBuild/config/schema.yaml
_schema = load_yaml(SCHEMA_PATH) if SCHEMA_PATH.exists() else {}
ENTITY_TYPE_MAP: Dict[str, str] = {}  # type_name -> layer_hint
for _tname, _tdef in (_schema.get("EntityTypes") or {}).items():
    ENTITY_TYPE_MAP[_tname] = (_tdef or {}).get("layer_hint", "auto")
LAYER_RULES = AggrCfg.get("LAYER_RULES", {})
CONCEPT_MIN_OCC = int(LAYER_RULES.get("concept_min_occurrences", 2))

logger.info(f"已加载 schema.yaml：{len(ENTITY_TYPE_MAP)} 种实体类型，layer_hint 映射={ENTITY_TYPE_MAP}")

   
   # ---- 类型归一化规则（从 schema.yaml 的 TypeMapping 加载）----
_TYPE_MAPPING_RULES: List[Tuple[List[str], str]] = []
for _rule in (_schema.get("TypeMapping") or []):
    _kw = _rule.get("keywords", [])
    _st = _rule.get("schema_type", "")
    if _kw and _st:
        _TYPE_MAPPING_RULES.append((_kw, _st))

def _normalize_type(raw_type: str) -> str:
    """将非 schema 类型归一化到 schema 类型（safety net）。
    Extraction.py 已做入口归一化，这里是对漏网之鱼的兜底。
    """
    t = (raw_type or "").strip()
    if t in ENTITY_TYPE_MAP:
        return t
    for keywords, schema_type in _TYPE_MAPPING_RULES:
        if any(kw in t for kw in keywords):
            return schema_type
    return "概念"  # 兜底

# ========================= 数据结构
@dataclass
class Occurrence:
    path: str
    node_id: str
    level: int
    title: str

@dataclass
class Neighbor:
    name: str
    snippet: str = ""  # 如 "cooccur|undirected|w=..." 或 "rel|directed|type=part-of|src=core|dst=non-core"
    type: str = ""     # 标准化关系名：cooccur / prerequisite / part-of / applies-to / example-of / synonym / contrasts-with / related

@dataclass
class EntityItem:
    name: str
    alias: List[str]
    type: str
    original: str
    occurrences: List[Occurrence]
    neighbors: List[Neighbor]
    layer: str = ""  # "concept" / "entity"（新四层模型）
    concept_id: str = ""  # concept 自身为自身 id；entity 指向所属 concept
    role: str = ""  # 旧字段，保留兼容（core=concept, non-core=entity）
    updated_description: str = ""  # 预留

# ========================= 工具：模板/解析/HTTP
def _prompts_section() -> dict:
    return PROMPTS or {}

def build_system_prompt_role() -> str:
    return (_prompts_section().get("system") or "").strip()

def build_system_prompt_relation() -> str:
    return (_prompts_section().get("relation_system") or "").strip()

def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return (s[:n] + "…") if len(s) > n else s

def _nb_list(ent: EntityItem, k=5) -> str:
    xs = []
    for nb in ent.neighbors[:k]:
        xs.append(f"- {nb.name}｜{_truncate(nb.snippet, 60)}")
    return "\n".join(xs) if xs else "(无)"

def build_user_prompt_for_layer(entity: EntityItem, section_summary: str) -> str:
    neighbors = entity.neighbors[:6]
    occs = entity.occurrences[:2]
    desc = _truncate(entity.original, 300)
    lines = []
    for nb in neighbors:
        sn = _truncate((nb.snippet or "").replace("\n", " "), 80)
        head = sn.split("|", 1)[0] if "|" in sn else "neighbor"
        lines.append(f"- {nb.name}（{head}）")
    occ_str = "; ".join(o.path for o in occs) or "(无)"
    neigh_str = "\n".join(lines) if lines else "(无)"

    return (
        "第一行只输出最终标签（concept 或 entity）。\n\n"
        f"目标实体：{entity.name}\n"
        f"实体类型：{entity.type}\n"
        f"实体描述：{desc}\n"
        f"同现目录（最多2条）：{occ_str}\n"
        "邻域实体（最多6个）：\n" + neigh_str + "\n"
        f"章节摘要（可选）：{_truncate(section_summary, 200)}\n"
    )

# —— 关系标签集合 —— #
REL_TYPES = {"prerequisite","part-of","applies-to","example-of","synonym","contrasts-with","related"}
REL_PATTERN = re.compile(r"\b(prerequisite|part-of|applies-to|example-of|synonym|contrasts-with|related)\b", re.I)
REL_LABEL_CHOICES = "prerequisite|part-of|applies-to|example-of|synonym|contrasts-with|related"

# 关键修复②：关系模板安全渲染，避免 KeyError
class _SafeDict(dict):
    def __missing__(self, key):
        # 未提供的占位符保持原样，避免 KeyError
        return "{" + key + "}"

def build_user_prompt_for_relation(core: EntityItem, child: EntityItem) -> str:
    tpl = (_prompts_section().get("relation_user_template") or "").strip()
    if not tpl:
        return (
            "只输出一行：在 {prerequisite|part-of|applies-to|example-of|synonym|contrasts-with|related} 中选择一个最贴切的关系类型。\n\n"
            f"【Core】{core.name}（type={core.type}）\n描述：{_truncate(core.original, 200)}\n\n"
            f"【Non-Core】{child.name}（type={child.type}）\n描述：{_truncate(child.original, 200)}\n"
        )

    # 兼容老模板：若发现单花括号的 7 类标签，把它替换成受控占位符
    legacy = "{prerequisite|part-of|applies-to|example-of|synonym|contrasts-with|related}"
    if legacy in tpl:
        tpl = tpl.replace(legacy, "{label_choices}")

    fields = _SafeDict({
        "core_name": core.name,
        "core_type": core.type,
        "core_desc": _truncate(core.original, 260),
        "core_neighbors": _nb_list(core, 5),
        "child_name": child.name,
        "child_type": child.type,
        "child_desc": _truncate(child.original, 260),
        "child_neighbors": _nb_list(child, 5),
        "label_choices": REL_LABEL_CHOICES,  # 给模板使用
    })

    # 只格式化一次，且缺失字段不报错
    return tpl.format_map(fields)

def call_llm(system_prompt: str, user_prompt: str) -> Tuple[str, bool, str, int, bool]:
    """OpenAI 兼容 /chat/completions；线程安全：每次请求独立调用 requests.post"""
    if DRY_RUN:
        return "", False, "dry_run_enabled", 0, True
    if not API_BASE:
        logger.warning("API_BASE 为空，跳过 LLM 调用。")
        return "", False, "api_base_empty", 0, False

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "top_p": 1,
        "frequency_penalty": 0,
        "presence_penalty": 0,
    }

    last_err = ""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(
                f"{API_BASE}{CHAT_COMPLETIONS_PATH}",
                headers=headers,
                json=payload,
                timeout=API_TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            if content:
                return content, True, "", attempt, False
            last_err = "empty_content"
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    return "", False, last_err or "unknown_error", RETRIES, False

# ========================= 层级判定（并发）
_RE_CONCEPT = r"(?:\bconcept\b|概念)"
_RE_ENTITY = r"(?:\bentity\b|实体)"
LAYER_PATTERN = re.compile(rf"{_RE_ENTITY}|{_RE_CONCEPT}", flags=re.I)

def _parse_layer(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r", "")
    # 只看第一行
    for ln in t.splitlines():
        ln = ln.strip().strip(" :：，。;；\"'`*").lower()
        if not ln:
            continue
        if ln in {"concept", "概念"}:
            return "concept"
        if ln in {"entity", "实体"}:
            return "entity"
        if ln.startswith(("concept", "概念")):
            return "concept"
        if ln.startswith(("entity", "实体")):
            return "entity"
        break
    # 回退：全文正则
    matches = list(LAYER_PATTERN.finditer(t))
    if not matches:
        return ""
    last = matches[-1].group(0).lower().strip()
    return "entity" if ("entity" in last or "实体" in last) else "concept"

def _gen_concept_id(name: str) -> str:
    """为概念节点生成稳定的 concept_id"""
    return f"concept_{hashlib.md5(name.encode('utf-8')).hexdigest()[:8]}"

def judge_layer(entity: EntityItem, section_summary: str) -> Tuple[str, str, dict]:
    """判定实体属于 concept 层还是 entity 层。

    三级决策：
    1. 规则优先：schema.yaml 的 layer_hint=concept/entity → 直接判定（省 LLM 调用）
    2. LLM 判定：layer_hint=auto 或 type 未在 schema 中 → 调 LLM
    3. 启发式兜底：LLM 失败 → occurrences >= CONCEPT_MIN_OCC → concept，否则 entity

    Returns:
        (layer, concept_id, diag)
        layer: "concept" / "entity"
        concept_id: concept 的自身 id；entity 暂为空（Stage 2 分配）
    """
    ent_type = _normalize_type(entity.type)
    hint = ENTITY_TYPE_MAP.get(ent_type, "auto")
    occ_count = len(entity.occurrences)
    used_fallback = False
    note = ""
    ok = True
    attempts = 0
    dry = False
    raw = ""

    # ---- 第一级：规则优先 ----
    if hint == "concept" and occ_count >= CONCEPT_MIN_OCC:
        layer = "concept"
        concept_id = _gen_concept_id(entity.name)
        note = f"rule: layer_hint=concept, occ={occ_count}>={CONCEPT_MIN_OCC}"
        diag = {"ok": True, "attempts": 0, "used_fallback": False, "note": note, "raw": "", "dry_run": False, "method": "schema_rule"}
        return layer, concept_id, diag

    if hint == "concept":
        # layer_hint=concept 但出现次数不够 → 仍判 concept（类型说了算）
        layer = "concept"
        concept_id = _gen_concept_id(entity.name)
        note = f"rule: layer_hint=concept, occ={occ_count}<{CONCEPT_MIN_OCC} (type 优先)"
        diag = {"ok": True, "attempts": 0, "used_fallback": False, "note": note, "raw": "", "dry_run": False, "method": "schema_rule"}
        return layer, concept_id, diag

    if hint == "entity":
        layer = "entity"
        concept_id = ""  # Stage 2 分配
        note = f"rule: layer_hint=entity"
        diag = {"ok": True, "attempts": 0, "used_fallback": False, "note": note, "raw": "", "dry_run": False, "method": "schema_rule"}
        return layer, concept_id, diag

    # ---- 第二级：LLM 判定（auto 或未知类型）----
    sys_p = build_system_prompt_role()
    usr_p = build_user_prompt_for_layer(entity, section_summary)
    content, ok, err, attempts, dry = call_llm(sys_p, usr_p)
    parsed = _parse_layer(content)
    raw = content

    if ok and parsed in ("concept", "entity"):
        layer = parsed
        concept_id = _gen_concept_id(entity.name) if layer == "concept" else ""
        note = f"llm: type={ent_type}(auto), occ={occ_count}"
        diag = {"ok": ok, "attempts": attempts, "used_fallback": False, "note": note, "raw": raw, "dry_run": dry, "method": "llm"}
        return layer, concept_id, diag

    # ---- 第三级：启发式兜底 ----
    used_fallback = True
    weight = float(AggrCfg.get("FALLBACK", {}).get("neighbor_weight", 0.5))
    threshold = float(AggrCfg.get("FALLBACK", {}).get("threshold", 4))
    score = occ_count + weight * len(entity.neighbors)
    layer = "concept" if score >= threshold else "entity"
    concept_id = _gen_concept_id(entity.name) if layer == "concept" else ""
    note = f"fallback_heuristic: score={score:.1f} threshold={threshold} -> {layer}"
    diag = {"ok": ok, "attempts": attempts, "used_fallback": used_fallback, "note": note, "raw": raw, "dry_run": dry, "method": "fallback"}
    return layer, concept_id, diag

# ========================= 实体→概念分配 + 兜底
def _is_horizontal(snippet: str) -> bool:
    head = (snippet or "").split("|", 1)[0].strip().lower()
    vertical_prefixes = [s.lower() for s in (AggrCfg.get("VERTICAL_REL_TYPES") or [])]
    return head not in vertical_prefixes and head != "rel"  # cooccur 等视为横向

def _cooccur_weight(s: str) -> int:
    m = re.search(r"\bw=(\d+)\b", s or "")
    try:
        return int(m.group(1)) if m else 0
    except Exception:
        return 0

def _build_entity_concept_candidates(entities: Dict[str, EntityItem]) -> Dict[str, List[str]]:
    """从 concept 的横向邻居里挑 entity 作为子候选"""
    cand: Dict[str, List[str]] = {}
    for concept_name, concept_ent in entities.items():
        if concept_ent.layer != "concept":
            continue
        for nb in concept_ent.neighbors:
            child = entities.get(nb.name)
            if not child:
                continue
            if child.layer == "entity" and _is_horizontal(nb.snippet):
                cand.setdefault(child.name, []).append(concept_name)
    return cand

def _fallback_pick_concept_for_entity(ent: EntityItem, entities: Dict[str, EntityItem]) -> Optional[str]:
    """兜底：优先选择与 entity 共现权重最高的 concept；再同 section 交集；再任取 concept。"""
    best_concept, best_w = None, -1
    for nb in ent.neighbors:
        ce = entities.get(nb.name)
        if not ce or ce.layer != "concept":
            continue
        w = _cooccur_weight(nb.snippet)
        if w > best_w:
            best_w, best_concept = w, ce.name
    if best_concept:
        return best_concept

    ent_secs = {o.node_id for o in ent.occurrences}
    if ent_secs:
        score: Dict[str, int] = {}
        for cname, cent in entities.items():
            if cent.layer != "concept":
                continue
            c_secs = {o.node_id for o in cent.occurrences}
            inter = len(ent_secs & c_secs)
            if inter > 0:
                score[cname] = inter
        if score:
            return max(score.items(), key=lambda kv: kv[1])[0]

    for cname, cent in entities.items():
        if cent.layer == "concept":
            return cname
    return None

# ========================= 关系类型判定（并发）
def _parse_relation_type(text: str) -> Optional[str]:
    if not text:
        return None
    first = text.splitlines()[0].strip().lower()
    first = re.sub(r"[\"'`*：:，,。;\\s]+$", "", first)
    if first in REL_TYPES:
        return first
    m = REL_PATTERN.search(text)
    if m:
        cand = m.group(1).lower()
        if cand in REL_TYPES:
            return cand
    return None

def classify_relation_type(core_ent: EntityItem, child_ent: EntityItem) -> str:
    if DRY_RUN:
        return "related"
    sys_p = build_system_prompt_relation()
    usr_p = build_user_prompt_for_relation(core_ent, child_ent)
    content, ok, err, attempts, dry = call_llm(sys_p, usr_p)
    rel = _parse_relation_type(content or "")
    if rel in REL_TYPES:
        # 小偏置：如果 core 是方法/函数/算法而 child 不是，优先 applies-to
        if rel == "related" and any(k in core_ent.type.lower() for k in ["method","function","algorithm"]) and \
           not any(k in child_ent.type.lower() for k in ["method","function","algorithm"]):
            return "applies-to"
        return rel
    # 兜底
    if core_ent.name == child_ent.name or (set(core_ent.alias) & set(child_ent.alias)):
        return "synonym"
    if any(k in core_ent.type.lower() for k in ["method","function","algorithm"]) and \
       not any(k in child_ent.type.lower() for k in ["method","function","algorithm"]):
        return "applies-to"
    return "related"

def _mk_rel_snippet(rel_type: str, src_layer="concept", dst_layer="entity") -> str:
    return f"rel|directed|type={rel_type}|src={src_layer}|dst={dst_layer}"

# ========================= 类型补齐（写文件前）
def _infer_type_from_snippet(sn: str) -> str:
    if not sn:
        return ""
    head = sn.split("|", 1)[0].lower()
    if head == "rel":
        m = re.search(r"type=([a-z\\-]+)", sn, flags=re.I)
        return (m.group(1).lower() if m else "related")
    return head  # e.g. "cooccur", "has_subordinate" 等

# ========================= 主流程
def run_aggr():
    # 路径检查
    if not CONV_IN_PATH.exists():
        raise FileNotFoundError(
            f"未找到 conv 实体文件：{CONV_IN_PATH}\n"
            f"请确认 KGBuild/output/ 目录或在 aggr.yaml 中设置 CONV_IN_NAME。"
        )

    with CONV_IN_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    # 转对象 + LIMIT
    items = list(raw.items())
    if LIMIT and LIMIT > 0:
        items = items[:LIMIT]

    entities: Dict[str, EntityItem] = {}
    for name, data in items:
        entities[name] = EntityItem(
            name=name,
            alias=data.get("alias", []),
            type=data.get("type", ""),
            original=data.get("original", ""),
            updated_description=data.get("updated_description", ""),
            occurrences=[Occurrence(**o) for o in data.get("occurrences", [])],
            neighbors=[Neighbor(**n) for n in data.get("neighbors", [])],
        )

    section_summary = "章节内容摘要"  # 如需，可从文件读取

    # 阶段1：层级判定（并发）—— concept / entity
    logger.info(f"===== [Stage 1] 层级判定 concept/entity（并发={WORKERS}）=====")
    def _layer_job(ent: EntityItem) -> Tuple[str, str, str, dict]:
        layer, concept_id, diag = judge_layer(ent, section_summary)
        return ent.name, layer, concept_id, diag

    futures = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for ent in entities.values():
            futures.append(ex.submit(_layer_job, ent))
        for fut in tqdm(as_completed(futures), total=len(futures), ncols=PROGRESS_NCOLS, desc="Assign layers"):
            try:
                name, layer, concept_id, diag = fut.result()
                entities[name].layer = layer
                entities[name].concept_id = concept_id
                # 兼容旧字段：core=concept, non-core=entity
                entities[name].role = "core" if layer == "concept" else "non-core"
                logger.info(f"[layer] {name} -> {layer} | method={diag.get('method','?')} fallback={diag['used_fallback']}")
            except Exception as e:
                logger.warning(f"[layer] 任务失败：{e}")

    total_concept = sum(1 for e in entities.values() if e.layer == "concept")
    total_entity  = sum(1 for e in entities.values() if e.layer == "entity")
    logger.info(f"层级分布：concept={total_concept}, entity={total_entity}")

    # 阶段2：实体→概念分配（保证每个 entity 至少有一个 concept；TREE_ENFORCE 时单父）
    logger.info("===== [Stage 2] 分配 entity→concept 并保证覆盖所有 entity =====")
    candidates = _build_entity_concept_candidates(entities)  # entity_name -> [concept...]
    chosen: Dict[str, str] = {}
    for child, parents in candidates.items():
        if parents:
            chosen[child] = parents[0]

    for name, ent in entities.items():
        if ent.layer != "entity":
            continue
        if name not in chosen:
            alt = _fallback_pick_concept_for_entity(ent, entities)
            if alt:
                chosen[name] = alt
        # 分配 concept_id
        if name in chosen:
            concept_ent = entities[chosen[name]]
            if concept_ent.concept_id:
                ent.concept_id = concept_ent.concept_id
            else:
                # concept 自身还没 id（理论上不该发生），补一个
                concept_ent.concept_id = _gen_concept_id(concept_ent.name)
                ent.concept_id = concept_ent.concept_id

    if TREE_ENFORCE:
        pass

    logger.info(f"entity 总数={total_entity}，已分配 concept_id={len(chosen)}")

    # 阶段3：LLM 并发判定关系类型 + 写边（仅 concept→entity）
    logger.info(f"===== [Stage 3] 关系类型判定（并发={WORKERS}）并写边 =====")
    def _rel_job(child_name: str, parent_name: str) -> Tuple[str, str, str]:
        concept_ent = entities[parent_name]
        child_ent   = entities[child_name]
        rel_type = classify_relation_type(concept_ent, child_ent)
        return child_name, parent_name, rel_type

    rel_futs = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for child_name, parent_name in chosen.items():
            rel_futs.append(ex.submit(_rel_job, child_name, parent_name))
        for fut in tqdm(as_completed(rel_futs), total=len(rel_futs), ncols=PROGRESS_NCOLS, desc="Classify relations"):
            try:
                child_name, parent_name, rel_type = fut.result()
                concept_ent = entities[parent_name]
                child_ent   = entities[child_name]

                # 移除两侧横向边
                old_concept = len(concept_ent.neighbors)
                concept_ent.neighbors = [
                    n for n in concept_ent.neighbors
                    if not (n.name == child_name and _is_horizontal(n.snippet))
                ]
                concept_removed = old_concept - len(concept_ent.neighbors)

                old_child = len(child_ent.neighbors)
                child_ent.neighbors = [
                    n for n in child_ent.neighbors
                    if not (n.name == parent_name and _is_horizontal(n.snippet))
                ]
                child_removed = old_child - len(child_ent.neighbors)

                # 添加 concept -> entity（不写回指），同时写 type
                sn = _mk_rel_snippet(rel_type, "concept", "entity")
                if not any(n.name == child_name and n.snippet == sn for n in concept_ent.neighbors):
                    concept_ent.neighbors.append(Neighbor(name=child_name, snippet=sn, type=rel_type))

                total_removed = max(concept_removed, 0) + max(child_removed, 0)
                logger.info(f"[edge] {parent_name} -> {child_name} ({rel_type}) | removed_horizontal={total_removed}")
            except Exception as e:
                logger.warning(f"[rel] 任务失败：{e}")

    # —— 写出前统一补齐 neighbors[*].type —— #
    for ent in entities.values():
        for nb in ent.neighbors:
            if not getattr(nb, "type", ""):
                nb.type = _infer_type_from_snippet(nb.snippet)

    # 写出结果
    out = {}
    for name, ent in tqdm(entities.items(), desc="Write results", ncols=PROGRESS_NCOLS):
        out[name] = {
            "name": ent.name,
            "alias": ent.alias,
            "type": ent.type,
            "original": ent.original,
            "updated_description": ent.updated_description,
            "layer": ent.layer,
            "concept_id": ent.concept_id,
            "role": ent.role,  # 旧字段保留兼容
            "occurrences": [asdict(o) for o in ent.occurrences],
            "neighbors": [asdict(n) for n in ent.neighbors],
        }

    AGGR_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AGGR_OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    logger.info(f"完成，共处理 {len(out)} 个实体。输出文件：{AGGR_OUT_PATH}")
    print(f"\n✅ Aggr 阶段完成（并发 {WORKERS}），日志：{LOG_PATH}")

# ========================= CLI
if __name__ == "__main__":
    run_aggr()
