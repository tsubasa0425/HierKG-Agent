# HierKG

面向教科书/技术文档的四层知识图谱构建与检索系统。将文档从"检索骨架"重构为 **L0 本体 / L1 概念 / L2 实体 / L3 证据** 四层分层模型，提供 15 个原子工具供 Agent 自主调用检索，并内置 Web UI（Agent 对话 + 图谱可视化）。

## 项目结构

```
HierKG-Agent/
├── src/
│   ├── KGBuild/                # 四层知识图谱构建流水线
│   │   ├── TextSegmentation.py     # 显式：docx → TOC + 原文小节
│   │   ├── Extraction.py           # 显式：小节 → 实体/关系抽取
│   │   ├── toc_graph.py            # 显式：构建 toc_graph.json
│   │   ├── Conv.py                 # 隐式：显式结果 → 隐式格式转换
│   │   ├── Aggr.py                 # 隐式：concept/entity 分层判定
│   │   ├── Embedding.py            # 隐式：BERT 嵌入
│   │   ├── Dedup.py                # 隐式：概念去重（调用 dedup/ 子包）
│   │   ├── Pred.py                 # 隐式：关系预测
│   │   ├── FinalKG.py              # 隐式：四层 KG 装配 → final_kg.json
│   │   ├── dedup/                   # 去重子包（knn / llm / name_similarity）
│   │   ├── model/                   # BERT 编码器 + bert-base-chinese 权重
│   │   ├── doc/                     # 输入文件（.docx 教材原文）
│   │   ├── config/                  # 显式+隐式阶段所有 YAML 配置 + schema.yaml
│   │   ├── output/                  # 构建产物（final_kg.json 等）
│   │   └── logs/                    # 运行日志
│   ├── KGRetrieve/             # 检索层 —— 15 个 Agent 原子工具
│   │   ├── tools.py                 # ToolRegistry + 15 个工具 + cross-encoder 重排
│   │   ├── models.py                # 共享数据模型：Node / Edge
│   │   ├── db_backend.py            # KGDBMemory（Neo4j + ChromaDB 后端）+ 导入脚本
│   │   ├── mcp_server.py            # MCP server（Pi Agent / Claude 直连）
│   │   └── TOOLS.md                 # 工具文档
│   ├── utils/
│   │   └── evidence_builder.py     # L3 证据构建工具
│   └── (schema.yaml 已移入 KGBuild/config/)
├── app/                         # Web UI：FastAPI 后端 + React 前端
│   ├── main.py                       # FastAPI 入口（:8777）
│   ├── config.py                     # LLM 配置加载（复用 KGBuild explicit_config）
│   ├── dependencies.py               # 懒加载单例（KGDBMemory / ToolRegistry）
│   ├── routers/                      # chat.py（SSE Agent 对话）+ graph.py（图谱可视化）
│   ├── services/                     # langgraph_agent.py（LangGraph StateGraph 循环）+ agent_loop.py（兼容 shim）+ graph_service.py
│   └── frontend/                     # React/Vite/AntD/sigma.js 前端（:5777）
├── requirements.txt
└── README.md
```

## 四层知识图谱模型

| 层 | 名称 | 内容 | 来源 |
|----|------|------|------|
| L0 | 本体层 | 实体类型定义、关系类型定义、`layer_hint` 分层规则 | `src/KGBuild/config/schema.yaml` |
| L1 | 概念层 | 抽象领域术语（如"强化学习""效用函数"），跨文档可合并 | `Aggr.py` 分层判定 + `Dedup.py` 去重 |
| L2 | 实体层 | 具体实例（如"AlphaGo"），带属性和别名 | `Extraction.py` 抽取 + `Aggr.py` 分层 |
| L3 | 证据层 | 原文小节片段，支撑 L1/L2 节点的溯源 | `TextSegmentation.py` 解析 + `evidence_builder.py` 构建 |

层间关系通过 `edges` 数组中的 `layer` 字段标注：同层（L1-L1、L2-L2）和跨层（L2→L1、L1→L3 `described_by`、L2→L3 `appears_in`）。跨层边的类型还编码了证据引用强度（`rel`），见[混合查询链路与引用加权](#混合查询链路与引用加权)。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 准备数据

- 将教材源文件（.docx）放到：`src/KGBuild/doc/`
- 将 BERT 模型文件夹放到：`src/KGBuild/model/bert-base-chinese/`
  （内含 `config.json`、`model.safetensors`、`vocab.txt` 等）

### 3. 配置

```bash
cp src/KGBuild/config/explicit_config.yaml.example src/KGBuild/config/explicit_config.yaml
```

编辑 `explicit_config.yaml` 填写 LLM API 密钥。`hidden_config.yaml` 通过 `include` 自动继承显式配置的密钥。

### 4. 构建知识图谱

**显式阶段**（TOC 解析 → 实体关系抽取 → toc_graph）：

```bash
python src/KGBuild/TextSegmentation.py
python src/KGBuild/Extraction.py
python src/KGBuild/toc_graph.py
```

**隐式阶段**（格式转换 → 分层 → 嵌入 → 去重 → 关系预测 → 四层装配）：

```bash
python src/KGBuild/Conv.py
python src/KGBuild/Aggr.py
python src/KGBuild/Embedding.py
python src/KGBuild/Dedup.py
python src/KGBuild/Pred.py
python src/KGBuild/FinalKG.py
```

输出：`src/KGBuild/output/final_kg.json`

### 5. 启动 Web UI（可选）

启动方法见下方 [Web UI](#web-ui) 章节。

## 知识图谱格式

`final_kg.json` 采用四层分层结构：

```json
{
  "L0_schema": {
    "entity_types": { ... },
    "relation_types": { ... }
  },
  "L1_concepts": [
    {"node_id": "concept_xxx", "name": "强化学习", "type": "学习方法", "aliases": ["RL"], ...}
  ],
  "L2_entities": [
    {"node_id": "ent_xxx", "name": "AlphaGo", "type": "实例", "aliases": [], ...}
  ],
  "L3_evidences": [
    {"node_id": "ev_1.1.1", "name": "传统视角下的智能体", "type": "subsection", "content": "...", ...}
  ],
  "edges": [
    {"source": "ent_xxx", "target": "concept_yyy", "type": "belongs_to", "layer": "L2-L1"},
    {"source": "concept_yyy", "target": "ev_1.1.1", "type": "described_by", "layer": "L1-L3"}
  ]
}
```

当前 POC 数据规模：453 概念 / 141 实体 / 42 证据 / 1226 条边。

## KGRetrieve —— 15 个原子工具

| 工具 | 作用 |
|------|------|
| `get_schema` | 获取 L0 本体定义 |
| `get_node_by_id` | 按 ID 精确获取任意层节点 |
| `get_concept_by_id` | 按 ID 精确获取 L1 概念节点 |
| `get_entity_by_id` | 按 ID 精确获取 L2 实体 |
| `get_evidence_by_id` | 按 ID 精确获取 L3 证据 |
| `get_relations` | 获取节点的所有关系（出边+入边） |
| `get_entities_of_concept` | 获取某概念下的所有实体（L1→L2） |
| `get_evidences_of_node` | 获取某节点的所有溯源证据 |
| `multi_hop_traverse` | 多跳遍历（BFS，可指定层数） |
| `search_concepts` | 按名称/语义模糊搜索 L1 概念 |
| `search_entities` | 按名称/语义模糊搜索 L2 实体 |
| `search_evidences` | 按关键词搜索 L3 证据 |
| `semantic_search` | 跨层语义搜索（bge-m3 向量 + 名称混合） |
| `rank_results` | 对候选 node_id 用 bge-reranker-v2-m3 做 cross-encoder 精排 |
| `assemble_context` | 把收集到的 ID 组装成带溯源标注、可粘贴的 context 块 |

后端为 `KGDBMemory`（Neo4j + ChromaDB + Ollama），可通过 `python -m src.KGRetrieve.mcp_server` 暴露为 MCP server，供 Pi Agent / Claude 等 Agent 直连调用。

## 混合查询链路与引用加权

### 1. 查询链路：向量定位入口，图内继续查询

Agent 对任何问题的检索都遵循同一链路：**先用向量/关键词工具定位入口节点，再进图沿边取证据**。

```
向量定位入口                  图内继续查询
semantic_search         →  get_*_by_id（确认节点）
search_concepts         →  get_relations（看关系）
search_entities         →  get_evidences_of_node（取溯源证据）
search_evidences        →  get_evidence_by_id（取证据原文）
```

关键点：**不存在「具体问题就跳过图查询」**。因为答案必须引用 L3 证据原文（否则引用 grounding 归零），而证据正文在图边背后，只能靠 `get_evidences_of_node` 沿 L1/L2→L3 边取到。向量库里存的是概念/实体简介，不是答案内容。

随问题模糊度变化的是**图遍历深度**，而非「用不用图」：

- 具体/单概念问题 → 浅遍历：单跳（节点 → 证据）
- 模糊/多概念/对比问题 → 深遍历：多跳（`multi_hop_traverse`、`get_entities_of_concept`）

（25 题 eval 实测：全部先向量后图、无纯向量作答；definition 类仅 1/5 用多跳，comparison 类 4/5 用多跳。）

### 2. 跨层边 rel 与证据引用加权

跨层边（L1/L2 → L3）的类型编码了证据的**引用强度**，`get_evidences_of_node` 返回时以 `rel` 字段标注：

| 边类型 | 层 | 语义 | rel | 权重 |
|--------|-----|------|-----|------|
| `described_by` | L1-L3 | 证据在「定义/讲解」此节点（强/定义性） | `definition` | 1.0 |
| `appears_in` | L2-L3 | 证据只是「提到」此节点（弱/提及性） | `mention` | 0.5 |

用于两处：

1. **检索传播加权**（`eval/retrieval/runner.py::rank_evidences_full`）：概念/实体命中后沿边传播证据，同排名时 `described_by` 排在 `appears_in` 前——「定义这段的证据」优先于「只是提到这个实体的证据」。
2. **judge 引用加权**（`eval/agent/runner.py::resolve_citation_rels` + `eval/agent/judge.py`）：把被引证据按 rel 分类（definition/mention）喂给 LLM judge，定义性引用权重高于提及性；同时产出确定性指标 `citation_weighted`（rel 加权引用真实性），作为不受 LLM judge 漂移影响的硬指标。

## Web UI

内置两页式 Web 界面，直接调用 `KGDBMemory` + `ToolRegistry`，走浏览器即可对话和看图。

| 页面 | 功能 |
|------|------|
| **Agent 对话**（`/chat`） | 与检索 Agent 多轮对话；实时展示可折叠的**工具调用轨迹**（工具名/耗时/参数/结果），回答 Markdown 流式输出并引用 `[ev_xxx]` 证据；支持中途停止、清空、历史回顾 |
| **知识图谱**（`/graph`） | L1 概念（蓝）/ L2 实体（绿）/ L3 证据（橙）分层配色的力导向图；挂载即加载全图采样，支持层级筛选、名称搜索、双击展开 2 跳邻域、单击查看节点/关系详情 |

### 技术栈

- 后端：FastAPI + SSE 流式（`app/routers/`），Agent 循环基于 LangGraph StateGraph（`app/services/langgraph_agent.py`，`agent_loop.py` 为兼容 shim），LLM 复用 `src/KGBuild/config/explicit_config.yaml` 的 API 配置
- 前端：React 19 + Vite + AntD 6 + Zustand + sigma.js/graphology（`app/frontend/`）

### 启动

前置：Neo4j 已启动，并已导入图谱数据（`python -m src.KGRetrieve.db_backend import`）。

```bash
# 后端（:8777）
python -m uvicorn app.main:app --port 8777

# 前端（:5777）
cd app/frontend
npm install          # 首次
npm run dev
```

浏览器打开 `http://localhost:5777`。

### 说明

- Agent 循环基于 **LangGraph StateGraph**（agent → tools 条件回环 + 流式回答），SSE 六事件协议（status / tool_call / tool_result / chunk / done / error）逐字保真，前端零改动接入。
- 图谱可视化查询（分层采样 / 邻域子图 / 统计）为 `KGDBMemory` 的纯追加方法，不触碰原有检索逻辑。
- **Windows 网络注意**：`vite.config.ts` 已把代理 target 指向 `127.0.0.1:8777` 并将 `host` 设为 `true`（同时监听 IPv6），避免 `localhost` 的 IPv6→IPv4 回退造成每次请求 ~2s 的固定延迟。若自行修改代理配置，请保持用 `127.0.0.1` 而非 `localhost`。

## 引用

- [TreeKG 复现](https://github.com/lzl8800/TreeKG)：提供了知识图谱构建的核心实现思路和代码基础。
