# -*- coding: utf-8 -*-
"""
db_backend —— Neo4j + ChromaDB 数据库后端
==========================================

包含两部分：
  1. KGDBMemory：保持与 KGMemory 完全相同的接口契约，内部走 Neo4j + ChromaDB
  2. import_kg()：把 final_kg.json 导入到 Neo4j + ChromaDB 的脚本

使用方式：
    # 导入数据（只需运行一次）
    python -m src.KGRetrieve.db_backend import

    # 使用数据库后端
    from src.KGRetrieve.db_backend import KGDBMemory
    kg = KGDBMemory()           # 自动连接 Neo4j + ChromaDB
    reg = ToolRegistry(kg)      # 工具层零改动
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from neo4j import GraphDatabase

from .memory import Edge, Node

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "neo4j": {
        "uri": "bolt://localhost:7687",
        "username": "neo4j",
        "password": "",
        "database": "treekg",
    },
    "chroma": {
        "persist_directory": "./chroma_db_v2",
        "node_collection": "treekg_v2_nodes",
        "edge_collection": "treekg_v2_edges",
        "evidence_collection": "treekg_v2_evidences",
        "embedding_model": "smartcreation/bge-large-zh-v1.5:latest",
        "ollama_host": "http://localhost:11434",
    },
    "kg_file": "src/KGBuild/output/final_kg.json",
}


def _load_config() -> Dict[str, Any]:
    """从 storage.yaml 读取配置，回退到默认值"""
    cfg_path = Path(__file__).resolve().parent / "config" / "storage.yaml"
    if not cfg_path.exists():
        return _DEFAULT_CONFIG
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    neo4j_raw = raw.get("Neo4jConfig", {})
    chroma_raw = raw.get("ChromaConfig", {})
    return {
        "neo4j": {
            "uri": neo4j_raw.get("URI", _DEFAULT_CONFIG["neo4j"]["uri"]),
            "username": neo4j_raw.get("USERNAME", _DEFAULT_CONFIG["neo4j"]["username"]),
            "password": neo4j_raw.get("PASSWORD", _DEFAULT_CONFIG["neo4j"]["password"]),
            "database": neo4j_raw.get("DATABASE", _DEFAULT_CONFIG["neo4j"]["database"]),
        },
        "chroma": {
            "persist_directory": chroma_raw.get("PERSIST_DIRECTORY", "./chroma_db_v2"),
            "node_collection": "treekg_v2_nodes",
            "edge_collection": "treekg_v2_edges",
            "evidence_collection": "treekg_v2_evidences",
            "embedding_model": chroma_raw.get("EMBEDDING_MODEL", _DEFAULT_CONFIG["chroma"]["embedding_model"]),
            "ollama_host": chroma_raw.get("OLLAMA_HOST", _DEFAULT_CONFIG["chroma"]["ollama_host"]),
        },
        "kg_file": "src/KGBuild/output/final_kg.json",
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _edge_type_to_rel_type(edge_type: str) -> str:
    """edge.type → Neo4j 关系类型（大写下划线）"""
    return re.sub(r"[^A-Z0-9_]", "_", edge_type.upper())


def _row_to_node(record: Dict, node_type: str) -> Node:
    """Neo4j 记录 → Node 对象"""
    return Node(
        node_id=record.get("node_id", ""),
        node_type=node_type,
        name=record.get("name", ""),
        type=record.get("type", ""),
        description=record.get("description", ""),
        aliases=record.get("aliases", []) or [],
        concept_id=record.get("concept_id"),
        evidence_ids=record.get("evidence_ids", []) or [],
        attributes=record.get("attributes", {}) or {},
        doc_id=record.get("doc_id", ""),
        section_id=record.get("section_id", ""),
        section_path=record.get("section_path", ""),
        section_level=record.get("section_level", ""),
        snippet=record.get("snippet", ""),
        entities_in_section=record.get("entities_in_section", []) or [],
        raw=record,
    )


def _row_to_edge(source: Dict, target: Dict, rel: Dict, rel_type: str) -> Edge:
    """Neo4j 关系记录 → Edge 对象"""
    return Edge(
        source=source.get("node_id", ""),
        target=target.get("node_id", ""),
        type=rel.get("edge_type", rel_type.lower()),
        layer=rel.get("layer", ""),
        description=rel.get("description", ""),
        keywords=rel.get("keywords", []) or [],
    )


# ===========================================================================
# KGDBMemory —— 数据库后端，与 KGMemory 接口完全一致
# ===========================================================================

class KGDBMemory:
    """
    Neo4j + ChromaDB 后端的四层知识图谱。

    接口与 KGMemory 完全一致：
      schema / list_layers / get_node / get_concept / get_entity / get_evidence
      get_entities_of_concept / get_edges / multi_hop / search_by_name
    """

    def __init__(self, config: Optional[Dict] = None, kg_json_path: Optional[str] = None):
        self.config = config or _load_config()
        self._kg_json_path = kg_json_path or self.config["kg_file"]

        # Neo4j 驱动
        neo = self.config["neo4j"]
        self._driver = GraphDatabase.driver(
            neo["uri"], auth=(neo["username"], neo["password"]),
        )
        # 测试连接
        with self._driver.session(database=neo["database"]) as sess:
            sess.run("RETURN 1")
        logger.info("KGDBMemory: Neo4j 连接成功 (%s)", neo["uri"])

        # ChromaDB（延迟初始化，只在 search_by_name 时才连接）
        self._chroma_client = None
        self._node_collection = None
        self._evidence_collection = None
        self._edge_collection = None
        self._embed_fn = None

        # schema 从 JSON 加载（不需要每次查数据库）
        self._schema_cache: Optional[Dict] = None

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> Dict[str, Any]:
        if self._schema_cache is None:
            path = Path(self._kg_json_path)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._schema_cache = raw.get("L0_schema", {})
            else:
                self._schema_cache = {}
        return self._schema_cache

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def list_layers(self) -> Dict[str, int]:
        cy = "MATCH (n) RETURN labels(n)[0] as label, count(n) as cnt"
        with self._session() as sess:
            result = sess.run(cy)
            counts = {}
            for r in result:
                label = r["label"].lower()
                if label == "concept":
                    counts["concept"] = r["cnt"]
                elif label == "entity":
                    counts["entity"] = r["cnt"]
                elif label == "evidence":
                    counts["evidence"] = r["cnt"]
            # 补零
            for k in ("concept", "entity", "evidence"):
                counts.setdefault(k, 0)
            return counts

    # ------------------------------------------------------------------
    # 精确查找
    # ------------------------------------------------------------------

    def _session(self):
        return self._driver.session(database=self.config["neo4j"]["database"])

    def _get_node_by_id(self, node_id: str, expected_type: Optional[str] = None) -> Optional[Node]:
        if expected_type:
            label = expected_type.capitalize()
            cy = f"MATCH (n:{label} {{node_id: $nid}}) RETURN n"
        else:
            cy = "MATCH (n {node_id: $nid}) RETURN n, labels(n)[0] as label"
        with self._session() as sess:
            result = sess.run(cy, nid=node_id)
            record = result.single()
            if not record:
                return None
            if expected_type:
                node_data = dict(record["n"])
                return _row_to_node(node_data, expected_type)
            else:
                node_data = dict(record["n"])
                label = record["label"].lower()
                return _row_to_node(node_data, label)

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._get_node_by_id(node_id)

    def get_concept(self, concept_id: str) -> Optional[Node]:
        return self._get_node_by_id(concept_id, "concept")

    def get_entity(self, entity_id: str) -> Optional[Node]:
        return self._get_node_by_id(entity_id, "entity")

    def get_evidence(self, evidence_id: str) -> Optional[Node]:
        return self._get_node_by_id(evidence_id, "evidence")

    # ------------------------------------------------------------------
    # 关联查询
    # ------------------------------------------------------------------

    def get_entities_of_concept(self, concept_id: str) -> List[Node]:
        """查 L2 实体：MATCH (e:Entity)-[:INSTANCE_OF]->(c:Concept)"""
        cy = """
        MATCH (e:Entity)-[:INSTANCE_OF]->(c:Concept {node_id: $cid})
        RETURN e
        """
        with self._session() as sess:
            result = sess.run(cy, cid=concept_id)
            return [_row_to_node(dict(r["e"]), "entity") for r in result]

    def get_edges(
        self,
        node_id: str,
        direction: str = "both",
        edge_type: Optional[str] = None,
        layer: Optional[str] = None,
    ) -> List[Edge]:
        """查节点的入/出边"""
        dir_clause = ""
        if direction == "out":
            dir_clause = "-[r]->"
        elif direction == "in":
            dir_clause = "<-[r]-"
        else:
            dir_clause = "-[r]-"

        # 关系类型过滤
        rel_type_filter = ""
        params: Dict[str, Any] = {"nid": node_id}
        if edge_type:
            rel_type = _edge_type_to_rel_type(edge_type)
            rel_type_filter = f":`{rel_type}`"
            dir_clause = dir_clause.replace("[r]", f"[r{rel_type_filter}]")

        cy = f"""
        MATCH (n {{node_id: $nid}}){dir_clause}(m)
        RETURN n, r, m, type(r) as rel_type
        """

        with self._session() as sess:
            result = sess.run(cy, **params)
            edges: List[Edge] = []
            for r in result:
                rel_data = dict(r["r"])
                rel_data.setdefault("edge_type", r["rel_type"].lower().replace("_", "-"))
                if layer and rel_data.get("layer") != layer:
                    continue
                source_data = dict(r["n"])
                target_data = dict(r["m"])
                # 根据方向调整 source/target
                if direction == "in":
                    source_data, target_data = target_data, source_data
                edges.append(_row_to_edge(source_data, target_data, rel_data, r["rel_type"]))
            return edges

    # ------------------------------------------------------------------
    # 多跳遍历（BFS）
    # ------------------------------------------------------------------

    def multi_hop(
        self,
        start_id: str,
        max_hop: int = 2,
        allowed_layers: Optional[Set[str]] = None,
        allowed_edge_types: Optional[Set[str]] = None,
        max_nodes: int = 100,
    ) -> Dict[str, Any]:
        """BFS 多跳，与 KGMemory.multi_hop 行为一致"""
        start = self.get_node(start_id)
        if not start:
            return {"nodes": [], "edges": [], "paths": []}

        visited: Set[str] = {start_id}
        visited_edges: Set[Tuple[str, str, str]] = set()
        result_nodes: List[str] = [start_id]
        result_edges: List[Edge] = []
        paths: List[List[str]] = [[start_id]]

        queue: List[Tuple[str, int, List[str]]] = [(start_id, 0, [start_id])]

        while queue:
            cur_id, hop, path = queue.pop(0)
            if hop >= max_hop:
                continue
            # 查出边
            edges = self.get_edges(cur_id, direction="out")
            for edge in edges:
                if allowed_edge_types:
                    rel_type = _edge_type_to_rel_type(edge.type)
                    allowed_rels = {_edge_type_to_rel_type(t) for t in allowed_edge_types}
                    if rel_type not in allowed_rels:
                        continue
                nxt = edge.target
                target_node = self.get_node(nxt)
                if not target_node:
                    continue
                if allowed_layers and target_node.node_type not in allowed_layers:
                    continue
                edge_key = (edge.source, edge.target, edge.type)
                if edge_key not in visited_edges:
                    visited_edges.add(edge_key)
                    result_edges.append(edge)
                if nxt not in visited:
                    visited.add(nxt)
                    result_nodes.append(nxt)
                    new_path = path + [nxt]
                    paths.append(new_path)
                    if len(result_nodes) < max_nodes:
                        queue.append((nxt, hop + 1, new_path))

        # 批量取节点详情
        node_dicts: List[Dict] = []
        for nid in result_nodes:
            n = self.get_node(nid)
            if n:
                node_dicts.append(n.to_dict())

        return {
            "nodes": node_dicts,
            "edges": [e.to_dict() for e in result_edges],
            "paths": paths,
        }

    # ------------------------------------------------------------------
    # 语义搜索（ChromaDB 向量 + 名称匹配混合）
    # ------------------------------------------------------------------

    def _ensure_chroma(self):
        """延迟初始化 ChromaDB 客户端"""
        if self._chroma_client is not None:
            return
        import chromadb
        from chromadb.config import Settings
        import ollama

        cfg = self.config["chroma"]
        Path(cfg["persist_directory"]).mkdir(parents=True, exist_ok=True)
        self._chroma_client = chromadb.PersistentClient(
            path=cfg["persist_directory"],
            settings=Settings(allow_reset=True, anonymized_telemetry=False),
        )
        self._node_collection = self._chroma_client.get_or_create_collection(
            name=cfg["node_collection"],
        )
        self._evidence_collection = self._chroma_client.get_or_create_collection(
            name=cfg["evidence_collection"],
        )
        self._edge_collection = self._chroma_client.get_or_create_collection(
            name=cfg["edge_collection"],
        )
        self._ollama = ollama.Client(host=cfg["ollama_host"])
        self._embed_model = cfg["embedding_model"]
        logger.info("KGDBMemory: ChromaDB 连接成功 (%s)", cfg["persist_directory"])

    def _embed(self, text: str) -> List[float]:
        """用 Ollama 生成向量"""
        resp = self._ollama.embeddings(model=self._embed_model, prompt=text)
        return resp["embedding"]

    def search_by_name(
        self,
        query: str,
        node_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Tuple[Node, float]]:
        """
        混合搜索：
          1. ChromaDB 向量搜索（语义相似）
          2. Neo4j 名称/别名精确匹配（boost 分数）
        返回 (Node, score) 列表，按分数降序。
        """
        self._ensure_chroma()
        q = query.strip().lower()

        # —— 1. 向量搜索 ——
        collection = self._evidence_collection if node_type == "evidence" else self._node_collection
        where = {"node_type": node_type} if node_type else None
        try:
            query_emb = self._embed(query)
            results = collection.query(
                query_embeddings=[query_emb],
                n_results=limit + 10,
                where=where,
            )
            vector_hits: Dict[str, float] = {}
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i]
                    similarity = 1.0 / (1.0 + distance)  # L2 → similarity
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    node_id = meta.get("node_id", doc_id)
                    vector_hits[node_id] = similarity * 10  # 放大到和名称匹配同量级
        except Exception as exc:
            logger.warning("向量搜索失败，回退到名称匹配: %s", exc)
            vector_hits = {}

        # —— 2. 名称/别名精确匹配（在 Neo4j 中做 CONTAINS 查询）——
        name_hits: Dict[str, float] = {}
        label_filter = ""
        if node_type:
            label_filter = f":{node_type.capitalize()}"
        cy = f"""
        MATCH (n{label_filter})
        WHERE toLower(n.name) CONTAINS $q OR
              any(a IN n.aliases WHERE toLower(a) CONTAINS $q)
        RETURN n
        LIMIT $limit
        """
        try:
            with self._session() as sess:
                result = sess.run(cy, q=q, limit=limit + 10)
                for r in result:
                    node_data = dict(r["n"])
                    nid = node_data.get("node_id", "")
                    if not nid:
                        continue
                    label = r["labels(n)[0]"].lower() if "labels(n)" in r.keys() else node_type or "concept"
                    # 精确匹配高分，包含匹配中分
                    name_lower = (node_data.get("name", "") or "").strip().lower()
                    if name_lower == q:
                        name_hits[nid] = name_hits.get(nid, 0) + 10.0
                    elif q in name_lower:
                        name_hits[nid] = name_hits.get(nid, 0) + 5.0
                    else:
                        name_hits[nid] = name_hits.get(nid, 0) + 3.0
        except Exception as exc:
            logger.warning("名称搜索失败: %s", exc)

        # —— 3. 合并去重，按分数降序 ——
        all_ids = set(vector_hits.keys()) | set(name_hits.keys())
        scored: List[Tuple[Node, float]] = []
        for nid in all_ids:
            node = self.get_node(nid)
            if not node:
                continue
            if node_type and node.node_type != node_type:
                continue
            score = vector_hits.get(nid, 0) + name_hits.get(nid, 0)
            scored.append((node, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def close(self):
        if self._driver:
            self._driver.close()


# ===========================================================================
# 导入脚本
# ===========================================================================

def import_kg(kg_path: Optional[str] = None) -> None:
    """把 final_kg.json 导入到 Neo4j + ChromaDB"""
    config = _load_config()
    kg_path = kg_path or config["kg_file"]
    path = Path(kg_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / kg_path
    if not path.exists():
        print(f"❌ KG 文件不存在: {path}")
        sys.exit(1)

    print(f"📂 加载 KG 文件: {path}")
    with open(path, "r", encoding="utf-8") as f:
        kg = json.load(f)

    concepts = kg.get("L1_concepts", [])
    entities = kg.get("L2_entities", [])
    evidences = kg.get("L3_evidences", [])
    edges = kg.get("edges", [])
    print(f"   L1 概念: {len(concepts)}, L2 实体: {len(entities)}, L3 证据: {len(evidences)}, 边: {len(edges)}")

    # —— 1. Neo4j 导入 ——
    _import_to_neo4j(config, concepts, entities, evidences, edges)

    # —— 2. ChromaDB 导入 ——
    _import_to_chroma(config, concepts, entities, evidences, edges)

    print("\n✅ 导入完成！")


def _import_to_neo4j(config, concepts, entities, evidences, edges):
    print("\n—— Neo4j 导入 ——")
    neo = config["neo4j"]
    driver = GraphDatabase.driver(neo["uri"], auth=(neo["username"], neo["password"]))

    with driver.session(database=neo["database"]) as sess:
        # 清空旧数据
        print("🧹 清空旧数据...")
        sess.run("MATCH (n) DETACH DELETE n")

        # 创建索引
        print("📋 创建索引...")
        for label in ("Concept", "Entity", "Evidence"):
            sess.run(f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.node_id)")
            sess.run(f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.name)")

        # 导入 L1 概念
        print(f"📥 导入 L1 概念 ({len(concepts)} 条)...")
        for c in concepts:
            sess.run("""
                CREATE (n:Concept {
                    node_id: $node_id, name: $name, type: $type,
                    description: $description, aliases: $aliases,
                    concept_id: $concept_id, evidence_ids: $evidence_ids
                })
            """,
                node_id=c["concept_id"],
                name=c.get("name", ""),
                type=c.get("type", ""),
                description=c.get("description", ""),
                aliases=c.get("aliases", []),
                concept_id=c["concept_id"],
                evidence_ids=c.get("evidence_ids", []),
            )

        # 导入 L2 实体
        print(f"📥 导入 L2 实体 ({len(entities)} 条)...")
        for e in entities:
            sess.run("""
                CREATE (n:Entity {
                    node_id: $node_id, name: $name, type: $type,
                    description: $description, aliases: $aliases,
                    concept_id: $concept_id, evidence_ids: $evidence_ids,
                    attributes: $attributes
                })
            """,
                node_id=e["entity_id"],
                name=e.get("name", ""),
                type=e.get("type", ""),
                description=e.get("description", ""),
                aliases=e.get("aliases", []),
                concept_id=e.get("concept_id", ""),
                evidence_ids=e.get("evidence_ids", []),
                attributes=e.get("attributes", {}),
            )

        # 导入 L3 证据
        print(f"📥 导入 L3 证据 ({len(evidences)} 条)...")
        for ev in evidences:
            sess.run("""
                CREATE (n:Evidence {
                    node_id: $node_id, name: $name, type: $type,
                    doc_id: $doc_id, section_id: $section_id,
                    section_path: $section_path, section_level: $section_level,
                    snippet: $snippet, entities_in_section: $entities_in_section
                })
            """,
                node_id=ev["evidence_id"],
                name=ev.get("title", ev.get("section_id", "")),
                type=ev.get("section_level", ""),
                doc_id=ev.get("doc_id", ""),
                section_id=ev.get("section_id", ""),
                section_path=ev.get("section_path", ""),
                section_level=ev.get("section_level", ""),
                snippet=ev.get("snippet", ev.get("content", "")),
                entities_in_section=ev.get("entities_in_section", []),
            )

        # 导入边
        print(f"📥 导入边 ({len(edges)} 条)...")
        t0 = time.perf_counter()
        success = 0
        failed = 0
        for edge in edges:
            rel_type = _edge_type_to_rel_type(edge.get("type", "related"))
            try:
                result = sess.run(f"""
                    MATCH (s {{node_id: $src}}), (t {{node_id: $tgt}})
                    CREATE (s)-[r:`{rel_type}` {{
                        layer: $layer, description: $desc, keywords: $kw,
                        edge_type: $et
                    }}]->(t)
                    RETURN r
                """,
                    src=edge["source"],
                    tgt=edge["target"],
                    layer=edge.get("layer", ""),
                    desc=edge.get("description", ""),
                    kw=edge.get("keywords", []),
                    et=edge.get("type", "related"),
                )
                if result.single():
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                if failed <= 3:
                    logger.warning("边导入失败: %s → %s (%s): %s",
                                   edge.get("source"), edge.get("target"), rel_type, exc)
        elapsed = time.perf_counter() - t0
        print(f"   ✅ 成功 {success} 条, 失败 {failed} 条 ({elapsed:.1f}s)")

        # 统计
        stats = sess.run("MATCH (n) RETURN labels(n)[0] as label, count(n) as cnt")
        print("   数据库统计:")
        for r in stats:
            print(f"     {r['label']}: {r['cnt']}")

    driver.close()


def _import_to_chroma(config, concepts, entities, evidences, edges):
    print("\n—— ChromaDB 导入 ——")
    import chromadb
    from chromadb.config import Settings
    import ollama

    cfg = config["chroma"]
    Path(cfg["persist_directory"]).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=cfg["persist_directory"],
        settings=Settings(allow_reset=True, anonymized_telemetry=False),
    )
    # 清空旧集合
    for name in (cfg["node_collection"], cfg["edge_collection"], cfg["evidence_collection"]):
        try:
            client.delete_collection(name)
        except Exception:
            pass

    node_col = client.get_or_create_collection(name=cfg["node_collection"])
    edge_col = client.get_or_create_collection(name=cfg["edge_collection"])
    ev_col = client.get_or_create_collection(name=cfg["evidence_collection"])

    ollama_client = ollama.Client(host=cfg["ollama_host"])
    model = cfg["embedding_model"]

    def embed(text: str) -> List[float]:
        return ollama_client.embeddings(model=model, prompt=text)["embedding"]

    # 导入 L1 + L2 节点
    all_nodes = []
    for c in concepts:
        all_nodes.append((c["concept_id"], "concept",
                          f"{c.get('name','')}: {c.get('description','')}",
                          {"node_id": c["concept_id"], "node_type": "concept",
                           "name": c.get("name", ""), "type": c.get("type", "")}))
    for e in entities:
        all_nodes.append((e["entity_id"], "entity",
                          f"{e.get('name','')}: {e.get('description','')}",
                          {"node_id": e["entity_id"], "node_type": "entity",
                           "name": e.get("name", ""), "type": e.get("type", "")}))

    print(f"📥 导入节点向量 ({len(all_nodes)} 条)...")
    t0 = time.perf_counter()
    for i in range(0, len(all_nodes), 50):
        batch = all_nodes[i:i + 50]
        ids = [b[0] for b in batch]
        docs = [b[2] for b in batch]
        metas = [b[3] for b in batch]
        embs = [embed(d) for d in docs]
        node_col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
        print(f"   {min(i+50, len(all_nodes))}/{len(all_nodes)}", end="\r")
    print(f"   ✅ 节点向量导入完成 ({time.perf_counter()-t0:.1f}s)")

    # 导入 L3 证据
    print(f"📥 导入证据向量 ({len(evidences)} 条)...")
    t0 = time.perf_counter()
    for i in range(0, len(evidences), 20):
        batch = evidences[i:i + 20]
        ids = [ev["evidence_id"] for ev in batch]
        docs = [f"{ev.get('title','')}: {ev.get('snippet','')[:500]}" for ev in batch]
        metas = [{"node_id": ev["evidence_id"], "node_type": "evidence",
                  "name": ev.get("title", ""), "section_id": ev.get("section_id", ""),
                  "section_path": ev.get("section_path", "")} for ev in batch]
        embs = [embed(d) for d in docs]
        ev_col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
        print(f"   {min(i+20, len(evidences))}/{len(evidences)}", end="\r")
    print(f"   ✅ 证据向量导入完成 ({time.perf_counter()-t0:.1f}s)")

    # 导入边
    print(f"📥 导入边向量 ({len(edges)} 条)...")
    t0 = time.perf_counter()
    for i in range(0, len(edges), 50):
        batch = edges[i:i + 50]
        ids = [f"{e['source']}_{e.get('type','rel')}_{e['target']}" for e in batch]
        docs = [f"{e.get('source','')} {e.get('type','')} {e.get('target','')}: {e.get('description','')}"
                for e in batch]
        metas = [{"source": e.get("source", ""), "target": e.get("target", ""),
                  "type": e.get("type", ""), "layer": e.get("layer", "")} for e in batch]
        embs = [embed(d) for d in docs]
        edge_col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
        print(f"   {min(i+50, len(edges))}/{len(edges)}", end="\r")
    print(f"   ✅ 边向量导入完成 ({time.perf_counter()-t0:.1f}s)")


# ===========================================================================
# 命令行入口
# ===========================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "import":
        import_kg()
    else:
        print("用法: python -m src.KGRetrieve.db_backend import")
        print("  import  把 final_kg.json 导入到 Neo4j + ChromaDB")
