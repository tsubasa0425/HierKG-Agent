import type { ReactNode } from 'react';

// ---------------------------------------------------------------------------
// 共用
// ---------------------------------------------------------------------------

type FlowKind = 'input' | 'llm' | 'rule' | 'evidence' | 'vector' | 'assemble';

const KIND_COLOR: Record<FlowKind, string> = {
  input: '#8c8c8c',
  llm: '#722ed1',
  rule: '#2f6fed',
  evidence: '#fa8c16',
  vector: '#00b578',
  assemble: '#eb2f96',
};

interface FlowNodeProps {
  x: number;
  y: number;
  no: string;
  title: string;
  sub: string;
  kind: FlowKind;
  width?: number;
  height?: number;
  color?: string;
  sub2?: string;
}

function FlowNode({
  x,
  y,
  no,
  title,
  sub,
  kind,
  width = 140,
  height = 64,
  color,
  sub2,
}: FlowNodeProps) {
  const accent = color ?? KIND_COLOR[kind];
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={10}
        fill="#ffffff"
        stroke="#e3e8f0"
      />
      <rect x={x} y={y} width={4} height={height} rx={2} fill={accent} />
      <text
        x={x + 16}
        y={y + 25}
        fontSize={12.5}
        fontWeight={600}
        fill="#1f2329"
      >
        {no} {title}
      </text>
      <text x={x + 16} y={y + 45} fontSize={10} fill="#8c8c8c">
        {sub}
      </text>
      {sub2 && (
        <text x={x + 16} y={y + 62} fontSize={10} fill="#b0b8c6">
          {sub2}
        </text>
      )}
    </g>
  );
}

function ArrowDefs({ id }: { id: string }) {
  return (
    <defs>
      <marker
        id={id}
        markerWidth={8}
        markerHeight={8}
        refX={7}
        refY={4}
        orient="auto"
      >
        <path d="M0,0 L8,4 L0,8 Z" fill="#b0b8c6" />
      </marker>
    </defs>
  );
}

interface LegendItem {
  color: string;
  label: string;
}

const BUILD_LEGEND: LegendItem[] = [
  { color: KIND_COLOR.input, label: '输入 / 产物' },
  { color: KIND_COLOR.rule, label: '规则 / 确定性步骤' },
  { color: KIND_COLOR.llm, label: 'LLM 步骤' },
  { color: KIND_COLOR.evidence, label: '证据层' },
  { color: KIND_COLOR.vector, label: '向量步骤' },
  { color: KIND_COLOR.assemble, label: '装配 / 发布' },
];

const QUERY_LEGEND: LegendItem[] = [
  { color: '#722ed1', label: '提问' },
  { color: '#2f6fed', label: '定位入口' },
  { color: '#00b578', label: '图内扩展' },
  { color: '#fa8c16', label: '回取证据' },
  { color: '#eb2f96', label: '带引用回答' },
];

export function FlowLegend({ variant = 'build' }: { variant?: 'build' | 'query' }): ReactNode {
  const items = variant === 'build' ? BUILD_LEGEND : QUERY_LEGEND;
  return (
    <div className="docs-flow-legend">
      {items.map((item) => (
        <span key={item.label} className="docs-flow-legend-item">
          <span className="docs-flow-swatch" style={{ background: item.color }} />
          {item.label}
        </span>
      ))}
      <span className="docs-flow-legend-item">
        <svg width="22" height="8" aria-hidden="true">
          <line x1="0" y1="4" x2="18" y2="4" stroke="#b0b8c6" strokeWidth="1.5" />
          <path d="M16,1 L21,4 L16,7 Z" fill="#b0b8c6" />
        </svg>
        数据流
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 构图流程
// ---------------------------------------------------------------------------

const B_W = 140;
const B_H = 64;
const ROW1_Y = 60;
const ROW2_Y = 220;
const ROW1_X = [110, 274, 438, 602, 766];
const ROW2_X = [110, 274, 438, 602, 766, 930];

const arrowX = (fromX: number, toX: number, width = B_W) => ({
  x1: fromX + width + 4,
  x2: toX - 4,
});

export function BuildFlowChart() {
  return (
    <div className="docs-chart-scroll">
      <svg
        className="docs-chart docs-build-chart"
        viewBox="0 0 1180 420"
        role="img"
        aria-labelledby="build-chart-title build-chart-desc"
      >
        <title id="build-chart-title">HierKG 构图流程</title>
        <desc id="build-chart-desc">
          从 docx 开始，依次经过文本分割、实体关系抽取、目录图、证据层、实体归并、分层归属、
          向量嵌入、同层去重、关系补全和四层装配，最终产出 final_kg.json 与 build_manifest.json。
        </desc>
        <ArrowDefs id="docs-build-arrow" />

        <text x={10} y={44} fontSize={11} fontWeight={700} fill="#722ed1">
          ① 解析与证据
        </text>
        <text x={10} y={204} fontSize={11} fontWeight={700} fill="#2f6fed">
          ② 归并与精修
        </text>
        <text x={930} y={204} fontSize={11} fontWeight={700} fill="#eb2f96">
          ③ 发布
        </text>

        <FlowNode x={ROW1_X[0]} y={ROW1_Y} no="IN" title="docx" sub="结构化文档" kind="input" />
        <FlowNode x={ROW1_X[1]} y={ROW1_Y} no="01" title="文本分割" sub="toc_structure.json" kind="rule" />
        <FlowNode x={ROW1_X[2]} y={ROW1_Y} no="02" title="实体关系抽取" sub="LLM · 闭集类型" kind="llm" />
        <FlowNode x={ROW1_X[3]} y={ROW1_Y} no="03" title="目录图" sub="toc_graph.json" kind="rule" />
        <FlowNode x={ROW1_X[4]} y={ROW1_Y} no="04" title="证据层构建" sub="L3 evidence.json" kind="evidence" />

        {[0, 1, 2, 3].map((index) => {
          const { x1, x2 } = arrowX(ROW1_X[index], ROW1_X[index + 1]);
          return (
            <line
              key={`row1-arrow-${index}`}
              x1={x1}
              y1={ROW1_Y + B_H / 2}
              x2={x2}
              y2={ROW1_Y + B_H / 2}
              stroke="#b0b8c6"
              strokeWidth={1.5}
              markerEnd="url(#docs-build-arrow)"
            />
          );
        })}

        <FlowNode x={ROW2_X[0]} y={ROW2_Y} no="05" title="实体归并" sub="别名 / 覆盖度信号" kind="rule" />
        <FlowNode x={ROW2_X[1]} y={ROW2_Y} no="06" title="分层归属" sub="规则 → L1/L2 → LLM" kind="rule" />
        <FlowNode x={ROW2_X[2]} y={ROW2_Y} no="07" title="向量嵌入" sub="bge-m3" kind="vector" />
        <FlowNode x={ROW2_X[3]} y={ROW2_Y} no="08" title="同层去重" sub="向量 + LLM 复核" kind="llm" />
        <FlowNode x={ROW2_X[4]} y={ROW2_Y} no="09" title="关系补全" sub="拓扑 + 向量 + LLM" kind="llm" />
        <FlowNode x={ROW2_X[5]} y={ROW2_Y} no="10" title="四层装配" sub="校验 / 证据边 / 清单" kind="assemble" />

        {[0, 1, 2, 3, 4].map((index) => {
          const { x1, x2 } = arrowX(ROW2_X[index], ROW2_X[index + 1]);
          return (
            <line
              key={`row2-arrow-${index}`}
              x1={x1}
              y1={ROW2_Y + B_H / 2}
              x2={x2}
              y2={ROW2_Y + B_H / 2}
              stroke="#b0b8c6"
              strokeWidth={1.5}
              markerEnd="url(#docs-build-arrow)"
            />
          );
        })}

        <polyline
          points="836,124 836,175 180,175 180,214"
          fill="none"
          stroke="#b0b8c6"
          strokeWidth={1.5}
          markerEnd="url(#docs-build-arrow)"
        />
        <text x={520} y={168} textAnchor="middle" fontSize={10.5} fill="#9aa3b2">
          进入归并阶段
        </text>

        <line
          x1={1000}
          y1={ROW2_Y + B_H + 4}
          x2={1000}
          y2={326}
          stroke="#b0b8c6"
          strokeWidth={1.5}
          markerEnd="url(#docs-build-arrow)"
        />
        <FlowNode
          x={845}
          y={330}
          width={310}
          height={56}
          no="OUT"
          title="final_kg.json"
          sub="+ build_manifest.json（L0–L3 + 清单）"
          kind="assemble"
        />
        <text x={1000} y={408} textAnchor="middle" fontSize={10.5} fill="#9aa3b2">
          导入 Neo4j（图谱）与 Chroma（向量）
        </text>
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 查询流程
// ---------------------------------------------------------------------------

const Q_W = 196;
const Q_H = 76;
const Q_Y = 40;
const Q_X = [16, 254, 492, 730, 968];

export function QueryFlowChart() {
  return (
    <div className="docs-chart-scroll">
      <svg
        className="docs-chart docs-query-chart"
        viewBox="0 0 1180 320"
        role="img"
        aria-labelledby="query-chart-title query-chart-desc"
      >
        <title id="query-chart-title">HierKG 查询流程</title>
        <desc id="query-chart-desc">
          用户提问后，先通过语义搜索定位入口节点，再沿图关系扩展上下文，
          回取 L3 证据，经精排与上下文组装后给出带证据引用的回答。
        </desc>
        <ArrowDefs id="docs-query-arrow" />

        <FlowNode x={Q_X[0]} y={Q_Y} width={Q_W} height={Q_H} no="01" title="提问" sub="SSE 流式返回" kind="assemble" color="#722ed1" />
        <FlowNode x={Q_X[1]} y={Q_Y} width={Q_W} height={Q_H} no="02" title="定位入口" sub="semantic_search · search_*" kind="rule" color="#2f6fed" />
        <FlowNode x={Q_X[2]} y={Q_Y} width={Q_W} height={Q_H} no="03" title="图内扩展" sub="get_relations · multi_hop" kind="vector" color="#00b578" />
        <FlowNode x={Q_X[3]} y={Q_Y} width={Q_W} height={Q_H} no="04" title="回取证据" sub="get_evidences_of_node" kind="evidence" color="#fa8c16" />
        <FlowNode x={Q_X[4]} y={Q_Y} width={Q_W} height={Q_H} no="05" title="带引用回答" sub="assemble_context · [ev_…]" kind="assemble" color="#eb2f96" />

        {[0, 1, 2, 3].map((index) => {
          const { x1, x2 } = arrowX(Q_X[index], Q_X[index + 1], Q_W);
          return (
            <line
              key={`query-arrow-${index}`}
              x1={x1}
              y1={Q_Y + Q_H / 2}
              x2={x2}
              y2={Q_Y + Q_H / 2}
              stroke="#b0b8c6"
              strokeWidth={1.5}
              markerEnd="url(#docs-query-arrow)"
            />
          );
        })}

        <line
          x1={Q_X[2] + Q_W / 2}
          y1={Q_Y + Q_H + 4}
          x2={Q_X[2] + Q_W / 2}
          y2={176}
          stroke="#b0b8c6"
          strokeWidth={1.5}
          strokeDasharray="5 4"
          markerEnd="url(#docs-query-arrow)"
        />

        <rect
          x={180}
          y={180}
          width={820}
          height={82}
          rx={10}
          fill="#fafbfc"
          stroke="#dbe7ff"
          strokeDasharray="5 4"
        />
        <text x={200} y={204} fontSize={12} fontWeight={600} fill="#1f2329">
          按问题复杂度决定遍历深度
        </text>
        <text x={200} y={226} fontSize={11} fill="#5b6472">
          具体 / 单概念问题 → 浅遍历：节点 → 证据
        </text>
        <text x={200} y={246} fontSize={11} fill="#5b6472">
          对比 / 多概念 / 模糊问题 → 深遍历：multi_hop_traverse + get_entities_of_concept
        </text>
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 四层关系示意
// ---------------------------------------------------------------------------

function ColorMarker({ id, color }: { id: string; color: string }) {
  return (
    <marker id={id} markerWidth={8} markerHeight={8} refX={7} refY={4} orient="auto">
      <path d="M0,0 L8,4 L0,8 Z" fill={color} />
    </marker>
  );
}

export function LayerStackDiagram() {
  return (
    <div className="docs-chart-scroll">
      <svg
        className="docs-chart docs-layer-chart"
        viewBox="0 0 1160 480"
        role="img"
        aria-labelledby="layer-chart-title layer-chart-desc"
      >
        <title id="layer-chart-title">四层图谱的分层结构与依赖</title>
        <desc id="layer-chart-desc">
          L0 本体层定义节点类型、关系类型、分层规则和证据结构，约束 L1 到 L3；
          L2 实体通过 instance_of / example_of 归属 L1 概念；L1 通过 described_by、
          L1 / L2 通过 appears_in 引用 L3 证据；L1 概念之间用语义关系相连。
        </desc>
        <defs>
          <ColorMarker id="layer-semantic" color="#8c8c8c" />
          <ColorMarker id="layer-instance" color="#2f6fed" />
          <ColorMarker id="layer-evidence" color="#fa8c16" />
          <ColorMarker id="layer-govern" color="#722ed1" />
        </defs>

        {/* L0：本体层，作为约束外壳 */}
        <rect x={160} y={20} width={880} height={76} rx={12} fill="#f9f4ff" stroke="#e3d3fb" />
        <rect x={160} y={20} width={5} height={76} rx={2.5} fill="#722ed1" />
        <text x={180} y={46} fontSize={13} fontWeight={600} fill="#1f2329">
          L0 本体层
        </text>
        <text x={180} y={64} fontSize={10} fill="#8c8c8c">
          schema.yaml
        </text>
        <text x={430} y={46} fontSize={11} fill="#5b6472">
          节点类型 · 关系类型 · LayerPolicy · 证据结构
        </text>
        <text x={430} y={64} fontSize={10} fill="#9aa3b2">
          闭集定义，约束下面所有层
        </text>

        {/* L1：概念层 */}
        <rect x={160} y={124} width={880} height={92} rx={12} fill="#f3f7ff" stroke="#d3e0ff" />
        <rect x={160} y={124} width={5} height={92} rx={2.5} fill="#2f6fed" />
        <text x={180} y={152} fontSize={13} fontWeight={600} fill="#1f2329">
          L1 概念层
        </text>
        <text x={180} y={170} fontSize={10} fill="#8c8c8c">
          跨章节复用的抽象概念 / 方法
        </text>
        <FlowNode
          x={420}
          y={136}
          width={150}
          height={68}
          no="L1"
          title="强化学习"
          sub="概念 / 方法"
          kind="rule"
          color="#2f6fed"
        />
        <FlowNode
          x={650}
          y={136}
          width={150}
          height={68}
          no="L1"
          title="监督学习"
          sub="概念 / 方法"
          kind="rule"
          color="#2f6fed"
        />
        <line
          x1={574}
          y1={170}
          x2={644}
          y2={170}
          stroke="#8c8c8c"
          strokeWidth={1.6}
          markerEnd="url(#layer-semantic)"
        />
        <text x={609} y={160} textAnchor="middle" fontSize={10.5} fill="#8c8c8c">
          contrasts_with
        </text>

        {/* L2：实体层 */}
        <rect x={160} y={248} width={880} height={92} rx={12} fill="#f2fbf7" stroke="#c6ecdc" />
        <rect x={160} y={248} width={5} height={92} rx={2.5} fill="#00b578" />
        <text x={180} y={276} fontSize={13} fontWeight={600} fill="#1f2329">
          L2 实体层
        </text>
        <text x={180} y={294} fontSize={10} fill="#8c8c8c">
          具体实例 / 产品 / 人物 / 配置值
        </text>
        <FlowNode
          x={420}
          y={260}
          width={150}
          height={68}
          no="L2"
          title="AlphaGo"
          sub="人工物 · 具体实例"
          kind="vector"
          color="#00b578"
        />
        <FlowNode
          x={650}
          y={260}
          width={150}
          height={68}
          no="L2"
          title="BERT"
          sub="人工物 · 具体实例"
          kind="vector"
          color="#00b578"
        />

        {/* L2 → L1：归属 */}
        <line
          x1={495}
          y1={256}
          x2={495}
          y2={208}
          stroke="#2f6fed"
          strokeWidth={1.6}
          strokeDasharray="6 4"
          markerEnd="url(#layer-instance)"
        />
        <text x={503} y={238} fontSize={10.5} fill="#2f6fed">
          instance_of
        </text>
        <line
          x1={725}
          y1={256}
          x2={725}
          y2={208}
          stroke="#2f6fed"
          strokeWidth={1.6}
          strokeDasharray="6 4"
          markerEnd="url(#layer-instance)"
        />
        <text x={733} y={238} fontSize={10.5} fill="#2f6fed">
          example_of
        </text>

        {/* L3：证据层 */}
        <rect x={160} y={372} width={880} height={92} rx={12} fill="#fff9f2" stroke="#ffdfbc" />
        <rect x={160} y={372} width={5} height={92} rx={2.5} fill="#fa8c16" />
        <text x={180} y={400} fontSize={13} fontWeight={600} fill="#1f2329">
          L3 证据层
        </text>
        <text x={180} y={418} fontSize={10} fill="#8c8c8c">
          可定位到小节 / 段落的原文片段
        </text>
        <FlowNode
          x={420}
          y={384}
          width={260}
          height={68}
          no="L3"
          title="证据片段"
          sub="ev_Hello-Agents_0:1.1.1"
          sub2="原文片段 · 可回溯"
          kind="evidence"
        />
        <FlowNode
          x={710}
          y={384}
          width={260}
          height={68}
          no="L3"
          title="证据片段"
          sub="ev_Hello-Agents_0:2.3.1"
          sub2="原文片段 · 可回溯"
          kind="evidence"
        />

        {/* L2 → L3：证据引用（appears_in 允许 L1-L3 / L2-L3） */}
        <line
          x1={520}
          y1={332}
          x2={520}
          y2={380}
          stroke="#fa8c16"
          strokeWidth={1.6}
          strokeDasharray="3 4"
          markerEnd="url(#layer-evidence)"
        />
        <line
          x1={760}
          y1={332}
          x2={760}
          y2={380}
          stroke="#fa8c16"
          strokeWidth={1.6}
          strokeDasharray="3 4"
          markerEnd="url(#layer-evidence)"
        />
        <text x={640} y={360} textAnchor="middle" fontSize={10.5} fill="#fa8c16">
          appears_in（L2 → L3）
        </text>

        {/* L1 → L3：绕开 L2 的证据引用 */}
        <polyline
          points="420,136 420,112 120,112 120,440 416,440"
          fill="none"
          stroke="#fa8c16"
          strokeWidth={1.6}
          strokeDasharray="3 4"
          markerEnd="url(#layer-evidence)"
        />
        <text
          x={112}
          y={290}
          textAnchor="middle"
          transform="rotate(-90 112 290)"
          fontSize={10.5}
          fill="#fa8c16"
        >
          described_by · L1 → L3
        </text>

        {/* L0 → L1/L2/L3：本体约束 */}
        <polyline
          points="1040,58 1094,58 1094,418"
          fill="none"
          stroke="#722ed1"
          strokeWidth={1.6}
          strokeDasharray="4 4"
        />
        <line
          x1={1094}
          y1={170}
          x2={1046}
          y2={170}
          stroke="#722ed1"
          strokeWidth={1.6}
          strokeDasharray="4 4"
          markerEnd="url(#layer-govern)"
        />
        <line
          x1={1094}
          y1={294}
          x2={1046}
          y2={294}
          stroke="#722ed1"
          strokeWidth={1.6}
          strokeDasharray="4 4"
          markerEnd="url(#layer-govern)"
        />
        <line
          x1={1094}
          y1={418}
          x2={1046}
          y2={418}
          stroke="#722ed1"
          strokeWidth={1.6}
          strokeDasharray="4 4"
          markerEnd="url(#layer-govern)"
        />
        <text
          x={1108}
          y={238}
          textAnchor="middle"
          transform="rotate(-90 1108 238)"
          fontSize={10.5}
          fill="#722ed1"
        >
          L0 定义 / 约束
        </text>
      </svg>
    </div>
  );
}
