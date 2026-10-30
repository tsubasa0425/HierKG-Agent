#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FinalKG v2 —— 按 schema.v2 装配四层图谱（final_kg.json）。

输入：
  * dedup_result.json    去重后的 L1/L2 节点 + 语义关系
  * pred_result.json     Pred 补出的候选边
  * evidence.json        L3 证据层
输出：
  * final_kg.json        {schema_version, L0_schema, L1_concepts, L2_entities,
                          L3_evidences, edges, build_manifest}
  * build_manifest.json  构建清单（哈希 / 版本 / 校验摘要）

关键规则：
  * 边是证据归属的权威来源；节点 evidence_ids 只是跨层出边的投影
  * derived 证据（容器小节）只做导航，不进入 evidence_ids 投影
  * 逆关系（has_part / has_example）不落库，由序列化层派生
  * 对称边按节点 ID 字符串序规范化
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

try:
    from .kg_models import Evidence, KnowledgeGraph, Node, Provenance, Relation, Salience
    from .schema_loader import Schema, get_schema
    from .validate_kg_v2 import validate
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from KGBuild.kg_models import Evidence, KnowledgeGraph, Node, Provenance, Relation, Salience
    from KGBuild.schema_loader import Schema, get_schema
    from KGBuild.validate_kg_v2 import validate

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
OUT_DIR = SCRIPT_DIR / "output"
SCHEMA = get_schema()

logger = logging.getLogger("FinalKG")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

DEFAULTS: Dict[str, Any] = {
    "DEDUP_NAME": "dedup_result.json",
    "PRED_NAME": "pred_result.json",
    "EVIDENCE_NAME": "evidence.json",
    "OUT_NAME": "final_kg.json",
    "MANIFEST_NAME": "build_manifest.json",
    "EXCLUDE_DERIVED_FROM_PROJECTION": True,
}


def _load_yaml(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_config() -> Dict[str, Any]:
    cfg_path = CONFIG_DIR / "config.yaml"
    merged: Dict[str, Any] = {}
    if cfg_path.exists():
        merged = dict(_load_yaml(cfg_path))
        for rel in merged.get("include_files", []) or []:
            p = (CONFIG_DIR / str(rel)).resolve()
            if p.exists():
                merged.update(_load_yaml(p))
    cfg = dict(DEFAULTS)
    cfg.update(merged.get("FinalConfig") or {})
    return cfg


CFG = _load_config()


def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _file_sha1(p: Path) -> str:
    try:
        return _sha1_bytes(p.read_bytes())
    except OSError:
        return ""


def _read_json(p: Path, required: bool = True) -> Any:
    if not p.exists():
        if required:
            raise FileNotFoundError(f"缺少输入：{p}")
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _prov(raw: Iterable[Dict[str, Any]], doc_id: str, default_section: str = "") -> List[Provenance]:
    out: List[Provenance] = []
    seen: Set[str] = set()
    for p in raw or []:
        d = dict(p)
        d.setdefault("doc_id", doc_id)
        if not d.get("doc_id"):
            d["doc_id"] = doc_id
        if not d.get("section_id") and default_section:
            d["section_id"] = default_section
        try:
            item = Provenance.from_dict(d)
        except Exception:
            continue
        key = json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _salience(entity: Dict[str, Any], degree: int) -> Salience:
    s = entity.get("signals") or {}
    sal = Salience(
        occurrence_count=int(s.get("occurrence_count", 0)),
        section_coverage=int(s.get("cross_section_coverage", 0)),
        degree=int(degree),
        definitional=bool(s.get("has_definition") or s.get("definitional_cue")),
    )
    return sal.finalize()


def _l0(schema: Schema) -> Dict[str, Any]:
    return {
        "schema_version": schema.version,
        "ontology_name": schema.raw.get("ontology_name", ""),
        "node_types": schema.raw.get("NodeTypes") or {},
        "relation_types": schema.raw.get("RelationTypes") or {},
        "layer_policy": schema.raw.get("LayerPolicy") or {},
        "evidence_schema": schema.raw.get("EvidenceSchema") or {},
    }


# ---------------------------------------------------------------------------
# 边工具
# ---------------------------------------------------------------------------

def _edge_key(src: str, tgt: str, typ: str) -> Tuple[str, str, str]:
    return (src, tgt, typ)


def _add_edge(edges: List[Dict[str, Any]], seen: Set[Tuple[str, str, str]],
              src: str, tgt: str, typ: str, layer: str, confidence: float,
              desc: str = "", keywords: Optional[List[str]] = None,
              evidence_ids: Optional[List[str]] = None,
              provenance: Optional[List[Provenance]] = None) -> bool:
    if not src or not tgt or src == tgt:
        return False
    key = _edge_key(src, tgt, typ)
    if key in seen:
        return False
    edges.append({
        "source": src, "target": tgt, "type": typ, "layer": layer,
        "confidence": round(float(confidence), 2),
        "description": desc,
        "keywords": list(keywords or []),
        "evidence_ids": list(evidence_ids or []),
        "provenance": [p.to_dict() for p in (provenance or [])],
    })
    seen.add(key)
    return True


def assemble(dedup_path: Optional[Path] = None, pred_path: Optional[Path] = None,
             evidence_path: Optional[Path] = None, out_path: Optional[Path] = None) -> Dict[str, Any]:
    dedup_path = dedup_path or (OUT_DIR / CFG["DEDUP_NAME"])
    pred_path = pred_path or (OUT_DIR / CFG["PRED_NAME"])
    evidence_path = evidence_path or (OUT_DIR / CFG["EVIDENCE_NAME"])
    out_path = out_path or (OUT_DIR / CFG["OUT_NAME"])

    dedup = _read_json(dedup_path)
    pred = _read_json(pred_path, required=False) or {}
    ev_doc = _read_json(evidence_path)
    if isinstance(dedup, list) or "entities" not in dedup:
        raise ValueError("dedup_result.json 不是 v2 产物")
    if isinstance(ev_doc, list) or "evidences" not in ev_doc:
        raise ValueError("evidence.json 不是 v2 产物")

    doc_id = str(dedup.get("doc_id") or ev_doc.get("doc_id") or "doc")
    merge_map = {str(k): str(v) for k, v in (dedup.get("merge_map") or {}).items()}

    # —— L3 证据 —— #
    evidences: List[Evidence] = []
    evidence_by_id: Dict[str, Evidence] = {}
    section_ev: Dict[str, str] = {}
    for raw in ev_doc["evidences"]:
        ev = Evidence.from_dict(raw)
        evidences.append(ev)
        evidence_by_id[ev.id] = ev
        if not ev.derived and ev.section_id not in section_ev:
            section_ev[ev.section_id] = ev.id

    # —— 节点 —— #
    nodes_by_id: Dict[str, Node] = {}
    node_by_name: Dict[str, Node] = {}
    entities: Dict[str, Dict[str, Any]] = {}
    skipped: List[Dict[str, str]] = []

    for e in dedup["entities"]:
        layer = str(e.get("layer") or "")
        etype = str(e.get("type") or "")
        if layer not in ("L1", "L2"):
            skipped.append({"name": e.get("name", ""), "reason": f"layer={layer}"})
            continue
        if not SCHEMA.is_valid_node_type(etype):
            skipped.append({"name": e.get("name", ""), "reason": f"type={etype}"})
            continue
        nid = str(e.get("id") or "")
        if not nid or nid in nodes_by_id:
            skipped.append({"name": e.get("name", ""), "reason": "missing/duplicate id"})
            continue
        node = Node(
            id=nid,
            name=str(e.get("name") or ""),
            type=etype,
            layer=layer,
            aliases=[str(a) for a in (e.get("aliases") or []) if a],
            definition=str(e.get("definition") or ""),
            confidence=float(e.get("confidence", 1.0)),
            provenance=_prov(e.get("provenance") or [], doc_id),
            attributes={
                "signals": e.get("signals") or {},
                "layer_decision": e.get("layer_decision") or {},
                "merged_from": e.get("merged_from") or [],
            },
        )
        nodes_by_id[nid] = node
        node_by_name[node.name] = node
        entities[nid] = e

    # —— 语义边（抽取 + Pred）—— #
    edges: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    pair_types: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    def semantic_pair(src: str, tgt: str) -> Tuple[str, str]:
        return tuple(sorted((src, tgt)))  # type: ignore[return-value]

    # 1) 抽取/归并阶段的关系
    for r in dedup.get("relations") or []:
        src, tgt = str(r.get("source", "")), str(r.get("target", ""))
        if src not in nodes_by_id or tgt not in nodes_by_id:
            continue
        rel = SCHEMA.canonical_relation(str(r.get("type") or ""))
        if not rel:
            continue
        ls, lt = nodes_by_id[src].layer, nodes_by_id[tgt].layer
        if not SCHEMA.relation_allowed(rel, ls, lt):
            continue
        if SCHEMA.is_symmetric(rel) and src > tgt:
            src, tgt = tgt, src
        evids = []
        for p in r.get("provenance") or []:
            sid = str(p.get("section_id") or "")
            if sid in section_ev:
                evids.append(section_ev[sid])
        _add_edge(edges, seen, src, tgt, rel, f"{ls}-{lt}",
                  float(r.get("confidence", 0.5)),
                  str(r.get("description") or ""), r.get("keywords") or [],
                  list(dict.fromkeys(evids))[:5],
                  _prov(r.get("provenance") or [], doc_id))
        pair_types[semantic_pair(src, tgt)].add(rel)

    # 2) Pred 预测边：同一对节点已有语义边时不再叠加
    for p in pred.get("predictions") or []:
        src, tgt = str(p.get("source", "")), str(p.get("target", ""))
        if src not in nodes_by_id or tgt not in nodes_by_id:
            continue
        rel = SCHEMA.canonical_relation(str(p.get("type") or ""))
        if not rel:
            continue
        ls, lt = nodes_by_id[src].layer, nodes_by_id[tgt].layer
        if not SCHEMA.relation_allowed(rel, ls, lt):
            continue
        if rel != "instance_of" and pair_types.get(semantic_pair(src, tgt)):
            continue
        if SCHEMA.is_symmetric(rel) and src > tgt:
            src, tgt = tgt, src
        evids = [section_ev[s] for s in (p.get("section_ids") or []) if s in section_ev]
        _add_edge(edges, seen, src, tgt, rel, f"{ls}-{lt}",
                  float(p.get("confidence", 0.5)),
                  str(p.get("description") or ""), p.get("keywords") or [],
                  list(dict.fromkeys(evids))[:5],
                  _prov(p.get("provenance") or [], doc_id))
        pair_types[semantic_pair(src, tgt)].add(rel)

    # —— 跨层证据边 —— #
    def add_evidence_edge(node: Node, evid: str, rel: str, confidence: float,
                          section_id: str) -> None:
        _add_edge(edges, seen, node.id, evid, rel,
                  f"{node.layer}-L3", confidence,
                  "", [], [evid],
                  [Provenance(doc_id=doc_id, section_id=section_id,
                              extractor="final.v2")])

    for node in nodes_by_id.values():
        e = entities[node.id]
        occs = e.get("occurrences") or []
        used_sections: List[str] = []
        definition_sid = ""
        for o in occs:
            sid = str(o.get("section_id") or "")
            if sid and sid not in used_sections:
                used_sections.append(sid)
            if not definition_sid and o.get("definitional_cue"):
                definition_sid = sid
        for sid in used_sections:
            evid = section_ev.get(sid)
            if not evid:
                continue
            add_evidence_edge(node, evid, "appears_in", 0.6, sid)
        if node.layer == "L1":
            sid = definition_sid or (used_sections[0] if used_sections else "")
            evid = section_ev.get(sid)
            if evid:
                add_evidence_edge(node, evid, "described_by", 0.9, sid)

    # 3) 证据侧声明的实体（兜底，补齐 occurrences 未覆盖的提及）
    for ev in evidences:
        if ev.derived:
            continue
        for raw_name in ev.entities_in_section or []:
            name = merge_map.get(str(raw_name), str(raw_name))
            node = node_by_name.get(name)
            if not node:
                continue
            # 证据侧声明的实体只算“提及”；described_by 只留给定义句所在小节
            add_evidence_edge(node, ev.id, "appears_in", 0.55, ev.section_id)

    # —— 度数 / salience / evidence_ids 投影 —— #
    degree: Dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["source"]] += 1
        if e["target"] in nodes_by_id:
            degree[e["target"]] += 1

    incoming_ev: Dict[str, Set[str]] = defaultdict(set)
    for e in edges:
        if e["target"] in evidence_by_id and e["source"] in nodes_by_id:
            ev = evidence_by_id[e["target"]]
            if not (CFG["EXCLUDE_DERIVED_FROM_PROJECTION"] and ev.derived):
                incoming_ev[e["source"]].add(e["target"])

    for nid, node in nodes_by_id.items():
        node.salience = _salience(entities[nid], degree.get(nid, 0))
        node.evidence_ids = sorted(incoming_ev.get(nid, set()))

    # —— 装配 —— #
    l1 = sorted((n for n in nodes_by_id.values() if n.layer == "L1"), key=lambda n: n.name)
    l2 = sorted((n for n in nodes_by_id.values() if n.layer == "L2"), key=lambda n: n.name)
    kg = KnowledgeGraph(
        schema_version=SCHEMA.version,
        L0_schema=_l0(SCHEMA),
        L1_concepts=l1,
        L2_entities=l2,
        L3_evidences=evidences,
        edges=[Relation.from_dict(e) for e in edges],
    )
    kg_dict = kg.to_dict()
    kg_dict["doc_id"] = doc_id

    validation = validate(kg_dict)
    if validation.get("errors"):
        logger.warning(f"校验发现 {len(validation['errors'])} 个错误（前 5 条）")
        for msg in validation["errors"][:5]:
            logger.warning(f"  - {msg}")

    # —— build manifest —— #
    section_hashes: Dict[str, str] = {}
    for ev in evidences:
        if ev.derived:
            continue
        section_hashes.setdefault(ev.section_id, _sha1_bytes(ev.text.encode("utf-8")))
    config_hashes = {}
    for name in ("schema.v2.yaml", "extraction.yaml", "conv.yaml", "aggr.yaml",
                 "emb.yaml", "dedup.yaml", "pred.yaml"):
        p = CONFIG_DIR / name
        if p.exists():
            config_hashes[name] = _file_sha1(p)
    api = {}
    cfg_path = CONFIG_DIR / "config.yaml"
    if cfg_path.exists():
        try:
            api = (_load_yaml(cfg_path).get("APIConfig") or {})
        except Exception:
            api = {}
    embedding_model = ""
    storage = SCRIPT_DIR.parent / "KGRetrieve" / "config" / "storage.yaml"
    if storage.exists():
        try:
            embedding_model = str((_load_yaml(storage).get("ChromaConfig") or {})
                                  .get("EMBEDDING_MODEL") or "")
        except Exception:
            embedding_model = ""

    payload_for_hash = json.dumps(kg_dict, ensure_ascii=False, sort_keys=True).encode("utf-8")
    doc_text_path = ev_doc.get("doc_text_path")
    doc_hash = _file_sha1(Path(doc_text_path)) if doc_text_path else _file_sha1(evidence_path)
    manifest = {
        "schema_version": SCHEMA.version,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "doc_id": doc_id,
        "doc_hashes": {doc_id: doc_hash},
        "section_hashes": section_hashes,
        "config_hashes": config_hashes,
        "prompt_versions": {
            "extraction": "extraction.v2",
            "conv": "conv.v2",
            "aggr": "aggr.v2",
            "dedup": "dedup.v2",
            "pred": "pred.v2",
        },
        "model_ids": {
            "llm": str(api.get("MODEL_NAME") or ""),
            "embedding": embedding_model,
        },
        "final_kg_sha1": _sha1_bytes(payload_for_hash),
        "counts": {
            "L1_concepts": len(l1),
            "L2_entities": len(l2),
            "L3_evidences": len(evidences),
            "edges": len(edges),
            "edges_by_type": dict(Counter(e["type"] for e in edges).most_common()),
        },
        "skipped": skipped,
        "validation": {
            "ok": validation.get("ok", False),
            "errors": len(validation.get("errors") or []),
            "warnings": len(validation.get("warnings") or []),
        },
    }
    kg_dict["build_manifest"] = manifest

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(kg_dict, f, ensure_ascii=False, indent=2)
    manifest_path = out_path.parent / CFG["MANIFEST_NAME"]
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    logger.info(f"✅ FinalKG：{out_path.resolve()}")
    logger.info(f"  L1 {len(l1)} / L2 {len(l2)} / L3 {len(evidences)} / 边 {len(edges)}")
    logger.info(f"  边类型：{manifest['counts']['edges_by_type']}")
    logger.info(f"  校验：errors={manifest['validation']['errors']} "
                f"warnings={manifest['validation']['warnings']}")
    logger.info(f"  manifest：{manifest_path.resolve()}")
    return kg_dict


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="装配 v2 四层知识图谱")
    ap.add_argument("--dedup", default=None)
    ap.add_argument("--pred", default=None)
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--loglevel", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logger.setLevel(getattr(logging, args.loglevel))
    assemble(Path(args.dedup) if args.dedup else None,
             Path(args.pred) if args.pred else None,
             Path(args.evidence) if args.evidence else None,
             Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
