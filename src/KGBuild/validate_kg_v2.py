# -*- coding: utf-8 -*-
"""v2 图谱产物校验器 —— 构建完成后、导入 Neo4j 之前必须通过。

用法：
    python -m src.KGBuild.validate_kg_v2 --kg src/KGBuild/output/final_kg.json
    python -m src.KGBuild.validate_kg_v2 --kg ... --json report.json

校验内容对应 schema.v2.yaml 的 ValidationRules：
  * 节点：类型在闭集内、layer 合法、ID 唯一且前缀/哈希一致、待定节点不得入图、
    L1/L2 必须有证据、confidence / salience 范围
  * 边：端点在图中、关系名规范、端点层约束、对称边方向规范、无重复边、
    逆关系不得单独存储、confidence 范围、证据引用存在
  * 证据：section_level / block_type 在枚举内、文本非空、偏移合法
  * 图级：evidence_ids 投影与跨层边一致、无孤儿证据、schema_version 匹配
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .schema_loader import Schema, get_schema, load_schema, make_evidence_id, make_node_id

NODE_SECTIONS: Tuple[Tuple[str, str], ...] = (("L1_concepts", "L1"), ("L2_entities", "L2"))


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(kg: Dict[str, Any], schema: Optional[Schema] = None) -> Dict[str, Any]:
    schema = schema or get_schema()
    errors: List[str] = []
    warnings: List[str] = []

    def err(rule: str, msg: str) -> None:
        errors.append(f"[{rule}] {msg}")

    def warn(rule: str, msg: str) -> None:
        warnings.append(f"[{rule}] {msg}")

    if not isinstance(kg, dict):
        return {"ok": False, "errors": ["[graph] 顶层必须是 JSON 对象"],
                "warnings": [], "summary": {}}

    # ---------------- 顶层 ----------------
    version = kg.get("schema_version")
    if not version:
        err("schema_version", "缺少 schema_version")
    elif str(version) != schema.version:
        err("schema_version", f"schema_version={version!r}，期望 {schema.version!r}")
    if not isinstance(kg.get("L0_schema"), dict):
        warn("graph", "缺少 L0_schema")

    id_re = re.compile(schema.node_id_pattern) if schema.node_id_pattern else None

    # ---------------- 节点 ----------------
    node_by_id: Dict[str, Dict[str, Any]] = {}
    node_layer: Dict[str, str] = {}
    node_type: Dict[str, str] = {}

    for section, expected_layer in NODE_SECTIONS:
        items = kg.get(section)
        if items is None:
            err("node", f"缺少 {section}")
            continue
        if not isinstance(items, list):
            err("node", f"{section} 必须是数组")
            continue

        for idx, node in enumerate(items):
            loc = f"{section}[{idx}]"
            if not isinstance(node, dict):
                err("node", f"{loc} 不是对象")
                continue

            nid = node.get("id")
            if not nid or not isinstance(nid, str):
                err("node", f"{loc} 缺少 id")
                continue

            for field in ("name", "type", "layer", "aliases", "definition",
                          "confidence", "provenance"):
                if field not in node:
                    err("node", f"{nid} 缺少必填字段 {field}")

            if nid in node_by_id:
                err("id_unique", f"节点 ID 重复：{nid}")
                continue
            node_by_id[nid] = node
            node_layer[nid] = str(node.get("layer") or "")

            prefix = "concept_" if expected_layer == "L1" else "ent_"
            if not nid.startswith(prefix):
                err("layer_valid", f"{nid} 位于 {section}，但 ID 前缀不是 {prefix}")
            if id_re is not None and not id_re.match(nid):
                err("id_format", f"{nid} 不符合 ID 模式 {schema.node_id_pattern}")

            layer = node.get("layer")
            if layer != expected_layer:
                err("layer_valid", f"{nid} 的 layer={layer!r}，与所在层 {expected_layer} 不符")
            if layer == "excluded":
                err("layer_valid", f"{nid} 是 excluded 节点，不应出现在 L1/L2 容器")

            ntype = node.get("type")
            if not isinstance(ntype, str) or not schema.is_valid_node_type(ntype):
                err("type_in_schema", f"{nid} 的类型 {ntype!r} 不在 schema 闭集内")
                node_type[nid] = str(ntype or "")
            else:
                node_type[nid] = ntype
                nt = schema.get_node_type(ntype)
                if not nt.resolved and layer != "excluded":
                    err("no_unresolved_in_graph", f"{nid} 是待定类型，但仍进入 {layer}")

            conf = node.get("confidence")
            if not _is_num(conf) or not (0.0 <= float(conf) <= 1.0):
                err("confidence_range", f"{nid} 的 confidence={conf!r} 不在 [0,1]")

            if not str(node.get("name") or "").strip():
                err("node", f"{nid} 的 name 为空")

            # 稳定 ID：name → sha1 前缀；改名/合并走 registry 时允许不一致，故只告警
            if isinstance(node.get("name"), str):
                expected_id = make_node_id(node["name"], expected_layer)
                if expected_id != nid:
                    warn("id_stable_hash", f"{nid} 与 name={node['name']!r} 推出的 {expected_id} 不一致")

            prov = node.get("provenance")
            if not isinstance(prov, list):
                err("node", f"{nid} 的 provenance 必须是数组")

            sal = node.get("salience")
            if sal is not None:
                if not isinstance(sal, dict):
                    err("node", f"{nid} 的 salience 必须是对象")
                else:
                    score = sal.get("score")
                    if score is not None and (not _is_num(score) or not (0.0 <= float(score) <= 1.0)):
                        err("salience", f"{nid} 的 salience.score={score!r} 不在 [0,1]")

    # ---------------- 证据 ----------------
    evidence_by_id: Dict[str, Dict[str, Any]] = {}
    evidence_section_levels = set(schema.evidence_section_levels)
    evidence_block_types = set(schema.evidence_block_types)

    evidences = kg.get("L3_evidences")
    if evidences is None:
        err("evidence", "缺少 L3_evidences")
        evidences = []
    if not isinstance(evidences, list):
        err("evidence", "L3_evidences 必须是数组")
        evidences = []

    for idx, ev in enumerate(evidences):
        loc = f"L3_evidences[{idx}]"
        if not isinstance(ev, dict):
            err("evidence", f"{loc} 不是对象")
            continue
        eid = ev.get("id")
        if not eid or not isinstance(eid, str):
            err("evidence", f"{loc} 缺少 id")
            continue
        if eid in evidence_by_id:
            err("id_unique", f"证据 ID 重复：{eid}")
            continue
        evidence_by_id[eid] = ev

        for field in ("doc_id", "section_id", "section_path", "section_level",
                      "title", "text"):
            if field not in ev:
                err("evidence", f"{eid} 缺少必填字段 {field}")

        if not str(ev.get("text") or "").strip():
            err("text_nonempty", f"{eid} 的 text 为空")

        level = ev.get("section_level")
        if level not in evidence_section_levels:
            err("evidence", f"{eid} 的 section_level={level!r} 不在枚举内")

        btype = ev.get("block_type")
        if btype is not None and btype not in evidence_block_types:
            err("evidence", f"{eid} 的 block_type={btype!r} 不在枚举内")

        if "char_start" in ev or "char_end" in ev:
            start, end = ev.get("char_start"), ev.get("char_end")
            if not (isinstance(start, int) and isinstance(end, int)):
                err("offsets_valid", f"{eid} 的 char_start/char_end 必须同时为整数")
            elif not (0 <= start <= end):
                err("offsets_valid", f"{eid} 的偏移非法：{start}..{end}")

        if eid.startswith("ev_") and isinstance(ev.get("doc_id"), str) \
                and isinstance(ev.get("section_id"), str):
            expected = make_evidence_id(ev["doc_id"], ev["section_id"],
                                        str(ev.get("block_id") or ""))
            if expected != eid:
                warn("id_format", f"{eid} 与字段推出的 {expected} 不一致")

    # ---------------- 边 ----------------
    edges = kg.get("edges")
    if edges is None:
        err("graph", "缺少 edges")
        edges = []
    if not isinstance(edges, list):
        err("graph", "edges 必须是数组")
        edges = []

    edge_seen: Set[Tuple[str, str, str]] = set()
    edge_types: Counter = Counter()
    incoming_evidence: Dict[str, Set[str]] = defaultdict(set)
    edge_count = 0

    def layer_of(node_id: str) -> Optional[str]:
        if node_id in node_layer:
            return node_layer[node_id]
        if node_id in evidence_by_id:
            return "L3"
        return None

    for idx, edge in enumerate(edges):
        loc = f"edges[{idx}]"
        if not isinstance(edge, dict):
            err("relation", f"{loc} 不是对象")
            continue

        for field in ("source", "target", "type", "layer", "confidence", "provenance"):
            if field not in edge:
                err("relation", f"{loc} 缺少必填字段 {field}")

        src, tgt = edge.get("source"), edge.get("target")
        rel = edge.get("type")
        if not all(isinstance(x, str) and x for x in (src, tgt, rel)):
            err("relation", f"{loc} 的 source/target/type 非法")
            continue

        if src == tgt:
            err("no_self_loop", f"{src} --{rel}--> 自身")

        src_layer, tgt_layer = layer_of(src), layer_of(tgt)
        if src_layer is None:
            err("edge_endpoints_exist", f"{loc} source 不存在：{src}")
        if tgt_layer is None:
            err("edge_endpoints_exist", f"{loc} target 不存在：{tgt}")

        is_derived_inverse = rel in schema.derived_inverses
        if is_derived_inverse:
            err("relation", f"{loc} 存储了派生逆关系 {rel}，应只存 "
                            f"{schema.derived_inverses[rel].get('inverse_of')}")

        canonical = None if is_derived_inverse else schema.canonical_relation(rel)
        if not is_derived_inverse and canonical is None:
            err("type_in_schema", f"{loc} 的关系 {rel!r} 不在闭集内")
        elif canonical != rel:
            warn("type_in_schema", f"{loc} 的关系名 {rel!r} 未归一，应为 {canonical!r}")

        if canonical and src_layer and tgt_layer:
            expected_layer = f"{src_layer}-{tgt_layer}"
            if edge.get("layer") != expected_layer:
                err("endpoint_policy", f"{loc} layer={edge.get('layer')!r}，"
                                       f"端点实际为 {expected_layer}")
            if not schema.relation_allowed(canonical, src_layer, tgt_layer):
                err("endpoint_policy", f"{loc} {canonical} 不允许 {src_layer}→{tgt_layer}")

            if schema.is_symmetric(canonical) and src > tgt:
                warn("symmetric_canonical", f"{loc} 对称边未按字符串序规范：{src} → {tgt}")

            rt = schema.get_relation(canonical)
            if rt and rt.is_fallback:
                conf = edge.get("confidence")
                if _is_num(conf) and float(conf) < rt.min_confidence:
                    err("confidence_range", f"{loc} 兜底关系 {canonical} 的 confidence="
                                            f"{conf} 低于阈值 {rt.min_confidence}")

        conf = edge.get("confidence")
        if not _is_num(conf) or not (0.0 <= float(conf) <= 1.0):
            err("confidence_range", f"{loc} 的 confidence={conf!r} 不在 [0,1]")

        if not isinstance(edge.get("provenance"), list):
            err("relation", f"{loc} 的 provenance 必须是数组")

        if canonical:
            key = (src, tgt, canonical)
            if key in edge_seen:
                err("no_duplicate", f"{loc} 重复边 {src} --{canonical}--> {tgt}")
            edge_seen.add(key)
            edge_types[canonical] += 1
            edge_count += 1

        cited = edge.get("evidence_ids") or []
        if not isinstance(cited, list):
            err("relation", f"{loc} 的 evidence_ids 必须是数组")
            cited = []
        for eid in cited:
            if eid not in evidence_by_id:
                err("relation", f"{loc} 引用了不存在的证据 {eid}")

        if tgt_layer == "L3" and src_layer in ("L1", "L2"):
            incoming_evidence[src].add(tgt)
        elif src_layer in ("L1", "L2") and tgt_layer in ("L1", "L2") \
                and canonical not in ("instance_of",) and not cited:
            warn("evidence_backed", f"{loc} {src} --{canonical}--> {tgt} 没有证据引用")

    # ---------------- 图级 ----------------
    for nid, layer in node_layer.items():
        if layer not in ("L1", "L2"):
            continue
        node = node_by_id[nid]
        declared = set(node.get("evidence_ids") or [])
        expected: Set[str] = set()
        for eid in incoming_evidence.get(nid, set()):
            ev = evidence_by_id.get(eid) or {}
            if not ev.get("derived", False):
                expected.add(eid)
        if declared != expected:
            missing = expected - declared
            extra = declared - expected
            detail = []
            if missing:
                detail.append(f"缺 {len(missing)} 条（如 {sorted(missing)[0]}）")
            if extra:
                detail.append(f"多 {len(extra)} 条（如 {sorted(extra)[0]}）")
            err("evidence_projection_consistent",
                f"{nid} 的 evidence_ids 投影不一致：{'；'.join(detail) or '集合不同'}")
        if not expected and not declared:
            err("node_has_evidence", f"{nid}（{layer}）没有任何证据")

    referenced: Set[str] = set()
    for targets in incoming_evidence.values():
        referenced |= targets
    for eid, ev in evidence_by_id.items():
        if eid not in referenced and not ev.get("derived", False):
            warn("no_orphan_evidence", f"证据 {eid} 没有被任何 L1/L2 节点引用")

    summary = {
        "schema_version": version,
        "concepts": sum(1 for n in node_by_id if node_layer[n] == "L1"),
        "entities": sum(1 for n in node_by_id if node_layer[n] == "L2"),
        "evidences": len(evidence_by_id),
        "edges": edge_count,
        "edge_types": dict(edge_types.most_common()),
        "errors": len(errors),
        "warnings": len(warnings),
    }
    return {"ok": not errors, "summary": summary,
            "errors": errors, "warnings": warnings}


def validate_file(kg_path: str, schema_path: Optional[str] = None) -> Dict[str, Any]:
    schema = get_schema() if not schema_path else load_schema(schema_path)
    with open(kg_path, "r", encoding="utf-8") as f:
        kg = json.load(f)
    return validate(kg, schema)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="校验 v2 final_kg.json")
    ap.add_argument("--kg", default="src/KGBuild/output/final_kg.json")
    ap.add_argument("--schema", default=None, help="自定义 schema 路径（默认 config/schema.v2.yaml）")
    ap.add_argument("--json", default=None, help="把完整报告写到指定 JSON 文件")
    ap.add_argument("--max-print", type=int, default=50)
    args = ap.parse_args(argv)

    report = validate_file(args.kg, args.schema)
    summary = report.get("summary") or {}

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"KG: {args.kg}")
    print(f"schema_version: {summary.get('schema_version')}")
    print(f"节点: L1={summary.get('concepts', 0)} "
          f"L2={summary.get('entities', 0)} "
          f"L3={summary.get('evidences', 0)}")
    print(f"边: {summary.get('edges', 0)}")
    for rel, n in (summary.get("edge_types") or {}).items():
        print(f"  {rel}: {n}")
    print(f"errors={len(report['errors'])} warnings={len(report['warnings'])}")

    for item in report["errors"][:args.max_print]:
        print(f"  ERROR   {item}")
    if len(report["errors"]) > args.max_print:
        print(f"  ... 还有 {len(report['errors']) - args.max_print} 条错误")
    for item in report["warnings"][:args.max_print]:
        print(f"  WARN    {item}")
    if len(report["warnings"]) > args.max_print:
        print(f"  ... 还有 {len(report['warnings']) - args.max_print} 条告警")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
