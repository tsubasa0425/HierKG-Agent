import { useEffect, useRef } from 'react';
import { Button } from 'antd';
import { ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons';
import Graph from 'graphology';
import Sigma from 'sigma';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import type { GraphNode, GraphEdge } from '../services/api';

// ---------------------------------------------------------------------------
// ProvenanceGraph —— 答题溯源 Drawer 内嵌的轻量 sigma 子图。
//
// 与 GraphView.tsx 的挂载模式同源（graphology → forceAtlas2 → Sigma），但只保留
// 「深底 + 命中节点高亮」语义：highlightIds 命中的节点橙色放大，未命中边的淡显。
// 节点坐标在挂载时由 forceAtlas2 铺开，效果与图谱页一致（无相机自 fit）。
//
// 滚轮约定（重要）：sigma 默认会把 canvas 上的 wheel 全吃掉做缩放，导致它上面盖着
// 的 Drawer 没法往下滚动看证据原文。这里在捕获阶段拦截：不带 Ctrl/Cmd 的滚轮
// 放行给外层滚动容器（图谱随之正常滚走），Ctrl/Cmd+滚轮 / 触控板捏合仍缩放。
// 右上角 +/− 按钮是显式的缩放入口，鼠标用户不必记组合键。
// ---------------------------------------------------------------------------

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** 高亮（本答案实际用到的）节点 id 集合 */
  highlightIds: string[];
  height?: string | number;
  /** 单击节点（证据 → 定位到下方原文列表） */
  onNodeClick?: (id: string) => void;
}

export default function ProvenanceGraph({
  nodes,
  edges,
  highlightIds,
  height = '46vh',
  onNodeClick,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const hlRef = useRef<Set<string>>(new Set(highlightIds));
  // 用 ref 转发点击回调：重建 effect 只依赖 nodes/edges，避免父组件重渲时重挂 sigma。
  // 渲染期不写 ref（react-hooks/refs），放到 effect 里随每次渲染同步最新回调。
  const onClickRef = useRef(onNodeClick);
  useEffect(() => {
    onClickRef.current = onNodeClick;
  });

  // 高亮集变化 → 只刷新渲染器，不重跑布局
  useEffect(() => {
    hlRef.current = new Set(highlightIds);
    sigmaRef.current?.refresh();
  }, [highlightIds]);

  const zoomBy = (factor: number) => {
    const cam = sigmaRef.current?.getCamera();
    if (cam) cam.animatedZoom({ factor });
  };

  // 数据变化 → 重建图实例
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    sigmaRef.current?.kill();
    sigmaRef.current = null;

    if (nodes.length === 0) return;

    // 滚轮捕获阶段拦截：sigma 的 wheel 监听在冒泡阶段会把事件吞掉(preventDefault+stopPropagation)，
    // 使 Drawer 无法滚动。这里不带 Ctrl/Cmd 时只 stopPropagation(不 preventDefault)：
    // 浏览器仍会滚动外层 .ant-drawer-body，图谱随之滚走；Ctrl/Cmd+滚轮/捏合则放行给 sigma 缩放。
    const onWheelCapture = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) return;
      e.stopPropagation();
    };
    container.addEventListener('wheel', onWheelCapture, true);

    const graph = new Graph({ multi: true });
    nodes.forEach((n) => {
      graph.addNode(n.id, {
        label: n.label,
        // 初始随机位置，forceAtlas2 负责铺开
        x: Math.random(),
        y: Math.random(),
        size: n.size,
        color: n.color,
      });
    });
    edges.forEach((e, idx) => {
      if (!graph.hasNode(e.from) || !graph.hasNode(e.to)) return;
      graph.addEdgeWithKey(`e-${idx}`, e.from, e.to, {
        size: Math.max(1, e.weight / 3),
        color: '#555566',
        type: 'arrow',
      });
    });

    forceAtlas2.assign(graph, {
      iterations: graph.order > 200 ? 100 : 180,
      settings: {
        ...forceAtlas2.inferSettings(graph),
        gravity: 0.5,
        scalingRatio: 12,
        slowDown: 2,
      },
    });

    const sigma = new Sigma(graph, container, {
      renderEdgeLabels: false,
      labelColor: { color: '#e0e0e0' },
      labelSize: 13,
      labelDensity: 0.5,
      labelGridCellSize: 80,
      defaultEdgeColor: '#555566',
      minCameraRatio: 0.1,
      maxCameraRatio: 8,
    });
    sigmaRef.current = sigma;

    // 高亮命中：橙色放大；两端的边提亮（其余边淡显，突出「答题路径」）
    sigma.setSetting('nodeReducer', (node, data) => {
      if (hlRef.current.has(node)) {
        return {
          ...data,
          color: '#ff6b00',
          size: Math.max((data.size ?? 4) * 1.7, 16),
        };
      }
      return data;
    });
    sigma.setSetting('edgeReducer', (edge, data) => {
      const ext = graph.extremities(edge);
      if (hlRef.current.has(ext[0]) || hlRef.current.has(ext[1])) {
        return { ...data, hidden: false, color: '#e8b067' };
      }
      return { ...data, hidden: true };
    });

    sigma.on('clickNode', ({ node }) => onClickRef.current?.(node));

    return () => {
      container.removeEventListener('wheel', onWheelCapture, true);
      sigmaRef.current?.kill();
      sigmaRef.current = null;
    };
  }, [nodes, edges]);

  return (
    <div style={{ position: 'relative', width: '100%', height }}>
      <div
        ref={containerRef}
        style={{
          position: 'absolute',
          inset: 0,
          background: '#1a1a2e',
          borderRadius: 8,
          overflow: 'hidden',
        }}
      />
      {/* 显式缩放入口：鼠标用户不用记 Ctrl/Cmd+滚轮 组合键 */}
      <div className="prov-graph-toolbar">
        <Button
          type="text"
          size="small"
          icon={<ZoomOutOutlined />}
          title="缩小"
          aria-label="缩小图谱"
          onClick={() => zoomBy(1 / 1.35)}
        />
        <Button
          type="text"
          size="small"
          icon={<ZoomInOutlined />}
          title="放大"
          aria-label="放大图谱"
          onClick={() => zoomBy(1.35)}
        />
      </div>
    </div>
  );
}
