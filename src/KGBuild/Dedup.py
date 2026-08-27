import os
import re
import json
import time
import pickle
import logging
import argparse
from typing import Dict, List, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from tqdm import tqdm
import yaml

# === 外部模块（保持不变） ===
from KGBuild.dedup.data_structures import Occurrence, Neighbor, EntityItem
from KGBuild.dedup.knn import knn_topk
from KGBuild.dedup.llm import llm_is_same, _normalize_name
from KGBuild.dedup.name_similarity import _name_jaccard_mode


# ===========配置加载（主配置 + include_files）===========
def load_config_with_includes(main_cfg_path: Path) -> Dict[str, Any]:
    with main_cfg_path.open("r", encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}
    includes = base.get("include_files", []) or []
    merged = dict(base)
    for rel in includes:
        inc = (main_cfg_path.parent / rel).resolve()
        with inc.open("r", encoding="utf-8") as ff:
            sub = yaml.safe_load(ff) or {}
        merged.update(sub)
    return merged


# ==========I/O 工具函数==========
def read_entities(infile: str) -> Dict[str, EntityItem]:
    with open(infile, "r", encoding="utf-8") as f:
        raw = json.load(f)
    entities: Dict[str, EntityItem] = {}
    for name, d in raw.items():
        entities[name] = EntityItem(
            name=name,
            alias=d.get("alias", []),
            type=d.get("type", ""),
            original=d.get("original", ""),
            updated_description=d.get("updated_description", ""),
            role=d.get("role", d.get("local_role", "")),
            layer=d.get("layer", ""),
            concept_id=d.get("concept_id", ""),
            occurrences=[Occurrence(**o) for o in d.get("occurrences", [])],
            neighbors=[Neighbor(**n) for n in d.get("neighbors", [])],
        )
    logging.info(f"载入实体 {len(entities)} 个：{os.path.basename(infile)}")
    return entities


def read_embeddings(pkl_path: str) -> Tuple[List[str], np.ndarray]:
    with open(pkl_path, "rb") as f:
        emb_dict = pickle.load(f)
    names = list(emb_dict.keys())
    embs = np.vstack([emb_dict[n] for n in names]).astype("float32")
    logging.info(f"载入嵌入 {embs.shape[0]} x {embs.shape[1]}：{os.path.basename(pkl_path)}")
    return names, embs


# ==========并查集==========
class DSU:
    def __init__(self, n: int):
        self.fa = list(range(n))
        self.sz = [1] * n

    def find(self, x: int) -> int:
        while self.fa[x] != x:
            self.fa[x] = self.fa[self.fa[x]]
            x = self.fa[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.sz[ra] < self.sz[rb]:
            ra, rb = rb, ra
        self.fa[rb] = ra
        self.sz[ra] += self.sz[rb]
        return True

# ==========主流程==========
def run():
    # —— 路径根：KGBuild/ —— #
    script_dir = Path(__file__).resolve().parent
    cfg_path = script_dir / "config" / "config.yaml"
    cfg = load_config_with_includes(cfg_path)

    # —— 取子配置 —— #
    api = cfg["APIConfig"]                # 若 Dedup.llm 用得到，可在该模块内部读取你的 YAML
    dcfg = cfg["DedupConfig"]

    # —— 目录与文件名 —— #
    output_dir = script_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    ent_aggr = output_dir / dcfg["ENTITIES_AGGR_NAME"]
    ent_conv = output_dir / dcfg["ENTITIES_CONV_NAME"]
    emb_path = output_dir / dcfg["EMBEDDINGS_NAME"]
    out_path = output_dir / dcfg["RESULT_NAME"]

    # —— 实体文件优先级：aggr > conv —— #
    if ent_aggr.exists():
        entities_path = ent_aggr
    elif ent_conv.exists():
        entities_path = ent_conv
    else:
        raise FileNotFoundError(
            f"未找到实体文件：\n 1) {ent_aggr}\n 2) {ent_conv}\n（至少存在一个）"
        )

    if not emb_path.exists():
        raise FileNotFoundError(f"未找到嵌入文件：{emb_path}")

    # —— 日志 —— #
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler()]
    )
    logger = logging.getLogger("Dedup")

    t0 = time.time()
    logger.info("📘 [1/5] 加载实体与嵌入文件...")
    entities = read_entities(str(entities_path))
    names_all, embs_all = read_embeddings(str(emb_path))
    name2idx = {n: i for i, n in enumerate(names_all)}

    # 四层模型：只对 concept 做去重合并，entity 原样通过
    concept_names_set = {n for n, e in entities.items() if getattr(e, "layer", "") == "concept"}
    entity_names_set  = {n for n, e in entities.items() if getattr(e, "layer", "") != "concept"}
    logger.info(f"📌 四层模型：concept={len(concept_names_set)} 个参与去重；entity={len(entity_names_set)} 个原样通过")

    # 对齐实体（KNN 只包含 concept）
    names_concept = [n for n in names_all if n in concept_names_set]
    names_entity_pass = [n for n in names_all if n in entity_names_set]
    names = names_concept + names_entity_pass
    if not names:
        raise ValueError("实体与嵌入文件无交集，请检查输入文件")
    idx_keep = np.array([name2idx[n] for n in names_concept], dtype=np.int64) if names_concept else np.zeros((0,), dtype=np.int64)
    embs = embs_all[idx_keep].astype("float32") if len(idx_keep) > 0 else np.zeros((0, embs_all.shape[1]), dtype="float32")
    if len(embs) > 0:
        embs /= (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-12)
    N_concept = len(names_concept)
    logger.info(f"✅ 对齐后：参与KNN/候选的concept={N_concept}，原样通过的entity={len(names_entity_pass)}")

    # —— KNN —— #
    logger.info("📘 [2/5] 开始近邻检索（FAISS/Sklearn）...")
    topk = int(dcfg["KNN_NEIGHBORS"]) + 1
    dists, idxs = knn_topk(embs, topk=topk)

    # KNN/候选对/LLM 只在 concept（前 N_concept 个）上运行，entity 不参与
    ents: List[EntityItem] = [entities[n] for n in names]
    roles = [e.role or "" for e in ents]
    dsu = DSU(len(names_concept))  # 只对 concept 做 DSU
    N = len(names_concept)
    # 如有概念为空，跳过 KNN 结果
    if N > 0:
        nbr_sets = [set(idxs[i, 1:]) for i in range(N)]
    else:
        nbr_sets = []

    # —— 规则参数 —— #
    dist_thresh       = float(dcfg["DIST_THRESHOLD"])
    mut_knn           = bool(dcfg["MUTUAL_KNN"])
    rank_top          = int(dcfg["RANK_TOP"])
    per_node_cap      = int(dcfg["PER_NODE_CAP"])
    strict_same_role  = bool(dcfg["STRICT_SAME_ROLE"])
    use_name_jacc     = bool(dcfg["USE_NAME_JACCARD"])
    name_jacc_min     = float(dcfg["NAME_JACCARD_MIN"])
    name_mode         = str(dcfg["NAME_MODE"])
    use_alias_fast    = bool(dcfg["USE_ALIAS_FASTPATH"])
    trunc_desc        = bool(dcfg["TRUNCATE_DESC"])
    desc_maxlen       = int(dcfg["DESC_MAXLEN"])
    workers           = int(api.get("WORKERS", 10))

    # —— 候选过滤 —— #
    logger.info("📘 [3/5] 候选对过滤（距离阈值 + 角色 + 名称Jaccard + 互近邻 + 排名/配额）...")
    candidates: List[Tuple[int, int, float]] = []
    cnt_pairs = 0
    used = [0] * N

    rej_dist = rej_role = rej_jac = rej_mutual = rej_cap = 0
    rank_limit = max(2, rank_top)

    for i in range(N):
        for rank in range(1, min(idxs.shape[1], rank_limit)):
            j = idxs[i, rank]
            if j <= i or j >= N:
                continue
            cnt_pairs += 1
            dist = float(dists[i, rank])

            if dist >= dist_thresh:
                rej_dist += 1
                continue

            if strict_same_role:
                if roles[i] != roles[j]:
                    rej_role += 1
                    continue
            else:
                if roles[i] and roles[j] and roles[i] != roles[j]:
                    rej_role += 1
                    continue

            if use_name_jacc:
                jac = _name_jaccard_mode(ents[i].name, ents[j].name, name_mode)
                if jac < name_jacc_min:
                    rej_jac += 1
                    continue

            if mut_knn and (i not in nbr_sets[j]):
                rej_mutual += 1
                continue

            if per_node_cap > 0 and (used[i] >= per_node_cap or used[j] >= per_node_cap):
                rej_cap += 1
                continue

            candidates.append((i, j, dist))
            used[i] += 1
            used[j] += 1

    logger.info(f"📌 候选对（通过初筛）：{len(candidates)} / 原始对数：{cnt_pairs}")
    logger.info(f"[诊断] 过滤统计: 距离={rej_dist} 角色={rej_role} 名称Jaccard={rej_jac} 互近邻={rej_mutual} 配额={rej_cap}")
    if candidates:
        kept_d = np.array([d for (_, _, d) in candidates], dtype=np.float32)
        qs = np.percentile(kept_d, [5, 10, 25, 50, 75, 90, 95])
        logger.info(f"[诊断] 初筛距离分位数 p5..p95: {np.round(qs, 4)}")

    # —— 别名快速合并（可选） —— #
    rep_fast_merge = 0
    kept_for_llm: List[Tuple[int, int, float]] = candidates
    if use_alias_fast:
        logger.info("📘 [3.5] 快速通道：别名/名称交集直接合并（可选）...")

        def alias_intersect(a: EntityItem, b: EntityItem) -> bool:
            alias_a = {_normalize_name(x) for x in ([a.name] + a.alias)}
            alias_b = {_normalize_name(x) for x in ([b.name] + b.alias)}
            return len(alias_a & alias_b) > 0

        tmp_kept: List[Tuple[int, int, float]] = []
        for (i, j, dist) in candidates:
            if alias_intersect(ents[i], ents[j]):
                if dsu.union(i, j):
                    rep_fast_merge += 1
            else:
                tmp_kept.append((i, j, dist))
        kept_for_llm = tmp_kept
        logger.info(f"✅ 快速合并完成：{rep_fast_merge} 条；进入 LLM 的对数：{len(kept_for_llm)}")
    else:
        logger.info(f"📘 [3.5] 跳过快速通道；进入 LLM 的对数：{len(kept_for_llm)}")

    # —— LLM 判定（并行） —— #
    logger.info(f"📘 [4/5] LLM 并行判定（并发={workers}；按距离升序）...")
    kept_for_llm.sort(key=lambda x: x[2])
    llm_cache: Dict[Tuple[str, str], Tuple[bool, str]] = {}
    cnt_llm_calls = cnt_fallback = rep_merged = 0
    rep_merged += rep_fast_merge
    merge_decisions: List[Tuple[str, str, bool, float, str]] = []

    def judge_pair(i: int, j: int, dist: float) -> Tuple[int, int, bool, float, str, bool]:
        ent_i, ent_j = ents[i], ents[j]
        key = tuple(sorted((ent_i.name, ent_j.name)))
        if key in llm_cache:
            is_same, reason = llm_cache[key]
            return (i, j, is_same, dist, reason, False)
        is_same, reason, used_fallback = llm_is_same(
            ent_i, ent_j, truncate_desc=trunc_desc, desc_maxlen=desc_maxlen
        )
        llm_cache[key] = (is_same, reason)
        return (i, j, is_same, dist, reason, used_fallback)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(judge_pair, i, j, dist) for (i, j, dist) in kept_for_llm]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="LLM判定中", ncols=80):
            i, j, is_same, dist, reason, used_fallback = fut.result()
            if used_fallback:
                cnt_fallback += 1
            else:
                cnt_llm_calls += 1
            merge_decisions.append((ents[i].name, ents[j].name, is_same, dist, reason))
            if is_same and dsu.union(i, j):
                rep_merged += 1

    # —— 聚类与合并 —— #
    logger.info("📘 [5/5] 构建等价类、合并并写出结果...")
    clusters: Dict[int, List[int]] = {}
    for x in range(len(names_concept)):
        root = dsu.find(x)
        clusters.setdefault(root, []).append(x)

    def desc_len(e: EntityItem) -> int:
        return len((e.updated_description or e.original or "").strip())

    def merge_two(base: EntityItem, other: EntityItem) -> None:
        merged_alias = list(dict.fromkeys(base.alias + other.alias + [other.name]))
        base.alias = [a for a in merged_alias if a != base.name]
        occ_seen = {(o.path, o.node_id) for o in base.occurrences}
        for o in other.occurrences:
            key = (o.path, o.node_id)
            if key not in occ_seen:
                base.occurrences.append(o)
                occ_seen.add(key)
        nb_seen = {(n.name, n.snippet) for n in base.neighbors}
        for n in other.neighbors:
            if n.name == base.name:
                continue
            key = (n.name, n.snippet)
            if key not in nb_seen:
                base.neighbors.append(n)
                nb_seen.add(key)
        if desc_len(other) > desc_len(base):
            base.updated_description = other.updated_description or other.original

    rep_map: Dict[str, str] = {}
    new_entities: Dict[str, EntityItem] = {}

    # concept 合并（clusters 的下标对应 names_concept）
    for root, members in clusters.items():
        if len(members) == 1:
            e = ents[members[0]]
            new_entities[e.name] = e
            continue
        members_sorted = sorted(members, key=lambda idx: desc_len(ents[idx]), reverse=True)
        rep_idx = members_sorted[0]
        rep = ents[rep_idx]
        rep_name = rep.name
        for idx in members_sorted[1:]:
            child = ents[idx]
            if child.name == rep_name:
                continue
            merge_two(rep, child)
            rep_map[child.name] = rep_name
        new_entities[rep_name] = rep

    # entity 原样通过（下标 N_concept..end 对应 names_entity_pass）
    for name in names_entity_pass:
        new_entities[name] = entities[name]

    # 邻居重定向（保留 type）
    redirect = rep_map.copy()
    for parent_ent in new_entities.values():
        new_neighbors: List[Neighbor] = []
        seen = set()
        for nb in parent_ent.neighbors:
            tgt = redirect.get(nb.name, nb.name)
            if tgt == parent_ent.name:
                continue
            key = (tgt, nb.snippet, getattr(nb, "type", ""))
            if key in seen:
                continue
            seen.add(key)
            new_neighbors.append(Neighbor(name=tgt, snippet=nb.snippet, type=getattr(nb, "type", "")))
        parent_ent.neighbors = new_neighbors

    # 输出
    out_entities = {
        e.name: {
            "name": e.name,
            "alias": e.alias,
            "type": e.type,
            "layer": getattr(e, "layer", ""),
            "concept_id": getattr(e, "concept_id", ""),
            "original": e.original,
            "updated_description": e.updated_description,
            "role": e.role,
            "occurrences": [o.__dict__ for o in e.occurrences],
            "neighbors": [n.__dict__ for n in e.neighbors],
        }
        for e in new_entities.values()
    }
    clusters_names = [[names_concept[i] for i in members] for members in clusters.values()]
    decisions_list = [
        {"a": a, "b": b, "is_same": same, "dist": round(float(dist), 6), "reason": reason}
        for a, b, same, dist, reason in merge_decisions
    ]

    t1 = time.time()
    result = {
        "meta": {
            "entities_in": str(entities_path.resolve()),
            "emb_pkl": str(emb_path.resolve()),
            "four_layer_model": True,
            "concept_count_in": len(concept_names_set),
            "entity_count_in": len(entity_names_set),
            "k_neighbors": int(dcfg["KNN_NEIGHBORS"]),
            "dist_thresh": dist_thresh,
            "workers": workers,
            "mutual_knn": mut_knn,
            "rank_top": rank_top,
            "per_node_cap": per_node_cap,
            "strict_same_role": strict_same_role,
            "use_name_jaccard": use_name_jacc,
            "name_jaccard_min": name_jacc_min,
            "name_mode": name_mode,
            "use_alias_fastpath": use_alias_fast,
            "truncate_desc": trunc_desc,
            "desc_maxlen": desc_maxlen,
            "duration_sec": round(t1 - t0, 2),
        },
        "summary": {
            "pairs_raw": cnt_pairs,
            "pairs_after_filter": len(candidates),
            "pairs_to_llm": len(kept_for_llm),
            "llm_calls": cnt_llm_calls,
            "fallback_calls": rep_merged - rep_fast_merge - cnt_llm_calls,
            "merged_edges": rep_merged,
            "entities_before": len(entities),
            "entities_after": len(new_entities),
        },
        "entities": out_entities,
        "clusters": clusters_names,
        "merge_map": rep_map,
        "decisions": decisions_list,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("\n—— 去重任务摘要 ——")
    logger.info(f"候选对原始数量：{cnt_pairs}")
    logger.info(f"进入 LLM 判定的数量：{len(kept_for_llm)}（已并行处理）")
    logger.info(f"成功合并的实体对：{rep_merged}")
    logger.info(f"实体数量变化：{len(entities)} -> {len(new_entities)}")
    logger.info(f"总耗时：{t1 - t0:.2f} 秒")
    logger.info(f"结果文件：{out_path.resolve()}")


def main():
    # CLI 仅作最小覆盖（多数参数全部走 YAML）
    ap = argparse.ArgumentParser(description="Tree-KG 实体去重（纯 YAML 配置版）")
    ap.add_argument("--entities", type=str, default="", help="（可选）实体 JSON 路径覆盖")
    ap.add_argument("--emb", type=str, default="", help="（可选）嵌入 PKL 路径覆盖")
    ap.add_argument("--out", type=str, default="", help="（可选）结果 JSON 路径覆盖")
    args = ap.parse_args()

    # 现在 run() 里完全从 YAML 取；如需覆盖，可以自行改 run() 接口。
    run()


if __name__ == "__main__":
    main()
