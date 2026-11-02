import { Fragment, type CSSProperties, type ReactNode } from 'react';
import {
  QuestionCircleOutlined,
  SearchOutlined,
  NodeIndexOutlined,
  FileSearchOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import {
  BuildFlowChart,
  EndToEndOverview,
  FlowLegend,
  LayerStackDiagram,
  QueryFlowChart,
} from '../components/docs/FlowCharts';

// ---------------------------------------------------------------------------
// 数据
// ---------------------------------------------------------------------------

type Layer = {
  id: string;
  name: string;
  color: string;
  desc: string;
  example: string;
  storage: string;
};

const LAYERS: Layer[] = [
  {
    id: 'L0',
    name: '本体层',
    color: '#722ed1',
    desc: '节点类型、关系类型、分层规则与证据结构',
    example: '概念 / 方法 / 工具 · instance_of',
    storage: 'schema.yaml',
  },
  {
    id: 'L1',
    name: '概念层',
    color: '#2f6fed',
    desc: '跨章节复用的抽象概念、方法与范式',
    example: '强化学习、提示工程、思维链',
    storage: 'final_kg.json · L1_concepts',
  },
  {
    id: 'L2',
    name: '实体层',
    color: '#00b578',
    desc: '具体实例、产品、人物、组织与配置值',
    example: 'AlphaGo、GPT-4、top_k',
    storage: 'final_kg.json · L2_entities',
  },
  {
    id: 'L3',
    name: '证据层',
    color: '#fa8c16',
    desc: '可定位的原文小节 / 段落片段，既是抽取单元，也是检索与引用的基本 chunk',
    example: 'ev_Hello-Agents_0:1.1.1',
    storage: 'final_kg.json · L3_evidences',
  },
];

type Store = {
  id: string;
  name: string;
  color: string;
  desc: string;
  content: string;
  file: string;
};

const STORES: Store[] = [
  {
    id: 'JSON',
    name: '构建产物',
    color: '#8c8c8c',
    desc: '四层节点、全部边与构建清单；重新导入时以它为准',
    content: 'final_kg.json · build_manifest.json',
    file: 'src/KGBuild/output/',
  },
  {
    id: 'NEO4J',
    name: '图数据库',
    color: '#2f6fed',
    desc: '存图结构与证据原文，负责多跳遍历、关系查询',
    content: 'L0–L3 节点 + 全部边',
    file: 'python -m src.KGRetrieve.db_backend import',
  },
  {
    id: 'CHROMA',
    name: '向量索引',
    color: '#00b578',
    desc: '存 L1/L2 归一化向量（bge-m3），负责语义召回',
    content: 'concept_* / ent_* embeddings',
    file: 'chroma_db/',
  },
];

type BuildStep = {
  no: string;
  name: string;
  label: string;
  desc: string;
  input: string;
  output: string;
  kind?: 'llm' | 'rule' | 'vector' | 'assemble';
};

type BuildPhase = {
  key: string;
  title: string;
  hint: string;
  no: string;
  steps: BuildStep[];
};

const BUILD_PHASES: BuildPhase[] = [
  {
    key: 'parse',
    title: '解析与抽取',
    hint: 'docx → 结构 → 实体',
    no: '①',
    steps: [
      {
        no: '01',
        name: 'textseg',
        label: '文本分割',
        desc: '识别标题层级，切分小节与段落，记录每个 block 的字符偏移',
        input: 'docx',
        output: 'toc_structure.json',
      },
      {
        no: '02',
        name: 'extraction',
        label: '实体关系抽取',
        desc: '逐小节调用 LLM，按闭集类型抽实体与同层语义关系，名称必须能在原文定位',
        input: 'toc_structure.json',
        output: 'toc_with_entities_and_relations.json',
        kind: 'llm',
      },
    ],
  },
  {
    key: 'evidence',
    title: '结构与证据',
    hint: '章节树与 L3 证据',
    no: '②',
    steps: [
      {
        no: '03',
        name: 'toc_graph',
        label: '目录图',
        desc: '构建章节父子关系，供结构视图与后续增量构建定位使用，不进入最终图谱',
        input: 'toc_structure.json',
        output: 'toc_graph.json',
      },
      {
        no: '04',
        name: 'evidence',
        label: '证据层构建',
        desc: '把每个小节（可选到段落）做成带章节路径与原文的 L3 证据',
        input: 'toc_with_entities_and_relations.json',
        output: 'evidence.json',
      },
    ],
  },
  {
    key: 'normalize',
    title: '归并与分层',
    hint: '信号 → 规则 → L1/L2',
    no: '③',
    steps: [
      {
        no: '05',
        name: 'conv',
        label: '实体归并',
        desc: '同名与别名归并，保留全部出现位置；计算覆盖度、定义性措辞等分层信号，并为缺定义的实体做证据化补全（LLM）',
        input: 'toc_with_entities_and_relations.json',
        output: 'conv_entities.json',
      },
      {
        no: '06',
        name: 'aggr',
        label: '分层归属',
        desc: 'LayerPolicy 规则优先定层；灰区查决策登记表或问 LLM；为每个 L2 选最多一个父概念',
        input: 'conv_entities.json',
        output: 'aggr_entities.json',
        kind: 'rule',
      },
    ],
  },
  {
    key: 'refine',
    title: '向量与精修',
    hint: '向量化、去重、补边',
    no: '④',
    steps: [
      {
        no: '07',
        name: 'embedding',
        label: '向量嵌入',
        desc: '用 bge-m3 为 L1 / L2 节点生成归一化向量，供检索与去重共用',
        input: 'aggr_entities.json',
        output: 'node_embeddings.pkl',
        kind: 'vector',
      },
      {
        no: '08',
        name: 'dedup',
        label: '同层去重',
        desc: '别名快速通道 + 向量近邻 + LLM 复核，合并同义节点并重映射关系',
        input: 'aggr_entities.json',
        output: 'dedup_result.json',
        kind: 'llm',
      },
      {
        no: '09',
        name: 'pred',
        label: '关系补全',
        desc: '拓扑候选 + 向量相似 + LLM 复核，补充缺失的语义边',
        input: 'dedup_result.json',
        output: 'pred_result.json',
        kind: 'llm',
      },
    ],
  },
  {
    key: 'final',
    title: '装配发布',
    hint: '四层 KG + 构建清单',
    no: '⑤',
    steps: [
      {
        no: '10',
        name: 'final',
        label: '四层装配',
        desc: '校验类型与端点策略，生成跨层证据边，输出 final_kg.json 与 build_manifest.json',
        input: 'dedup + pred + evidence',
        output: 'final_kg.json',
        kind: 'assemble',
      },
    ],
  },
];

type QueryStep = {
  title: string;
  desc: string;
  icon: ReactNode;
  color: string;
};

const QUERY_STEPS: QueryStep[] = [
  {
    title: '提问',
    desc: '用户在 Agent 对话页输入问题，后端以 SSE 流式返回过程与答案。',
    icon: <QuestionCircleOutlined />,
    color: '#722ed1',
  },
  {
    title: '定位入口',
    desc: 'Agent 先把问题拆成若干检索角度，再用 semantic_search 与 search_* 找到概念、实体或证据候选。',
    icon: <SearchOutlined />,
    color: '#2f6fed',
  },
  {
    title: '图内扩展',
    desc: '沿 instance_of、part_of 等关系取邻居与多跳路径，补足上下文。',
    icon: <NodeIndexOutlined />,
    color: '#00b578',
  },
  {
    title: '回取证据',
    desc: '沿跨层边拿到 L3 原文，必要时按证据 ID 精确读取。',
    icon: <FileSearchOutlined />,
    color: '#fa8c16',
  },
  {
    title: '带引用回答',
    desc: 'assemble_context 组装上下文，答案必须标注 [ev_...] 来源。',
    icon: <SafetyCertificateOutlined />,
    color: '#eb2f96',
  },
];

const TOOL_GROUPS = [
  {
    title: '定位入口',
    hint: '向量 / 关键词先落点',
    color: '#2f6fed',
    tools: [
      ['semantic_search', '跨层语义搜索，向量与名称混合召回'],
      ['search_concepts', '按名称 / 别名查 L1 概念'],
      ['search_entities', '按名称 / 别名查 L2 实体'],
      ['search_evidences', '按关键词查 L3 证据'],
      ['get_schema', '读取 L0 类型与关系定义'],
    ],
  },
  {
    title: '图内查询',
    hint: '沿边继续找答案',
    color: '#00b578',
    tools: [
      ['get_relations', '节点的出边 + 入边，理解它如何连接'],
      ['get_entities_of_concept', '列出某概念下的所有 L2 实体'],
      ['multi_hop_traverse', 'BFS 多跳遍历，处理多概念 / 对比问题'],
      ['get_evidences_of_node', '取节点溯源证据，带 described_by / appears_in 强度'],
      ['get_*_by_id', '按稳定 ID 精确读取节点或证据'],
    ],
  },
  {
    title: '精排与组装',
    hint: '把线索变成可引用上下文',
    color: '#fa8c16',
    tools: [
      ['rank_results', '显式重排候选；检索工具已内置精排，通常无需单独调用'],
      ['assemble_context', '把节点与证据拼成带溯源标注的上下文块'],
    ],
  },
];

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export default function DocsView() {
  return (
    <div className="docs-page">
      <header className="docs-hero">
        <div className="docs-kicker">HierKG / 系统说明</div>
        <h1 className="docs-hero-title">离线批量构图，在线图内检索</h1>
        <p className="docs-hero-sub">
          构建侧把结构化文档装配成 L0 本体、L1 概念、L2 实体、L3 证据四层图谱；
          查询侧先向量定位入口节点，再沿图取证据，回答必须可溯源到原文。
        </p>
        <div className="docs-hero-chips">
          <span className="docs-chip">10 步离线构图</span>
          <span className="docs-chip">15 个原子检索工具</span>
          <span className="docs-chip">证据可定位到小节 / 段落</span>
          <span className="docs-chip">规则优先 + 人工覆写</span>
        </div>
      </header>

      {/* ── 端到端总览 ───────────────────────────────────────────── */}
      <section className="docs-section">
        <div className="docs-section-head">
          <span className="docs-section-kicker">End to End</span>
          <h2 className="docs-section-title">从文档到回答</h2>
          <p className="docs-section-desc">
            先看全局：离线构图把文档装配成四层图谱，在线检索只读图谱、沿边取证据。
          </p>
        </div>
        <EndToEndOverview />
      </section>

      {/* ── 四层模型 ─────────────────────────────────────────────── */}
      <section className="docs-section">
        <div className="docs-section-head">
          <span className="docs-section-kicker">L0 – L3</span>
          <h2 className="docs-section-title">四层图谱模型</h2>
          <p className="docs-section-desc">
            类型回答「它是什么」，层级回答「它在图里扮演什么角色」。两者正交：同一个「算法」类型，
            既可能是 L1 概念，也可能是 L2 的具体实现。
          </p>
        </div>

        <div className="docs-layer-grid">
          {LAYERS.map((layer) => (
            <article
              key={layer.id}
              className="docs-layer-card"
              style={{ '--layer-color': layer.color } as CSSProperties}
            >
              <div className="docs-layer-top">
                <span className="docs-layer-badge">{layer.id}</span>
                <span className="docs-layer-name">{layer.name}</span>
              </div>
              <p className="docs-layer-desc">{layer.desc}</p>
              <div className="docs-layer-example">{layer.example}</div>
              <div className="docs-layer-file">{layer.storage}</div>
            </article>
          ))}
        </div>

        <div className="docs-chart-block">
          <div className="docs-chart-head">
            <strong>四层怎么叠、层间怎么连</strong>
            <span>L0 定义并约束全部层；L2 归属 L1；L1 / L2 引用 L3</span>
          </div>
          <LayerStackDiagram />
          <div className="docs-edge-legend">
            <span className="docs-edge-item">
              <span className="docs-edge-line is-semantic" /> 语义关系（part_of / prerequisite / applies_to…）
            </span>
            <span className="docs-edge-item">
              <span className="docs-edge-line is-instance" /> 归属关系（instance_of / example_of）
            </span>
            <span className="docs-edge-item">
              <span className="docs-edge-line is-evidence" /> 证据引用（described_by：L1→L3；appears_in：L1/L2→L3）
            </span>
            <span className="docs-edge-item">
              <span className="docs-edge-line is-govern" /> 本体约束（L0 → 各层）
            </span>
          </div>
        </div>
      </section>

      {/* ── 构图流程 ─────────────────────────────────────────────── */}
      <section className="docs-section">
        <div className="docs-section-head">
          <span className="docs-section-kicker">Build / Offline</span>
          <h2 className="docs-section-title">构图流程</h2>
          <p className="docs-section-desc">
            固定 10 步串行执行，每步读取上一步的产物。任一步失败即停止，可用{' '}
            <code>--start</code> / <code>--only</code> 断点续跑；Web 端「图谱构建」页走同一套流程。
          </p>
        </div>

        <div className="docs-chart-block">
          <div className="docs-chart-head">
            <strong>构图流程图</strong>
            <span>箭头表示产物传递；节点左侧色条表示执行方式</span>
          </div>
          <BuildFlowChart />
          <FlowLegend />
        </div>

        <div className="docs-build-flow">
          {BUILD_PHASES.map((phase) => (
            <div key={phase.key} className={`docs-phase is-${phase.key}`}>
              <div className="docs-phase-label">
                <span className="docs-phase-no">{phase.no}</span>
                <div>
                  <div className="docs-phase-title">{phase.title}</div>
                  <div className="docs-phase-hint">{phase.hint}</div>
                </div>
              </div>
              <div className="docs-phase-steps">
                {phase.steps.map((step, index) => (
                  <Fragment key={step.name}>
                    {index > 0 && <span className="docs-arrow">→</span>}
                    <article className={`docs-step${step.kind ? ` is-${step.kind}` : ''}`}>
                      <div className="docs-step-head">
                        <span className="docs-step-no">{step.no}</span>
                        <strong className="docs-step-label">{step.label}</strong>
                        <code className="docs-step-name">{step.name}</code>
                      </div>
                      <p className="docs-step-desc">{step.desc}</p>
                      <div className="docs-step-io">
                        <span className="docs-io-tag is-in">{step.input}</span>
                        <span className="docs-io-arrow">→</span>
                        <span className="docs-io-tag is-out">{step.output}</span>
                      </div>
                    </article>
                  </Fragment>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="docs-callout">
          <strong>最终产物</strong>
          <span>
            <code>final_kg.json</code> 提供 L0–L3 与全部边；<code>build_manifest.json</code>{' '}
            记录文档 / 小节 / 配置哈希、prompt 与模型版本、分层兜底率、L1/L2 翻转和校验结果，
            是后续增量构建与变更审计的依据。
          </span>
        </div>

        <div className="docs-chart-block docs-store-block">
          <div className="docs-chart-head">
            <strong>存储与导入</strong>
            <span>两个库各司其职；final_kg.json 始终是事实源</span>
          </div>
          <div className="docs-store-grid">
            {STORES.map((store) => (
              <article
                key={store.id}
                className="docs-layer-card"
                style={{ '--layer-color': store.color } as CSSProperties}
              >
                <div className="docs-layer-top">
                  <span className="docs-layer-badge">{store.id}</span>
                  <span className="docs-layer-name">{store.name}</span>
                </div>
                <p className="docs-layer-desc">{store.desc}</p>
                <div className="docs-layer-example">{store.content}</div>
                <div className="docs-layer-file">{store.file}</div>
              </article>
            ))}
          </div>
          <p className="docs-store-note">
            导入前会清空 Neo4j 并重建 Chroma collection；<code>build_manifest.json</code>{' '}
            记录文档 / 配置哈希、版本与校验结果，是增量构建和审计依据。
          </p>
        </div>
      </section>

      {/* ── 查询流程 ─────────────────────────────────────────────── */}
      <section className="docs-section">
        <div className="docs-section-head">
          <span className="docs-section-kicker">Retrieve / Online</span>
          <h2 className="docs-section-title">查询流程</h2>
          <p className="docs-section-desc">
            查询侧只读图谱，不触发构图。Agent 对任何问题都遵循同一链路：
            <strong>先定位入口节点，再进图沿边取证据</strong>。随问题复杂度变化的只是遍历深度。
          </p>
        </div>

        <div className="docs-chart-block">
          <div className="docs-chart-head">
            <strong>查询流程图</strong>
            <span>先定位入口，再沿图取证据，最后组装带引用的回答</span>
          </div>
          <QueryFlowChart />
          <FlowLegend variant="query" />
        </div>

        <div className="docs-query-flow">
          {QUERY_STEPS.map((step, index) => (
            <Fragment key={step.title}>
              {index > 0 && <span className="docs-query-arrow">→</span>}
              <article className="docs-query-step" style={{ '--query-color': step.color } as CSSProperties}>
                <div className="docs-query-icon">{step.icon}</div>
                <div className="docs-query-index">{String(index + 1).padStart(2, '0')}</div>
                <div className="docs-query-title">{step.title}</div>
                <p className="docs-query-desc">{step.desc}</p>
              </article>
            </Fragment>
          ))}
        </div>

        <div className="docs-query-note">
          <span className="docs-query-note-dot" />
          具体 / 单概念问题 → 浅遍历（节点 → 证据）；模糊 / 多概念 / 对比问题 → 深遍历
          （<code>multi_hop_traverse</code>、<code>get_entities_of_concept</code>）。
        </div>

        <div className="docs-tool-grid">
          {TOOL_GROUPS.map((group) => (
            <div key={group.title} className="docs-tool-group" style={{ '--group-color': group.color } as CSSProperties}>
              <div className="docs-tool-group-head">
                <strong>{group.title}</strong>
                <span>{group.hint}</span>
              </div>
              <div className="docs-tool-list">
                {group.tools.map(([name, desc]) => (
                  <div key={name} className="docs-tool">
                    <code className="docs-tool-name">{name}</code>
                    <span className="docs-tool-desc">{desc}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="docs-seq-block">
          <div className="docs-seq-head">一次查询的典型路径</div>
          <div className="docs-seq">
            <div>
              <span className="docs-seq-q">问</span>
              <span>强化学习和监督学习是什么关系？</span>
            </div>
            <div>
              <span className="docs-seq-cmd">semantic_search</span>
              <span className="docs-seq-arg">(&quot;强化学习&quot;, &quot;监督学习&quot;)</span>
              <span className="docs-seq-arrow">→</span>
              <span className="docs-seq-res">concept_xxx / concept_yyy</span>
            </div>
            <div>
              <span className="docs-seq-cmd">get_relations</span>
              <span className="docs-seq-arg">(concept_xxx)</span>
              <span className="docs-seq-arrow">→</span>
              <span className="docs-seq-res">prerequisite / contrasts_with</span>
            </div>
            <div>
              <span className="docs-seq-cmd">get_evidences_of_node</span>
              <span className="docs-seq-arg">(concept_xxx)</span>
              <span className="docs-seq-arrow">→</span>
              <span className="docs-seq-res">described_by 证据</span>
            </div>
            <div>
              <span className="docs-seq-cmd">get_evidence_by_id</span>
              <span className="docs-seq-arg">(ev_...)</span>
              <span className="docs-seq-arrow">→</span>
              <span className="docs-seq-res">原文小节</span>
            </div>
            <div className="docs-seq-answer">
              <span className="docs-seq-q">答</span>
              <span>
                两者是并列的学习范式…… <span className="docs-seq-cite">[ev_Hello-Agents_0:1.1.1]</span>
              </span>
            </div>
          </div>
        </div>

        <div className="docs-seq-block">
          <div className="docs-seq-head">assemble_context 组装出的上下文（示意）</div>
          <pre className="docs-context">{`### [概念] 强化学习 (concept_xxx)
- 描述: 智能体通过与环境交互、根据反馈调整策略来学习……
- 相关证据: [ev_Hello-Agents_0:1.1.1] (1.1.1) 传统视角下的智能体

### 相关证据原文
[ev_Hello-Agents_0:1.1.1] (第1章 > 1.1 > 1.1.1) 传统视角下的智能体
  强化学习是……`}</pre>
        </div>
        <div className="docs-query-note">
          <span className="docs-query-note-dot" />
          <code>assemble_context</code> 只做格式化：节点卡、证据引用和原文片段，不做语义取舍；
          证据原文默认最多 12 条，超出按上限截断。
        </div>
      </section>
    </div>
  );
}
