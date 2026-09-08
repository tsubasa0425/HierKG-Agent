import { create } from 'zustand';
import type {
  AgentFrame,
  SessionInfo,
  StoredMessage,
  TurnDoneData,
  UsageInfo,
} from '../services/api';

/** 一段 Agent 推理（原生 THINKING_BLOCK_*，用 block_id 归组累积）。 */
export interface ThinkingBlock {
  id: string;
  order: number;
  text: string;
  running: boolean;
}

/** 一次工具调用（原生 TOOL_CALL_* → TOOL_RESULT_*，用 tool_call_id 配对成 step）。 */
export interface ToolStep {
  id: string;
  order: number;
  name: string;
  /** TOOL_CALL_DELTA 拼出的 arguments JSON（未闭合时是片段） */
  args: string;
  /** TOOL_RESULT_TEXT_DELTA 拼出的结果文本（工具 payload JSON） */
  result: string;
  success: boolean | null; // null = 尚未返回结果
  running: boolean; // 结果尚未返回
}

export type Phase = 'connecting' | 'thinking' | 'tool' | 'answer' | 'idle';

export interface ChatTurn {
  id: number;
  question: string;
  thinking: ThinkingBlock[];
  steps: ToolStep[];
  answer: string;
  status: 'streaming' | 'done' | 'error';
  /** 实时阶段（供顶部 live 文案），由原生事件推进 */
  phase: Phase;
  error?: string;
  toolRounds?: number;
  toolCalls?: number;
  elapsedMs?: number;
  /** 热缓存命中层级（none/evidence/answer），答案 meta 徽章 */
  cacheHit?: string;
  /** LLM 用量（calls/tokens；L1 直出为全 0，历史会话从 meta 恢复） */
  usage?: UsageInfo;
}

interface ChatStore {
  sessionId: string | null;
  sessions: SessionInfo[];
  turns: ChatTurn[];
  current: ChatTurn | null;
  streaming: boolean;
  setSessionId: (sid: string | null) => void;
  setSessions: (list: SessionInfo[]) => void;
  /** 从服务端历史重建当前会话的 turns（工具过程不持久化，只重建 问题/答案/meta） */
  loadSession: (sid: string, messages: StoredMessage[]) => void;
  /** 新建会话：清空本地状态，下次发送时服务端创建新 session */
  newSession: () => void;
  startTurn: (question: string) => void;
  /** 消费一帧原生/应用层事件（streaming 中的唯一状态入口） */
  handleFrame: (frame: AgentFrame) => void;
  finishTurn: (data: TurnDoneData) => void;
  failTurn: (message: string) => void;
  setStreaming: (streaming: boolean) => void;
  clear: () => void;
}

/** 同步 current 与 turns 数组：组件订阅 turns 渲染，必须两边同写引用才触发重渲。 */
const patchCurrent = (state: ChatStore, fn: (t: ChatTurn) => ChatTurn) => {
  if (!state.current) return {};
  const updated = fn(state.current);
  return {
    current: updated,
    turns: state.turns.map((t) => (t.id === updated.id ? updated : t)),
  };
};

/** 当前 turn 下一个活动项序号（保证 thinking/tool 可按真实时序穿插渲染） */
const nextOrder = (t: ChatTurn): number =>
  Math.max(0, ...t.thinking.map((b) => b.order), ...t.steps.map((s) => s.order)) + 1;

export const useChatStore = create<ChatStore>((set) => ({
  sessionId: null,
  sessions: [],
  turns: [],
  current: null,
  streaming: false,

  setSessionId: (sid) => set({ sessionId: sid }),
  setSessions: (list) => set({ sessions: list }),

  loadSession: (sid, messages) =>
    set(() => {
      const turns: ChatTurn[] = [];
      let id = 0;
      let pendingQ = '';
      for (const m of messages) {
        if (m.role === 'user') {
          pendingQ = m.content;
          continue;
        }
        if (m.role === 'assistant' && pendingQ) {
          turns.push({
            id: id++,
            question: pendingQ,
            thinking: [],
            steps: [],
            answer: m.content,
            status: 'done',
            phase: 'idle',
            toolRounds: (m.meta?.tool_rounds as number) ?? 0,
            toolCalls: (m.meta?.tool_calls as number) ?? 0,
            elapsedMs: (m.meta?.elapsed_ms as number) ?? 0,
            cacheHit: (m.meta?.cache_hit as string) || undefined,
            usage: (m.meta?.usage as UsageInfo) || undefined,
          });
          pendingQ = '';
        }
      }
      // 末尾还有未配对的 user 消息（中断/未答完）
      if (pendingQ) {
        turns.push({
          id: id++,
          question: pendingQ,
          thinking: [],
          steps: [],
          answer: '',
          status: 'error',
          phase: 'idle',
          error: '该消息当时未收到回答',
        });
      }
      return { sessionId: sid, turns, current: null, streaming: false };
    }),

  newSession: () => set({ sessionId: null, turns: [], current: null, streaming: false }),

  startTurn: (question) => {
    const id = Date.now();
    const turn: ChatTurn = {
      id,
      question,
      thinking: [],
      steps: [],
      answer: '',
      status: 'streaming',
      phase: 'connecting',
    };
    set((state) => ({ turns: [...state.turns, turn], current: turn, streaming: true }));
  },

  // ---------------------------------------------------------------------
  // 原生事件帧 → turn 状态（归组：thinking 用 block_id、tool 用 tool_call_id）
  // ---------------------------------------------------------------------
  handleFrame: (frame) =>
    set((state) => {
      const type = frame.type;
      const t = state.current;
      if (!t || t.status !== 'streaming') return {};

      if (type === 'THINKING_BLOCK_START') {
        const id = frame.block_id as string;
        if (t.thinking.some((b) => b.id === id)) return {};
        return patchCurrent(state, (cur) => ({
          ...cur,
          phase: 'thinking',
          thinking: [
            ...cur.thinking,
            { id, order: nextOrder(cur), text: '', running: true },
          ],
        }));
      }
      if (type === 'THINKING_BLOCK_DELTA') {
        const id = frame.block_id as string;
        const delta = (frame.delta as string) || '';
        return patchCurrent(state, (cur) => ({
          ...cur,
          thinking: cur.thinking.map((b) =>
            b.id === id ? { ...b, text: b.text + delta } : b),
        }));
      }
      if (type === 'THINKING_BLOCK_END') {
        const id = frame.block_id as string;
        return patchCurrent(state, (cur) => ({
          ...cur,
          thinking: cur.thinking.map((b) =>
            b.id === id ? { ...b, running: false } : b),
        }));
      }

      if (type === 'TOOL_CALL_START') {
        const id = frame.tool_call_id as string;
        if (t.steps.some((s) => s.id === id)) return {};
        return patchCurrent(state, (cur) => ({
          ...cur,
          phase: 'tool',
          steps: [
            ...cur.steps,
            {
              id,
              order: nextOrder(cur),
              name: (frame.tool_call_name as string) || '',
              args: '',
              result: '',
              success: null,
              running: true,
            },
          ],
        }));
      }
      if (type === 'TOOL_CALL_DELTA') {
        const id = frame.tool_call_id as string;
        const delta = (frame.delta as string) || '';
        return patchCurrent(state, (cur) => ({
          ...cur,
          steps: cur.steps.map((s) =>
            s.id === id ? { ...s, args: s.args + delta } : s),
        }));
      }

      if (type === 'TOOL_RESULT_START') {
        const id = frame.tool_call_id as string;
        const name = (frame.tool_call_name as string) || '';
        return patchCurrent(state, (cur) => ({
          ...cur,
          steps: cur.steps.map((s) =>
            s.id === id ? { ...s, name: s.name || name } : s),
        }));
      }
      if (type === 'TOOL_RESULT_TEXT_DELTA') {
        const id = frame.tool_call_id as string;
        const delta = (frame.delta as string) || '';
        return patchCurrent(state, (cur) => ({
          ...cur,
          steps: cur.steps.map((s) =>
            s.id === id ? { ...s, result: s.result + delta } : s),
        }));
      }
      if (type === 'TOOL_RESULT_END') {
        const id = frame.tool_call_id as string;
        const success = frame.state === 'success';
        return patchCurrent(state, (cur) => ({
          ...cur,
          steps: cur.steps.map((s) =>
            s.id === id ? { ...s, success, running: false } : s),
        }));
      }

      if (type === 'TEXT_BLOCK_START') {
        return patchCurrent(state, (cur) => ({ ...cur, phase: 'answer' }));
      }
      if (type === 'TEXT_BLOCK_DELTA') {
        const delta = (frame.delta as string) || '';
        return patchCurrent(state, (cur) => ({ ...cur, answer: cur.answer + delta }));
      }

      if (type === 'MODEL_CALL_START') {
        // 模型开始生成：默认视作思考（随后 TEXT/TOOL 事件会纠正阶段）
        return patchCurrent(state, (cur) =>
          cur.phase === 'answer' ? cur : { ...cur, phase: 'thinking' });
      }
      if (type === 'REPLY_START' && frame.session_id) {
        return patchCurrent(state, (cur) => ({ ...cur, phase: 'thinking' }));
      }

      return {}; // REPLY_END / HINT / DATA_* / 其他：无需入状态
    }),

  finishTurn: (data) =>
    set((state) => {
      if (!state.current) return { streaming: false };
      const updated = {
        ...state.current,
        answer: data.answer ?? state.current.answer,
        status: 'done' as const,
        phase: 'idle' as const,
        toolRounds: data.tool_rounds,
        toolCalls: data.tool_calls,
        elapsedMs: data.elapsed_ms,
        cacheHit: data.cache_hit || undefined,
        usage: data.usage || undefined,
      };
      return {
        current: updated,
        turns: state.turns.map((t) => (t.id === updated.id ? updated : t)),
        streaming: false,
      };
    }),

  failTurn: (message) =>
    set((state) => {
      if (!state.current) return { streaming: false };
      const updated = {
        ...state.current,
        status: 'error' as const,
        phase: 'idle' as const,
        error: message,
      };
      return {
        current: updated,
        turns: state.turns.map((t) => (t.id === updated.id ? updated : t)),
        streaming: false,
      };
    }),

  setStreaming: (streaming) => set({ streaming }),

  clear: () => set({ turns: [], current: null, streaming: false }),
}));
