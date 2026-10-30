# -*- coding: utf-8 -*-
"""L0 Schema v2 加载器 —— 构建流水线的单一事实来源。

职责：
  * 读取 config/schema.v2.yaml，索引节点类型、关系类型、分层规则、别名
  * 提供类型/关系校验、关系名归一化、对称边规范化、逆关系派生
  * 提供 LayerPolicy 的规则求值（规则优先，必要时交回调用方走 LLM）
  * 提供稳定 ID 生成

不做任何 v1 兼容。v2 从头构建，旧 schema.yaml 不再被本模块读取。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

SCHEMA_PATH = Path(__file__).resolve().parent / "config" / "schema.v2.yaml"

VALID_LAYERS = ("L1", "L2", "L3")
EDGE_LAYERS = ("L1-L1", "L2-L2", "L1-L2", "L2-L1", "L1-L3", "L2-L3")


@dataclass(frozen=True)
class NodeType:
    name: str
    description: str
    examples: Tuple[str, ...]
    parent: Optional[str]
    domain: str            # "core" 或领域包名
    resolved: bool = True


@dataclass(frozen=True)
class RelationType:
    name: str
    direction: str         # directed | symmetric
    inverse: Optional[str]
    store_inverse: bool
    endpoint_policy: str
    layers: Tuple[str, ...]
    description: str
    is_fallback: bool = False
    min_confidence: float = 0.0


@dataclass(frozen=True)
class LayerRule:
    id: str
    when: Dict[str, Any]
    layer: str             # L1 | L2 | llm | excluded
    confidence: float


@dataclass(frozen=True)
class LayerDecision:
    layer: Optional[str]
    rule_id: str
    confidence: float
    needs_llm: bool = False


def _match(cond: Dict[str, Any], values: Dict[str, Any]) -> bool:
    """求值 LayerPolicy.rules[].when。

    支持：
      {all: [...]}, {any: [...]}
      {field: value}
      {field: {gte|gt|lte|lt|eq|in: ...}}
    """
    if not cond:
        return True
    if "all" in cond:
        return all(_match(c, values) for c in cond["all"])
    if "any" in cond:
        return any(_match(c, values) for c in cond["any"])

    for key, expected in cond.items():
        actual = values.get(key)
        if isinstance(expected, dict):
            for op, val in expected.items():
                if op == "gte" and not (actual is not None and actual >= val):
                    return False
                if op == "gt" and not (actual is not None and actual > val):
                    return False
                if op == "lte" and not (actual is not None and actual <= val):
                    return False
                if op == "lt" and not (actual is not None and actual < val):
                    return False
                if op == "eq" and actual != val:
                    return False
                if op == "in" and actual not in val:
                    return False
        elif actual != expected:
            return False
    return True


class Schema:
    def __init__(self, raw: Dict[str, Any], path: Path):
        self.raw = raw
        self.path = path
        self.version = str(raw.get("schema_version") or "")
        self.node_types: Dict[str, NodeType] = {}
        self.relation_types: Dict[str, RelationType] = {}
        self.derived_inverses: Dict[str, Dict[str, Any]] = {}
        self.relation_aliases: Dict[str, Dict[str, Any]] = {}
        self.layer_rules: List[LayerRule] = []
        self.layer_signal_names: Tuple[str, ...] = ()
        self.evidence_section_levels: Tuple[str, ...] = ()
        self.evidence_block_types: Tuple[str, ...] = ()
        self.node_id_pattern = ""
        self._index()

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def _index(self) -> None:
        raw = self.raw

        core = raw["NodeTypes"]["core"]
        for name, meta in core.items():
            self.node_types[name] = NodeType(
                name=name,
                description=str(meta.get("description") or ""),
                examples=tuple(meta.get("examples") or ()),
                parent=meta.get("parent"),
                domain="core",
                resolved=bool(meta.get("resolved", True)),
            )

        for pack_name, pack in (raw["NodeTypes"].get("domain_packs") or {}).items():
            for name, meta in (pack.get("types") or {}).items():
                if name in self.node_types:
                    raise ValueError(f"重复的节点类型：{name}（来自领域包 {pack_name}）")
                self.node_types[name] = NodeType(
                    name=name,
                    description=str(meta.get("description") or ""),
                    examples=tuple(meta.get("examples") or ()),
                    parent=meta.get("parent"),
                    domain=pack_name,
                    resolved=bool(meta.get("resolved", True)),
                )

        for section in ("core", "cross_layer"):
            for name, meta in (raw["RelationTypes"].get(section) or {}).items():
                if name in self.relation_types:
                    raise ValueError(f"重复的关系类型：{name}")
                self.relation_types[name] = RelationType(
                    name=name,
                    direction=str(meta.get("direction") or "directed"),
                    inverse=meta.get("inverse"),
                    store_inverse=bool(meta.get("store_inverse", False)),
                    endpoint_policy=str(meta.get("endpoint_policy") or "layer_constraint"),
                    layers=tuple(meta.get("layers") or ()),
                    description=str(meta.get("description") or ""),
                    is_fallback=bool(meta.get("is_fallback", False)),
                    min_confidence=float(meta.get("min_confidence", 0.0)),
                )

        self.derived_inverses = dict(raw["RelationTypes"].get("derived_inverses") or {})
        self.relation_aliases = dict(raw.get("RelationAliases") or {})

        self.layer_rules = [
            LayerRule(
                id=str(rule["id"]),
                when=dict(rule.get("when") or {}),
                layer=str(rule["layer"]),
                confidence=float(rule.get("confidence", 0.0)),
            )
            for rule in (raw.get("LayerPolicy", {}).get("rules") or [])
        ]
        self.layer_signal_names = tuple((raw.get("LayerPolicy", {}).get("signals") or {}).keys())

        ev = raw.get("EvidenceSchema") or {}
        self.evidence_section_levels = tuple((ev.get("section_level") or {}).get("enum") or ())
        self.evidence_block_types = tuple((ev.get("block_type") or {}).get("enum") or ())

        node_pattern = (
            raw.get("Records", {}).get("Node", {}).get("fields", {})
            .get("id", {}).get("pattern")
        )
        self.node_id_pattern = str(node_pattern or "")

    # ------------------------------------------------------------------
    # 类型 / 关系查询
    # ------------------------------------------------------------------
    def is_valid_node_type(self, name: str) -> bool:
        return name in self.node_types

    def get_node_type(self, name: str) -> NodeType:
        return self.node_types[name]

    def get_relation(self, name: str) -> Optional[RelationType]:
        return self.relation_types.get(name)

    def is_symmetric(self, name: str) -> bool:
        rt = self.relation_types.get(name)
        return bool(rt and rt.direction == "symmetric")

    def canonical_relation(self, name: str) -> Optional[str]:
        """把书写变体归一为规范名；未知关系返回 None。"""
        if not name:
            return None
        raw = name.strip()
        if raw in self.relation_types:
            return raw
        key = raw.lower().replace("-", "_").replace(" ", "_")
        if key in self.relation_types:
            return key
        alias = self.relation_aliases.get(raw) or self.relation_aliases.get(key)
        if alias:
            canonical = alias.get("canonical")
            if canonical in self.relation_types:
                return canonical
        return None

    def relation_allowed(
        self,
        rel_type: str,
        source_layer: str,
        target_layer: str,
        source_type: Optional[str] = None,
        target_type: Optional[str] = None,
    ) -> bool:
        rt = self.relation_types.get(rel_type)
        if rt is None:
            return False
        if source_layer not in VALID_LAYERS or target_layer not in VALID_LAYERS:
            return False

        pair = f"{source_layer}-{target_layer}"
        if rt.endpoint_policy == "same_layer":
            return source_layer == target_layer and source_layer in ("L1", "L2")
        if rt.endpoint_policy in ("cross_layer", "layer_constraint"):
            return pair in rt.layers
        if rt.endpoint_policy == "type_constraint":
            return pair in rt.layers
        return False

    def normalize_edge(self, source: str, target: str, rel_type: str) -> Tuple[str, str, str]:
        """归一化一条边：关系名转规范名；对称边按字符串序固定方向。"""
        canonical = self.canonical_relation(rel_type)
        if canonical is None:
            raise ValueError(f"未知关系类型：{rel_type!r}")
        if not source or not target:
            raise ValueError("边的 source/target 不能为空")
        if source == target:
            raise ValueError(f"不允许自环：{source}")
        if self.is_symmetric(canonical) and source > target:
            source, target = target, source
        return source, target, canonical

    def derive_inverse_edges(self, source: str, target: str, rel_type: str) -> List[Tuple[str, str, str]]:
        """按 derived_inverses 派生逆边；不写入存储，只在序列化时使用。

        逆边的端点必然互换：A part_of B ⇒ B has_part A。
        """
        out: List[Tuple[str, str, str]] = []
        for inv_name, meta in self.derived_inverses.items():
            if meta.get("inverse_of") != rel_type:
                continue
            out.append((target, source, inv_name))
        return out

    # ------------------------------------------------------------------
    # 分层策略
    # ------------------------------------------------------------------
    def evaluate_layer(
        self,
        node_type: str,
        signals: Dict[str, Any],
        llm_fn: Optional[Callable[[str, Dict[str, Any]], Optional[str]]] = None,
    ) -> LayerDecision:
        """按 LayerPolicy.rules 顺序求值。

        signals 支持的键见 schema 的 LayerPolicy.signals。
        命中 llm 规则且未提供 llm_fn 时，返回 needs_llm=True，由调用方决定。
        """
        values = dict(signals)
        values["type"] = node_type

        for rule in self.layer_rules:
            if not _match(rule.when, values):
                continue
            if rule.layer in ("L1", "L2", "excluded"):
                return LayerDecision(rule.layer, rule.id, rule.confidence)
            if rule.layer == "llm":
                if llm_fn is None:
                    return LayerDecision(None, rule.id, rule.confidence, needs_llm=True)
                layer = llm_fn(node_type, signals)
                if layer not in ("L1", "L2"):
                    return LayerDecision("excluded", rule.id, 0.0)
                default_conf = float(
                    (self.raw.get("LayerPolicy", {}).get("llm_fallback") or {})
                    .get("confidence_default", 0.7)
                )
                return LayerDecision(layer, rule.id, default_conf)
        return LayerDecision(None, "no_match", 0.0, needs_llm=True)

    # ------------------------------------------------------------------
    # schema 自检
    # ------------------------------------------------------------------
    def validate(self) -> List[str]:
        errs: List[str] = []
        if not self.version:
            errs.append("缺少 schema_version")

        for name, nt in self.node_types.items():
            if nt.parent and nt.parent not in self.node_types:
                errs.append(f"节点类型 {name} 的 parent 不存在：{nt.parent}")

        for name, rt in self.relation_types.items():
            if rt.direction not in ("directed", "symmetric"):
                errs.append(f"关系 {name} 的 direction 非法：{rt.direction}")
            if rt.endpoint_policy not in ("same_layer", "cross_layer", "layer_constraint", "type_constraint"):
                errs.append(f"关系 {name} 的 endpoint_policy 非法：{rt.endpoint_policy}")
            for layer in rt.layers:
                if layer not in EDGE_LAYERS:
                    errs.append(f"关系 {name} 的 layer 非法：{layer}")
            if rt.inverse and rt.inverse not in self.relation_types and rt.inverse not in self.derived_inverses:
                errs.append(f"关系 {name} 的 inverse 不存在：{rt.inverse}")

        for inv_name, meta in self.derived_inverses.items():
            if meta.get("inverse_of") not in self.relation_types:
                errs.append(f"派生逆关系 {inv_name} 的 inverse_of 不存在：{meta.get('inverse_of')}")

        for alias, meta in self.relation_aliases.items():
            if meta.get("canonical") not in self.relation_types:
                errs.append(f"关系别名 {alias} 指向未知关系：{meta.get('canonical')}")

        ids = [r.id for r in self.layer_rules]
        if len(ids) != len(set(ids)):
            errs.append("LayerPolicy 规则 id 重复")

        if "subsubsection" not in self.evidence_section_levels:
            errs.append("EvidenceSchema.section_level 缺少 subsubsection")
        return errs


@lru_cache(maxsize=4)
def load_schema(path: Optional[str] = None) -> Schema:
    p = Path(path) if path else SCHEMA_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    schema = Schema(raw, p)
    errs = schema.validate()
    if errs:
        raise ValueError("schema 自检失败：\n  - " + "\n  - ".join(errs))
    return schema


def get_schema() -> Schema:
    return load_schema()


# ----------------------------------------------------------------------
# 稳定 ID
# ----------------------------------------------------------------------
def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def make_node_id(name: str, layer: str) -> str:
    prefix = "concept" if layer == "L1" else "ent"
    return f"{prefix}_{_sha1(' '.join(name.split()))[:8]}"


def make_section_id(numbering: str, section_path: str) -> str:
    """numbered → 编号路径；unnumbered → path_{hash}。"""
    if numbering and numbering.strip():
        return numbering.strip()
    return f"path_{_sha1(' > '.join(p.strip() for p in section_path.split('>')))[:8]}"


def make_evidence_id(doc_id: str, section_id: str, block_id: str = "") -> str:
    base = f"ev_{doc_id}:{section_id}"
    return f"{base}:{block_id}" if block_id else base
