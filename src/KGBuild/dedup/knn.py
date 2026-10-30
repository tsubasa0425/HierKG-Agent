import numpy as np
from typing import Tuple  # 新增导入

# 优先使用 FAISS；其次 sklearn；都没有则用 numpy 暴力 top-k（向量已归一化，规模不大时足够）
try:
    import faiss  # type: ignore
    HAS_FAISS = True
    HAS_SKLEARN = False
except Exception:
    HAS_FAISS = False
    try:
        from sklearn.neighbors import NearestNeighbors  # type: ignore
        HAS_SKLEARN = True
    except Exception:
        HAS_SKLEARN = False

def knn_topk(embs: np.ndarray, topk: int) -> Tuple[np.ndarray, np.ndarray]:  # 修改返回值注解
    """
    返回 (dist, idx) 形状为 (N, topk)。使用 L2 距离。
    numpy 兜底用 ||a-b||^2 = 2 - 2·a·b（输入应已 L2 归一化）。
    """
    n, dim = embs.shape
    if HAS_FAISS:
        index = faiss.IndexFlatL2(dim)
        index.add(embs)
        dists, idxs = index.search(embs, topk)
    elif HAS_SKLEARN:
        nbrs = NearestNeighbors(n_neighbors=topk, algorithm="auto", metric="euclidean")
        nbrs.fit(embs)
        dists, idxs = nbrs.kneighbors(embs)
    else:
        sims = embs @ embs.T
        idxs = np.argpartition(-sims, kth=min(topk - 1, n - 1), axis=1)[:, :topk]
        rows = np.arange(n)[:, None]
        order = np.argsort(-sims[rows, idxs], axis=1)
        idxs = idxs[rows, order]
        dists = np.maximum(0.0, 2.0 - 2.0 * sims[rows, idxs])
    return dists, idxs
