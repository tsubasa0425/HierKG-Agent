# -*- coding: utf-8 -*-
"""图谱数据纪律校验：evidence_ids（投影）必须与跨层边（canonical）一致。

用法:  python -m eval.validate_kg [--kg <final_kg.json>]
退出码: 0 = 投影与边一致（可能有孤儿证据警告）；1 = 存在投影漂移。

为什么存在：get_evidences_of_node 与检索传播都改成了「以边为权威、只读边」，
evidence_ids 只是边的投影缓存。本脚本守护这条不变量——一旦构建过程让两者
漂移（节点上多了/少了证据而边表没同步），检索会漏证据或引用失真，必须报警。

同时报告：
  - 每个概念/实体 evidence_ids 与其跨层出边 target 集合的差集（漂移明细）
  - 没有任何跨层边可达的「孤儿证据」——只能靠向量搜索命中，图导航到不了
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

_CROSS_LAYERS = ("L1-L3", "L2-L3")


def _evid(n: Dict[str, Any]) -> List[str]:
    return list(n.get("evidence_ids") or [])


def validate(kg: Dict[str, Any]) -> Dict[str, Any]:
    edges = kg["edges"]
    cross_by_source: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        if e.get("layer") in _CROSS_LAYERS:
            cross_by_source.setdefault(e["source"], []).append(e)

    drift: List[Dict[str, Any]] = []
    nodes_checked = 0
    for layer, idkey in (("L1_concepts", "concept_id"), ("L2_entities", "entity_id")):
        for n in kg[layer]:
            nid = n[idkey]
            evids = set(_evid(n))
            edge_targets = {e["target"] for e in cross_by_source.get(nid, [])}
            nodes_checked += 1
            if evids != edge_targets:
                drift.append({
                    "node_id": nid, "layer": layer,
                    "only_in_evidence_ids": sorted(evids - edge_targets),
                    "only_in_edges": sorted(edge_targets - evids),
                })

    reachable = {e["target"] for e in edges if e.get("layer") in _CROSS_LAYERS}
    all_ev = [e["evidence_id"] for e in kg["L3_evidences"]]
    orphans = [eid for eid in all_ev if eid not in reachable]

    return {
        "nodes_checked": nodes_checked,
        "n_drift": len(drift),
        "drift": drift,
        "n_evidence": len(all_ev),
        "n_orphans": len(orphans),
        "orphans": orphans,
        "reachable_ratio": (len(all_ev) - len(orphans)) / len(all_ev) if all_ev else 1.0,
    }


def main(argv=None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="校验 evidence_ids(投影) 与跨层边(canonical) 一致性")
    default_kg = Path(__file__).resolve().parent.parent / "src/KGBuild/output/final_kg.json"
    ap.add_argument("--kg", default=str(default_kg))
    args = ap.parse_args(argv)

    kg = json.loads(Path(args.kg).read_text(encoding="utf-8"))
    r = validate(kg)
    print(f"节点校验: {r['nodes_checked']} 个，投影漂移 {r['n_drift']} 个")
    for d in r["drift"]:
        print(f"  ⚠ {d['node_id']} ({d['layer']}): "
              f"仅 evidence_ids={d['only_in_evidence_ids']} 仅边={d['only_in_edges']}")
    print(f"证据: {r['n_evidence']} 个，孤儿（无跨层边可达）{r['n_orphans']} 个，"
          f"图可达率 {r['reachable_ratio']:.1%}")
    if r["orphans"]:
        print("  孤儿证据: " + ", ".join(r["orphans"]))

    if r["n_drift"]:
        print("✗ 存在投影漂移：evidence_ids 与跨层边不一致，请修构建逻辑")
        return 1
    if r["n_orphans"]:
        print("⚠ evidence_ids 与跨层边完全一致；有孤儿证据（非漂移，但图导航到不了它们）")
    else:
        print("✓ evidence_ids 与跨层边完全一致，图可达全部证据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
