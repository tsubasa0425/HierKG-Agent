import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Input,
  Layout,
  List,
  Space,
  Tag,
  Typography,
} from 'antd';
import {
  SendOutlined,
  StopOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  HistoryOutlined,
  CommentOutlined,
  PlusOutlined,
  DeleteOutlined,
} from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  agentChatStream,
  createSession,
  deleteSession,
  getSessionMessages,
  listSessions,
  type SessionInfo,
} from '../services/api';
import { useChatStore, type ChatTurn, type ToolStep } from '../stores/chatStore';
import type { TextAreaRef } from 'antd/es/input/TextArea';

const { TextArea } = Input;
const { Text, Title } = Typography;

const LS_KEY = 'hierkg_session_id';

// 证据引用徽章：把答案里的 [ev_1.1.1] / [ev_1.4#2] 内部编码转成可读的章节徽章。
// ev_ 是图谱证据 ID 前缀，1.1.1 即章节号；用户只看章节号，看不懂内部编码。
// 转成 markdown 内联代码 `§1.1.1`，再由下方 code 组件渲染为 .ev-ref 徽章样式。
const CITE_RE = /\[ev_([0-9.]+)(?:#[0-9]+)?\]/g;
function formatAnswer(text: string): string {
  return text.replace(CITE_RE, (_m, sec: string) => '`§' + sec + '`');
}
const { Sider, Content } = Layout;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function formatSummary(data: unknown): string {
  if (typeof data === 'string') return data;
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

function formatArgs(args: Record<string, unknown>): string {
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

function StepIcon({ success }: { success: boolean | null }) {
  if (success === true) return <CheckCircleOutlined style={{ color: '#52c41a' }} />;
  if (success === false) return <CloseCircleOutlined style={{ color: '#ff4d4f' }} />;
  return <LoadingOutlined style={{ color: '#1677ff' }} />;
}

// ---------------------------------------------------------------------------
// single turn: question bubble + tool timeline + streaming answer
// ---------------------------------------------------------------------------

function stepState(s: ToolStep): string {
  if (s.success === true) return 'is-done';
  if (s.success === false) return 'is-error';
  return 'is-running';
}

function cacheBadge(hit: string | undefined) {
  if (!hit || hit === 'none') return null;
  return (
    <Tag color={hit === 'answer' ? 'gold' : 'blue'} style={{ marginInlineEnd: 8 }}>
      ⚡ {hit === 'answer' ? '命中缓存·答案直出' : '命中缓存·证据回放'}
    </Tag>
  );
}

function TurnBlock({ turn }: { turn: ChatTurn }) {
  const finished = turn.status === 'done';
  const isError = turn.status === 'error';
  const connecting = turn.status === 'streaming' && !turn.thinking && turn.steps.length === 0 && !turn.answer;

  // 实时状态条：此刻 Agent 正在做什么（让等待过程"看得到"）
  const liveAction = (() => {
    if (turn.status !== 'streaming') return null;
    if (turn.thinking) return `🧠 ${turn.thinking.message}`;
    if (turn.steps.some((s) => s.success === null))
      return `🛠 正在执行工具 ${turn.steps.find((s) => s.success === null)!.name} ...`;
    if (connecting) return turn.statusMessage || '🤖 正在连接模型...';
    if (turn.answer) return '✍ 正在生成回答...';
    return null;
  })();

  // 把 推理轮次 + 工具步骤 按 round 穿插成完整时间线：think(1)→tools(1)→think(2)→tools(2)→…
  const seq = (() => {
    const thinkByRound = new Map((turn.thinkingLog ?? []).map((e) => [e.round, e.message]));
    const toolsByRound = new Map<number, ToolStep[]>();
    for (const s of turn.steps) {
      const r = s.round ?? 1;
      if (!toolsByRound.has(r)) toolsByRound.set(r, []);
      toolsByRound.get(r)!.push(s);
    }
    const rounds = Array.from(new Set([...thinkByRound.keys(), ...toolsByRound.keys()])).sort((a, b) => a - b);
    const items: { kind: 'think' | 'tool'; round: number; message?: string; step?: ToolStep }[] = [];
    for (const r of rounds) {
      const msg = thinkByRound.get(r);
      if (msg != null) items.push({ kind: 'think', round: r, message: msg });
      for (const s of toolsByRound.get(r) ?? []) items.push({ kind: 'tool', round: r, step: s });
    }
    return items;
  })();

  const showPanel = seq.length > 0 || turn.answer || connecting || finished || isError;

  return (
    <div className="turn-block">
      {/* user question */}
      <div className="turn-question">{turn.question}</div>

      {/* Agent 实时动作面板（过程：浅灰底、小号字，与答案明确区分） */}
      {showPanel && (
        <div className="agent-panel">
          <div className="agent-panel-header">
            <span>🤖 Agent 动作过程</span>
            {liveAction && (
              <span className="agent-live">
                <span className="agent-live-dot" />
                {liveAction}
              </span>
            )}
          </div>
          <div className="agent-steps">
            {seq.map((it) =>
              it.kind === 'think' ? (
                <div
                  key={`t${it.round}`}
                  className={`agent-step ${turn.thinking && turn.thinking.round === it.round ? 'is-running' : 'is-done'}`}
                >
                  <div className="agent-step-head">
                    {turn.thinking && turn.thinking.round === it.round ? (
                      <LoadingOutlined spin style={{ color: '#1677ff' }} />
                    ) : (
                      <span className="agent-step-no">🧠</span>
                    )}
                    <Text type="secondary" style={{ fontSize: 12 }}>{it.message}</Text>
                  </div>
                </div>
              ) : (
                <div key={it.step!.id} className={`agent-step ${stepState(it.step!)}`}>
                  <div className="agent-step-head">
                    <span className="agent-step-no">{it.round}</span>
                    <Tag color={it.step!.success === false ? 'red' : 'blue'} style={{ marginInlineEnd: 0 }}>
                      {it.step!.name}
                    </Tag>
                    <StepIcon success={it.step!.success} />
                    {it.step!.thinkingMs != null && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        🤔 推理 {(it.step!.thinkingMs / 1000).toFixed(1)}s
                      </Text>
                    )}
                    {it.step!.elapsedMs != null && (
                      <Text type="secondary" style={{ fontSize: 11 }}>⚡ {it.step!.elapsedMs}ms</Text>
                    )}
                    {it.step!.success === false && it.step!.message && (
                      <Text type="secondary" style={{ fontSize: 11 }}>{it.step!.message}</Text>
                    )}
                  </div>
                  {it.step!.success === null ? (
                    <div className="agent-step-result">
                      <pre>执行中...</pre>
                    </div>
                  ) : it.step!.dataSummary !== undefined ? (
                    <div className="agent-step-result">
                      <pre>{formatSummary(it.step!.dataSummary)}</pre>
                    </div>
                  ) : null}
                  <details className="agent-step-detail">
                    <summary>查看参数</summary>
                    <pre>{formatArgs(it.step!.arguments)}</pre>
                  </details>
                </div>
              ),
            )}
          </div>
        </div>
      )}

      {/* 最终答案（大号正文 + 蓝色标题，与过程视觉区分） */}
      {(turn.answer || finished || isError) && (
        <div className="answer-block">
          <div className="answer-header">📝 最终回答</div>
          {turn.answer ? (
            <div className="answer-markdown markdown-body">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  code({ className, children, ...props }) {
                    const text = String(children ?? '');
                    // `§1.1.1` 内联代码（由 formatAnswer 生成）→ 渲染为来源徽章
                    if (!className && text.startsWith('§')) {
                      return (
                        <span className="ev-ref" title="证据来源章节">
                          {text}
                        </span>
                      );
                    }
                    return (
                      <code className={className} {...props}>
                        {children}
                      </code>
                    );
                  },
                }}
              >
                {formatAnswer(turn.answer)}
              </ReactMarkdown>
            </div>
          ) : null}
          {isError && (
            <Alert type="error" showIcon message={turn.error || '出错了，请重试'} style={{ marginTop: 8 }} />
          )}
          {finished && (
            <div className="answer-meta">
              {cacheBadge(turn.cacheHit)}
              检索 {turn.toolRounds ?? 0} 轮 · {turn.toolCalls ?? 0} 次工具调用 ·{' '}
              {((turn.elapsedMs ?? 0) / 1000).toFixed(1)}s
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

export default function AgentChat() {
  const turns = useChatStore((s) => s.turns);
  const streaming = useChatStore((s) => s.streaming);
  const sessionId = useChatStore((s) => s.sessionId);
  const sessions = useChatStore((s) => s.sessions);
  const startTurn = useChatStore((s) => s.startTurn);
  const setStatus = useChatStore((s) => s.setStatus);
  const setThinking = useChatStore((s) => s.setThinking);
  const addToolCall = useChatStore((s) => s.addToolCall);
  const setToolResult = useChatStore((s) => s.setToolResult);
  const appendChunk = useChatStore((s) => s.appendChunk);
  const finishTurn = useChatStore((s) => s.finishTurn);
  const failTurn = useChatStore((s) => s.failTurn);
  const setSessionId = useChatStore((s) => s.setSessionId);
  const setSessions = useChatStore((s) => s.setSessions);
  const loadSession = useChatStore((s) => s.loadSession);
  const newSession = useChatStore((s) => s.newSession);

  const [input, setInput] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<TextAreaRef | null>(null);

  // auto-scroll to bottom on new content
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [
    turns,
    turns[turns.length - 1]?.answer,
    turns[turns.length - 1]?.steps.length,
    turns[turns.length - 1]?.thinkingLog.length,
  ]);

  const refreshSessions = () => {
    listSessions().then(setSessions).catch(() => {});
  };

  // 初始化：加载会话列表 + 恢复 localStorage 里的当前会话
  useEffect(() => {
    refreshSessions();
    const saved = localStorage.getItem(LS_KEY);
    if (saved) {
      getSessionMessages(saved)
        .then((msgs) => {
          setSessionId(saved);
          loadSession(saved, msgs);
        })
        .catch(() => {
          // 会话已被删/失效 → 重置为新会话
          localStorage.removeItem(LS_KEY);
          newSession();
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const streamWith = (sid: string, q: string) => {
    abortRef.current = agentChatStream(q, sid, {
      onSession: (actual) => {
        if (actual !== sid) {
          setSessionId(actual);
          localStorage.setItem(LS_KEY, actual);
        }
      },
      onStatus: (status, message, round) => {
        if (status === 'thinking') {
          setThinking(round ?? 1, message);
        } else {
          setStatus(status, message);
        }
      },
      onToolCall: (ev) => addToolCall(ev),
      onToolResult: (ev) => setToolResult(ev),
      onChunk: (text) => appendChunk(text),
      onDone: (ev) => {
        finishTurn(ev.answer, {
          toolRounds: ev.tool_rounds,
          toolCalls: ev.tool_calls,
          elapsedMs: ev.elapsed_ms,
          cacheHit: ev.cache_hit,
        });
        refreshSessions();
      },
      onError: (message) => failTurn(message),
    });
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const handleSend = () => {
    if (streaming) return;
    const q = input.trim();
    if (!q) return;

    setInput('');
    startTurn(q);

    if (sessionId) {
      streamWith(sessionId, q);
      return;
    }
    // 无会话 → 先创建，拿到 id 再流式
    createSession('', q.slice(0, 30))
      .then((res) => {
        const sid = res.session_id;
        setSessionId(sid);
        localStorage.setItem(LS_KEY, sid);
        streamWith(sid, q);
      })
      .catch(() => failTurn('创建会话失败，请重试'));
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    failTurn('已取消');
  };

  const handleNewChat = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    localStorage.removeItem(LS_KEY);
    newSession();
    refreshSessions();
  };

  const handleSelectSession = async (sid: string) => {
    if (streaming) return; // 正在回答时不切换
    abortRef.current?.abort();
    abortRef.current = null;
    try {
      const msgs = await getSessionMessages(sid);
      setSessionId(sid);
      localStorage.setItem(LS_KEY, sid);
      loadSession(sid, msgs);
    } catch {
      failTurn('加载会话失败');
    }
  };

  const handleDeleteSession = async (sid: string) => {
    try {
      await deleteSession(sid);
      setSessions(sessions.filter((s: SessionInfo) => s.id !== sid));
      if (sid === sessionId) handleNewChat();
    } catch {
      // 静默：删除失败不打断
    }
  };

  return (
    <div style={{ margin: -24, height: 'calc(100vh - 64px)' }}>
      <Layout style={{ height: '100%' }}>
        {/* ── 主区：消息流 + 输入 ─────────────────────────── */}
        <Content style={{ position: 'relative', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          {/* messages */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '24px 28px' }}>
            {turns.length === 0 ? (
              <Empty
                description={
                  <Space direction="vertical" size={8} style={{ alignItems: 'center' }}>
                    <CommentOutlined style={{ fontSize: 48, color: '#bbb' }} />
                    <Text style={{ color: '#888' }}>
                      与 HierKG 知识图谱 Agent 对话 —— 它会自主调用检索工具，引用图谱证据回答
                    </Text>
                    <Text type="secondary" style={{ fontSize: 13 }}>
                      试试：「强化学习和学习型智能体有什么关系？有哪些实例？」
                    </Text>
                  </Space>
                }
                style={{ marginTop: 80 }}
              />
            ) : (
              turns.map((t) => <TurnBlock key={t.id} turn={t} />)
            )}
            <div ref={bottomRef} />
          </div>

          {/* input */}
          <div style={{ borderTop: '1px solid #f0f0f0', padding: '12px 16px', background: '#fff' }}>
            <TextArea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPressEnter={(e) => {
                if (e.shiftKey) return;
                e.preventDefault();
                handleSend();
              }}
              placeholder="输入问题，Enter 发送，Shift+Enter 换行"
              autoSize={{ minRows: 2, maxRows: 6 }}
              disabled={streaming}
            />
            <div style={{ marginTop: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {streaming ? 'Agent 正在处理，可随时停止...' : '支持多轮追问，回答会标注引用来源章节'}
              </Text>
              <Space>
                {streaming ? (
                  <Button danger icon={<StopOutlined />} onClick={handleCancel}>
                    停止
                  </Button>
                ) : (
                  <Button
                    type="primary"
                    icon={<SendOutlined />}
                    onClick={handleSend}
                    disabled={!input.trim()}
                  >
                    发送
                  </Button>
                )}
              </Space>
            </div>
          </div>
        </Content>

        {/* ── 右侧：会话列表 ─────────────────────────────── */}
        <Sider
          width={260}
          theme="light"
          style={{
            padding: '16px',
            overflowY: 'auto',
            borderLeft: '1px solid #f0f0f0',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <Title level={5} style={{ marginBottom: 0 }}>
              <HistoryOutlined style={{ marginRight: 8 }} />
              会话
            </Title>
            <Button size="small" type="text" icon={<PlusOutlined />} onClick={handleNewChat}>
              新建
            </Button>
          </div>
          <List
            size="small"
            dataSource={sessions}
            locale={{ emptyText: '暂无会话' }}
            renderItem={(s) => (
              <List.Item
                key={s.id}
                onClick={() => handleSelectSession(s.id)}
                style={{
                  padding: '8px 10px',
                  cursor: 'pointer',
                  borderRadius: 6,
                  background: s.id === sessionId ? '#e6f4ff' : undefined,
                }}
                actions={[
                  <Button
                    key="del"
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDeleteSession(s.id);
                    }}
                  />,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Text strong ellipsis style={{ fontSize: 13, maxWidth: 170 }} title={s.title}>
                      {s.title || '未命名会话'}
                    </Text>
                  }
                  description={
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {s.updated_at.replace('T', ' ').slice(0, 16)}
                    </Text>
                  }
                />
              </List.Item>
            )}
          />
        </Sider>
      </Layout>
    </div>
  );
}
