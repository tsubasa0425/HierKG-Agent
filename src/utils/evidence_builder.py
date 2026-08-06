#!/usr/bin/env python3
"""
L3 证据层构建脚本

从 toc_with_entities_and_relations.json 生成 evidence.json。
每个有 content 的节点（chapter/section/subsection）都成为一个 L3 证据节点，
带完整的章节路径、原文、以及该章节中抽取到的实体名称列表。

输出结构（对应 schema.yaml 的 EvidenceSchema）：
{
  "evidence_id": "ev_1.1.1",
  "doc_id": "Hello-Agents",
  "section_id": "1.1.1",
  "section_path": "初识智能体 > 什么是智能体? > 传统视角下的智能体",
  "section_level": "subsection",
  "title": "传统视角下的智能体",
  "content": "原文...",
  "entities_in_section": ["大语言模型", "反射智能体", ...]
}
"""

import argparse
import json
from pathlib import Path
from typing import Dict, List, Any


# 章节层级映射
LEVEL_MAP = {1: "chapter", 2: "section", 3: "subsection", 4: "subsubsection"}


def build_evidence(toc: List[Dict[str, Any]], doc_id: str = "Hello-Agents") -> List[Dict[str, Any]]:
    """遍历 TOC 树，为每个有 content 的节点生成一个 L3 证据节点。

    Args:
        toc: toc_with_entities_and_relations.json 的根列表
        doc_id: 文档标识

    Returns:
        L3 证据节点列表
    """
    evidences: List[Dict[str, Any]] = []

    def walk(nodes: List[Dict[str, Any]], path: List[str]):
        for node in nodes:
            title = node.get("title", "")
            section_id = node.get("id", "")
            level = node.get("level", 0)
            content = node.get("content", "")
            cur_path = path + [title]

            # 只为有正文的节点生成证据
            if content and content.strip():
                # 收集该节点抽取到的实体名称
                entities_in_section = [
                    e.get("name", "")
                    for e in node.get("entities", [])
                    if e.get("name")
                ]

                evidences.append({
                    "evidence_id": f"ev_{section_id}" if section_id else f"ev_{len(evidences)}",
                    "doc_id": doc_id,
                    "section_id": section_id,
                    "section_path": " > ".join(cur_path),
                    "section_level": LEVEL_MAP.get(level, f"level_{level}"),
                    "title": title,
                    "content": content,
                    "entities_in_section": entities_in_section,
                })

            # 递归处理子节点
            children = node.get("children", [])
            if children:
                walk(children, cur_path)

    walk(toc, [])
    return evidences


def main():
    ap = argparse.ArgumentParser(description="从 TOC JSON 构建 L3 证据层")
    ap.add_argument("--input", default="src/KGBuild/output/toc_with_entities_and_relations.json",
                    help="输入文件路径")
    ap.add_argument("--output", default="src/KGBuild/output/evidence.json",
                    help="输出文件路径")
    ap.add_argument("--doc-id", default="Hello-Agents",
                    help="文档标识")
    args = ap.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)

    if not in_path.exists():
        raise FileNotFoundError(f"输入文件不存在：{in_path}")

    with in_path.open("r", encoding="utf-8") as f:
        toc = json.load(f)

    evidences = build_evidence(toc, doc_id=args.doc_id)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(evidences, f, ensure_ascii=False, indent=2)

    print(f"✅ L3 证据层构建完成：{out_path.resolve()}")
    print(f"   证据节点数：{len(evidences)}")
    if evidences:
        print(f"   第一个：{evidences[0]['evidence_id']} - {evidences[0]['title']}")
        print(f"   最后一个：{evidences[-1]['evidence_id']} - {evidences[-1]['title']}")

        # 统计各层级数量
        level_counts: Dict[str, int] = {}
        for ev in evidences:
            lv = ev["section_level"]
            level_counts[lv] = level_counts.get(lv, 0) + 1
        print(f"   层级分布：{level_counts}")

        # 统计有实体的证据数
        with_entities = sum(1 for ev in evidences if ev["entities_in_section"])
        total_entities = sum(len(ev["entities_in_section"]) for ev in evidences)
        print(f"   含实体的证据：{with_entities}/{len(evidences)}")
        print(f"   实体总引用次数：{total_entities}")


if __name__ == "__main__":
    main()
