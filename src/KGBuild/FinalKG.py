"""
FinalKG.py —— 四层知识图谱装配器

按 L0-L3 四层模型装配最终图谱，输出 final_kg.json：
  - L0_schema        : 来自 config/schema.yaml（本体层）
  - L1_concepts      : layer==concept 的节点（概念层）
  - L2_entities      : layer==entity  的节点（实体层）
  - L3_evidences     : 来自 evidence.json（证据层）
  - edges            : 同层关系 + 跨层关系，标注 layer 字段

输入：
  - src/KGBuild/config/schema.yaml
  - src/KGBuild/output/dedup_result.json
  - src/KGBuild/output/pred_result.json
  - src/KGBuild/output/evidence.json
输出：
  - src/KGBuild/output/final_kg.json
"""

from __future__ import annotations
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

# === 目录锚点 ===
HERE = Path(__file__).resolve().parent      # .../src/KGBuild
SRC = HERE.parent                            # .../src
EXPLICIT_OUT = HERE / "output"
HIDDEN_OUT = HERE / "output"

SCHEMA_FILE = HERE / "config" / "schema.yaml"  # src/KGBuild/config/schema.yaml
DEDUP_FILE = HIDDEN_OUT / "dedup_result.json"
PRED_FILE = HIDDEN_OUT / "pred_result.json"
EVIDENCE_FILE = EXPLICIT_OUT / "evidence.json"
FINAL_FILE = HIDDEN_OUT / "final_kg.json"


def read_json(p: Path) -> Any:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_yaml(p: Path) -> dict:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _ent_id(name: str) -> str:
    """为 L2 实体生成稳定 ID"""
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return f"ent_{h}"


def load_l0_schema(schema: dict) -> dict:
    """从 schema.yaml 抽取 L0 本体（实体类型 + 关系类型）"""
    entity_types = []
    for t, meta in (schema.get("EntityTypes") or {}).items():
        item = {"type": t, "layer_hint": meta.get("layer_hint", "auto")}
        if meta.get("description"):
            item["description"] = meta["description"]
        if meta.get("examples"):
            item["examples"] = meta["examples"]
        entity_types.append(item)
    return {
        "entity_types": entity_types,
        "relation_types": schema.get("RelationTypes") or {},
    }


def build_section_to_evidence(evidences: List[dict]) -> Dict[str, str]:
    """section_id → evidence_id 索引"""
    idx = {}
    for ev in evidences:
        sid = (ev.get("section_id") or "").strip()
        if sid:
            idx[sid] = ev["evidence_id"]
    return idx


def assemble():
    # 输入校验
    for p in (SCHEMA_FILE, DEDUP_FILE, EVIDENCE_FILE):
        if not p.exists():
            raise FileNotFoundError(f"missing {p}")

    schema = read_yaml(SCHEMA_FILE)
    dedup = read_json(DEDUP_FILE)
    entities: Dict[str, dict] = dedup.get("entities", {}) if isinstance(dedup, dict) else {}
    pred_edges: List[dict] = read_json(PRED_FILE) if PRED_FILE.exists() else []
    evidences: List[dict] = read_json(EVIDENCE_FILE)

    # L0
    l0 = load_l0_schema(schema)

    # 节点名 → layer / 节点ID 映射
    name_to_layer: Dict[str, str] = {}
    name_to_node_id: Dict[str, str] = {}
    concept_ids: set = set()

    l1_concepts: List[dict] = []
    l2_entities: List[dict] = []

    for name, ent in entities.items():
        layer = (ent.get("layer") or "concept").strip().lower()
        concept_id = (ent.get("concept_id") or "").strip()
        aliases = ent.get("alias") or []
        desc = (ent.get("updated_description") or ent.get("original") or "").strip()

        if layer == "entity":
            eid = _ent_id(name)
            name_to_layer[name] = "entity"
            name_to_node_id[name] = eid
            l2_entities.append({
                "entity_id": eid,
                "name": name,
                "type": ent.get("type", ""),
                "concept_id": concept_id,
                "aliases": aliases,
                "description": desc,
                "attributes": {},
                "evidence_ids": [],  # 下面填充
            })
        else:
            cid = concept_id or f"concept_{hashlib.md5(name.encode('utf-8')).hexdigest()[:8]}"
            concept_ids.add(cid)
            name_to_layer[name] = "concept"
            name_to_node_id[name] = cid
            l1_concepts.append({
                "concept_id": cid,
                "name": name,
                "type": ent.get("type", ""),
                "aliases": aliases,
                "description": desc,
                "evidence_ids": [],  # 下面填充
            })

    # L3
    l3_evidences: List[dict] = []
    for ev in evidences:
        l3_evidences.append({
            "evidence_id": ev.get("evidence_id", ""),
            "doc_id": ev.get("doc_id", ""),
            "section_id": ev.get("section_id", ""),
            "section_path": ev.get("section_path", ""),
            "section_level": ev.get("section_level", ""),
            "title": ev.get("title", ""),
            "summary": ev.get("title", ""),
            "snippet": ev.get("content", ""),
            "entities_in_section": ev.get("entities_in_section") or [],
        })

    sec2ev = build_section_to_evidence(l3_evidences)

    # 3.5) 父级证据补边：章/节导语补齐 entities_in_section。
    #     根因：Extraction.collect_subsections 只把「没有子节点的节」送进 extract_entities，
    #     章（level1）与带子节的节（如 1.1 / 2.1）整个被跳过——它们的导语内容有正文
    #     （267-536 字）却从未被扫描实体 → entities_in_section 为空 → 无跨层边 → 孤儿证据，
    #     图导航到不了、只能靠向量命中。
    #     修法：空 entities_in_section 的父级证据，按 section_id 层级从「后代证据的
    #     entities_in_section」取并集继承（1章→1.x…，1.1→1.1.x…），pass 4 自然建边。
    #     从快照读取，避免中间派生互相级联（顺序无关）。叶子节无后代 → 不变。
    #     派生边在 pass 4 一律降权为 appears_in（章/节导语只是概述「提到」其下概念，
    #     不是定义，不应带 described_by 的 1.0 定义权重注入检索）。
    _orig_ent = {e["evidence_id"]: list(e.get("entities_in_section") or [])
                 for e in l3_evidences}
    _derived: set = set()   # 被 3.5 派生的证据 ID（pass 4 据此降权）
    for ev in l3_evidences:
        if _orig_ent.get(ev["evidence_id"]):
            continue
        prefix = (ev.get("section_id") or "").rstrip("章") + "."
        child_ents = set()
        for other in l3_evidences:
            osid = other.get("section_id") or ""
            if osid != ev.get("section_id") and osid.startswith(prefix):
                child_ents.update(_orig_ent.get(other["evidence_id"]) or [])
        if child_ents:
            ev["entities_in_section"] = sorted(child_ents)
            ev["derived"] = True      # 打标：派生导语只做图导航锚点，不进检索投影（见 pass 5）
            _derived.add(ev["evidence_id"])

    # === 边装配 ===
    edges: List[dict] = []
    edge_set: set = set()

    def add_edge(src: str, tgt: str, typ: str, layer: str,
                 desc: str = "", keywords: List[str] | None = None):
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, typ, layer)
        if key in edge_set:
            return
        item = {"source": src, "target": tgt, "type": typ, "layer": layer}
        if desc:
            item["description"] = desc
        if keywords:
            item["keywords"] = keywords
        edges.append(item)
        edge_set.add(key)

    # 1) 同层关系：来自 pred_result（按两端节点 layer 标注）
    for e in pred_edges or []:
        u = (e.get("u") or "").strip()
        v = (e.get("v") or "").strip()
        if u not in name_to_node_id or v not in name_to_node_id:
            continue
        lu = name_to_layer[u]
        lv = name_to_layer[v]
        if lu == "concept" and lv == "concept":
            layer_tag = "L1-L1"
        elif lu == "entity" and lv == "entity":
            layer_tag = "L2-L2"
        else:
            layer_tag = "L1-L2"
        llm = e.get("llm") or {}
        typ = (llm.get("type") or "related").strip()
        desc = (llm.get("description") or "").strip()
        keywords = llm.get("keywords") or []
        add_edge(name_to_node_id[u], name_to_node_id[v], typ, layer_tag, desc, keywords)

    # 2) 跨层 L2→L1：instance_of（实体归属概念）
    for ent in l2_entities:
        cid = ent.get("concept_id") or ""
        if cid and cid in concept_ids:
            add_edge(ent["entity_id"], cid, "instance_of", "L2-L1",
                     desc="L2 实体归属 L1 概念")

    # 3) 跨层 L1→L3 / L2→L3：appears_in
    #    依据：实体 occurrences 的 node_id(=section_id) → evidence_id
    #    注意：evidence_ids 不在此回填，统一在 pass 4 之后从跨层边派生（见 #5）——
    #    同一节点会经 occurrence（本 pass）与 entities_in_section（pass 4）两条路径挂边，
    #    两条路径各自回填必漏一条造成投影漂移（2026-08-25 实测 20 个漂移节点）。

    def _link_to_evidence(node_id: str, layer_name: str, occurrences: List[dict]):
        for occ in occurrences or []:
            sid = (occ.get("node_id") or "").strip()
            evid = sec2ev.get(sid)
            if not evid:
                continue
            # L1→L3 用 described_by，L2→L3 用 appears_in（区分概念定义出处 vs 实体出现处）
            rel = "described_by" if layer_name == "concept" else "appears_in"
            add_edge(node_id, evid, rel, f"L{1 if layer_name=='concept' else 2}-L3")

    for c in l1_concepts:
        _link_to_evidence(c["concept_id"], "concept",
                          entities.get(c["name"], {}).get("occurrences") or [])
    for e2 in l2_entities:
        _link_to_evidence(e2["entity_id"], "entity",
                          entities.get(e2["name"], {}).get("occurrences") or [])

    # 4) 补充：evidence.entities_in_section → 反向连接（确保证据侧声明的实体也被挂上）
    #    3.5 派生的父级证据（_derived）边一律 appears_in：章/节导语只概述「提到」其下
    #    概念，不是定义，降为提及级（0.5），避免最强权重注入噪声。
    for ev in l3_evidences:
        evid = ev["evidence_id"]
        derived = evid in _derived
        for nm in ev.get("entities_in_section") or []:
            if nm not in name_to_node_id:
                continue
            node_id = name_to_node_id[nm]
            layer_name = name_to_layer[nm]
            rel = "appears_in" if (derived or layer_name == "entity") else "described_by"
            add_edge(node_id, evid, rel, f"L{1 if layer_name=='concept' else 2}-L3")

    # 5) 统一回填 evidence_ids：从跨层边派生（边为唯一权威，evidence_ids 只是投影缓存）。
    #    全部跨层边（L1-L3/L2-L3）建完后重算，保证 evidence_ids 恒等于节点跨层出边
    #    target 集合 —— eval/validate_kg.py 守护的不变量，缺了会漏证据/引用失真。
    #    2026-08-25 例外：_derived（3.5 步派生的父级导语证据）只进边、不进投影——
    #    它们只是图导航锚点，若进 evidence_ids 会被检索传播当候选证据，排挤真实
    #    叶子证据（实测 0005/0009/0014 Recall@10 掉 5.4pp）。投影=检索、边=导航，
    #    两条职责分离。validate_kg 已同步豁免派生证据。
    cross_layers = ("L1-L3", "L2-L3")
    evidence_by_node: Dict[str, set] = {}
    for e in edges:
        if e["layer"] in cross_layers and e["target"] not in _derived:
            evidence_by_node.setdefault(e["source"], set()).add(e["target"])
    for c in l1_concepts:
        c["evidence_ids"] = sorted(evidence_by_node.get(c["concept_id"], set()))
    for e2 in l2_entities:
        e2["evidence_ids"] = sorted(evidence_by_node.get(e2["entity_id"], set()))

    # === 输出 ===
    kg = {
        "L0_schema": l0,
        "L1_concepts": l1_concepts,
        "L2_entities": l2_entities,
        "L3_evidences": l3_evidences,
        "edges": edges,
    }
    FINAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FINAL_FILE.open("w", encoding="utf-8") as f:
        json.dump(kg, f, ensure_ascii=False, indent=2)

    # 统计
    print(f"✅ 输出: {FINAL_FILE.resolve()}")
    print(f"- L0 实体类型: {len(l0['entity_types'])} 种")
    print(f"- L1 概念节点: {len(l1_concepts)}")
    print(f"- L2 实体节点: {len(l2_entities)}")
    print(f"- L3 证据节点: {len(l3_evidences)}")
    print(f"- 总边数: {len(edges)}")
    by_layer = Counter(e["layer"] for e in edges)
    for k, v in sorted(by_layer.items()):
        print(f"  · {k}: {v}")


if __name__ == "__main__":
    assemble()
