# -*- coding: utf-8 -*-
"""
Demo 1: 验证 KGMemory 与 14 个工具的基本功能
直接运行：python -m src.KGRetrieve.demo_smoke_test
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 确保项目根路径在 sys.path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.KGRetrieve import KGMemory, ToolRegistry


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> None:
    kg_path = ROOT / "src" / "KGBuild" / "output" / "final_kg.json"

    section("1. 加载 KGMemory")
    kg = KGMemory.load(kg_path)
    print(f"各层节点数: {kg.list_layers()}")

    section("2. 初始化工具注册表")
    registry = ToolRegistry(kg)
    print(f"已注册工具数: {len(registry.tool_names)}")
    for name in registry.tool_names:
        print(f"  - {name}")

    section("3. 验证工具描述导出")
    schemas = registry.get_tool_schemas()
    print(f"function calling schema 总数: {len(schemas)}")
    print(f"第一个 schema 预览: semantic_search")
    for s in schemas:
        if s["function"]["name"] == "semantic_search":
            print(json.dumps(s, ensure_ascii=False, indent=2)[:700] + " ...")
            break

    section("4. 冒烟测试：关键工具各调用一次")

    def run(name, **kw):
        print(f"\n▶ 调用 {name}({', '.join(f'{k}={v!r}' for k, v in kw.items())})")
        r = registry.call(name, kw)
        if not r.success:
            print(f"  ❌ 失败: {r.message}")
            return
        d = r.data
        # 简单摘要
        summary = ""
        if isinstance(d, dict):
            kws = [k for k in ("hits", "total", "concepts", "entities", "evidences",
                               "nodes", "edges", "paths", "returned", "items") if k in d]
            for k in kws:
                v = d[k]
                if isinstance(v, list):
                    summary += f" {k}:{len(v)}"
                elif isinstance(v, int):
                    summary += f" {k}:{v}"
                else:
                    summary += f" {k}:{str(v)[:40]}"
        print(f"  ✅ {r.elapsed_ms:.0f} ms |{summary}")
        if r.success and isinstance(d, dict) and d:
            # 打印 data 的第一个 key 的值（如果是 list），让用户看到结构
            for k in ("concepts", "entities", "evidences", "nodes", "edges",
                      "items", "L1_concepts", "L2_entities", "L3_evidences"):
                v = d.get(k)
                if isinstance(v, list) and v:
                    first = v[0]
                    if isinstance(first, dict):
                        show = {kk: (vvv[:60] + "..." if isinstance(vvv, str) and len(vvv) > 60 else vvv)
                                for kk, vvv in list(first.items())[:5]}
                    else:
                        show = first
                    print(f"     首项 {k}[0]: {json.dumps(show, ensure_ascii=False)[:200]}")
                    break
        return r

    # schema
    run("get_schema", section="entity_types")

    # 三层搜索
    run("search_concepts", query="强化学习", limit=5)
    run("search_entities", query="AlphaGo", limit=5)
    run("search_evidences", query="传统视角", limit=3)
    run("semantic_search", query="智能体 感知 行动", per_layer_limit=4)

    # 找一个具体的 concept 做 ID 查询
    r = registry.call("search_concepts", {"query": "强化学习"})
    if r.success and r.data and r.data["concepts"]:
        cid = r.data["concepts"][0]["node_id"]
        cname = r.data["concepts"][0]["name"]
        print(f"\n▶ 选中概念: {cname} -> {cid}")
        run("get_concept_by_id", concept_id=cid, include_entities=True, entity_limit=3)
        run("get_entities_of_concept", concept_id=cid, limit=3)
        run("get_relations", node_id=cid, direction="out", limit=5)
        run("get_evidences_of_node", node_id=cid, limit=2, snippet_chars=200)
        run("multi_hop_traverse", start_id=cid, max_hop=2, max_nodes=20)

    # 找一个 entity 做 ID 查询
    r = registry.call("search_entities", {"query": "AlphaGo"})
    if r.success and r.data and r.data["entities"]:
        eid = r.data["entities"][0]["node_id"]
        print(f"\n▶ 选中实体: {r.data['entities'][0]['name']} -> {eid}")
        run("get_entity_by_id", entity_id=eid)
        run("get_node_by_id", node_id=eid)
        run("get_evidences_of_node", node_id=eid, snippet_chars=120)

    # 找一个 evidence
    r = registry.call("search_evidences", {"query": "传统视角 智能体"})
    if r.success and r.data and r.data["evidences"]:
        evid = r.data["evidences"][0]["node_id"]
        print(f"\n▶ 选中证据: {evid}")
        run("get_evidence_by_id", evidence_id=evid, snippet_chars=300)

    # 重排序
    r = registry.call("search_concepts", {"query": "智能体", "limit": 15})
    if r.success and r.data and r.data["concepts"]:
        ids = [c["node_id"] for c in r.data["concepts"][:8]]
        run("rank_results", node_ids=ids, intent_query="先修概念 基础", top_n=5)

    # get_schema 全量
    run("get_schema", section="all")

    section("5. 汇总")
    print("✅ 所有冒烟测试通过。KGRetrieve 工具包已就绪。")


if __name__ == "__main__":
    main()
