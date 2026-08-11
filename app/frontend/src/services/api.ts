import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
});

// ---------------------------------------------------------------------------
// 图谱可视化
// ---------------------------------------------------------------------------

export interface GraphNode {
  id: string;
  label: string;
  type: string; // concept / entity / evidence
  layer: string; // L1 / L2 / L3
  description: string;
  color: string;
  size: number;
}

export interface GraphEdge {
  from: string;
  to: string;
  label: string;
  weight: number;
}

export interface GraphDataResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphStatsResponse {
  total_nodes: number;
  total_edges: number;
  layers: { concept: number; entity: number; evidence: number };
}

export interface GraphSearchResult {
  node_id: string;
  name: string;
  layer: string;
  description: string;
}

// 全图分层采样（types = concept/entity/evidence 逗号分隔）
export const getGraphData = (types?: string[], limit?: number): Promise<GraphDataResponse> =>
  api.get('/graph', { params: { types: types?.join(','), limit: limit ?? 200 } }).then(r => r.data);

export const getGraphStats = (): Promise<GraphStatsResponse> =>
  api.get('/graph/stats').then(r => r.data);

// N 跳邻域子图（双击展开）
export const getEntityNeighborhood = (entityId: string, depth = 2): Promise<GraphDataResponse> =>
  api.get('/graph/neighborhood', { params: { entity: entityId, depth } }).then(r => r.data);

// 名称/别名模糊搜节点
export const searchEntities = (query: string, limit = 20): Promise<GraphSearchResult[]> =>
  api.get('/graph/search', { params: { q: query, limit } }).then(r => r.data.results);

// ---------------------------------------------------------------------------
// Agent 对话（SSE 流式）
// ---------------------------------------------------------------------------

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ToolCallEvent {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  thinking_ms?: number; // 本轮 LLM 推理耗时（ReAct 过程可视化）
}

export interface ToolResultEvent {
  id: string;
  name: string;
  tool_name: string;
  success: boolean;
  message: string;
  elapsed_ms: number;
  data: unknown;
}

export interface DoneEvent {
  answer: string;
  tool_rounds: number;
  tool_calls: number;
  elapsed_ms: number;
}

export interface AgentStreamCallbacks {
  onStatus: (status: string, message: string, round?: number) => void;
  onToolCall: (ev: ToolCallEvent) => void;
  onToolResult: (ev: ToolResultEvent) => void;
  onChunk: (text: string) => void;
  onDone: (ev: DoneEvent) => void;
  onError: (message: string) => void;
}

/**
 * 发一条 user 消息给 Agent，SSE 解析事件流。
 * 事件协议：status / tool_call / tool_result / chunk / done / error
 * 返回 AbortController 用于中途取消。
 */
export const agentChatStream = (
  messages: ChatMessage[],
  callbacks: AgentStreamCallbacks,
): AbortController => {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages }),
        signal: controller.signal,
      });

      if (!response.ok) {
        callbacks.onError(`HTTP ${response.status}: ${response.statusText}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        callbacks.onError('无法读取响应流');
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() || '';

        for (const eventBlock of events) {
          if (!eventBlock.trim()) continue;

          let eventType = '';
          let eventData = '';

          for (const line of eventBlock.split('\n')) {
            if (line.startsWith('event: ')) eventType = line.slice(7);
            else if (line.startsWith('data: ')) eventData = line.slice(6);
          }

          if (!eventData) continue;

          try {
            const data = JSON.parse(eventData);
            switch (eventType) {
              case 'status':
                callbacks.onStatus(data.status, data.message, data.round);
                break;
              case 'tool_call':
                callbacks.onToolCall(data);
                break;
              case 'tool_result':
                callbacks.onToolResult(data);
                break;
              case 'chunk':
                callbacks.onChunk(data.text);
                break;
              case 'done':
                callbacks.onDone(data);
                break;
              case 'error':
                callbacks.onError(data.message);
                break;
            }
          } catch {
            // skip malformed events
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      callbacks.onError('网络连接失败，请稍后重试');
    }
  })();

  return controller;
};

export default api;
