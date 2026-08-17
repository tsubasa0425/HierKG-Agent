import { create } from 'zustand';

export interface ToolStep {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  success: boolean | null; // null = 尚未返回结果
  thinkingMs?: number; // 本轮 LLM 推理耗时
  message?: string;
  elapsedMs?: number;
  dataSummary?: unknown;
  /** 所属检索轮次（由 thinking 事件的 round 推断，用于穿插展示推理→工具） */
  round?: number;
}

export interface ThinkingState {
  round: number;
  message: string;
}

export interface ChatTurn {
  id: number;
  question: string;
  steps: ToolStep[];
  answer: string;
  status: 'streaming' | 'done' | 'error';
  statusMessage?: string;
  /** 当前正在进行的 LLM 推理（ReAct 的 Think 阶段） */
  thinking: ThinkingState | null;
  /** 已发生的推理轮次记录（持久展示 think→tool→result 完整时间线） */
  thinkingLog: ThinkingState[];
  error?: string;
  toolRounds?: number;
  toolCalls?: number;
  elapsedMs?: number;
}

interface ChatStore {
  turns: ChatTurn[];
  current: ChatTurn | null;
  streaming: boolean;
  startTurn: (question: string) => void;
  setStatus: (status: string, message: string) => void;
  setThinking: (round: number, message: string) => void;
  addToolCall: (ev: { id: string; name: string; arguments: Record<string, unknown>; thinking_ms?: number }) => void;
  setToolResult: (ev: {
    id: string;
    name: string;
    success: boolean;
    message: string;
    elapsed_ms: number;
    data: unknown;
  }) => void;
  appendChunk: (text: string) => void;
  finishTurn: (answer: string, meta: { toolRounds: number; toolCalls: number; elapsedMs: number }) => void;
  failTurn: (message: string) => void;
  setStreaming: (streaming: boolean) => void;
  clear: () => void;
}

const patchCurrent = (state: ChatStore, fn: (t: ChatTurn) => ChatTurn) => {
  if (!state.current) return {};
  const updated = fn(state.current);
  // 关键：组件订阅的是 turns（turns.map 渲染），若只改 current，
  // turns 引用不变 → zustand 按引用比较判定状态未变 → 不重渲染。
  // 必须同时把当前 turn 同步回 turns 数组，流式过程才能实时刷新。
  return {
    current: updated,
    turns: state.turns.map((t) => (t.id === updated.id ? updated : t)),
  };
};

export const useChatStore = create<ChatStore>((set) => ({
  turns: [],
  current: null,
  streaming: false,

  startTurn: (question) => {
    const id = Date.now();
    const turn: ChatTurn = {
      id,
      question,
      steps: [],
      answer: '',
      status: 'streaming',
      statusMessage: '正在连接模型...',
      thinking: null,
      thinkingLog: [],
    };
    set((state) => ({ turns: [...state.turns, turn], current: turn, streaming: true }));
  },

  setStatus: (_status, message) =>
    set((state) => patchCurrent(state, (t) => ({ ...t, statusMessage: message }))),

  setThinking: (round, message) =>
    set((state) =>
      patchCurrent(state, (t) => {
        const log = t.thinkingLog ?? []; // 防御：旧模块状态无此字段
        const last = log[log.length - 1];
        const dup = !!last && last.round === round; // 同轮重复事件不去重记录
        return {
          ...t,
          thinking: { round, message },
          thinkingLog: dup ? log : [...log, { round, message }],
        };
      }),
    ),

  addToolCall: (ev) =>
    set((state) =>
      patchCurrent(state, (t) => {
        const log = t.thinkingLog ?? [];
        const round = log[log.length - 1]?.round ?? 1; // 归属当前推理轮次
        return {
          ...t,
          thinking: null, // 推理结束，进入工具执行阶段
          steps: [
            ...t.steps,
            {
              id: ev.id,
              name: ev.name,
              arguments: ev.arguments,
              success: null,
              thinkingMs: ev.thinking_ms,
              round,
            },
          ],
        };
      }),
    ),

  setToolResult: (ev) =>
    set((state) =>
      patchCurrent(state, (t) => ({
        ...t,
        steps: t.steps.map((s) =>
          s.id === ev.id
            ? {
                ...s,
                success: ev.success,
                message: ev.message,
                elapsedMs: ev.elapsed_ms,
                dataSummary: ev.data,
              }
            : s,
        ),
      })),
    ),

  appendChunk: (text) =>
    set((state) => patchCurrent(state, (t) => ({ ...t, answer: t.answer + text }))),

  finishTurn: (answer, meta) =>
    set((state) => ({
      ...patchCurrent(state, (t) => ({
        ...t,
        answer,
        status: 'done',
        statusMessage: undefined,
        toolRounds: meta.toolRounds,
        toolCalls: meta.toolCalls,
        elapsedMs: meta.elapsedMs,
      })),
      streaming: false,
    })),

  failTurn: (message) =>
    set((state) => ({
      ...patchCurrent(state, (t) => ({
        ...t,
        status: 'error',
        error: message,
        statusMessage: undefined,
      })),
      streaming: false,
    })),

  setStreaming: (streaming) => set({ streaming }),

  clear: () => set({ turns: [], current: null, streaming: false }),
}));
