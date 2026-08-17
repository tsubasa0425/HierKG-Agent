# -*- coding: utf-8 -*-
"""GoldenResolver：从 final_kg.json 把名称解析成 node_id，镜像导入时的证据去重。

纯函数，不碰 Neo4j/ChromaDB。出题约定：数据集里概念/实体写【名称】，ID 由 resolver 生成，
防手打错别；证据 ID（如 ev_1.1.1）本身就是稳定的章节号，直接写 ID。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from eval.common import EVIDENCE_ID_RE

_DEFAULT_KG = Path(__file__).resolve().parents[2] / "src" / "KGBuild" / "output" / "final_kg.json"


def _dedupe_evidence_ids(evidences: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """镜像 src/KGRetrieve/db_backend.py::_dedupe_evidence_ids —— 保证 golden ID 与 Neo4j/ChromaDB 一致。

    同 section_id 出现多个小节时，第一个保持 `ev_1.4`，后续改成 `ev_1.4#2` 等。
    """
    seen: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for ev in evidences:
        eid = ev["evidence_id"]
        n = seen.get(eid, 0)
        seen[eid] = n + 1
        if n > 0:
            ev = dict(ev)
            ev["evidence_id"] = f"{eid}#{n + 1}"
        out.append(ev)
    return out


class GoldenResolver:
    def __init__(self, final_kg_path: Optional[str | Path] = None):
        self.kg_path = Path(final_kg_path or _DEFAULT_KG)
        kg = json.loads(self.kg_path.read_text(encoding="utf-8"))
        self.concepts: List[Dict[str, Any]] = kg.get("L1_concepts", [])
        self.entities: List[Dict[str, Any]] = kg.get("L2_entities", [])
        self.evidences: List[Dict[str, Any]] = _dedupe_evidence_ids(kg.get("L3_evidences", []))
        self.edges: List[Dict[str, Any]] = kg.get("edges", [])

        # 名字 -> 候选 ID 列表（图谱里存在同名/近名节点，如两个「智能体」），
        # resolve 时取证据数最多者，和检索系统应返回的最佳命中一致。
        self.concept_by_name: Dict[str, List[str]] = {}
        self.entity_by_name: Dict[str, List[str]] = {}
        self.evidence_by_name: Dict[str, str] = {}
        self.node_index: Dict[str, Dict[str, Any]] = {}
        self.evidence_ids_of: Dict[str, List[str]] = {}

        for c in self.concepts:
            self._index(c, c.get("concept_id"), self.concept_by_name)
        for e in self.entities:
            self._index(e, e.get("entity_id"), self.entity_by_name)
        for ev in self.evidences:
            eid = ev["evidence_id"]
            self.node_index[eid] = ev
            for key in (ev.get("title"), ev.get("section_id"), ev.get("section_path")):
                self._add_name(self.evidence_by_name, key, eid)

    # ---------------- 内部 ----------------
    def _index(self, node: Dict[str, Any], nid: Optional[str], by_name: Dict[str, List[str]]) -> None:
        if not nid:
            return
        self.node_index[nid] = node
        self.evidence_ids_of[nid] = list(node.get("evidence_ids") or [])
        self._add_name(by_name, node.get("name"), nid)
        for a in node.get("aliases") or []:
            self._add_name(by_name, a, nid)

    @staticmethod
    def _add_name(d: Dict[str, List[str]], name: Any, nid: str) -> None:
        if not name:
            return
        key = GoldenResolver._norm(str(name))
        if not key:
            return
        d.setdefault(key, [])
        if nid not in d[key]:
            d[key].append(nid)

    def _best(self, by_name: Dict[str, List[str]], name: str) -> Optional[str]:
        """返回候选 ID 中证据数最多的（同名节点择优）。"""
        ids = by_name.get(self._norm(name)) or []
        if not ids:
            return None
        return max(ids, key=lambda nid: len(self.evidence_ids_of.get(nid, [])))


    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s).lower()

    # ---------------- 解析 ----------------
    def resolve_concept(self, name: str) -> Optional[str]:
        return self._best(self.concept_by_name, name)

    def resolve_entity(self, name: str) -> Optional[str]:
        return self._best(self.entity_by_name, name)

    def resolve_evidence(self, name: str) -> Optional[str]:
        return self.evidence_by_name.get(self._norm(name))

    def all_evidence_for(self, node_id: str) -> List[str]:
        return list(self.evidence_ids_of.get(node_id, []))

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        return self.node_index.get(node_id)

    # ---------------- 数据集校验 ----------------
    def resolve_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """把 golden_concept_names / golden_entity_names 解析成 ID，并补充到 item。"""
        out = dict(item)
        cids, problems = self._resolve_names(item.get("golden_concept_names") or [], self.resolve_concept, "概念")
        out["golden_concept_ids"] = cids
        eids, ep = self._resolve_names(item.get("golden_entity_names") or [], self.resolve_entity, "实体")
        out["golden_entity_ids"] = eids
        out.setdefault("_problems", [])
        out["_problems"] = out["_problems"] + problems + ep
        return out

    def validate_item(self, item: Dict[str, Any]) -> List[str]:
        """返回问题列表：golden ID 不在图谱里、或证据 ID 格式非法。"""
        problems = list(item.get("_problems") or [])
        for key in ("golden_concept_ids", "golden_entity_ids", "golden_evidence_ids"):
            for nid in item.get(key) or []:
                if nid not in self.node_index:
                    problems.append(f"{key} 中 {nid} 不在 final_kg.json")
        for eid in item.get("golden_evidence_ids") or []:
            if not EVIDENCE_ID_RE.match(str(eid)):
                problems.append(f"证据 ID 格式非法: {eid!r}")
        return problems

    @staticmethod
    def _resolve_names(names: List[str], resolver, kind: str):
        ids: List[str] = []
        problems: List[str] = []
        for n in names:
            nid = resolver(n)
            if nid:
                ids.append(nid)
            else:
                problems.append(f"golden_concept_names 中 {kind} {n!r} 解析不到 ID")
        return ids, problems


# ---------------- CLI ----------------
def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台默认 GBK 打不出 ✓/✗ 等字符，强制 UTF-8
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="黄金数据集校验 / 名称解析")
    ap.add_argument("--kg", default=str(_DEFAULT_KG), help="final_kg.json 路径")
    ap.add_argument("--validate", metavar="DATASET", help="校验数据集 JSON")
    ap.add_argument("--names", nargs="*", default=[], help="打印一批名称的解析结果（出题助手）")
    args = ap.parse_args(argv)

    r = GoldenResolver(args.kg)

    if args.names:
        print("== 概念 ==")
        for n in args.names:
            print(f"  {n} -> {r.resolve_concept(n)}")
        print("== 实体 ==")
        for n in args.names:
            print(f"  {n} -> {r.resolve_entity(n)}")
        return 0

    if args.validate:
        data = json.loads(Path(args.validate).read_text(encoding="utf-8"))
        questions = data.get("questions", [])
        total_problems = 0
        print(f"数据集: {args.validate}  ({len(questions)} 题)")
        from collections import Counter
        cats = Counter(q.get("category") for q in questions)
        print("类别分布:", dict(cats))
        for q in questions:
            q = r.resolve_item(q)
            problems = r.validate_item(q)
            if problems:
                total_problems += len(problems)
                print(f"  ✗ {q.get('id')}: {'; '.join(problems)}")
            else:
                print(f"  ✓ {q.get('id')} [{q.get('category')}] "
                      f"concepts={len(q.get('golden_concept_ids', []))} "
                      f"entities={len(q.get('golden_entity_ids', []))} "
                      f"evidences={q.get('golden_evidence_ids')}")
        if total_problems:
            print(f"\n❌ 共 {total_problems} 个问题")
            return 1
        print("\n✅ 全部通过")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
