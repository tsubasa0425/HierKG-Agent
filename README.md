# TreeKG

面向教科书/技术文档的四层知识图谱构建与检索系统。将文档从"检索骨架"重构为 **L0 本体 / L1 概念 / L2 实体 / L3 证据** 四层分层模型，并提供 14 个原子工具供 Agent 自主调用检索。

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
│   ├── KGRetrieve/             # 检索层 —— 14 个 Agent 原子工具
│   │   ├── tools.py                 # ToolRegistry + 14 个工具实现
│   │   ├── memory.py                # KGMemory（纯内存后端）
│   │   ├── db_backend.py            # KGDBMemory（Neo4j + ChromaDB 后端）
│   │   ├── demo_smoke_test.py       # 冒烟测试
│   │   ├── demo_agent_orchestration.py  # 伪 Agent 多轮编排示例
│   │   └── TOOLS.md                 # 工具文档
│   ├── utils/
│   │   └── evidence_builder.py     # L3 证据构建工具
│   └── (schema.yaml 已移入 KGBuild/config/)
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

层间关系通过 `edges` 数组中的 `layer` 字段标注：同层（L1-L1、L2-L2）和跨层（L2→L1、L1→L3 `described_by`、L2→L3 `appears_in`）。

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

### 5. 验证检索工具

```bash
python -m src.KGRetrieve.demo_smoke_test
```

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

## KGRetrieve —— 14 个原子工具

| 工具 | 作用 |
|------|------|
| `search_concepts` | 按名称模糊搜索 L1 概念 |
| `search_entities` | 按名称模糊搜索 L2 实体 |
| `search_evidences` | 按关键词搜索 L3 证据 |
| `semantic_search` | 跨层语义搜索（需 ChromaDB 后端） |
| `get_entity_by_id` | 按 ID 精确获取实体 |
| `get_node_by_id` | 按 ID 精确获取任意层节点 |
| `get_evidence_by_id` | 按 ID 精确获取证据 |
| `get_evidences_of_node` | 获取某节点的所有溯源证据 |
| `get_relations` | 获取节点的所有关系（出边+入边） |
| `get_neighbors` | 获取邻居节点 |
| `multi_hop_traverse` | 多跳遍历（BFS，可指定层数） |
| `rank_results` | 对一组 node_id 按意图相关性重排 |
| `get_schema` | 获取 L0 本体定义 |
| `get_layer_overview` | 获取某层的统计概览 |

支持双后端：`KGMemory`（纯内存，零依赖）和 `KGDBMemory`（Neo4j + ChromaDB，生产级），接口一致、零改动切换。

## 引用

- [TreeKG 复现](https://github.com/lzl8800/TreeKG)：提供了知识图谱构建的核心实现思路和代码基础。
