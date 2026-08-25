# KGRetrieve 工具包文档

> 面向 Agent 的四层知识图谱原子检索工具集
> 版本：2026-08-09（单 DB 后端化：移除 KGMemory 内存后端；新增 cross-encoder 重排与 assemble_context；MCP 接入）

---

## 一、设计原则

1. **每个工具只干一件事** —— 输入输出严格结构化，不混合多个职责
2. **只做检索不做生成** —— KGRetrieve 没有任何 LLM 生成调用，答案合成由外层 Agent / LLM 负责（重排用的是本地 cross-encoder 模型，属于检索侧精排，非生成）
3. **统一元数据** —— 每个工具有 `name` / `description` / `parameters`（JSON Schema）/ `output_schema`
4. **单后端** —— 唯一后端是 `KGDBMemory`（Neo4j + ChromaDB + Ollama），不再有纯内存后端
5. **Agent 自主编排** —— 调用什么工具、调几轮、什么顺序，全部交给 Agent 决定

---

## 二、快速开始

### 依赖

| 组件 | 用途 |
|------|------|
| Neo4j | 图结构持久化（节点 / 边 / 多跳遍历） |
| ChromaDB | 语义向量检索 |
| Ollama（bge-m3） | 检索侧 embedding |
| sentence-transformers + bge-reranker-v2-m3 | `rank_results` 的精排模型（本地 models/，从 ModelScope 下载，可 GPU 推理） |

### 初始化

```python
from src.KGRetrieve import KGDBMemory, ToolRegistry

kg = KGDBMemory()                        # 自动读取 storage.yaml 配置并连接 Neo4j
reg = ToolRegistry(kg)

# 首次使用需要先导入数据（只需一次）：
# python -m src.KGRetrieve.db_backend import

# —— 通用调用 ——
schemas = reg.get_tool_schemas()          # 15 条 OpenAI function schema
docs    = reg.get_tool_docs()             # 文本版工具列表

result = reg.call("search_concepts", {"query": "强化学习", "limit": 5})
print(result.success)      # True
print(result.data)         # {"query": "...", "hits": 5, "concepts": [...]}
print(result.to_json())    # 标准字符串，可直接塞给下一轮 LLM
```

### 通过 MCP 接入 Agent（Pi Agent / Claude 等）

```bash
python -m src.KGRetrieve.mcp_server            # stdio，默认
python -m src.KGRetrieve.mcp_server --transport streamable-http --http-port 8000
```

Agent 侧（Claude Code `.mcp.json` 风格）：
```json
{
  "mcpServers": {
    "hierkg": {
      "command": "conda",
      "args": ["run", "-n", "TreeKG", "python", "-m", "src.KGRetrieve.mcp_server"],
      "cwd": "d:\\workspace\\HierKG-Agent"
    }
  }
}
```

---

## 三、工具总览（15 个，按职责分 5 组）

| 组 | 工具数 | 工具名 | 典型场景 |
|---|------|--------|---------|
| **精确查找** | 5 | `get_schema` / `get_node_by_id` / `get_concept_by_id` / `get_entity_by_id` / `get_evidence_by_id` | 已有 node_id，精确取详情 |
| **关联查询** | 3 | `get_relations` / `get_entities_of_concept` / `get_evidences_of_node` | 查邻居、查跨层关联 |
| **跨概念遍历** | 1 | `multi_hop_traverse` | BFS 多跳找路径 |
| **模糊搜索** | 4 | `search_concepts` / `search_entities` / `search_evidences` / `semantic_search` | 不知道 ID，按关键词/语义探索 |
| **二次加工** | 2 | `rank_results` / `assemble_context` | 精排候选 / 组装可粘贴的 context 块 |

---

## 四、工具详解

### 4.1 精确查找组

---

#### `get_schema`

获取知识图谱的本体层（L0 Schema）定义，包括所有实体类型、关系类型、跨层关系说明。当你不确定某个类型或关系的语义时调用。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `section` | string (enum: `all` / `entity_types` / `relation_types`) | 否 | `all` | 返回 schema 的哪个部分 |

**返回结构**

```jsonc
// section="all"
{
  "entity_types": [ {"type": "概念", "layer_hint": "concept", "description": "...", "examples": [...]}, ... ],
  "relation_types": { "entity_relations": {...}, "cross_layer_relations": {...} }
}
```

**示例**

```python
reg.call("get_schema", {"section": "entity_types"})
reg.call("get_schema", {"section": "all"})
```

---

#### `get_node_by_id`

通过 `node_id`（concept_id / entity_id / evidence_id 三者通用）精确查找节点详情。

**参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `node_id` | string | ✅ | 节点ID，如 `concept_7446050b` / `ent_db974238` / `ev_1.1.1` |

**返回结构**：单个节点的完整字段（名称/类型/描述/别名/证据链接等）

```python
reg.call("get_node_by_id", {"node_id": "concept_7446050b"})
```

---

#### `get_concept_by_id`

通过 `concept_id` 精确获取 L1 概念详情，可选同时返回挂在此概念下的 L2 实体列表。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `concept_id` | string | ✅ | — | L1 概念ID，如 `concept_7446050b` |
| `include_entities` | boolean | 否 | `false` | 是否同时返回挂在此概念下的所有 L2 实体 |
| `entity_limit` | integer | 否 | `20` | `include_entities=true` 时最多返回的实体数 |

**返回结构**

```jsonc
{
  "node_id": "concept_7446050b",
  "node_type": "concept",
  "name": "Agent",
  "type": "概念",
  "aliases": ["智能体"],
  "description": "...",
  "evidence_ids": ["ev_1.4.3"],
  "entities": [ {"entity_id": "ent_xxx", "name": "...", ...} ],  // include_entities=true
  "entity_count": 3
}
```

---

#### `get_entity_by_id`

通过 `entity_id` 精确获取 L2 实体详情，自动带出它归属的 L1 概念节点。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `entity_id` | string | ✅ | — | L2 实体ID，如 `ent_db974238` |
| `include_concept` | boolean | 否 | `true` | 是否同时返回该实体归属的 L1 概念信息 |

**返回结构**

```jsonc
{
  "entity": {"node_id": "ent_db974238", "name": "API", "type": "工具", ...},
  "concept": {"node_id": "concept_f068f0da", "name": "...", ...}
}
```

---

#### `get_evidence_by_id`

通过 `evidence_id` 获取 L3 证据详情（原文片段 / 章节路径 / 文档位置，用于溯源）。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `evidence_id` | string | ✅ | — | L3 证据ID，如 `ev_1.1.1` |
| `include_snippet` | boolean | 否 | `true` | 是否返回原文片段 |
| `snippet_chars` | integer | 否 | `2000` | 原文片段最多返回的字符数 |

**返回结构**

```jsonc
{
  "node_id": "ev_1.1.1",
  "node_type": "evidence",
  "name": "传统视角下的智能体",
  "type": "subsection",
  "doc_id": "Hello-Agents",
  "section_id": "1.1.1",
  "section_path": "初识智能体 > 什么是智能体? > 传统视角下的智能体",
  "snippet": "原文片段...(truncated, total N chars)"
}
```

---

### 4.2 关联查询组

---

#### `get_relations`

查询某个节点的所有入边/出边/双向边。可按 `edge_type`（如 prerequisite/synonym/part_of）或跨层标注（layer 如 L1-L1 / L2-L1 / L1-L3）过滤。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `node_id` | string | ✅ | — | 起点或终点的 node_id |
| `direction` | string (enum: `in` / `out` / `both`) | 否 | `both` | 边的方向 |
| `edge_type` | string | 否 | — | 按关系类型精确过滤，例 `prerequisite` / `synonym` / `instance_of` / `appears_in` |
| `layer` | string | 否 | — | 按层标注过滤，例 `L1-L1` / `L1-L2` / `L2-L1` / `L1-L3` / `L2-L3` |
| `limit` | integer | 否 | `50` | 最多返回的边数 |

**返回结构**

```jsonc
{
  "total": 12,
  "edges": [
    {"source": "concept_xxx", "target": "concept_yyy", "type": "prerequisite",
     "layer": "L1-L1", "description": "...", "keywords": [...]}
  ]
}
```

---

#### `get_entities_of_concept`

获取归属于某个 L1 概念下的所有 L2 实体（instance_of 关系）。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `concept_id` | string | ✅ | — | L1 概念ID |
| `limit` | integer | 否 | `50` | 最多返回的实体数 |

**返回结构**

```jsonc
{
  "concept_id": "concept_6964a395",
  "total": 5,
  "entities": [ {"entity_id": "ent_xxx", "name": "AlphaGo", ...}, ... ]
}
```

---

#### `get_evidences_of_node`

获取某个 L1 概念或 L2 实体关联的所有 L3 证据（appears_in / described_by 跨层关系），直接返回原文片段和章节定位，用于溯源。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `node_id` | string | ✅ | — | L1/L2 节点ID |
| `include_snippet` | boolean | 否 | `true` | 是否返回原文片段 |
| `snippet_chars` | integer | 否 | `1000` | 原文片段最多字符数 |
| `limit` | integer | 否 | `20` | 最多返回的证据数 |

**返回结构**

```jsonc
{
  "node_id": "concept_6964a395",
  "node_name": "强化学习",
  "total": 5,
  "evidences": [
    {"node_id": "ev_1.1.1", "section_path": "...", "snippet": "...", ...}
  ]
}
```

---

### 4.3 跨概念遍历组

---

#### `multi_hop_traverse`

从某个节点出发进行 BFS 多跳遍历。用于跨概念查询（如「A 和 B 之间有什么路径」或「某个概念的二跳邻居有哪些」）。可按允许的层集合、允许的边类型过滤。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `start_id` | string | ✅ | — | 起点 node_id |
| `max_hop` | integer (1-5) | 否 | `2` | 最大跳数 |
| `allowed_layers` | array<string> | 否 | — | 允许经过的节点类型，元素为 `concept` / `entity` / `evidence`；空表示全部允许 |
| `allowed_edge_types` | array<string> | 否 | — | 允许经过的边类型（例 `["prerequisite","part_of","synonym"]`）；空表示全部 |
| `max_nodes` | integer | 否 | `100` | 最多遍历的节点数 |
| `end_id` | string | 否 | — | 可选。如果指定，一旦 BFS 到达 `end_id` 就提前停止，并在 `paths_to_end` 中标记命中路径 |

**返回结构**

```jsonc
{
  "nodes": [ {"node_id": "...", "name": "...", ...}, ... ],
  "edges": [ {"source": "...", "target": "...", "type": "...", "layer": "..."}, ... ],
  "paths": [ ["id1", "id2", "id3"], ... ],  // 每条从起点出发的路径
  "reached_end": true,                      // end_id 指定时才有
  "paths_to_end": [["id1","id2","end_id"]]   // 命中 end_id 的路径
}
```

**示例**：查「学习型智能体」和「强化学习」之间有没有路径

```python
reg.call("multi_hop_traverse", {
    "start_id": "concept_43a9d06c",
    "end_id":   "concept_6964a395",
    "max_hop":  3,
    "max_nodes": 50
})
```

---

### 4.4 模糊搜索组

> 搜索走 `KGDBMemory` 的混合检索：ChromaDB 向量语义搜索（bge-m3）+ Neo4j 名称/别名精确匹配加权，
> 语义相关性更强。返回的每条结果都带 `match_score` 字段，分数越高越相关。

---

#### `search_concepts`

按关键词搜索 L1 概念层（名称 / 别名 / 描述模糊匹配）。当你想知道「有没有讲过 X 这个概念」时调用。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | — | 搜索关键词，如 `强化学习` |
| `limit` | integer (1-50) | 否 | `10` | 最多返回数 |
| `min_score` | number | 否 | `0.5` | 最低匹配分数，调高可降低噪声 |

**返回结构**

```jsonc
{
  "query": "强化学习",
  "hits": 5,
  "concepts": [
    {"node_id": "concept_6964a395", "name": "强化学习",
     "type": "学习方法", "aliases": ["Reinforcement Learning","RL"],
     "match_score": 10.5, ...}
  ]
}
```

---

#### `search_entities`

按关键词搜索 L2 实体层（具体实例 / 工具 / 角色 / 参数）。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | — | 搜索关键词，如 `AlphaGo` |
| `limit` | integer (1-50) | 否 | `10` | 最多返回数 |
| `entity_type` | string | 否 | — | 按 schema 定义的实体类型过滤：`实例` / `工具` / `角色` / `参数` |

**返回结构**

```jsonc
{
  "query": "AlphaGo",
  "hits": 3,
  "entities": [ {"node_id": "ent_xxx", "name": "AlphaGo", "type": "实例", "match_score": 9.0, ...} ]
}
```

---

#### `search_evidences`

按关键词搜索 L3 证据层（章节标题 / 章节路径 / 原文片段 / 章节内出现的实体名）。当你想找「某段原文」或「XX 这个词出现在哪几节」时使用。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | — | 关键词，如 `传统视角` |
| `limit` | integer (1-50) | 否 | `10` | 最多返回数 |
| `include_snippet` | boolean | 否 | `true` | 是否返回原文片段 |
| `snippet_chars` | integer | 否 | `800` | 原文片段最多字符数 |

**返回结构**

```jsonc
{
  "query": "传统视角",
  "hits": 3,
  "evidences": [
    {"node_id": "ev_1.1.1", "title": "传统视角下的智能体",
     "section_path": "初识智能体 > 什么是智能体? > 传统视角下的智能体",
     "snippet": "...", "match_score": 5.2}
  ]
}
```

---

#### `semantic_search`

混合语义搜索：一次性跨 L1+L2+L3 三层搜索，把命中结果按层分类返回。当你不确定要查的东西在概念层、实体层还是证据层时，用这个工具做第一步探索。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | — | 搜索关键词或自然语言描述 |
| `per_layer_limit` | integer | 否 | `8` | 每层最多返回多少条 |

**返回结构**

```jsonc
{
  "query": "智能体 感知 行动",
  "L1_concepts":  [ {..., "match_score": 4.0}, ... ],
  "L2_entities":  [ {..., "match_score": 3.2}, ... ],
  "L3_evidences": [ {..., "match_score": 2.8}, ... ]
}
```

**使用建议**：这是探索型问题的「第一步」工具，拿到候选 ID 后再用精确查找 / 关联查询 / 多跳遍历深入。

---

### 4.5 二次加工组

---

#### `rank_results`

对前几步检索返回的候选节点列表做二次精排。

**两种模式**：
- **精排模式**（推荐）：传入 `node_ids` + `intent_query`，用本地 **bge-reranker-v2-m3（cross-encoder）** 对 query × 每个候选打分，按相关度降序返回 top_n。模型能看到 query 与候选的逐词交互，比向量相似度准得多。模型放本地 `models/bge-reranker-v2-m3/`（约 2.2GB，从 ModelScope 下载，断点续传脚本见仓库根 `_download_reranker.py`），本地缺失时才回退走 HF 镜像（`HF_ENDPOINT=hf-mirror.com` 已在 tools.py 默认设置）
- **手动挑选模式**：只传 `node_ids` 不传 `intent_query`，按原顺序截取 top_n

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `node_ids` | array<string> | ✅ | — | 待排序的 node_id 列表 |
| `intent_query` | string | 否 | — | 排序意图（如 `找智能体的先修概念`）。有则精排，无则按原顺序 |
| `top_n` | integer | 否 | `10` | 返回前 N 个 |
| `include_evidences` | boolean | 否 | `false` | 是否把每个节点关联的 L3 证据 ID 一并带上 |

**返回结构**

```jsonc
{
  "input_count": 8,
  "returned": 5,
  "intent_query": "先修概念 基础",
  "rerank_method": "cross_encoder",   // cross_encoder | keyword_overlap | original_order
  "items": [
    {"node_id": "concept_ffee9ff0", "name": "大语言模型驱动的智能体",
     "rank_score": 0.972, "related_evidence_ids": ["ev_xxx"]}
  ]
}
```

> `rank_score` 在 cross-encoder 模式下是 sigmoid 后的 0~1 相关度；若模型加载失败会降级为关键词重叠打分（`rerank_method=keyword_overlap`），模型不可用时返回 `original_order`。

**使用建议**：
- 当前面的搜索工具返回太多结果（如 `search_concepts` 返回 15 条），而你只想要最相关的 3-5 条时，用精排模式
- 当 Agent 已经自己挑好了一些 ID（基于 LLM 的判断），只想按原顺序取前 N 条时，用手动挑选模式
- 精排只对候选集（top 20~50）生效，属于检索侧，不依赖 LLM

---

#### `assemble_context`

把已经收集到的节点/证据 ID 组装成一段紧凑、去重、带溯源标注的 **context 文本**，可直接粘进回答 prompt。它**只做格式化**（拼块/去重/截断/贴引用 ID），不做任何取舍决策——查什么、信什么由 Agent 自己决定。

**参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `node_ids` | array<string> | ✅ | — | 已收集的 concept/entity/evidence node_id 列表 |
| `evidence_ids` | array<string> | 否 | — | 额外要带的证据 ID；不传则自动取各节点关联的证据 |
| `max_nodes` | integer | 否 | `20` | 最多组装多少个节点 |
| `max_evidence_per_node` | integer | 否 | `5` | 每个节点下列出多少条相关证据引用 |
| `snippet_chars` | integer | 否 | `400` | 每条证据原文片段最多多少字符 |

**返回结构**

```jsonc
{
  "node_count": 3,
  "evidence_count": 5,
  "context": "### [概念] 学习型智能体 (concept_xxx)\n- 描述: ...\n- 相关证据: [ev_1.1.1] (1.1.1) 传统视角下的智能体\n\n### 相关证据原文\n[ev_1.1.1] (1.1.1 > ...) 传统视角下的智能体\n  原文片段...\n",
  "node_ids": ["concept_xxx", "ent_yyy"],
  "evidence_ids": ["ev_1.1.1", "ev_2.3"]
}
```

**使用建议**：当 Agent 已收集到一批 ID、准备写最终答案时调用一次，把 `context` 字段直接放进回答 prompt；跨节点重复出现的证据会自动去重，引用格式统一为 `[ev_xxx]`，便于答案溯源。

---

## 五、ToolResult 统一输出结构

所有工具都返回 `ToolResult` 对象，结构如下：

```python
@dataclass
class ToolResult:
    tool_name: str           # 工具名
    success: bool            # 是否成功
    data: Any = None          # 结构化数据（每个工具不同）
    message: str = ""        # 失败时的错误信息
    elapsed_ms: float = 0.0   # 执行耗时（毫秒）
```

序列化方法：

```python
result.to_dict()  # → Dict
result.to_json()  # → JSON 字符串（ensure_ascii=False）
```

---

## 六、ToolRegistry API

```python
class ToolRegistry:
    def __init__(self, kg: KGDBMemory): ...   # 只接 KGDBMemory（Neo4j + ChromaDB）

    @property
    def tool_names(self) -> List[str]:               # 所有工具名
    def get(self, name: str) -> Optional[BaseTool]:   # 取单个工具
    def __getitem__(self, name: str) -> BaseTool:     # 同上（会抛 KeyError）

    def get_tool_schemas(self) -> List[Dict]:         # OpenAI function calling schema
    def get_tool_docs(self) -> str:                   # 一行一个工具的文本列表
    def call(self, tool_name: str, params: Dict) -> ToolResult:  # 统一调用入口
```

---

## 七、Agent 编排示例

以「学习型智能体和强化学习有什么关系？它有哪些具体实例？」为例，Agent 可能这样自主决策：

```
Step 1  Thought: 先把两个术语对齐到 concept_id
        Tool: search_concepts(query="学习型智能体", limit=5)
        Tool: search_concepts(query="强化学习", limit=5)

Step 2  Thought: 查两个概念之间有没有直接关系 / 多跳路径
        Tool: get_relations(node_id=cid_a, direction="both", limit=15)
        Tool: multi_hop_traverse(start_id=cid_a, end_id=cid_b, max_hop=3)

Step 3  Thought: 用户还问了「有哪些具体实例」——对应 instance_of
        Tool: get_entities_of_concept(concept_id=cid_a, limit=10)

Step 4  Thought: 实例太多，按「强化学习」相关度重排
        Tool: rank_results(node_ids=[...], intent_query="强化学习 实例 应用场景", top_n=5)

Step 5  Thought: 拉取 top-1 实例的 L3 证据，供答案溯源
        Tool: get_evidences_of_node(node_id=best_ent_id, snippet_chars=400)
```

---



## 八、设计决策说明

1. **为什么不做答案生成？** 职责单一。Agent 框架（Pi Agent / LangChain / LlamaIndex）自己有 answer generation 能力，KGRetrieve 把检索结果结构化返回即可，避免双重 LLM 调用浪费 token。
2. **为什么 `rank_results` 用 cross-encoder 而不是 LLM 重排？** 精排是检索侧的确定性步骤，bge-reranker-v2-m3 本地跑、快且便宜；需要更灵活的语义级重排时，Agent 自己用 LLM 过一遍 `node_ids` 的描述即可，不必做成工具。
3. **为什么有 `semantic_search` 还要分 `search_concepts/entities/evidences`？** `semantic_search` 是「探索型第一步」，而单层搜索用于「明确知道要查哪层」的精确场景，返回结果更聚焦。
4. **为什么删掉了纯内存后端（KGMemory）？** 目标已锁定 Neo4j + ChromaDB 生产栈，双后端维护两套索引逻辑收益低。`Node`/`Edge` 数据类保留在 `models.py` 作为共享模型，工具层保持轻量。
5. **旧版 `AgenticRAG` 已删除**：旧版硬编码 `classifier → selector → executor → generator` 路由流水线整体弃用，当前唯一检索路径是 KGRetrieve 15 个原子工具 + Agent 自主调用。

---

## 九、文件清单

| 文件 | 作用 |
|------|------|
| [__init__.py](file:///d:/workspace/KG-for-structure-file/src/KGRetrieve/__init__.py) | 包入口，导出 KGDBMemory / import_kg / 工具类 |
| [models.py](file:///d:/workspace/KG-for-structure-file/src/KGRetrieve/models.py) | 共享数据模型：Node / Edge |
| [db_backend.py](file:///d:/workspace/KG-for-structure-file/src/KGRetrieve/db_backend.py) | KGDBMemory（Neo4j+ChromaDB 后端）+ 导入脚本 |
| [tools.py](file:///d:/workspace/KG-for-structure-file/src/KGRetrieve/tools.py) | 15 个原子工具 + BaseTool + ToolRegistry + cross-encoder 重排器 |
| [mcp_server.py](file:///d:/workspace/KG-for-structure-file/src/KGRetrieve/mcp_server.py) | MCP server：把 15 个工具暴露给 Pi Agent / Claude 等 |
| [TOOLS.md](file:///d:/workspace/KG-for-structure-file/src/KGRetrieve/TOOLS.md) | 本文档 |
