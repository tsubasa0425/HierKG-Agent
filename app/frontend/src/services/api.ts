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
// 会话管理
// ---------------------------------------------------------------------------

export interface SessionInfo {
  id: string;
  user_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface StoredMessage {
  id: number;
  session_id: string;
  role: 'user' | 'assistant';
  content: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export const createSession = (userId = '', title = ''): Promise<{ session_id: string }> =>
  api.post('/sessions', { user_id: userId, title }).then((r) => r.data);

export const listSessions = (userId = ''): Promise<SessionInfo[]> =>
  api.get('/sessions', { params: { user_id: userId } }).then((r) => r.data.sessions);

export const getSessionMessages = (sessionId: string): Promise<StoredMessage[]> =>
  api.get(`/sessions/${sessionId}/messages`).then((r) => r.data.messages);

export const deleteSession = (sessionId: string): Promise<{ deleted: boolean }> =>
  api.delete(`/sessions/${sessionId}`).then((r) => r.data);

// ---------------------------------------------------------------------------
// Agent 对话（SSE 流式，原生事件直通）
// ---------------------------------------------------------------------------

/** 后端每个 SSE 帧：原生 AgentScope 事件 或 应用层帧（session/TURN_DONE/ERROR）。
 *  顶层 type 分派（REPLY_START / TEXT_BLOCK_DELTA / TOOL_CALL_START / ...）。 */
export interface AgentFrame {
  type: string;
  // 原生事件：reply_id / block_id / tool_call_id / delta / finished_reason …
  // 应用帧：  session → {session_id}
  //           TURN_DONE → {data: TurnDoneData}
  //           ERROR    → {data: {message}}
  [k: string]: unknown;
}

/** 一次回复的 LLM 用量（stream_reply 在 MODEL_CALL_END 上聚合；L1 直出为全 0）。 */
export interface UsageInfo {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_input_tokens: number;
  cache_creation_tokens: number;
}

/** TURN_DONE 权威收尾：answer + 检索统计（chat.py finally 据此持久化）。 */
export interface TurnDoneData {
  answer: string;
  tool_rounds: number;
  tool_calls: number;
  elapsed_ms: number;
  /** 热缓存命中层级：none / evidence（证据回放） / answer（答案直出） */
  cache_hit?: string;
  evidence_ids?: string[];
  node_ids?: string[];
  /** LLM 用量（无模型调用时为 undefined / 全 0） */
  usage?: UsageInfo;
}

export interface AgentStreamCallbacks {
  /** 流开始时后端回传实际使用的 session_id（处理前端陈旧 id 被重建的场景） */
  onSession: (sessionId: string) => void;
  /** 逐帧投递（type 由调用方在 store 里分派）。session 帧不投这里，走 onSession。 */
  onFrame: (frame: AgentFrame) => void;
  /** 出错（ERROR 帧 / 连接失败 / stall 超时），message 可直接展示 */
  onError: (message: string) => void;
}

/**
 * 发一条 user 消息给 Agent，SSE 解析原生事件帧（会话式：服务端按 session_id 拼历史）。
 * 帧协议：event 名统一 message，前端只按 data.type 分派；另有 session/TURN_DONE/ERROR 应用帧。
 * 返回 AbortController 用于中途取消。
 *
 * 健壮性：连接中断/长时间无事件/流自然结束但没收到 TURN_DONE 时，
 * 都会回调 onError 明确提示，避免 UI 永远停在"正在连接模型"。
 */
export const agentChatStream = (
  message: string,
  sessionId: string | null,
  callbacks: AgentStreamCallbacks,
): AbortController => {
  const controller = new AbortController();
  const STALL_MS = 60_000; // 60s 无任何事件 → 判定连接中断
  let receivedDone = false;
  let ended = false; // 已报错/已完成，防止重复回调

  const error = (msg: string) => {
    if (!ended) {
      ended = true;
      callbacks.onError(msg);
    }
  };

  const dispatch = (data: AgentFrame) => {
    if (data.type === 'session') {
      callbacks.onSession(data.session_id as string);
      return;
    }
    if (data.type === 'ERROR') {
      const inner = data.data as { message?: string } | undefined;
      error(inner?.message || '出错了，请重试');
      return;
    }
    if (data.type === 'TURN_DONE') {
      receivedDone = true;
    }
    callbacks.onFrame(data);
  };

  (async () => {
    let lastEventAt = Date.now();
    // 心跳：长时间无事件则中止，防止无限挂起
    const stallTimer = setInterval(() => {
      if (Date.now() - lastEventAt > STALL_MS && !receivedDone) {
        clearInterval(stallTimer);
        controller.abort();
        error('长时间无响应，连接已中断，请重试');
      }
    }, 5000);

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, session_id: sessionId }),
        signal: controller.signal,
      });

      if (!response.ok) {
        error(`HTTP ${response.status}: ${response.statusText}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        error('无法读取响应流');
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

          let eventData = '';
          for (const line of eventBlock.split('\n')) {
            if (line.startsWith('data: ')) eventData = line.slice(6);
          }
          if (!eventData) continue;

          try {
            const data = JSON.parse(eventData);
            lastEventAt = Date.now();
            dispatch(data);
          } catch {
            // skip malformed events
          }
        }
      }

      // 流自然结束但没有 TURN_DONE → 说明中途被掐断
      if (!receivedDone && !ended) error('连接中断，未收到完整回答，请重试');
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') return; // 主动取消 / stall 超时已报错
      error('网络连接失败，请稍后重试');
    } finally {
      clearInterval(stallTimer);
    }
  })();

  return controller;
};

export default api;
