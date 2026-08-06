# TreeKG 知识图谱分层重构方案

> 面向企业知识管理场景的分层知识图谱设计说明
> 目标岗位：AI 应用开发工程师（简历项目）
> 日期：2026-07-31

---

## 一、背景与问题

### 1.1 现有分层方案（文档结构分层）

```
章 → 节 → 小节 → core 实体 → non-core 实体
```

### 1.2 存在的问题

| # | 问题 | 说明 |
|---|------|------|
| 1 | **文档结构 ≠ 知识结构** | 章-节-小节是作者的行文组织，不是知识本身的组织。实体挂在 `section_id` 下，等于把网状知识强行压成树状归属，知识被锁死在文档结构里 |
| 2 | **小节边界 ≠ 知识边界** | 一个概念可能 1.1 引入、3.2 深入、5.4 应用，挂在哪一层都是错的 |
| 3 | **core/non-core 静态划分 vs 动态问题** | 重要性判定是一次性的，但"问题里什么是核心"是随问题变化的。问到 non-core 实体时恰恰答不上来 |
| 4 | **检索漏斗错误级联放大** | 意图分类错 → 章节定位错 → 子图检索错 → 生成错误答案，前端一错全错 |
| 5 | **多文档时结构层崩坏** | 两篇文档都讲"变压器"，结构层出现两个独立节点，跨文档知识永远无法合并 |
| 6 | **无法评估** | 没有 ground truth，无法证明"分层检索比普通 RAG 好" |

### 1.3 核心结论

> **分层按"知识的类型"分，不按"文档的结构"分。**
>
> 文档的章-节-小节是作者的行文组织。企业知识管理场景下文档类型五花八门（需求文档、设计文档、SOP、运维手册、会议纪要），结构不统一——按文档结构分层，每换一种文档就废一次。按知识类型分层，才是跨文档、跨项目都能用的骨架。

---

## 二、四层模型（核心设计）

```
┌─────────────────────────────────────────────────┐
│ L0 本体层 (Schema)   概念类型/关系类型/属性定义      │
│     如: 项目、模块、接口、负责人、状态               │
├─────────────────────────────────────────────────┤
│ L1 概念层 (Concepts)  跨文档统一的领域术语           │
│     如: 需求评审、验收标准、环境部署、数据字典        │
├─────────────────────────────────────────────────┤
│ L2 实体层 (Entities)  具体实例                     │
│     如: XX银行项目、结算模块、李工、/api/pay        │
├─────────────────────────────────────────────────┤
│ L3 证据层 (Evidence)  原文片段/章节/文件位置         │
│     如: 需求文档§3.2、设计文档§5.1                  │
└─────────────────────────────────────────────────┘
```

### 2.1 新旧方案对比

| | 旧分层（文档结构） | 新分层（知识类型） |
|---|---|---|
| 骨架 | 章/节/小节（作者叙事） | 本体+概念（知识本质） |
| 实体归属 | 挂在某个小节下（锁死） | 挂在概念下（跨文档天然可合并） |
| 章节 | 检索骨架 | 降级为证据层的一部分（溯源用） |
| core/non-core | 静态重要性划分 | 弱化，由"概念层 vs 实体层"天然替代 |
| 跨文档 | 结构层分裂，合并不了 | 同一概念节点被多文档共享，天然合并 |

**关键转变：文档从"骨架"降级为"证据"。** 它不再决定知识怎么组织，只负责"这个知识出自哪"——溯源功能一点不丢，但检索不再被文档结构绑架。

---

## 三、每层构建方法（结合现有代码）

### 3.1 L0 本体层 —— 手工起步，LLM 辅助

- **放什么**：实体类型（项目/模块/接口/人员/术语）、关系类型（负责/属于/依赖/包含/先决条件）、属性（负责人、状态、有效期）
- **谁来建**：初期**人工定义 10-20 个类型**即可，跑通再扩。动态 Ontology（LLM 自动生成 Schema）作为二期
- **为什么手工**：本体错了下面全错，这是全图最需要人把关的一层。LLM 归纳候选类型可以，最终拍板要人
- **落地产物**：一个 `schema.yaml`，写死即可

### 3.2 L1 概念层 —— LLM 抽取 + 人工审一批

- **放什么**：领域术语，**跨文档统一**的词汇表。外包场景例子："需求评审"、"验收标准"、"灰度发布"、"数据字典"
- **谁来建**：从文档集合里 LLM 抽取候选术语，人工过一遍（去重、定标准名）
- **关键作用**：这是旧方案里"实体去重"问题的主要解决地——**概念天然就该是去重后的**。两篇文档讲"AI"和"人工智能"，在概念层必须是一个节点
- **实现方式**：现有实体抽取 Prompt 加一个字段 `is_concept: bool`，LLM 判断抽出来的是概念还是实例；概念标准化可复用现有 Dedup 逻辑

### 3.3 L2 实体层 —— 现有抽取逻辑直接复用

- **放什么**：具体实例。`结算模块`、`XX银行项目`、`李工`、`接口/api/pay`
- **谁来建**：现有 LLM 实体抽取逻辑，增加概念/实例分拣（见 3.2）
- **属性跟着实体走**：`结算模块 负责人=李工 状态=维护中`

### 3.4 L3 证据层 —— 现有章节摘要的归宿

- **放什么**：原文片段（带文件+章节+行号）、章节摘要
- **谁来建**：现有 TextSegmentation + Summarize 几乎不用改
- **核心作用**：**每个 L1/L2 节点和事实都带 evidence 链接**，回答时"答案→概念/实体→原文"溯源链完整

---

## 四、检索按层路由（分层价值的真正体现）

检索漏斗从"问题→章节"变成"问题→层"。

| 问题类型 | 路由到 | 例子 |
|---------|--------|------|
| "什么是 XX？"（定义类） | L1 概念层 | "什么是需求评审？" |
| "XX 是谁/哪个/在哪？"（事实类） | L2+L0 关系查询 | "结算模块谁负责？" |
| "XX 和 XX 什么关系？"（关系类） | L1/L2 跨层关系 | "部署环境和测试环境什么关系？" |
| "XX 项目用什么架构？"（实例属性类） | L2 属性查询 | "XX银行项目用微服务吗？" |
| "这个说法出自哪？"（溯源类） | L3 证据层 | "验收标准这条出自哪份文档？" |

**分层真正的价值**：不同的问题走不同的层，而不是"所有问题都先定位章节"。跨文档查询变得自然——概念层节点被多文档共享，问"变压器"直接命中一个节点，所有相关文档的证据都能拉出来。

---

## 五、迁移路径（从现有代码出发）

1. **改动最小的第一步**：保留 ExplicitKG/HiddenKG 全部代码，只改一个地方——**实体节点不再以 `section_id` 为唯一归属，改为挂"概念节点"，概念节点再挂多个文档的证据**。等于在实体层上面加一层概念层，证据层就是现有章节
2. **概念层抽取**：现有实体抽取 Prompt 加 `is_concept` 字段；概念标准化复用现有 Dedup 逻辑
3. **本体层手工建**：`schema.yaml`，10-20 个类型和关系
4. **检索路由**：现有写死的 `classifier → selector` 改为"按层路由"——这也是后续接入 PiAgent 的切入点，让 LLM 自己决定走哪层

---

## 六、必须提前想的坑

1. **概念层别过度抽象**：概念是"业务名词"，不是"哲学范畴"。抽"需求评审/验收/部署"这种，别抽"协作/流程/质量"这种虚词——虚概念没法用，还污染图谱
2. **本体层是双刃剑**：手工定义可控，但 **schema 一改，下面三层全要重挂**。初期 schema 要小、要稳，跑起来再谈演进
3. **概念/实体的边界判定**：LLM 判断 `is_concept` 可能不稳定，需要人工抽检 + 兜底规则（如出现次数高、跨文档出现 → 倾向概念）

---

## 七、简历/面试价值

### 7.1 项目叙事升级

- 旧：*"我实现了一个从结构化文档构建分层知识图谱的系统"*
- 新：*"我意识到文档结构分层的局限，重构为知识类型四层模型（本体/概念/实体/证据），解决跨文档知识合并与按需检索问题"*

### 7.2 面试话术要点

- 能讲清楚**旧方案的 6 个问题**（见 §1.2）→ 展示系统化思考能力
- 能讲清楚**为什么按知识类型分层** → 展示对知识工程本质的理解
- 能讲清楚**每层怎么建、检索怎么路由** → 展示可落地能力
- 能讲清楚**迁移路径**（改动最小的第一步）→ 展示工程务实性
- 能主动说**局限**（概念过度抽象、schema 变更成本）→ 展示知道自己系统的边界

---

## 八、下一步建议

- [ ] 用真实企业文档（脱敏）跑一遍新四层模型的最小验证
- [ ] 对比旧方案，记录"跨文档合并"和"按层检索"的效果差异
- [ ] 补一个简单评估：同一批问题，分层检索 vs 普通 RAG 的准确率对比
- [ ] 为接入 PiAgent 做准备：检索能力拆成按层路由的工具集

---

## 附录 A：数据源选择指南（个人项目落地）

> 本项目定位为**个人项目**，未真实部署于企业场景。以下数据源均为公开可获取语料，用于验证分层方案的有效性。选型核心原则：**能让四层价值"看得见"**——尤其是「跨文档合并」和「按层路由」这两个核心卖点。

### A.1 选型标准

一个合格的数据集应同时满足以下条件，缺一不可：

| 条件 | 说明 | 对应分层价值 |
|------|------|-------------|
| ① 多文档且同主题 | 同一概念在多份文档中反复出现 | 跨文档合并（L1） |
| ② 本体类型可区分 | 能明确分出概念/实体/证据/关系类型 | L0 本体层 |
| ③ 带章节结构 | 原文可定位到具体段落/章节 | L3 证据层溯源 |
| ④ 概念存在歧义 | 同一术语在不同文档含义不同，或跨文档出现 | 概念标准化价值 |
| ⑤ 可人工构造 QA | 能基于原文造出 5 类问题（定义/事实/关系/属性/溯源） | 评估可行性 |

### A.2 推荐数据源（按优先级）

#### 🥇 方案 A：多本同主题中文技术书（首选，最贴合现有代码）

**适用性**：本项目已在用 `Hello-Agents_0~5.docx`，扩展此思路改动最小。

**数据源示例**：
- GitHub 开源中文技术书系列（如 datawhalechina 组织的教程）
- 同主题多作者书籍：如《机器学习》讲义 + 《统计学习方法》讲义 + 相关论文综述
- 多个智能体/大模型主题的开源电子书

**分层价值体现**：
| 层 | 内容示例 |
|----|---------|
| L0 本体 | 概念/方法/算法/数据集/评估指标 |
| L1 概念 | "梯度下降""注意力机制""Transformer"（跨多本书重复出现） |
| L2 实体 | 具体模型名（BERT/GPT）、具体数据集（ImageNet） |
| L3 证据 | 原文段落带章节定位 |

**简历叙事**："用 N 本开源技术书模拟企业多份技术文档的异构性"

#### 🥈 方案 B：中文法律语料（本体天然清晰，最适合展示 L0）

**适用性**：法律领域是分层 KG 价值最明显的领域之一，合规是真实企业痛点。

**数据源示例**：
- 国家法律法规数据库（flk.npc.gov.cn）——免费、权威、可批量下载
- CAIL2018/CAIL2019（中国法研杯）——法律案例检索/问答数据集，有标注
- LawGPT / DISC-Law-SLLM 项目里的法律语料

**分层价值体现**：
- L0 本体天然存在：民法典「编/章/节/条/款」；刑法「罪名/构成要件/刑罚」
- 跨文档引用密集：民法典↔合同法↔司法解释，跨文档合并是刚需
- 概念歧义真实存在："住所"在民法和刑法里定义不同

**简历叙事**："以中国法律语料模拟企业合规知识库，展示本体层在处理跨法条引用和术语歧义上的价值"

#### 🥉 方案 C：开源项目文档集（最贴近"企业内部文档"形态）

**适用性**：若希望数据"看起来像企业内部文档"，这是最佳选择。

**数据源示例**：
- Kubernetes 官方文档全集（架构 + API + 教程 + FAQ + 故障排查）
- Apache 顶级项目文档（Spark/Flink/Kafka）
- Linux 内核文档

**分层价值体现**：
- 文档类型异构（design/API/tutorial/runbook）→ 对应方案 §1.3 说的"企业文档类型五花八门"
- 跨文档概念重复："Pod"在架构文档、API 文档、教程中均出现
- L2 实体清晰：具体 API、组件名、配置项

### A.3 备选数据源（按场景选用）

| 数据源 | 适合展示 | 局限性 |
|--------|---------|--------|
| HotpotQA / 2WikiMultihopQA | 跨文档多跳推理 | 英文；QA 形式不像"知识库" |
| DuReader / CMRC2018 | 中文阅读理解 | 偏 QA，本体层不好展示 |
| CMeKG / cMedQA | 医疗领域，本体成熟 | 数据获取与清洗成本高 |
| arXiv 论文集（CS.AI 子集） | 概念-论文-作者三层 | 概念层太学术，不像企业场景 |

### A.4 推荐组合策略

基于本项目现状（已跑通 ExplicitKG/HiddenKG 流程，已用 Hello-Agents 文档）：

1. **主数据**：方案 A（2-3 本开源中文技术书）——改动最小，最贴合现有代码
2. **对比实验**（可选）：补一个方案 B 的法律语料子集——证明方案能迁移到不同领域
3. **数据规模建议**：3-5 份文档，总计 5-15 万字——既能体现跨文档价值，又能在单机跑通

### A.5 诚实性声明（简历写作红线）

- ❌ 不可写："用于企业场景" / "部署于企业知识库"
- ✅ 可写："针对企业知识管理场景的痛点设计，在公开数据集上验证方案有效性"

后者更诚实，且更容易在面试中展开技术细节。

---

## 附录 B：评估方案设计

> 方案 §1.2 问题6 将"无法评估"列为旧方案大问题。本附录给出可量化的评估框架，避免重蹈覆辙。

### B.1 评估目标

用数据回答两个核心问题：
1. **分层检索是否优于普通向量 RAG？**（整体有效性）
2. **分层检索在哪类问题上优势最大？**（分层价值定位）

### B.2 评测集构造

**规模**：30-50 道题（个人项目可行量级）

**分类配比**：严格对应方案 §4 的路由表，每类 6-10 道：

| 问题类型 | 路由层 | 题量 | 示例 |
|---------|--------|------|------|
| 定义类 | L1 概念层 | 8-10 | "什么是需求评审？" |
| 事实类 | L2+L0 | 8-10 | "结算模块谁负责？" |
| 关系类 | L1/L2 跨层 | 6-8 | "部署环境和测试环境什么关系？" |
| 属性类 | L2 属性查询 | 6-8 | "XX项目用微服务吗？" |
| 溯源类 | L3 证据层 | 6-8 | "验收标准这条出自哪份文档？" |

**标注要求**：每道题需标注
- 标准答案（人工基于原文构造）
- 期望命中的层（用于验证路由是否正确）
- 期望溯源的文档/章节（用于验证 L3 是否准确）

### B.3 对比基线

| 方法 | 说明 |
|------|------|
| **Baseline-1**：纯向量 RAG | 朴素 chunk 检索 + LLM 生成，无图谱 |
| **Baseline-2**：旧方案（文档结构分层） | 现有 TreeKG，章-节-小节-core-noncore |
| **新方案**：四层知识分层 | 本体/概念/实体/证据

三者用同一评测集、同一 LLM、同一 prompt 模板，只变检索方式。

### B.4 评估指标

#### 主指标：答案准确率

采用 LLM-as-Judge（GPT-4 级模型打分，0/1 二值或 1-5 分）：

```
准确率 = 答对题数 / 总题数
```

按 5 类分别统计，得到分类型的准确率曲线——这是展示"分层在哪类问题上优势最大"的关键证据。

#### 副指标：溯源命中率（仅溯源类问题）

```
命中率 = 正确指向原文档+章节的题数 / 溯源类总题数
```

这是普通 RAG 给不了的硬指标，也是分层方案（尤其 L3）的护城河。

#### 辅助指标：检索召回率（可选）

对每道题，人工标注"应该检索到的关键节点/边"，统计：
```
召回率 = 检索到的关键节点数 / 应检索节点数
```
用于诊断"答错是因为检索不到，还是检索到了但生成错了"。

### B.5 评估执行流程

1. 构造评测集（30-50 题，按 B.2 标注）
2. 三套方法各跑一遍评测集，记录：
   - 最终答案
   - 检索到的节点/边
   - 溯源信息（如有）
3. LLM-as-Judge 打分 + 人工复核争议题（约 10%）
4. 输出对比报告：
   - 整体准确率对比（柱状图）
   - 分类型准确率对比（分组柱状图）
   - 溯源命中率对比（表格）

### B.6 面试可陈述的结果形态

理想情况下，你能说出这样的话：

> "在 40 道题的评测集上，新方案整体准确率 82%，对比纯向量 RAG 的 65% 和旧方案（文档结构分层）的 71%。优势集中在溯源类（90% vs 30%/45%）和跨文档关系类（80% vs 40%/55%）——这正是分层设计针对的痛点。"

**有数字、有分类、有对比、有归因**——这比任何架构图都有说服力。

### B.7 评估局限声明

需在简历和面试中诚实说明：
- 评测集规模较小（30-50 题），统计显著性有限
- LLM-as-Judge 存在主观偏差
- 数据集为公开语料，非真实企业场景
- 指标为离线评估，未做在线 A/B

### B.8 最小验证版本（POC 快速落地）

> 若完整评估（B.1-B.7）受限于精力，可采用本节的**最小可行验证**方案，2-3 天内完成，足以撑住面试追问。

#### 设计原则

POC 不需要完整评估，但**必须有证据**。面试官不会问"准确率多少"，但一定会问"你怎么知道你的方案比普通 RAG 好？拿个例子给我看看"。答不上来，方案可信度全場。

完整评估 vs 最小验证 vs 不评估的对比：

| | 完整评估（B.1-B.7） | 最小验证（本节） | 不评估 |
|---|---|---|---|
| 题量 | 30-50 题 | 5-8 个典型 case | 0 |
| 标注 | 人工标注 + 分 5 类 | 刻意挑能体现卖点的 case | 无 |
| 指标 | 准确率 + 召回 + 溯源率 | 定性对比 + 截图 | 无 |
| 工作量 | 2-3 周 | 2-3 天 | 0 |
| 面试说服力 | 强 | 够用 | 危险 |

#### Case 设计（5-8 个，刻意覆盖核心卖点）

| Case 类型 | 数量 | 要证明什么 |
|----------|------|-----------|
| 跨文档合并 | 2 | 同一概念在多份文档出现，新方案能合并，普通 RAG 答不全 |
| 溯源 | 2 | 问"这条出自哪"，新方案能指到章节，普通 RAG 给不了 |
| 关系查询 | 1-2 | 问"X 和 Y 什么关系"，图谱多跳能答，向量检索答不准 |
| 定义类（对照组） | 1-2 | 普通问题两种方案都能答——证明新方案不是万能的 |

**最后一行特别重要**：主动展示"新方案也有不擅长的"，比一味吹嘘更可信。

#### 每个 Case 的产出

对每个 case 做三件事：
1. 跑一遍纯向量 RAG，截图输出
2. 跑一遍新方案，截图输出
3. 写 2-3 行说明：为什么新方案赢/输

#### 最终产物

一个 `demo_cases.md`，内容为 5-8 个对比截图 + 简短说明。面试时打开给面试官看，比任何 PPT 都有杀伤力。

---

## 附录 C：简历与面试话术

> 针对一年经验的 AI 应用开发工程师岗位，提供务实的简历表述与面试应对策略。

### C.1 项目定位（重要）

**定位为 POC，不要说成企业级**。

- ❌ 不可写："企业级知识库" / "生产环境部署" / "服务于 XX 业务"
- ✅ 可写："面向企业知识管理场景的 POC 验证" / "在公开数据集上验证方案有效性"

原因：
1. 把个人项目说成企业级，会被追问"线上 QPS 多少""怎么灰度""数据怎么治理"——没有答案则可信度坍塌
2. 一年经验做 POC 级别的系统设计，完全在合理预期内，反而显得懂得边界
3. POC 定位同时展示了"理解企业痛点"和"诚实承认探索性质"两点品质

### C.2 简历项目描述模板

```
TreeKG RAG —— 面向企业知识管理的分层知识图谱 POC

· 针对传统文档结构分层在跨文档合并、按需检索上的局限，设计"本体/概念/
  实体/证据"四层知识分层模型，将文档从"检索骨架"降级为"证据层"
· 包含 KGBuild（原 ExplicitKG/HiddenKG 合并）四层图谱构建流水线，与 KGRetrieve
  14 个原子工具供 Agent 自主调用（硬编码路由旧 AgenticRAG 已删除）
· 在公开技术文档语料上完成 POC 验证，通过 5+ 典型 case 对比展示分层检索
  在跨文档查询与溯源场景的优势与边界
```

**关键词解析**：
- "POC 验证" —— 诚实定位，不虚报
- "典型 case" —— 没写虚假数字，但有证据
- "优势与边界" —— 主动承认局限，展示思考深度

### C.3 技术栈表述（按面试官关注点排序）

```
· 后端：FastAPI + Uvicorn + Pydantic
· 存储：ChromaDB（向量）+ Neo4j（图）+ SQLite（文档原文）
· 模型：Ollama（本地嵌入 bge-large-zh）+ Qwen3-8B（LLM 抽取/生成）
· 抽取：LLM 实体关系抽取 + KNN+Jaccard+LLM 三段式消歧
· 检索：KGRetrieve 14 原子工具（概念/实体/证据单搜 + 语义搜索 + 多跳 + 重排 + schema）
```

### C.4 面试高频问题与应答要点

#### Q1：为什么不用纯向量 RAG，要搞这么复杂的分层？

**应答框架**（不要背答案，按这个逻辑展开）：
1. 先承认向量 RAG 的价值："单文档定义类问题，向量 RAG 完全够用"
2. 点出三个向量 RAG 搞不定的场景：跨文档同概念合并、精确溯源、结构化关系查询
3. 用 case 说话："我做过一个对比，问'XX概念在哪几份文档出现'，向量 RAG 只能召回 1 份，分层方案能拉出全部 3 份"

#### Q2：你怎么知道你的方案比普通 RAG 好？

**应答要点**：
- 不说"准确率提升 X%"（除非真做了完整评估）
- 说"我设计了 5-8 个典型 case 做定性对比，覆盖跨文档合并/溯源/关系查询三类场景"
- 打开 `demo_cases.md` 给面试官看截图
- 主动说"也有新方案不如向量 RAG 的 case，比如单文档简单定义类问题"——展示边界意识

#### Q3：L0 本体层怎么建的？

**应答要点**：
- 诚实说"手工定义了 10 个左右的类型和关系"
- 展示对本体设计的理解："初期 schema 要小要稳，因为 schema 一改下面三层全要重挂"
- 提及未来演进："二期可以考虑 LLM 辅助归纳候选类型，但最终拍板要人"

#### Q4：这个项目用在哪？

**应答要点**（关键：不要编故事）：
- "这是个人 POC 项目，没有真实企业场景"
- "但我分析了企业知识管理的痛点：文档类型异构、跨文档知识合并、合规溯源需求"
- "所以方案设计是面向这些痛点的，在公开语料上验证可行性"

#### Q5：概念层和实体层怎么区分？

**应答要点**（这是方案的弱点，要准备好）：
- 承认边界判定是难点："LLM 判断 is_concept 不够稳定"
- 给出兜底规则："我用规则辅助——跨文档出现 ≥2 次、总频次 ≥N 的倾向概念"
- 诚实说"这是一个需要持续优化的点"

### C.5 面试红线（不可踩）

| 红线 | 为什么不能踩 |
|------|-------------|
| 说"用于企业场景" | 会被追问线上指标，答不上 |
| 说"准确率提升 X%" | 没做完整评估就是造数字 |
| 说"原创设计了四层架构" | 分层 KG 是业界已有思路，说"借鉴 GraphRAG/LightRAG 思路"更诚实 |
| 说"生产可用" | POC 离生产差得远，会被追问稳定性 |
| 贬低纯向量 RAG | 显得不懂技术选型，向量 RAG 在很多场景就是最优解 |

### C.6 项目亮点与局限的平衡表述

面试中主动展示局限，反而加分。建议表述：

> "这个项目的主价值是系统化思考——我分析了文档结构分层的 6 个问题，设计了四层模型来解决跨文档合并和溯源。但我也清楚它的局限：概念/实体边界判定还不稳定、本体层是手工的小规模 schema、评测集规模有限。这些是我下一步想优化的方向。"

这种表述展示了三个品质：
1. 系统化思考能力（6 个问题）
2. 工程务实性（知道改动最小的迁移路径）
3. 边界意识（主动说局限）——**这是高级工程师才有的品质，一年经验能展示这一点会显得超出预期**

---

## 附录 D：四层重构改动计划

> 基于现有 ExplicitKG/HiddenKG 代码，重构为 L0-L3 四层模型的分阶段落地计划。核心策略：**加层不删层，保留旧版逐步迁移**。

### D.1 现有数据流（重构基础）

```
ExplicitKG（文档结构层）               HiddenKG（隐式知识层）
─────────────────────                 ─────────────────────
TextSegmentation.py                    Conv.py
  → toc_structure.json                   → conv_entities.json
Extraction.py                            (实体聚合 + 共现邻居)
  → toc_with_entities_and_relations    Aggr.py
Summarize.py                              → aggr_entities.json
  → toc_with_summaries.json              (core/noncore 角色 + 关系类型)
toc_graph.py                            Embedding.py
  → toc_graph.json                       → node_embeddings.pkl
EventRelation.py                        Dedup.py
  → event_relations.json                 → dedup_result.json
                                       Pred.py
                                         → pred_result.json
                                       FinalKG.py  ◄── 汇总点
                                         → final_kg.json
```

**关键发现**：现有 `final_kg.json` 的节点结构已经半只脚踏进四层模型：
- `type` 字段已有 `技术架构/技术概念/参数/算法/系统` 等值 → **proto-L0 类型**
- `level: core/noncore` → 旧的静态划分，要换成 L1/L2
- `occurrence_id` 绑死章节 → 要换成 evidence 链接数组

### D.2 四层映射表（现有代码 → 新分层）

| 新分层 | 数据来源 | 现有代码 | 改动量 |
|--------|---------|---------|--------|
| **L0 本体层** | 新建 `schema.yaml` | 复用 agentic_rag.yaml 的 RelationConfig | 🟢 小 |
| **L1 概念层** | 从现有实体中分拣 | Dedup.py 消歧逻辑 + Aggr.py 加 is_concept | 🟡 中 |
| **L2 实体层** | 现有实体抽取 | Extraction.py + Aggr.py | 🟢 小（加概念外键） |
| **L3 证据层** | 现有章节摘要 | TextSegmentation.py + Summarize.py | 🟢 小（独立化） |
| **汇总重构** | — | FinalKG.py | 🔴 大（核心改动） |

### D.3 分阶段实施计划

#### 阶段 1：L0 本体层 + L3 证据层（1-2 天，零风险）

**任务清单**：
- [ ] 新建 `src/KGBuild/config/schema.yaml`，定义实体类型、关系类型、证据 schema
- [ ] 新建 `src/utils/evidence_builder.py`，把 `toc_with_summaries.json` 转成 L3 证据节点列表
- [ ] 验证：输出 `evidence.json`，检查 evidence_id / section_path / summary 字段完整

**L0 schema.yaml 内容**：
```yaml
EntityTypes:
  - 概念        # 抽象术语，如"强化学习"
  - 方法        # 算法/技术手段，如"注意力机制"
  - 实例        # 具体对象，如"BERT"、"AlphaGo"
  - 角色        # 人员/组织
  - 指标        # 评估指标
  - 参数        # 配置项/API参数

RelationTypes:
  prerequisite: 先修/前置
  part-of: 组成/包含
  applies-to: 方法用于对象
  example-of: 实例代表
  synonym: 同义/别名
  contrasts-with: 对比
  related: 相关（兜底）

EvidenceSchema:
  doc_id: 文档标识
  section_id: 章节定位（如 "2.4.5"）
  section_path: 章节路径
  snippet: 原文片段
```

**L3 evidence_builder 核心逻辑**：
- 遍历 `toc_with_summaries.json` 的树结构
- 每个章节节点生成一个 evidence 节点
- 字段：`evidence_id` / `doc_id` / `section_id` / `section_path` / `summary` / `raw_snippets`

**阶段 1 产物**：`schema.yaml` + `evidence.json`。现有流程完全不动，零风险。

---

#### 阶段 2：L1 概念层 + L2 实体层（3-5 天，核心改动）

**任务清单**：
- [ ] 修改 `src/HiddenKG/Aggr.py` 的角色判定逻辑：`core/non-core` → `concept/entity`
- [ ] 新增规则兜底：跨文档出现 ≥2 次 + type 属于 {概念/方法/指标} → 直接判 concept
- [ ] 模糊地带才调 LLM 判定，节省调用成本
- [ ] 概念标准化：复用 `Dedup.py` 的 KNN+Jaccard+LLM 三段式消歧，合并 concept 节点
- [ ] L2 实体层加 `concept_id` 外键字段
- [ ] 验证：新版 `aggr_entities.json` 带 `layer` + `concept_id` 字段

**Aggr.py 关键改动**（`judge_role` → `judge_layer`）：
```python
# 现有：判 core / non-core
def judge_role(ent, section_summary): ...

# 新版：判 concept / entity，并关联到概念层
def judge_layer(ent, all_entities, section_summary):
    """
    返回 (layer, concept_id, diag)
    layer: "concept" | "entity"
    concept_id: entity 指向所属概念；concept 为自身
    """
    occurrence_count = len(ent.occurrences)
    type_hint_concept = ent.type in {"概念", "方法", "算法", "指标", "技术概念"}
    
    if occurrence_count >= 2 and type_hint_concept:
        layer = "concept"
        concept_id = f"concept_{hashlib.md5(ent.name.encode()).hexdigest()[:8]}"
    else:
        layer, concept_id = _llm_judge_concept(ent, all_entities)
    return layer, concept_id, {...}
```

**新版 aggr_entities.json 结构**：
```json
{
  "基于目标的智能体": {
    "name": "基于目标的智能体",
    "layer": "entity",           // ← 新增
    "concept_id": "concept_xxx", // ← 新增，指向所属概念
    "type": "智能体类型",
    "occurrences": [...]          // ← 保留，转成 evidence 链接
  }
}
```

**阶段 2 产物**：新版 `aggr_entities.json`（带 layer + concept_id）。

---

#### 阶段 3：重构 FinalKG 输出四层结构（2-3 天）

**任务清单**：
- [ ] 新建 `src/HiddenKG/FinalKG.py`（保留旧版不动）
- [ ] 按 L0/L1/L2/L3 分离节点存储
- [ ] 实体节点：`level: core/noncore` → `layer: concept/entity` + `concept_id`
- [ ] 实体节点：`occurrence_id` 单值 → `evidence_ids` 数组
- [ ] TOC 节点从"检索骨架"降级为 L3 证据的属性
- [ ] 边按层标注：`layer: L1-L1` / `L2-L3` 等
- [ ] 验证：新版 `final_kg.json` 结构完整，下游检索器可读

**新版 final_kg.json 结构**：
```json
{
  "L0_schema": {
    "entity_types": [...],
    "relation_types": {...}
  },
  "L1_concepts": [
    {
      "concept_id": "concept_a1b2c3d4",
      "name": "强化学习",
      "type": "方法",
      "aliases": ["RL"],
      "description": "..."
    }
  ],
  "L2_entities": [
    {
      "entity_id": "ent_xxx",
      "name": "基于目标的智能体",
      "type": "智能体类型",
      "concept_id": "concept_a1b2c3d4",
      "attributes": {"负责人": "", "状态": ""},
      "evidence_ids": ["ev_1_1_1", "ev_2_4_5"]
    }
  ],
  "L3_evidences": [
    {
      "evidence_id": "ev_1_1_1",
      "doc_id": "Hello-Agents",
      "section_id": "1.1.1",
      "section_path": "初识智能体 > 什么是智能体? > 传统视角下的智能体",
      "summary": "...",
      "snippet": "原始片段..."
    }
  ],
  "edges": [
    {
      "source": "concept_a1b2c3d4",
      "target": "concept_b2c3d4e5",
      "type": "prerequisite",
      "layer": "L1-L1"
    },
    {
      "source": "ent_xxx",
      "target": "ev_1_1_1",
      "type": "appears_in",
      "layer": "L2-L3"
    }
  ]
}
```

**关键转变**（对照旧版 FinalKG.py:163-181）：

| 旧版 | 新版 |
|------|------|
| `level: core/noncore` | `layer: concept/entity` + `concept_id` 外键 |
| `occurrence_id` 绑死一个章节 | `evidence_ids` 数组，可挂多个证据 |
| 实体和 TOC 节点混在一个 nodes 数组 | 按 L1/L2/L3 分开存储 |
| TOC 节点 = 检索骨架 | TOC 降级为 L3 证据的属性 |

**阶段 3 产物**：`final_kg.json`，一个真正的四层 KG。

---

#### 阶段 4：检索侧按层路由（**已弃用** — 原 AgenticRAG 目录整体已删除）

> 原方案 D.3 打算在 `AgenticRAG` 上硬编码 5 类问题 → 5 条路由的 `strategy_map`。
> 实际重构时改为：**新建 `KGRetrieve` 模块，把检索能力拆成 14 个原子工具，交给 Agent 自主编排调用**（见 E.9 / E.10）。
> 硬编码路由是"我替 Agent 决定走哪层"，工具集是"Agent 自己决定"——后者更灵活，且是 PiAgent 的天然接入点。
> 因此 `src/AgenticRAG/` 整个硬编码流水线（classifier/selector/executor/generator/retrievers）已全部删除，阶段 4 方案作废，实际落地走「KGRetrieve 14 工具」路径。

### D.4 落地优先级

```
阶段 1（L0 + L3）  ──►  阶段 2（L1 + L2）  ──►  阶段 3（FinalKG 重构）  ──►  阶段 4（检索路由）
   1-2 天                3-5 天                  2-3 天                    3-5 天
   零风险                核心改动                 数据重组                  可选延后
```

**最小可演示路径**：做完阶段 1-3，就有了一个真正的四层 KG，可以拿去做附录 B.8 的 5-8 个 case 验证。阶段 4 是锦上添花，时间紧可以先用现有 selector 跑。

### D.5 风险控制策略

1. **保留旧版 FinalKG.py**：新建 `FinalKG.py`，不直接改旧文件。~~旧版 `final_kg.json` 被 AgenticRAG/retrievers.py 和 Storage 模块消费，直接改会让下游全挂~~ （AgenticRAG 已整包删除，此风险已不存在。）
2. **下游逐步迁移**：新版跑通后，再让下游逐步迁移到新结构
3. **每阶段独立验证**：每个阶段都有明确的产物和验证点，不跨阶段累积风险
4. **简历加分项**：v1→v2 的架构演进本身就是简历亮点

### D.6 关键文件清单（改动范围）

| 文件 | 操作 | 阶段 |
|------|------|------|
| `src/KGBuild/config/schema.yaml` | 新建 | 1 |
| `src/utils/evidence_builder.py` | 新建 | 1 |
| `src/HiddenKG/Aggr.py` | 修改 judge_role → judge_layer | 2 |
| `src/HiddenKG/Dedup.py` | 复用，不改 | 2 |
| `src/HiddenKG/FinalKG.py` | 新建（保留旧版） | 3 |
| ~~`src/AgenticRAG/selector.py`~~ | ~~修改 strategy_map~~ | **4 已删除** |
| ~~`src/AgenticRAG/classifier.py`~~ | ~~修改问题分类~~ | **4 已删除** |
| ~~`src/AgenticRAG/executor.py`~~ | ~~修改检索编排~~ | **4 已删除** |

### D.7 验收标准

每阶段完成后的验收点：

- **阶段 1**：`schema.yaml` + `evidence.json` 生成，字段完整无空值
- **阶段 2**：`aggr_entities.json` 每个实体带 `layer` 和 `concept_id`，concept 节点数量合理（约为实体总数的 20-40%）
- **阶段 3**：`final_kg.json` 包含 L0/L1/L2/L3 四个顶层字段，边按层标注，可被下游读取
- **阶段 4**：5 类问题各跑通一个 case，路由准确

---

## 附录 E：重构进度记录

> 最后更新：2026-08-08（删除 AgenticRAG 旧硬编码路由；FinalKG_v2 → FinalKG 规范化）

### E.1 已完成：阶段 1（L0 本体层 + L3 证据层）

#### 1. L0 本体层 — `src/KGBuild/config/schema.yaml` ✅

- 10 个实体类型（概念/方法/智能体类型/学习方法/数学概念/指标/实例/工具/角色/参数），每个带 `layer_hint`（concept/entity/auto）
- 7 种同层关系（prerequisite / part_of / applies_to / example_of / synonym / contrasts_with / related）
- 3 种跨层关系（instance_of: L2→L1 / appears_in: L1·L2→L3 / described_by: L1→L3）
- EvidenceSchema 定义（evidence_id / doc_id / section_id / section_path / section_level / title / content / entities_in_section）
- **决策**：删除了事件关系（事理图谱 8 种），纯知识图谱分层，不掺杂事理逻辑

#### 2. TextSegmentation.py 改造 ✅

- 新增 `_append_content()` 函数：正文段落追加到当前最深层级节点的 `content` 字段（subsection > section > chapter 优先级）
- 两处正文收集逻辑（ENABLE_REGEX_FALLBACK=false 时 + 正则兜底未命中时）
- 验证：44 个节点，42 个有 content（2 个纯导航节点无正文）
- **关键设计**：任何有正文的节点都是 chunk，不只是 subsection（section 引言也收集）

#### 3. Summarize.py 废弃 ✅

- 从 `main.py` 脚本序列中移除
- 从 `config.yaml` 的 `include_files` 中移除
- **决策**：直接从原文抽实体，不再"先摘要再抽取"——避免摘要丢信息，L3 证据从摘要变原文，溯源更可信

#### 4. Extraction.py 改造 ✅

- 输入从 `toc_with_summaries.json` 改为 `toc_structure.json`
- 从读 `summary` 字段改为读 `content` 字段
- prompt 从"从摘要中提取"改为"从原文中提取"
- **schema 动态渲染**：启动时读 `src/KGBuild/config/schema.yaml`，把 EntityTypes 和 RelationTypes 渲染成 prompt 的【本体约束】段落
  - `render_entity_types()` / `render_relation_types()` 渲染函数
  - 用 `string.Template` 代替 `str.format`，避免 JSON 示例里的大括号冲突
  - 两阶段替换：第一次注入 schema 类型列表，第二次填入 section_text / entity_list
- prompt 占位符：`${entity_types_with_desc}` / `${relation_types_with_desc}` / `${section_text}` / `${entity_list}`
- 验证：第一个节点（1.1.1 传统视角下的智能体）抽出 36 个实体、48 个关系，type 字段符合 schema 定义

#### 5. EventRelation.py 废弃 ✅

- 从 `main.py` 和 `config.yaml` 中移除
- schema.yaml 中不保留事件关系类型

#### 6. evidence_builder.py 新建 ✅

- 路径：`src/utils/evidence_builder.py`
- 输入：`toc_with_entities_and_relations.json`
- 输出：`src/ExplicitKG/output/evidence.json`
- 遍历 TOC 树，为每个有 content 的节点生成一个 L3 证据节点
- 证据节点字段：evidence_id / doc_id / section_id / section_path / section_level / title / content / entities_in_section
- 验证：42 个证据节点（3 chapter + 9 section + 30 subsection），31 个含实体，实体总引用 779 次

#### 7. 下游适配 ✅

- `ExplicitKG/main.py`：脚本序列改为 TextSegmentation → Extraction → toc_graph（删 Summarize + EventRelation）
- `ExplicitKG/config/config.yaml`：删除 summarize.yaml / event_relation.yaml 的 include；API 换成 DeepSeek（deepseek-v4-flash）
- `ExplicitKG/config/extraction.yaml`：IN_NAME 改为 toc_structure.json；prompt 改用 `${}` 占位符 + 本体约束段落
- `Storage/sql_storage.py`：移除 docx 依赖，`import_section_data` 直接从 toc_structure.json 读 content
- `Storage/config/storage.yaml`：SUMMARIZE_PATH + DOCX_PATH → TOC_PATH

### E.2 阶段 1 产物清单

| 产物 | 路径 | 状态 |
|------|------|------|
| L0 本体层 | `src/KGBuild/config/schema.yaml` | ✅ |
| L3 证据层 | `src/ExplicitKG/output/evidence.json` | ✅ |
| 改造后的 TOC 结构 | `src/ExplicitKG/output/toc_structure.json`（含 content） | ✅ |
| 实体+关系抽取结果 | `src/ExplicitKG/output/toc_with_entities_and_relations.json` | ✅ |

### E.3 已完成：阶段 2（L1 概念层 + L2 实体层）

#### 1. schema.yaml 补充 TypeMapping ✅

- 新增 `TypeMapping` 段：10 条关键词→schema 类型归一化规则
- 覆盖 LLM 常见输出（算法/系统/框架/范式等）→ 归一到 schema 定义的 10 种类型
- **决策**：归一化规则作为单一事实源，Extraction.py 和 Aggr.py 共用

#### 2. Extraction.py 入口归一化 ✅

- 新增 `_normalize_entity_type()`：LLM 输出的 type 先查 schema 类型表，命中直接返回；未命中按 TypeMapping 关键词匹配；最终兜底为"概念"
- 保证 L2 抽取产物的 type 字段严格落在 schema 定义的 10 种类型内

#### 3. Aggr.py 层级判定重构 ✅

- `judge_role` → `judge_layer`：返回 `(layer, concept_id, hint_info)`
- 新增 `_normalize_type()` 安全网：先归一化 type，再查 `ENTITY_TYPE_MAP` 的 `layer_hint`
- 判定逻辑：schema `layer_hint` 决定 concept/entity 倾向，结合 occurrence 次数做最终分拣
- 输出 `aggr_entities.json` 每个实体带 `layer`（concept/entity）+ `concept_id`

#### 4. Dedup.py 四层适配 ✅

- 只对 `layer == "concept"` 的节点做语义消歧合并
- `layer == "entity"` 的节点原样通过（实体是具体实例，不跨文档合并）
- 输出 `dedup_result.json`：453 个 concept + 141 个 entity

#### 5. 下游数据结构适配 ✅

- `Conv.py`：EntityItem 增加 `layer` / `concept_id` 字段
- `Pred.py`：EntityItem 同步增加字段
- `Embedding.py`：BERT 本地目录不存在时回退到 HuggingFace 在线模型名
- `Dedup/data_structures.py`：EntityItem 同步增加字段

### E.4 阶段 2 产物清单

| 产物 | 路径 | 状态 |
|------|------|------|
| L1+L2 分层后的实体 | `src/HiddenKG/output/aggr_entities.json` | ✅ |
| 去重后的 L1 概念 + L2 实体 | `src/HiddenKG/output/dedup_result.json` | ✅ |
| 实体嵌入向量 | `src/HiddenKG/output/node_embeddings.pkl` | ✅ |
| 关系预测结果 | `src/HiddenKG/output/pred_result.json` | ✅ |
| 类型归一化规则 | `src/KGBuild/config/schema.yaml` → TypeMapping | ✅ |

### E.5 已完成：阶段 3（FinalKG 重构四层结构）

#### 1. FinalKG.py 新建 ✅

- 路径：`src/HiddenKG/FinalKG.py`（旧版 FinalKG.py 保留不动）
- 装配四层结构：L0_schema / L1_concepts / L2_entities / L3_evidences / edges
- 边按层标注 `layer` 字段：L1-L1 / L2-L2 / L1-L2 / L2-L1 / L1-L3 / L2-L3

#### 2. 关键转变（对照旧版 FinalKG.py）

| 旧版 | 新版 |
|------|------|
| `level: core/noncore` | `concept_id` 外键 + `layer` 标注 |
| `occurrence_id` 单值绑死章节 | `evidence_ids` 数组，可挂多个证据 |
| 实体和 TOC 节点混在一个 nodes 数组 | 按 L1/L2/L3 分离存储 |
| TOC 节点 = 检索骨架 | TOC 降级为 L3 证据的属性 |
| 边无层标注 | 边带 `layer` 字段，区分同层/跨层 |

#### 3. 边装配逻辑

- **同层关系**（来自 pred_result）：按两端节点 layer 标注 L1-L1 / L2-L2 / L1-L2
- **跨层 instance_of**（L2→L1）：每个 L2 实体的 concept_id 指向其归属概念
- **跨层 described_by**（L1→L3）：概念由某证据定义/解释
- **跨层 appears_in**（L2→L3）：实体出现在某证据中
- 双向校验：实体 occurrences 的 node_id → evidence_id，叠加 evidence.entities_in_section 反查

#### 4. 输出统计

| 层级 | 内容 | 数量 |
|------|------|------|
| L0 | 本体（10 种实体类型 + 同层/跨层关系） | schema.yaml |
| L1 | 概念节点 | 453 |
| L2 | 实体节点 | 141 |
| L3 | 证据节点 | 42 |

总边数 1226 条：L1-L1=212, L2-L2=12, L1-L2=49, L2-L1=141, L1-L3=635, L2-L3=177

### E.6 阶段 3 产物清单

| 产物 | 路径 | 状态 |
|------|------|------|
| 四层知识图谱 | `src/HiddenKG/output/final_kg.json` | ✅ |
| 四层装配脚本 | `src/HiddenKG/FinalKG.py` | ✅ |

### E.7 待做

> 阶段 1-4 已完成，目录重组也已完成（见 E.11）。剩余后续工作：

| 内容 | 状态 |
|------|------|
| 目录重组：ExplicitKG + HiddenKG 合并为 `src/KGBuild/` | ✅ 完成（见 E.11） |
| 用 5-8 个 case 验证四层图谱（附录 B.8，对比纯向量 RAG） | ⬜ 待做 |
| 接入真实 Agent（Pi Agent / LangChain）做端到端问答 | ⬜ 待做 |
| 把 `final_kg.json` 导入 Neo4j + ChromaDB（`db_backend import`）并验证 KGDBMemory 后端 | ⬜ 待做 |

### E.8 关键设计决策记录

1. **去掉 Summarize.py**：从原文直接抽实体，避免摘要丢信息。L3 证据从"摘要"变"原文"，溯源更可信
2. **去掉 EventRelation.py**：事理图谱和知识分层是正交的，放一起会模糊架构焦点
3. **subsection 作为 chunk**：利用文档章节结构做语义切分，保留 chapter→section→subsection 路径作为证据元数据。任何有正文的节点都是 chunk（不只是 subsection）
4. **schema 动态注入 prompt**：L0 类型定义只在 schema.yaml 写一遍，Extraction.py 启动时自动渲染进 prompt，避免重复维护
5. **Template 代替 format**：prompt 里有 JSON 示例（大括号），`str.format` 会冲突，改用 `string.Template`（`${}` 语法）
6. **保留旧版 FinalKG.py**：新建 FinalKG.py，不直接改旧文件，避免下游 retrievers/Storage 全挂
7. **类型归一化双层兜底**：Extraction.py（入口）和 Aggr.py（下游）都做类型归一化，共用 schema.yaml 的 TypeMapping 规则，单一事实源
8. **只对 concept 层做去重**：概念是跨文档可合并的抽象术语，实体是具体实例不合并。Dedup.py 按 `layer` 字段分流
9. **边按层标注 layer 字段**：同层关系（L1-L1/L2-L2）和跨层关系（L2-L1/L1-L3/L2-L3）在 edges 里显式标注，方便下游按层路由检索
10. **L1→L3 用 described_by，L2→L3 用 appears_in**：概念由证据"定义"，实体在证据中"出现"，语义上区分清楚
11. **阶段 4 改方案：不硬编码路由，改做工具集**：原计划（D.3）是改 AgenticRAG 的 `strategy_map` 把"定义类→L1、事实类→L2+L0"写死。实际改为新建 `KGRetrieve` 模块，把检索能力拆成 14 个原子工具，交给 Agent 自主编排调用。硬编码路由是"我替 Agent 决定走哪层"，工具集是"Agent 自己决定"——后者更灵活，且天然就是 PiAgent 的接入点
12. **旧版 AgenticRAG 已整体删除**：`classifier → selector → executor → generator` 硬编码路由流水线已移除。KGRetrieve 14 工具 + Agent 自主调用是当前唯一检索路径，不再保留并行旧方案
13. **双后端同一接口**：`KGMemory`（纯内存，零依赖）和 `KGDBMemory`（Neo4j + ChromaDB）接口契约完全一致，`ToolRegistry` 和 14 个工具零改动。原型用内存、生产用数据库，切换零成本

### E.9 已完成：阶段 4（KGRetrieve —— 检索能力工具化）

> 与原 D.3 计划的差异见 E.8 决策 11。核心思路：检索不再"按问题类型硬编码走某层"，而是拆成 14 个原子工具，由 Agent（Pi Agent / LangChain 等）自主决定调用什么、调几轮、何时停止。

#### 1. 新建 `src/KGRetrieve/` 模块 ✅

- 包含 6 个文件：`__init__.py` / `memory.py` / `db_backend.py` / `tools.py` / `demo_smoke_test.py` / `demo_agent_orchestration.py` + 文档 `TOOLS.md`
- 设计原则：每个工具只干一件事；只做检索不做生成（无 LLM 调用）；统一 `name/description/parameters/output_schema` 元数据；支持 dry_run

#### 2. 双后端实现 ✅

| 后端 | 类名 | 依赖 | 适用 |
|------|------|------|------|
| 纯内存 | `KGMemory` | 零依赖 | 原型、< 5000 节点 |
| 数据库 | `KGDBMemory` | Neo4j + ChromaDB + Ollama | 持久化、跨进程、向量语义搜索 |

- `KGMemory`：从 `final_kg.json` 加载，构建 5 类索引（id→node / name→ids / concept_id→entity_ids / node_id→邻接表 / layer→ids），支持 O(1) 查找和 BFS 多跳
- `KGDBMemory`：Neo4j 存图 + ChromaDB 存向量；`search_by_name` 做"向量语义 + 名称精确匹配"混合搜索
- 两者接口完全一致，`ToolRegistry` 和 14 个工具零改动

#### 3. 14 个原子工具（5 组）✅

| 组 | 工具数 | 工具 |
|---|------|------|
| 精确查找 | 5 | `get_schema` / `get_node_by_id` / `get_concept_by_id` / `get_entity_by_id` / `get_evidence_by_id` |
| 关联查询 | 3 | `get_relations` / `get_entities_of_concept` / `get_evidences_of_node` |
| 跨概念遍历 | 1 | `multi_hop_traverse`（BFS 多跳，支持 end_id 命中停止） |
| 模糊搜索 | 4 | `search_concepts` / `search_entities` / `search_evidences` / `semantic_search`（跨 L1+L2+L3） |
| 二次加工 | 1 | `rank_results`（关键词命中打分重排，零 LLM 依赖） |

- `ToolRegistry`：统一注册/枚举/调用；`get_tool_schemas()` 直接产出 OpenAI function calling 格式，可喂给任意 Agent 框架
- 工具严格"零 LLM 依赖"，答案合成由外层 Agent / LLM 负责

#### 4. 数据库导入脚本 ✅

- `import_kg()`：把 `final_kg.json` 一键导入 Neo4j + ChromaDB
- Neo4j：建 Concept/Entity/Evidence 三类节点 + 索引，边按 `edge_type` 转大写关系类型
- ChromaDB：节点/证据/边分别建集合，用 Ollama（bge-large-zh-v1.5）生成向量
- 命令：`python -m src.KGRetrieve.db_backend import`

#### 5. 验证：冒烟测试通过 ✅

`python -m src.KGRetrieve.demo_smoke_test` 全量通过：

- KGMemory 加载：concept:453, entity:141, evidence:42
- 14 个工具全部注册成功
- function calling schema 导出 14 条
- 关键工具各调用一次均 ✅：
  - `search_concepts("强化学习")` → concept_6964a395
  - `search_entities("AlphaGo")` → ent_275d8e2f
  - `search_evidences("传统视角")` → ev_1.1.1
  - `get_relations` → 5 条边（含 described_by L1-L3）
  - `multi_hop_traverse(max_hop=2)` → 6 nodes / 5 edges / 6 paths
  - `rank_results` → 重排返回 top 5
- 汇总："✅ 所有冒烟测试通过。KGRetrieve 工具包已就绪。"

### E.10 阶段 4 产物清单

| 产物 | 路径 | 状态 |
|------|------|------|
| 工具包入口 | `src/KGRetrieve/__init__.py` | ✅ |
| 内存后端 + Node/Edge 数据类 | `src/KGRetrieve/memory.py` | ✅ |
| 数据库后端 + 导入脚本 | `src/KGRetrieve/db_backend.py` | ✅ |
| 14 个原子工具 + ToolRegistry | `src/KGRetrieve/tools.py` | ✅ |
| 冒烟测试 | `src/KGRetrieve/demo_smoke_test.py` | ✅ 通过 |
| 伪 Agent 多轮编排示例 | `src/KGRetrieve/demo_agent_orchestration.py` | ✅ |
| 工具文档 | `src/KGRetrieve/TOOLS.md` | ✅ |

### E.11 已完成：目录重组（ExplicitKG + HiddenKG → `src/KGBuild/`）

> 原方案 E.7 待做中的第 1 项。ExplicitKG 是"显式阶段"（TOC 解析 → 实体关系抽取 → 证据层/TOC 图），HiddenKG 是"隐式阶段"（Conv/实体关系转换 → Aggr/分层判定 → Embed → Dedup/概念去重 → Pred/关系预测 → FinalKG 装配）。两者合起来就是从 docx 到最终四层 KG 的一条完整流水线，语义上是一条流水线的两个 half，所以并到 `src/KGBuild/`。

#### 1. 新目录结构 ✅

```
src/
├── KGBuild/              (原 src/ExplicitKG/ + src/HiddenKG/ 合并平铺)
│   ├── __init__.py        (新建空)
│   │
│   │ ——— 显式阶段（原 ExplicitKG，共 3 .py，已删除 main.py 总调度）———
│   ├── TextSegmentation.py    TOC + 原文解析
│   ├── Extraction.py          实体/关系抽取（读 schema.yaml）
│   └── toc_graph.py           构建 toc_graph.json
│   │
│   │ ——— 隐式阶段（原 HiddenKG，共 6 .py，已删除 main.py/FinalKG.py/visualize.py）———
│   ├── Conv.py                显式→隐式实体关系格式转换
│   ├── Aggr.py                concept/entity 分层判定
│   ├── Embedding.py           BERT 嵌入
│   ├── Dedup.py               概念去重总调度（读子包）
│   ├── Pred.py                关系预测
│   └── FinalKG.py          四层 KG 装配（主）
│   │
│   ├── dedup/                 (Python 子包，原 HiddenKG/Dedup/ → 小写)
│   │   ├── __init__.py
│   │   ├── data_structures.py
│   │   ├── knn.py
│   │   ├── llm.py
│   │   └── name_similarity.py
│   ├── model/                 (原 HiddenKG/model/)
│   │   ├── bert.py / main.py  +  bert-base-chinese/ 权重目录
│   │
│   │ ——— 配置（两阶段 config 合并，同名 config.yaml 改前缀避免冲突）———
│   └── config/
│       ├── explicit_config.yaml        原 ExplicitKG/config/config.yaml 改名
│       ├── explicit_config.yaml.example
│       ├── extraction.yaml / text.yaml 不变
│       ├── hidden_config.yaml          原 HiddenKG/config/config.yaml 改名
│       ├── conv.yaml / aggr.yaml / emb.yaml / dedup.yaml / pred.yaml 不变
│   │
│   ├── output/                (两阶段 output 合并，TOC+final_kg* 文件名不冲突)
│   └── logs/                  (两阶段 logs 合并)
│
├── KGRetrieve/  (检索层/Agent 工具层 —— 14 原子工具)
└── utils/       (evidence_builder.py 等辅助工具)
注：schema.yaml 已移入 KGBuild/config/；原 AgenticRAG/ 旧硬编码路由流水线已整体删除。
```

**命名冲突的处理：**
| 冲突点 | 原 | 新 |
|-------|---|---|
| `ExplicitKG/main.py` 和 `HiddenKG/main.py` 同名 | 各叫各的 main.py | 两份 main.py 均已删除（显式/隐式阶段各自直接调用 step 脚本即可） |
| `ExplicitKG/config/config.yaml` 和 `HiddenKG/config/config.yaml` 同名 + 跨阶段 include | 各叫各的 config.yaml，hidden 用 `../../ExplicitKG/config/config.yaml` 跨阶段读 API_KEY | `explicit_config.yaml` + `hidden_config.yaml`，hidden 在同目录 include `"explicit_config.yaml"` |

#### 2. 路径改动清单 ✅

| 类别 | 涉及文件数 | 说明 |
|------|-----------|------|
| 各阶段主配置文件名更新 | 5（TextSeg/Extract/Aggr/Conv/Pred/Embed/Dedup/dedup/llm 共 8 文件）| `config/config.yaml` → `config/explicit_config.yaml` 或 `config/hidden_config.yaml` |
| 跨阶段 explicit 路径 | 2（FinalKG / Conv） | `SRC / "ExplicitKG" / "output"` → 统一 `HERE / "output"` 或 `KGBuild_dir / "output"`（平铺后同目录了） |
| KGRetrieve 读 KG 路径 | 4（db_backend / memory / demo_smoke_test / demo_agent） | `src/HiddenKG/output/final_kg.json` → `src/KGBuild/output/final_kg.json` |
| utils evidence 构建默认参数 | 1（evidence_builder.py）| `src/ExplicitKG/output/…` → `src/KGBuild/output/…` |
| Embedding.py import model | 1 | `from HiddenKG.model.main` / `from model.main` → `from KGBuild.model.main` |
| Dedup.py import 子包 | 1 | `from Dedup.xxx` → `from KGBuild.dedup.xxx` |
| 所有注释/help 文案/配置文件注释里的旧路径 | 10+ | 全部同步改为新路径，避免人看文档时误导 |

#### 3. 验证 ✅

- **18 项静态路径检查**：从每个文件里精确 grep 关键路径字符串，全 ✅。包括：8 个主配置文件名、跨阶段 include、子包 import、调度器 subprocess 路径、Embedding model import、KGRetrieve 4 文件、evidence_builder 默认参数等。
- **FinalKG.py 锚点路径实存检查**：8 个路径锚点（HERE/SRC/EXPLICIT_OUT/HIDDEN_OUT/SCHEMA_FILE/DEDUP_FILE/PRED_FILE/EVIDENCE_FILE/FINAL_FILE）**全部指向 KGBuild 新位置且全部实际 exist**。
- **`python -m src.KGRetrieve.demo_smoke_test` 端到端**：重新实跑，全部工具注册成功、`search_concepts`/`search_entities`/`search_evidences`/`get_relations`/`multi_hop_traverse`/`rank_results`/`get_schema` 全部 ✅，最终输出"✅ 所有冒烟测试通过。KGRetrieve 工具包已就绪。"
- **git 历史保持单提交干净**：所有 rename/delete/edit 一次性 `git add -A && git commit --amend` 合成进唯一的 Initial commit，仍然只有 1 个根 commit，status clean。

#### 4. 重组后实际的 src 模块边界

```
从输入到输出的调用链（完整流水线，各 step 直接调用）：

    显式阶段                           隐式阶段
    │                                 │
    ├─ TextSegmentation.py           ├─ Conv.py  (读 output/toc_with_entities_and_relations.json ← explicit 产物)
    ├─ Extraction.py                 ├─ Aggr.py  (读 hidden_config.yaml ← 同目录 include explicit_config.yaml 取 API 密钥)
    └─ toc_graph.py                  ├─ Embedding.py  (from KGBuild.model.main)
        ▼                            ├─ Dedup.py      (from KGBuild.dedup.*)
    KGBuild/output/ (TOC系列/证据)   ├─ Pred.py
        ▲                            └─ FinalKG.py (HERE/output = 同目录 output)
        │                                │
        └── 共享 KGBuild/output ──────┘ ▼
                                 final_kg.json
                                       ▼
                          KGRetrieve （14 工具读 final_kg.json）
```

### E.12 目录重组后的完整产物地图（以 KGBuild 为中心）

| 类别 | 路径 | 说明 |
|------|------|------|
| **流水线显式阶段** | `src/KGBuild/TextSegmentation.py` | docx → TOC + 原文小节 |
| | `src/KGBuild/Extraction.py` | 小节 → 实体/关系（读 `src/KGBuild/config/schema.yaml` L0 本体约束） |
| | `src/KGBuild/toc_graph.py` | 实体/关系 → toc_graph.json（2024 旧版可视化格式） |
| **流水线隐式阶段** | `src/KGBuild/Conv.py` | 显式抽取结果 → HiddenKG 内部格式 |
| | `src/KGBuild/Aggr.py` | 分层判定（L1 concept / L2 entity），含 schema.layer_hint 规则 |
| | `src/KGBuild/Embedding.py` | BERT 嵌入 → node_embeddings.pkl |
| | `src/KGBuild/Dedup.py` | 概念去重，调用 `src/KGBuild/dedup/` 子包 |
| | `src/KGBuild/Pred.py` | 关系预测（LLM）→ pred_result.json |
| | `src/KGBuild/FinalKG.py` | 四层装配 → final_kg.json（主产物） |
| **子包** | `src/KGBuild/dedup/` | 去重：data_structures / knn / llm / name_similarity |
| | `src/KGBuild/model/` | 编码器：bert.py / main.py + bert-base-chinese/ 权重 |
| **配置（统一）** | `src/KGBuild/config/explicit_config.yaml` + `.example` | 显式阶段主配置，含 API Key |
| | `src/KGBuild/config/extraction.yaml` / `text.yaml` | 显式阶段子配置 |
| | `src/KGBuild/config/hidden_config.yaml` | 隐式阶段主配置，include `explicit_config.yaml` |
| | `src/KGBuild/config/conv/aggr/emb/dedup/pred.yaml` | 隐式阶段各 step 子配置 |
| **共享输出** | `src/KGBuild/output/` | 14 文件：TOC 系列 + evidence.json + conv/aggr/dedup/pred 中间产物 + final_kg*.json/html |
| **共享日志** | `src/KGBuild/logs/` | aggr.log / conv.log / pred_*.log |
| **L0 全局本体** | `src/KGBuild/config/schema.yaml` | EntityTypes / RelationTypes / TypeMapping.layer_hint |
| **L3 构建工具** | `src/utils/evidence_builder.py` | 从 toc 构建 evidence.json（默认读写 `src/KGBuild/output/`） |
| **检索/Agent 工具层** | `src/KGRetrieve/`（7 文件） | 14 个原子工具 + ToolRegistry + KGMemory 内存后端 + KGDBMemory 数据库后端 |
| **旧版流水线（已删除）** | ~~`src/AgenticRAG/`（6 文件 + config）~~ | 硬编码路由已弃用；当前检索路径统一走 `KGRetrieve` 14 工具 |
