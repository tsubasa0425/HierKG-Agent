# HierKG 评测子系统

证明「四层知识图谱检索 > 扁平 RAG」的评估体系：手写 25 道黄金 QA 题，跑**检索层**（确定性、零成本）+ **端到端 Agent 层**（真实 `run_agent` + LLM-as-judge），并加朴素 RAG 扁平向量检索做**消融基线**。

## 快速开始

前置：Neo4j（`neo4j.bat console`）与 Ollama（bge-m3）在跑，`chroma_db/` 存在。

```bash
# ① 校验数据集（不碰 DB）
python -m eval.datasets.resolver --validate eval/datasets/hierkg_qa.json

# ② 数据纪律：evidence_ids(投影) 与跨层边(canonical) 一致性（不碰 DB，秒级）
python -m eval.validate_kg

# ③ 检索层 + 消融（<1 分钟，不调 LLM）
python -m eval.run --mode retrieval --baseline --limit 25

# ③ 端到端 Agent 层（先冒烟省钱）
python -m eval.run --mode agent --limit 3 --judge --max-rounds 5

# ④ 全量两层 + 判分（约 10–40 分钟，DeepSeek flash 几元）
python -m eval.run --mode all --baseline --judge --max-rounds 8

# ⑤ 生成报告
python -m eval.report eval/runs/<最新目录>
```

每次运行产物落在 `eval/runs/<UTC 时间戳>/`：
- `config.jsonl` 运行配置 + git HEAD + stack 状态（含 `chroma_fallback` 降级标记）
- `retrieval.jsonl` 逐题三层排序 + 指标（含 multi-hop 结构化路径探针）
- `retrieval_summary.json` 跨题聚合 + Δ 消融
- `traces.jsonl`（agent 层）逐题 SSE 轨迹 + 引用/grounding 指标 + judge 分
- `report.md` markdown 报告（头号结论 + 检索表 + 按类别表 + Agent 表 + 逐题明细 + 复现命令）

## 两层设计

### 检索层（`eval/retrieval/`）
确定性流水线，直接调 `registry.call()` 拿完整 `ToolResult.data`，**不解析 SSE**（SSE 的 `tool_result.data` 被 `compact_summary` 压成计数、无 node_id）。

每道题：
1. `rank_concepts(q)` = `semantic_search` L1 + `search_concepts`（去重保序）
2. `rank_entities(q)` 同构（L2 + `search_entities`）
3. `rank_evidences_full(q)` = 直接证据命中 + **概念/实体 `evidence_ids` 传播**，按「来源位次」合并取 min 排序——传播是 KG 相对扁平检索的结构性优势。传播**成员**来自检索结果自带的 `evidence_ids`（与 agent 实际所见一致），**权重**来自跨层边类型（`described_by` 1.0 / `appears_in` 0.5，`weight_type=False` 可关闭做消融）；本数据集上类型加权对 Recall@k 无影响（节点层即决定边类型），其价值在工具层 `get_evidences_of_node` 返回的 `rel` 上
4. multi-hop 题额外做结构化路径探针（`get_relations` 出边直达 / `multi_hop_traverse`）

指标：Recall@k、Hit@k、Precision@k（k∈{1,3,5,10}）、MRR。

### 朴素 RAG 基线（`eval/retrieval/baseline.py`）
`FlatEvidenceBaseline.retrieve(q, k)` **只走 `search_evidences`**，与 KG 方法共用同一 `search_by_name` 后端（ChromaDB + Neo4j 名称匹配）。图「不图」的唯一差异 = 有没有概念/实体传播，保证 Δ 可归因于图谱结构。

### Agent 层（`eval/agent/`）
驱动完整 `run_agent`（LangGraph 图），收集 SSE 事件。指标：
- `tool_rounds / tool_calls / elapsed_ms / has_error / no_done`
- `cited_ids`：从 `done.answer` 用 `\[(ev_[0-9A-Za-z_.#一-鿿]+)\]` 提取引用
- **引用 grounding**（重放）：对每条 `tool_call` 事件重新 `registry.call()`，用 `extract_evidence_ids` 汇总系统真检索到的证据，算答案引用里被检索到的比例
- `citation_coverage`：golden 证据被答案引用的比例

judge 上下文喂的是 agent **实际检索到的证据片段**（不是重新搜索的 top-5），避免误判。

### LLM-as-judge（`eval/agent/judge.py`）
DeepSeek（temp 0，OpenAI 兼容），一次评四维（faithfulness/completeness/relevance/citation，1–5）+ 一句理由，严格 JSON。3 次重试退避；失败回退确定性启发式（引用 grounding → 忠实度/引用，中文 bigram Dice → 完整度/相关性），标 `judge_source: llm|fallback`，报告只在 llm 行上断言硬指标。

## 黄金数据集（`eval/datasets/hierkg_qa.json`）

25 题，5 类各 5 题：`definition / single_hop / multi_hop / instance / comparison`。

出题规则：
- **写名不写 ID**：概念/实体只写节点名称，ID 由 `GoldenResolver` 从 `final_kg.json` 解析（防手打错别）；证据 ID（如 `ev_1.1.1`）本身是稳定章节号，直接写
- `golden_evidence_ids` 来自 golden 节点的 `evidence_ids`，必须用**去重后** ID（镜像 `ev_1.4#2`，见 `resolver.py::_dedupe_evidence_ids`）
- multi-hop 题额外给 `golden_src_name / golden_tgt_name`（路径探针用）

数据校验：`python -m eval.datasets.resolver --validate` 检查全部名称可解析、ID 存在且格式合法。

## 模块结构

```
eval/
  run.py              # CLI 入口（--mode retrieval|agent|all）
  validate_kg.py      # 数据纪律：evidence_ids(投影)==跨层边(canonical) 一致性 + 孤儿证据报告
  common.py           # ID/引用正则、extract_evidence_ids 深遍历、JSONL 写出、run 目录
  report.py           # 聚合 → report.md
  datasets/
    hierkg_qa.json    # 黄金数据集
    resolver.py       # GoldenResolver：名称→ID、证据去重镜像、校验
  retrieval/
    runner.py         # RetrievalRunner：确定性工具流水线 → 分层排序
    metrics.py        # recall@k / hit@k / precision@k / mrr + 聚合
    baseline.py       # FlatEvidenceBaseline：朴素 RAG 扁平证据检索
  agent/
    runner.py         # AgentRunner：SSE 轨迹、引用覆盖率 + grounding
    judge.py          # LLM-as-judge + 重试 + 关键词回退
  runs/               # 运行产物（git 忽略）
```

`eval/` 只读使用现有代码（`app.services.agentscope_agent`（其 `run_agent` 兼容适配仍输出旧事件词表）、`app.config`、`src.KGRetrieve`），不 import `app.main`，不改动运行时。
