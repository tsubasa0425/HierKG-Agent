import { useEffect, useRef, useState, useMemo } from 'react';
import {
  Layout,
  Select,
  Slider,
  Button,
  Drawer,
  Spin,
  Empty,
  Tag,
  Typography,
  Space,
  Descriptions,
  Divider,
  AutoComplete,
  Input,
} from 'antd';
import {
  ReloadOutlined,
  NodeIndexOutlined,
  ClusterOutlined,
  InfoCircleOutlined,
  SearchOutlined,
  ArrowLeftOutlined,
  LinkOutlined,
} from '@ant-design/icons';
import Graph from 'graphology';
import Sigma from 'sigma';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import {
  getGraphData,
  getGraphStats,
  searchEntities,
  getEntityNeighborhood,
  type GraphDataResponse,
  type GraphStatsResponse,
  type GraphSearchResult,
} from '../services/api';

const { Sider, Content } = Layout;
const { Title, Text, Paragraph } = Typography;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function GraphView() {
  // Refs for sigma.js
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);

  // Data state
  const [graphData, setGraphData] = useState<GraphDataResponse | null>(null);
  const [stats, setStats] = useState<GraphStatsResponse | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [graphLoading, setGraphLoading] = useState(false);

  // Filter state（按层级筛选）
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [nodeLimit, setNodeLimit] = useState(200);

  // Drawer state
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedNode, setSelectedNode] = useState<{
    id: string;
    label: string;
    type: string;
    layer: string;
    description: string;
    color: string;
  } | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<{
    from: string;
    to: string;
    label: string;
    weight: number;
    fromNode?: GraphDataResponse['nodes'][number];
    toNode?: GraphDataResponse['nodes'][number];
  } | null>(null);
  const [drawerType, setDrawerType] = useState<'node' | 'edge'>('node');

  // Entity search / exploration state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchOptions, setSearchOptions] = useState<
    Array<{ value: string; label: React.ReactNode; node_id: string; name: string }>
  >([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [exploringEntity, setExploringEntity] = useState<string | null>(null);
  const [exploringLabel, setExploringLabel] = useState<string>('');
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ---------------------------------------------------------------------------
  // Initial load: stats + full graph（无数据集概念，挂载即拉）
  // ---------------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    setStatsLoading(true);
    getGraphStats()
      .then((data) => {
        if (!cancelled) setStats(data);
      })
      .catch(() => {
        if (!cancelled) setStats(null);
      })
      .finally(() => {
        if (!cancelled) setStatsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ---------------------------------------------------------------------------
  // Load graph data when filters change (skip when exploring an entity)
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (exploringEntity) return;

    let cancelled = false;
    setGraphLoading(true);

    getGraphData(selectedTypes.length > 0 ? selectedTypes : undefined, nodeLimit)
      .then((data) => {
        if (!cancelled) setGraphData(data);
      })
      .catch(() => {
        if (!cancelled) setGraphData(null);
      })
      .finally(() => {
        if (!cancelled) setGraphLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedTypes, nodeLimit, exploringEntity]);

  // ---------------------------------------------------------------------------
  // Entity search: debounced fuzzy search as user types
  // ---------------------------------------------------------------------------

  const handleSearchChange = (value: string) => {
    setSearchQuery(value);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);

    if (!value.trim()) {
      setSearchOptions([]);
      return;
    }

    setSearchLoading(true);
    searchTimerRef.current = setTimeout(() => {
      searchEntities(value.trim())
        .then((results: GraphSearchResult[]) => {
          setSearchOptions(
            results.map((r) => ({
              value: r.name,
              label: (
                <Space>
                  <span>{r.name}</span>
                  <Tag color={layerColor(r.layer)} style={{ fontSize: 11 }}>{r.layer}</Tag>
                </Space>
              ),
              node_id: r.node_id,
              name: r.name,
            })),
          );
        })
        .catch(() => setSearchOptions([]))
        .finally(() => setSearchLoading(false));
    }, 300);
  };

  const handleExploreEntity = (nodeId: string, label?: string) => {
    if (!nodeId) return;
    setExploringEntity(nodeId);
    setExploringLabel(label ?? nodeId);
    setSearchQuery(label ?? nodeId);
    setSearchOptions([]);
    setDrawerOpen(false);
  };

  const handleBackToFullGraph = () => {
    setExploringEntity(null);
    setExploringLabel('');
    setSearchQuery('');
  };

  // ---------------------------------------------------------------------------
  // Load neighborhood subgraph when exploring an entity
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (!exploringEntity) return;

    let cancelled = false;
    setGraphLoading(true);

    getEntityNeighborhood(exploringEntity, 2)
      .then((data) => {
        if (!cancelled) setGraphData(data);
      })
      .catch(() => {
        if (!cancelled) setGraphData(null);
      })
      .finally(() => {
        if (!cancelled) setGraphLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [exploringEntity]);

  // ---------------------------------------------------------------------------
  // Initialize / update sigma.js whenever graphData changes
  // ---------------------------------------------------------------------------

  useEffect(() => {
    if (!containerRef.current || !graphData) return;

    // Tear down previous instance
    sigmaRef.current?.kill();
    sigmaRef.current = null;

    if (graphData.nodes.length === 0) return;

    const graph = new Graph({ multi: true });

    graphData.nodes.forEach((n) => {
      const isRoot = exploringEntity != null && n.id === exploringEntity;
      graph.addNode(n.id, {
        label: n.label,
        // Random initial placement; forceAtlas2 spreads them out.
        x: Math.random(),
        y: Math.random(),
        size: isRoot ? Math.min(n.size * 1.4, 60) : n.size,
        color: isRoot ? '#ff6b00' : n.color,
      });
    });

    graphData.edges.forEach((e, idx) => {
      if (!graph.hasNode(e.from) || !graph.hasNode(e.to)) return;
      graph.addEdgeWithKey(`e-${idx}`, e.from, e.to, {
        size: Math.max(1, e.weight / 3),
        color: '#555566',
        type: 'arrow',
      });
    });

    // Force-directed layout for a readable spread.
    forceAtlas2.assign(graph, {
      iterations: graph.order > 400 ? 120 : 200,
      settings: {
        ...forceAtlas2.inferSettings(graph),
        gravity: 0.5,
        scalingRatio: 12,
        slowDown: 2,
      },
    });

    const sigma = new Sigma(graph, containerRef.current, {
      renderEdgeLabels: false,
      labelColor: { color: '#e0e0e0' },
      labelSize: 13,
      labelDensity: 0.4,
      labelGridCellSize: 80,
      defaultEdgeColor: '#555566',
      minCameraRatio: 0.1,
      maxCameraRatio: 10,
    });
    sigmaRef.current = sigma;

    // Hover highlighting: emphasize hovered node's neighborhood.
    let hoveredNode: string | null = null;
    sigma.setSetting('nodeReducer', (node, data) => {
      if (hoveredNode && node !== hoveredNode && !graph.areNeighbors(hoveredNode, node)) {
        return { ...data, color: '#2a2a3e', label: '' };
      }
      return data;
    });
    sigma.setSetting('edgeReducer', (edge, data) => {
      if (hoveredNode && !graph.extremities(edge).includes(hoveredNode)) {
        return { ...data, hidden: true };
      }
      return data;
    });

    sigma.on('enterNode', ({ node }) => {
      hoveredNode = node;
      sigma.refresh();
    });
    sigma.on('leaveNode', () => {
      hoveredNode = null;
      sigma.refresh();
    });

    // Single click → open detail drawer.
    sigma.on('clickNode', ({ node }) => {
      const n = graphData.nodes.find((x) => x.id === node);
      if (n) {
        setSelectedNode({
          id: n.id,
          label: n.label,
          type: n.type,
          layer: n.layer,
          description: n.description,
          color: n.color,
        });
        setSelectedEdge(null);
        setDrawerType('node');
        setDrawerOpen(true);
      }
    });

    // Double click → explore that node's neighborhood.
    sigma.on('doubleClickNode', ({ node, preventSigmaDefault }) => {
      preventSigmaDefault();
      const n = graphData.nodes.find((x) => x.id === node);
      handleExploreEntity(node, n?.label);
    });

    sigma.on('clickEdge', ({ edge }) => {
      const idx = Number(edge.replace('e-', ''));
      const e = graphData.edges[idx];
      if (!e) return;
      const fromNode = graphData.nodes.find((n) => n.id === e.from);
      const toNode = graphData.nodes.find((n) => n.id === e.to);
      setSelectedEdge({
        from: e.from,
        to: e.to,
        label: e.label,
        weight: e.weight,
        fromNode,
        toNode,
      });
      setSelectedNode(null);
      setDrawerType('edge');
      setDrawerOpen(true);
    });

    return () => {
      sigmaRef.current?.kill();
      sigmaRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphData, exploringEntity]);

  // ---------------------------------------------------------------------------
  // Layer filter options derived from stats
  // ---------------------------------------------------------------------------

  const typeOptions = useMemo(() => {
    if (!stats) return [];
    return [
      { label: `L1 概念 (${stats.layers.concept ?? 0})`, value: 'concept' },
      { label: `L2 实体 (${stats.layers.entity ?? 0})`, value: 'entity' },
      { label: `L3 证据 (${stats.layers.evidence ?? 0})`, value: 'evidence' },
    ];
  }, [stats]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div style={{ margin: -24, height: 'calc(100vh - 64px)' }}>
      <Layout style={{ height: '100%' }}>
        {/* ── Left sidebar: filters & stats ─────────────────────────── */}
        <Sider
          width={300}
          theme="light"
          style={{
            padding: '16px',
            overflowY: 'auto',
            borderRight: '1px solid #f0f0f0',
          }}
        >
          <Title level={4} style={{ marginBottom: 20 }}>
            <NodeIndexOutlined style={{ marginRight: 8 }} />
            图谱控制面板
          </Title>

          {/* Usage tip */}
          <div
            style={{
              marginBottom: 16,
              padding: '8px 12px',
              background: '#e6f4ff',
              borderRadius: 6,
              fontSize: 12,
              color: '#1677ff',
            }}
          >
            <InfoCircleOutlined style={{ marginRight: 4 }} />
            提示：单击查看详情，双击节点展开其邻域关系
          </div>

          {/* Layer legend */}
          <div style={{ marginBottom: 20 }}>
            <Text strong style={{ display: 'block', marginBottom: 6 }}>
              层级图例
            </Text>
            <Space wrap>
              <Tag color="#2f6fed">L1 概念</Tag>
              <Tag color="#00b578">L2 实体</Tag>
              <Tag color="#fa8c16">L3 证据</Tag>
            </Space>
          </div>

          {/* Layer filter */}
          <div style={{ marginBottom: 20 }}>
            <Text strong style={{ display: 'block', marginBottom: 6 }}>
              层级筛选
            </Text>
            <Select
              mode="multiple"
              style={{ width: '100%' }}
              placeholder="全部层级"
              value={selectedTypes}
              onChange={setSelectedTypes}
              options={typeOptions}
              loading={statsLoading}
              allowClear
              maxTagCount="responsive"
              disabled={!!exploringEntity}
            />
          </div>

          {/* Entity search / explore */}
          <div style={{ marginBottom: 20 }}>
            <Text strong style={{ display: 'block', marginBottom: 6 }}>
              <SearchOutlined style={{ marginRight: 4 }} />
              实体查找
            </Text>
            <AutoComplete
              style={{ width: '100%' }}
              options={searchOptions}
              onSearch={handleSearchChange}
              onSelect={(value, option) => {
                const o = option as { node_id: string; name: string } | undefined;
                handleExploreEntity(o?.node_id ?? value, o?.name ?? value);
              }}
              value={searchQuery}
            >
              <Input
                placeholder="输入名称/别名模糊搜索..."
                prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
                allowClear
                suffix={searchLoading ? <Spin size="small" /> : undefined}
              />
            </AutoComplete>
            {exploringEntity && (
              <div style={{ marginTop: 8 }}>
                <Tag
                  closable
                  color="orange"
                  onClose={handleBackToFullGraph}
                  style={{ fontSize: 13, padding: '4px 8px' }}
                >
                  探索: {exploringLabel} (2层关系)
                </Tag>
                <Button
                  type="link"
                  size="small"
                  icon={<ArrowLeftOutlined />}
                  onClick={handleBackToFullGraph}
                  style={{ padding: 0, fontSize: 12 }}
                >
                  返回全图
                </Button>
              </div>
            )}
          </div>

          {/* Node limit */}
          <div style={{ marginBottom: 20 }}>
            <Text strong style={{ display: 'block', marginBottom: 6 }}>
              节点数量限制: {nodeLimit}
            </Text>
            <Slider
              min={10}
              max={500}
              step={10}
              value={nodeLimit}
              onChange={setNodeLimit}
              marks={{ 10: '10', 250: '250', 500: '500' }}
              disabled={!!exploringEntity}
            />
          </div>

          {/* Refresh */}
          <Button
            icon={<ReloadOutlined />}
            onClick={() => {
              if (exploringEntity) {
                const entity = exploringEntity;
                setExploringEntity(null);
                setTimeout(() => setExploringEntity(entity), 50);
              } else {
                setSelectedTypes([...selectedTypes]);
              }
            }}
            loading={graphLoading}
            block
            style={{ marginBottom: 20 }}
          >
            刷新图谱
          </Button>

          <Divider style={{ margin: '12px 0' }} />

          {/* Graph statistics */}
          {stats && (
            <div>
              <Text strong style={{ display: 'block', marginBottom: 10 }}>
                <InfoCircleOutlined style={{ marginRight: 4 }} />
                图谱统计
              </Text>
              <Space direction="vertical" style={{ width: '100%' }} size={6}>
                <div style={statRowStyle}>
                  <Text>节点总数</Text>
                  <Text strong>{stats.total_nodes}</Text>
                </div>
                <div style={statRowStyle}>
                  <Text>关系总数</Text>
                  <Text strong>{stats.total_edges}</Text>
                </div>
              </Space>

              <div style={{ marginTop: 12 }}>
                <Text strong style={{ display: 'block', marginBottom: 8 }}>
                  各层节点数
                </Text>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  <Tag color="#2f6fed">L1 概念: {stats.layers.concept ?? 0}</Tag>
                  <Tag color="#00b578">L2 实体: {stats.layers.entity ?? 0}</Tag>
                  <Tag color="#fa8c16">L3 证据: {stats.layers.evidence ?? 0}</Tag>
                </div>
              </div>
            </div>
          )}
        </Sider>

        {/* ── Graph canvas ──────────────────────────────────────────── */}
        <Content style={{ position: 'relative', overflow: 'hidden' }}>
          {graphData && graphData.nodes.length === 0 && !graphLoading ? (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100%',
                background: '#1a1a2e',
              }}
            >
              <Empty
                description={
                  <Text style={{ color: '#888' }}>暂无图谱数据</Text>
                }
                image={<ClusterOutlined style={{ fontSize: 80, color: '#444' }} />}
              />
            </div>
          ) : (
            <Spin spinning={graphLoading} tip="加载图谱数据..." size="large">
              <div
                ref={containerRef}
                style={{
                  width: '100%',
                  height: 'calc(100vh - 64px)',
                  background: '#1a1a2e',
                }}
              />
            </Spin>
          )}
        </Content>
      </Layout>

      {/* ── Node/Edge detail drawer ──────────────────────────────────────── */}
      <Drawer
        title={drawerType === 'node' ? '节点详情' : '关系详情'}
        open={drawerOpen}
        onClose={() => {
          setDrawerOpen(false);
          setSelectedNode(null);
          setSelectedEdge(null);
        }}
        width={420}
        destroyOnClose
      >
        {/* Edge detail */}
        {drawerType === 'edge' && selectedEdge && (
          <div>
            <div style={{ marginBottom: 16 }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  marginBottom: 12,
                  padding: '12px',
                  background: '#fff7e6',
                  borderRadius: 6,
                  border: '1px solid #ffd591',
                }}
              >
                <LinkOutlined style={{ fontSize: 20, color: '#fa8c16', marginRight: 12 }} />
                <div style={{ flex: 1 }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 4 }}>
                    关系描述
                  </Text>
                  <Text style={{ fontSize: 13, color: '#595959' }}>
                    {selectedEdge.label || '暂无描述'}
                  </Text>
                </div>
              </div>

              <Descriptions column={1} bordered size="small">
                <Descriptions.Item label="关系强度">
                  <Tag color="orange" style={{ fontSize: 14, padding: '2px 10px' }}>
                    {selectedEdge.weight.toFixed(1)}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="起始节点">
                  {selectedEdge.fromNode ? (
                    <Space>
                      <Tag color={selectedEdge.fromNode.color} style={{ fontSize: 13 }}>
                        {selectedEdge.fromNode.layer || selectedEdge.fromNode.type}
                      </Tag>
                      <Text strong>{selectedEdge.fromNode.label}</Text>
                    </Space>
                  ) : (
                    <Text>{selectedEdge.from}</Text>
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="目标节点">
                  {selectedEdge.toNode ? (
                    <Space>
                      <Tag color={selectedEdge.toNode.color} style={{ fontSize: 13 }}>
                        {selectedEdge.toNode.layer || selectedEdge.toNode.type}
                      </Tag>
                      <Text strong>{selectedEdge.toNode.label}</Text>
                    </Space>
                  ) : (
                    <Text>{selectedEdge.to}</Text>
                  )}
                </Descriptions.Item>
              </Descriptions>
            </div>
          </div>
        )}

        {/* Node detail */}
        {drawerType === 'node' && selectedNode && (
          <div>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="节点名称">
                <Text strong>{selectedNode.label}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="节点类型">
                <Tag
                  color={selectedNode.color}
                  style={{ fontSize: 14, padding: '2px 10px' }}
                >
                  {selectedNode.layer} · {selectedNode.type}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="描述">
                <Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
                  {selectedNode.description || '暂无描述'}
                </Paragraph>
              </Descriptions.Item>
            </Descriptions>
            <Button
              type="primary"
              icon={<NodeIndexOutlined />}
              block
              style={{ marginTop: 16 }}
              onClick={() => handleExploreEntity(selectedNode.id, selectedNode.label)}
            >
              展开此节点的关系
            </Button>
          </div>
        )}
      </Drawer>
    </div>
  );
}

const statRowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  padding: '4px 8px',
  background: '#fafafa',
  borderRadius: 4,
};

function layerColor(layer: string): string {
  if (layer === 'L1') return '#2f6fed';
  if (layer === 'L2') return '#00b578';
  if (layer === 'L3') return '#fa8c16';
  return 'blue';
}
