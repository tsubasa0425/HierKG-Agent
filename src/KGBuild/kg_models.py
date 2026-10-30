# -*- coding: utf-8 -*-
"""v2 构建产物数据模型。

这些 dataclass 是各阶段之间传递数据的统一形状；序列化后的字段必须满足
schema.v2.yaml 的 Records 契约。边是证据归属的权威来源，节点上的
evidence_ids 只是投影缓存（由 FinalKG 从跨层边回填）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .schema_loader import Schema, make_evidence_id, make_node_id


@dataclass
class Provenance:
    doc_id: str
    section_id: str = ""
    block_id: str = ""
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    extractor: str = ""
    model: str = ""
    prompt_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Provenance":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Salience:
    occurrence_count: int = 0
    section_coverage: int = 0
    degree: int = 0
    definitional: bool = False
    score: float = 0.0

    def compute_score(self) -> float:
        score = (
            0.4 * min(self.section_coverage / 3, 1)
            + 0.3 * min(self.occurrence_count / 5, 1)
            + 0.2 * (1.0 if self.definitional else 0.0)
            + 0.1 * min(self.degree / 10, 1)
        )
        return round(score, 4)

    def finalize(self) -> "Salience":
        self.score = self.compute_score()
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "occurrence_count": self.occurrence_count,
            "section_coverage": self.section_coverage,
            "degree": self.degree,
            "definitional": self.definitional,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Salience":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Node:
    id: str
    name: str
    type: str
    layer: str
    aliases: List[str] = field(default_factory=list)
    definition: str = ""
    confidence: float = 1.0
    salience: Salience = field(default_factory=Salience)
    provenance: List[Provenance] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    evidence_ids: List[str] = field(default_factory=list)   # 投影，由跨层边回填

    @classmethod
    def create(cls, name: str, type: str, layer: str, **kwargs: Any) -> "Node":
        return cls(
            id=make_node_id(name, layer),
            name=name,
            type=type,
            layer=layer,
            **kwargs,
        )

    def add_provenance(self, prov: Provenance) -> None:
        self.provenance.append(prov)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "layer": self.layer,
            "aliases": list(self.aliases),
            "definition": self.definition,
            "confidence": self.confidence,
            "salience": self.salience.to_dict(),
            "provenance": [p.to_dict() for p in self.provenance],
            "attributes": dict(self.attributes),
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        return cls(
            id=d["id"],
            name=d["name"],
            type=d["type"],
            layer=d["layer"],
            aliases=list(d.get("aliases") or []),
            definition=str(d.get("definition") or ""),
            confidence=float(d.get("confidence", 1.0)),
            salience=Salience.from_dict(d.get("salience") or {}),
            provenance=[Provenance.from_dict(p) for p in d.get("provenance") or []],
            attributes=dict(d.get("attributes") or {}),
            evidence_ids=list(d.get("evidence_ids") or []),
        )


@dataclass
class Relation:
    source: str
    target: str
    type: str
    layer: str
    confidence: float = 1.0
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    provenance: List[Provenance] = field(default_factory=list)
    strength: Optional[int] = None

    @classmethod
    def create(
        cls,
        schema: Schema,
        source: str,
        target: str,
        rel_type: str,
        source_layer: str,
        target_layer: str,
        confidence: float = 1.0,
        **kwargs: Any,
    ) -> "Relation":
        source, target, canonical = schema.normalize_edge(source, target, rel_type)
        if not schema.relation_allowed(canonical, source_layer, target_layer):
            raise ValueError(
                f"关系 {canonical} 不允许 {source_layer}→{target_layer}："
                f"{source} → {target}"
            )
        return cls(
            source=source,
            target=target,
            type=canonical,
            layer=f"{source_layer}-{target_layer}",
            confidence=confidence,
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "layer": self.layer,
            "confidence": self.confidence,
            "description": self.description,
            "keywords": list(self.keywords),
            "evidence_ids": list(self.evidence_ids),
            "provenance": [p.to_dict() for p in self.provenance],
        }
        if self.strength is not None:
            out["strength"] = self.strength
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Relation":
        return cls(
            source=d["source"],
            target=d["target"],
            type=d["type"],
            layer=d["layer"],
            confidence=float(d.get("confidence", 1.0)),
            description=str(d.get("description") or ""),
            keywords=list(d.get("keywords") or []),
            evidence_ids=list(d.get("evidence_ids") or []),
            provenance=[Provenance.from_dict(p) for p in d.get("provenance") or []],
            strength=d.get("strength"),
        )


@dataclass
class Evidence:
    id: str
    doc_id: str
    section_id: str
    section_path: str
    section_level: str
    title: str
    text: str
    block_id: str = ""
    block_type: str = "unknown"
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    entities_in_section: List[str] = field(default_factory=list)
    derived: bool = False

    @classmethod
    def create(
        cls,
        doc_id: str,
        section_id: str,
        section_path: str,
        section_level: str,
        title: str,
        text: str,
        block_id: str = "",
        **kwargs: Any,
    ) -> "Evidence":
        return cls(
            id=make_evidence_id(doc_id, section_id, block_id),
            doc_id=doc_id,
            section_id=section_id,
            section_path=section_path,
            section_level=section_level,
            title=title,
            text=text,
            block_id=block_id,
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "doc_id": self.doc_id,
            "section_id": self.section_id,
            "section_path": self.section_path,
            "section_level": self.section_level,
            "title": self.title,
            "text": self.text,
            "block_id": self.block_id,
            "block_type": self.block_type,
            "entities_in_section": list(self.entities_in_section),
            "derived": self.derived,
        }
        # 偏移是可选项：derived 证据的文本由标题拼接，本来就没有原文偏移
        if self.char_start is not None and self.char_end is not None:
            out["char_start"] = self.char_start
            out["char_end"] = self.char_end
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Evidence":
        return cls(
            id=d["id"],
            doc_id=d["doc_id"],
            section_id=d["section_id"],
            section_path=d["section_path"],
            section_level=d["section_level"],
            title=d["title"],
            text=d["text"],
            block_id=str(d.get("block_id") or ""),
            block_type=str(d.get("block_type") or "unknown"),
            char_start=d.get("char_start"),
            char_end=d.get("char_end"),
            entities_in_section=list(d.get("entities_in_section") or []),
            derived=bool(d.get("derived", False)),
        )


@dataclass
class KnowledgeGraph:
    schema_version: str
    L0_schema: Dict[str, Any]
    L1_concepts: List[Node] = field(default_factory=list)
    L2_entities: List[Node] = field(default_factory=list)
    L3_evidences: List[Evidence] = field(default_factory=list)
    edges: List[Relation] = field(default_factory=list)
    build_manifest: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "L0_schema": self.L0_schema,
            "L1_concepts": [n.to_dict() for n in self.L1_concepts],
            "L2_entities": [n.to_dict() for n in self.L2_entities],
            "L3_evidences": [e.to_dict() for e in self.L3_evidences],
            "edges": [r.to_dict() for r in self.edges],
        }
        if self.build_manifest:
            out["build_manifest"] = self.build_manifest
        return out
