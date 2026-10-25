# HierKG-Agent

面向有结构文档（比如教科书）的四层知识图谱构建与检索系统。将文档重构为 **L0 本体 / L1 概念 / L2 实体 / L3 证据** 四层分层图谱，提供 15 个原子工具供 Agent 自主调用检索，并内置 Web UI（Agent 对话 + 图谱可视化）。

## 核心亮点

- **四层知识图谱**：L0 本体 / L1 概念 / L2 实体 / L3 证据，跨层边编码引用强度（定义 / 提及），答案可溯源到原文小节
- **15 个原子检索工具**：精确查找 / 图查询 / 语义搜索 / 精排与上下文组装，供 Agent 自主编排
- **混合查询链路**：向量定位入口节点，再沿图谱边取证据——回答必须引用 L3 证据原文，杜绝无源回答
- **LangGraph Agent**：StateGraph 工具循环 + SSE 流式回答，工具调用全过程实时可视化
- **多端接入**：内置 Web UI（对话 + 图谱可视化），亦可作为 MCP server 供 Claude / Pi Agent 直连

## 快速开始

### 1. 安装依赖

本项目用 [uv](https://docs.astral.sh/uv/) 管理环境：Python 版本固定在 `.python-version`（3.12），依赖版本锁定在 `uv.lock`。

```bash
uv sync
```

`uv sync` 会创建 `.venv/` 并按 `uv.lock` 精确安装。依赖包含 `sentence-transformers` 与 CPU 版 `torch`（供 `rank_results` 精排使用，无需 CUDA）。

国内网络可加镜像加速：

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
```

之后有两种用法，任选其一：

```bash
# 方式一：用 uv run 执行（自动使用项目环境，无需激活）
uv run python -m uvicorn app.main:app    # 默认监听 127.0.0.1:8000

# 方式二：先激活虚拟环境，后续沿用下面文档里的 python 命令
source .venv/Scripts/activate    # Windows bash
.venv\Scripts\activate           # cmd / PowerShell
```

### 2. 准备数据

- 将教材源文件（.docx）放到：`src/KGBuild/doc/`
- 启动 Ollama 并拉取嵌入模型（建图第 7 步与检索侧共用同一模型）：

  ```bash
  ollama serve            # 或直接运行 Ollama 桌面端
  ollama pull bge-m3
  ```

- （可选）`rank_results` 的精排模型 `bge-reranker-v2-m3` 约 2.2GB：放到 `src/KGRetrieve/models/bge-reranker-v2-m3/` 可离线使用；缺失时首次调用会自动从 HF 镜像下载到缓存。

### 3. 配置

```bash
cp src/KGBuild/config/config.yaml.example src/KGBuild/config/config.yaml
```

编辑 `config.yaml` 填写 LLM API 密钥。流水线各阶段配置（`extraction` / `text` / `conv` / `aggr` / `dedup` / `pred` / `emb`）通过 `include_files` 自动合并；`schema.yaml`（L0 本体）由各阶段直接读取。

### 4. 构建知识图谱

一键串联整条流水线（docx 解析 → 实体抽取 → toc_graph → 证据构建 → 格式转换 → 分层 → 嵌入 → 去重 → 关系预测 → 四层装配）：

```bash
python src/KGBuild/pipeline.py          # 完整跑一遍
python src/KGBuild/pipeline.py --check  # 只预检各步骤输入文件，不开跑
```

中途失败可用 `--start <步骤名>` 断点续跑，`--only / --skip / --until` 精细选择步骤（`python src/KGBuild/pipeline.py --help` 查看）。

输出：`src/KGBuild/output/final_kg.json`

以上步骤也可在 Web 端「图谱构建」（`/build`）完成：上传或选择 docx、在线编辑 LLM/存储配置与各阶段 YAML（含 `schema.yaml`）、实时查看 10 步进度与日志、取消/续跑，并在二次确认后导入 Neo4j。若还没有 `config.yaml`，可直接在该页「编辑配置」中填写，无需手动复制模板。

### 5. 启动 Web UI

前置：Neo4j 已启动，并已导入图谱数据（`python -m src.KGRetrieve.db_backend import`）。

```bash
# 后端（默认 :8000）
python -m uvicorn app.main:app

# 前端（:5777）
cd app/frontend
npm install          # 首次
npm run dev
```

浏览器打开 `http://localhost:5777`。前端通过 Vite 把 `/api` 代理到 `http://127.0.0.1:8000`；若后端换了端口，启动前端时用 `VITE_API_TARGET=http://127.0.0.1:<端口> npm run dev` 覆盖，无需改代码。

## 工作原理

### 四层知识图谱模型

| 层 | 名称 | 内容 | 来源 |
|----|------|------|------|
| L0 | 本体层 | 实体类型定义、关系类型定义、`layer_hint` 分层规则 | `src/KGBuild/config/schema.yaml` |
| L1 | 概念层 | 抽象领域术语（如"强化学习""效用函数"），跨文档可合并 | `Aggr.py` 分层判定 + `Dedup.py` 去重 |
| L2 | 实体层 | 具体实例（如"AlphaGo"），带属性和别名 | `Extraction.py` 抽取 + `Aggr.py` 分层 |
| L3 | 证据层 | 原文小节片段，支撑 L1/L2 节点的溯源 | `TextSegmentation.py` 解析 + `evidence_builder.py` 构建 |

层间关系通过 `edges` 数组中的 `layer` 字段标注：同层（L1-L1、L2-L2）和跨层（L2→L1、L1→L3 `described_by`、L2→L3 `appears_in`）。跨层边的类型还编码了证据引用强度（`rel`），见下方[证据引用加权](#证据引用加权)。

### 证据引用加权（rel）

跨层边（L1/L2 → L3）的类型编码了证据的**引用强度**，`get_evidences_of_node` 返回时以 `rel` 字段标注：

| 边类型 | 层 | 语义 | rel | 权重 |
|--------|-----|------|-----|------|
| `described_by` | L1-L3 | 证据在「定义/讲解」此节点（强/定义性） | `definition` | 1.0 |
| `appears_in` | L2-L3 | 证据只是「提到」此节点（弱/提及性） | `mention` | 0.5 |

加权用于两处：

1. **检索传播加权**：概念/实体命中后沿边传播证据，同排名时 `described_by` 排在 `appears_in` 前——「定义这段的证据」优先于「只是提到这个实体的证据」。
2. **judge 引用加权**：LLM 判分时定义性引用权重高于提及性；并产出确定性指标 `citation_weighted`（rel 加权引用真实性），不受 LLM judge 漂移影响。

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


### 混合查询链路：向量定位入口，图内继续查询

Agent 对任何问题的检索都遵循同一链路：**先用向量/关键词工具定位入口节点，再进图沿边取证据**。

```
向量定位入口                  图内继续查询
semantic_search         →  get_*_by_id（确认节点）
search_concepts         →  get_relations（看关系）
search_entities         →  get_evidences_of_node（取溯源证据）
search_evidences        →  get_evidence_by_id（取证据原文）
```


随问题模糊度变化的是**图遍历深度**：

- 具体/单概念问题 → 浅遍历：单跳（节点 → 证据）
- 模糊/多概念/对比问题 → 深遍历：多跳（`multi_hop_traverse`、`get_entities_of_concept`）



## 检索工具与架构

### 15 个原子工具

| 工具 | 作用 |
|------|------|
| `get_schema` | 获取 L0 本体定义 |
| `get_node_by_id` | 按 ID 精确获取任意层节点 |
| `get_concept_by_id` | 按 ID 精确获取 L1 概念节点 |
| `get_entity_by_id` | 按 ID 精确获取 L2 实体 |
| `get_evidence_by_id` | 按 ID 精确获取 L3 证据 |
| `get_relations` | 获取节点的所有关系（出边+入边） |
| `get_entities_of_concept` | 获取某概念下的所有实体（L1→L2） |
| `get_evidences_of_node` | 获取某节点的所有溯源证据（带 rel 引用强度） |
| `multi_hop_traverse` | 多跳遍历（BFS，可指定层数） |
| `search_concepts` | 按名称/语义模糊搜索 L1 概念 |
| `search_entities` | 按名称/语义模糊搜索 L2 实体 |
| `search_evidences` | 按关键词搜索 L3 证据 |
| `semantic_search` | 跨层语义搜索（bge-m3 向量 + 名称混合） |
| `rank_results` | 对候选 node_id 用 bge-reranker-v2-m3 做 cross-encoder 精排，模型不可用时自动降级为关键词重叠 |
| `assemble_context` | 把收集到的 ID 组装成带溯源标注、可粘贴的 context 块 |

后端为 `KGDBMemory`（Neo4j + ChromaDB + Ollama），可通过 `python -m src.KGRetrieve.mcp_server` 暴露为 MCP server，供 Pi Agent / Claude 等 Agent 直连调用。

### 项目结构

```
HierKG-Agent/
├── src/
│   ├── KGBuild/                # 四层知识图谱构建流水线
│   │   ├── TextSegmentation.py     # docx → TOC + 原文小节
│   │   ├── Extraction.py           # 小节 → 实体/关系抽取
│   │   ├── toc_graph.py            # 构建 toc_graph.json
│   │   ├── Conv.py                 # 结果格式转换
│   │   ├── Aggr.py                 # concept/entity 分层判定
│   │   ├── Embedding.py            # Ollama 向量化 → node_embeddings.pkl
│   │   ├── Dedup.py                # 概念去重（调用 dedup/ 子包）
│   │   ├── Pred.py                 # 关系预测
│   │   ├── FinalKG.py              # 四层 KG 装配 → final_kg.json
│   │   ├── pipeline.py              # 一键串联全部构图步骤
│   │   ├── evidence_builder.py      # L3 证据构建
│   │   ├── dedup/                   # 去重子包（knn / llm / name_similarity）
│   │   ├── doc/                     # 输入文件（.docx 教材原文）
│   │   ├── config/                  # 流水线各阶段 YAML 配置 + schema.yaml
│   │   ├── output/                  # 构建产物（final_kg.json 等）
│   │   └── logs/                    # 运行日志
│   ├── KGRetrieve/             # 检索层 —— 15 个 Agent 原子工具
│   │   ├── tools.py                 # ToolRegistry + 15 个工具 + 可选 cross-encoder 重排
│   │   ├── models.py                # 共享数据模型：Node / Edge
│   │   ├── db_backend.py            # KGDBMemory（Neo4j + ChromaDB 后端）+ 导入脚本
│   │   ├── mcp_server.py            # MCP server（Pi Agent / Claude 直连）
│   │   └── TOOLS.md                 # 工具文档
│   └── (schema.yaml 已移入 KGBuild/config/)
├── app/                         # Web UI：FastAPI 后端 + React 前端
│   ├── main.py                       # FastAPI 入口（默认 :8000）
│   ├── config.py                     # LLM 配置加载（复用 KGBuild config.yaml）
│   ├── dependencies.py               # 懒加载单例（KGDBMemory / ToolRegistry）
│   ├── routers/                      # chat.py（SSE Agent 对话）+ graph.py（图谱可视化）
│   ├── services/                     # agentscope_agent.py（AgentScope 2.0.7 编排，原生事件帧流）+ chat_service.py（热缓存编排）+ chat_store.py/qa_cache.py/kg_version.py + graph_service.py
│   └── frontend/                     # React/Vite/AntD/sigma.js 前端（:5777）
├── eval/                         # 评测子系统
├── pyproject.toml                # 依赖声明（uv 管理）
├── uv.lock                       # 锁定版本，保证可复现
├── .python-version               # Python 3.12
└── README.md
```

## Web UI

内置三页式 Web 界面，直接调用 `KGDBMemory` + `ToolRegistry`，走浏览器即可建图、对话和看图。

| 页面 | 功能 |
|------|------|
| **Agent 对话**（`/chat`） | 与检索 Agent 多轮对话；实时展示可折叠的**思考片段**与**工具调用轨迹**（推理/工具名/耗时/参数/结果），回答 Markdown 流式输出并引用 `[ev_xxx]` 证据；支持中途停止、清空、历史回顾 |
| **知识图谱**（`/graph`） | L1 概念（蓝）/ L2 实体（绿）/ L3 证据（橙）分层配色的力导向图；挂载即加载全图采样，支持层级筛选、名称搜索、双击展开 2 跳邻域、单击查看节点/关系详情 |
| **图谱构建**（`/build`） | 在线编辑 LLM / Neo4j / Ollama 配置与各阶段 YAML（含 L0 `schema.yaml`，服务端校验后原子写入）；选择或上传 docx，带心跳的 SSE 实时进度与日志；取消、从失败步骤重试；导入前强制确认（会清空 Neo4j 并重建 Chroma collection，预检失败不会执行任何破坏性操作） |

### 技术栈

- 后端：FastAPI + SSE 流式（`app/routers/`），Agent 引擎为 AgentScope 2.0.7 统一 Agent（`app/services/agentscope_agent.py`，编排层逐事件转发原生事件帧），LLM 复用 `src/KGBuild/config/config.yaml` 的 API 配置
- 前端：React 19 + Vite + AntD 6 + Zustand + sigma.js/graphology（`app/frontend/`）