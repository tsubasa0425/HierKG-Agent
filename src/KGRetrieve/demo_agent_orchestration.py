# -*- coding: utf-8 -*-
"""
Demo 2: 模拟一个 Agent，自主多轮编排调用 KGRetrieve 工具解决问题
===================================================================

场景：用户问「学习型智能体和强化学习有什么关系？它有哪些具体实例？」
      这是一个跨概念的问题，需要 Agent 自己决定：
        1. 先搜概念（学习型智能体 / 强化学习）
        2. 查它们之间的关系（get_relations / multi_hop_traverse）
        3. 找出「学习型智能体」下挂的具体实例（get_entities_of_concept）
        4. 拉取证据溯源（get_evidences_of_node）
        5. 对结果做二次重排序（rank_results）

这个 Demo 是「硬编码」一个伪 Agent 的决策顺序，演示给 Pi Agent 看：
    「你可以像这样自由地选择工具、决定调用几轮、自己编排流程。」
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.KGRetrieve import KGMemory, ToolRegistry
from src.KGRetrieve.tools import ToolResult


def log_step(n: int, thought: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"🤖 Step {n} · Thought: {thought}")
    print(f"{'─' * 60}")


def call_and_show(reg: ToolRegistry, name: str, **params) -> ToolResult:
    pstr = ", ".join(f"{k}={v!r}" for k, v in params.items())
    print(f"  🔧 Tool: {name}({pstr})")
    r = reg.call(name, params)
    if r.success:
        summary = f"ok · {r.elapsed_ms:.0f} ms"
        d = r.data
        if isinstance(d, dict):
            extras = []
            for k, v in d.items():
                if isinstance(v, list):
                    extras.append(f"{k}={len(v)}")
                elif isinstance(v, int):
                    extras.append(f"{k}={v}")
            if extras:
                summary += " | " + ", ".join(extras)
        print(f"  ✅ {summary}")
    else:
        print(f"  ❌ {r.message}")
    return r


def main() -> None:
    kg_path = ROOT / "src" / "KGBuild" / "output" / "final_kg.json"
    kg = KGMemory.load(kg_path)
    reg = ToolRegistry(kg)

    # —— 把可用工具列表展示给 Agent ——
    print("📚 Agent 可用工具清单：")
    print(reg.get_tool_docs())
    print()
    print("🎯 User Question: 学习型智能体和强化学习有什么关系？它有哪些具体实例？")

    state = {
        "question": "学习型智能体和强化学习有什么关系？它有哪些具体实例？",
        "concept_ids": [],
        "entity_ids": [],
        "evidence_ids": [],
        "relation_paths": [],
    }

    # ---------------------------------------------------------------
    # Step 1: 先搜索两个概念
    # ---------------------------------------------------------------
    log_step(1, "用户提到了『学习型智能体』和『强化学习』两个术语。先搜概念层把它们对齐到 concept_id。")
    r1 = call_and_show(reg, "search_concepts", query="学习型智能体", limit=5)
    r2 = call_and_show(reg, "search_concepts", query="强化学习", limit=5)

    if r1.success and r1.data["concepts"]:
        c1 = r1.data["concepts"][0]
        state["concept_ids"].append(c1["node_id"])
        print(f"     对齐: 学习型智能体 → {c1['node_id']} ({c1['name']})")

    if r2.success and r2.data["concepts"]:
        c2 = r2.data["concepts"][0]
        state["concept_ids"].append(c2["node_id"])
        print(f"     对齐: 强化学习 → {c2['node_id']} ({c2['name']})")

    # ---------------------------------------------------------------
    # Step 2: 查两者的关系（双向边 + 多跳路径）
    # ---------------------------------------------------------------
    log_step(2, "拿到两个 concept_id，查它们之间有没有直接关系；如果没有，做多跳找路径。")
    if len(state["concept_ids"]) >= 2:
        cid_a, cid_b = state["concept_ids"][0], state["concept_ids"][1]
        call_and_show(reg, "get_relations", node_id=cid_a, direction="both", limit=15)
        call_and_show(reg, "multi_hop_traverse", start_id=cid_a, max_hop=3, end_id=cid_b, max_nodes=50)

    # ---------------------------------------------------------------
    # Step 3: 拉出「学习型智能体」下挂的所有 L2 具体实例
    # ---------------------------------------------------------------
    log_step(3, "用户还问了「有哪些具体实例」——对应 instance_of 关系，调用 get_entities_of_concept。")
    if state["concept_ids"]:
        r_ent = call_and_show(
            reg, "get_entities_of_concept",
            concept_id=state["concept_ids"][0], limit=10,
        )
        if r_ent.success:
            state["entity_ids"] = [e["node_id"] for e in r_ent.data.get("entities", [])]
            print(f"     收集到的实体实例数: {len(state['entity_ids'])}")
            if state["entity_ids"]:
                # 对实例再按跟「强化学习」的相关度做重排序
                r_rank = call_and_show(
                    reg, "rank_results",
                    node_ids=state["entity_ids"],
                    intent_query="强化学习 实例 应用场景",
                    top_n=5,
                    include_evidences=True,
                )
                if r_rank.success and r_rank.data["items"]:
                    # 把排第一的实体拿出来，拉取它的证据溯源
                    best_ent = r_rank.data["items"][0]
                    log_step(4, f"重排后最相关的实例是『{best_ent['name']}』。拉取它的证据用来做溯源。")
                    call_and_show(
                        reg, "get_evidences_of_node",
                        node_id=best_ent["node_id"], limit=2, snippet_chars=400,
                    )

    # ---------------------------------------------------------------
    # Step 5: 给「强化学习」也拉证据，保证回答时两边都能溯源
    # ---------------------------------------------------------------
    if len(state["concept_ids"]) >= 2:
        log_step(5, "最后给『强化学习』概念也拉取关键证据，构造回答时可以把两边证据都带上。")
        call_and_show(
            reg, "get_evidences_of_node",
            node_id=state["concept_ids"][1], limit=2, snippet_chars=300,
        )

    # ---------------------------------------------------------------
    # 汇总状态
    # ---------------------------------------------------------------
    print("\n" + "=" * 60)
    print("📋 Agent 最终状态（可用于后续 LLM 生成回答）:")
    print("=" * 60)
    print(json.dumps({k: v for k, v in state.items() if k != "relation_paths"},
                     ensure_ascii=False, indent=2))
    print()
    print("💡 接下来交给 LLM Answer Generator：")
    print("   - 把上面 concept / entity / evidence 的详情作为 context 喂给 LLM")
    print("   - 由 LLM 合成最终答案和溯源引用")
    print("   - KGRetrieve 本身不做任何答案生成，严格只做检索 🧱")


if __name__ == "__main__":
    main()
