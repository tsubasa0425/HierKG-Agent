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
  deleteSession,
  getSessionMessages,
  listSessions,
  type AgentFrame,
  type SessionInfo,
  type TurnDoneData,
} from '../services/api';
import { useChatStore, type ChatTurn, type Phase, type ToolStep } from '../stores/chatStore';
import type { TextAreaRef } from 'antd/es/input/TextArea';

const { TextArea } = Input;
const { Text, Title } = Typography;

const LS_KEY = 'hierkg_session_id';

// 证据引用徽章：把答案里的 [ev_1.1.1] / [ev_1.4#2] 内部编码转成可读的章节徽章。
const CITE_RE = /\[ev_([0-9.]+)(?:#[0-9]+)?\]/g;
function formatAnswer(text: string): string {
  return text.replace(CITE_RE, (_m, sec: string) => '`§' + sec + '`');
}
const { Sider, Content } = Layout;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function pretty(text: string): string {
  if (!text) return '';
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function StepIcon({ success }: { success: boolean | null }) {
  if (success === true) return <CheckCircleOutlined style={{ color: '#52c41a' }} />;
  if (success === false) return <CloseCircleOutlined style={{ color: '#ff4d4f' }} />;
  return <LoadingOutlined spin style={{ color: '#1677ff' }} />;
}

/** 从工具结果 payload 里抠一行摘要（成功 → elapsed，失败 → message）。 */
function resultLine(step: ToolStep): string {
  try {
    const o = JSON.parse(step.result);
    if (o && typeof o === 'object') {
      if (o.success === false && o.message) return `失败：${o.message}`;
      if (o.elapsed_ms != null) return `⚡ ${Math.round(o.elapsed_ms)}ms`;
    }
  } catch {
    /* result 可能是未闭合文本，忽略 */
  }
  return '';
}

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

function phaseCaption(phase: Phase, runningTool?: ToolStep): string | null {
  switch (phase) {
    case 'connecting':
      return '🤖 正在连接模型...';
    case 'thinking':
      return '🧠 正在思考…';
    case 'tool':
      return runningTool ? `🛠 正在执行 ${runningTool.name} …` : '🛠 工具调用中…';
    case 'answer':
      return '✍ 正在生成回答…';
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// single turn: question + thinking/tool timeline (可折叠) + streaming answer
// ---------------------------------------------------------------------------

type Activity = { kind: 'think'; order: number; blockId: string } | { kind: 'tool'; order: number; step: ToolStep };

function TurnBlock({ turn }: { turn: ChatTurn }) {
  const finished = turn.status === 'done';
  const isError = turn.status === 'error';
  const streaming = turn.status === 'streaming';

  // 思考块与工具步骤按真实到达序穿插成时间线（原生事件无轮号，用自增 order）
  const act: Activity[] = [
    ...turn.thinking.map((b) => ({ kind: 'think' as const, order: b.order, blockId: b.id })),
    ...turn.steps.map((s) => ({ kind: 'tool' as const, order: s.order, step: s })),
  ].sort((a, b) => a.order - b.order);

  const runningTool = turn.steps.find((s) => s.running);
  const live = streaming ? phaseCaption(turn.phase, runningTool) : null;
  const showPanel = streaming || act.length > 0;

  return (
    <div className="turn-block">
      {/* user question */}
      <div className="turn-question">{turn.question}</div>

      {/* Agent 动作面板：思考（可折叠）+ 工具 step，与答案视觉区分 */}
      {showPanel && (
        <div className="agent-panel">
          <div className="agent-panel-header">
            <span>🤖 Agent 动作过程</span>
            {live && (
              <span className="agent-live">
                <span className="agent-live-dot" />
                {live}
              </span>
            )}
          </div>
          <div className="agent-steps">
            {act.map((it) =>
              it.kind === 'think' ? (
                <ThinkingRow key={it.blockId} turn={turn} blockId={it.blockId} />
              ) : (
                <ToolRow key={it.step.id} step={it.step} />
              ),
            )}
          </div>
        </div>
      )}

      {/* 最终回答（大号正文 + 蓝色标题，与过程视觉区分） */}
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

/** 一段推理：运行时展开实时流，结束后折叠成摘要行。 */
function ThinkingRow({ turn, blockId }: { turn: ChatTurn; blockId: string }) {
  const b = turn.thinking.find((x) => x.id === blockId);
  if (!b) return null;
  return (
    <details className={`agent-step think-block ${b.running ? 'is-running' : 'is-done'}`} open={b.running}>
      <summary className="agent-step-head">
        {b.running ? (
          <LoadingOutlined spin style={{ color: '#1677ff' }} />
        ) : (
          <span className="agent-step-no">🧠</span>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {b.running ? '思考中…' : '思考片段'}
        </Text>
        {!b.running && b.text && (
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
            {b.text.slice(0, 60)}
            {b.text.length > 60 ? '…' : ''}
          </Text>
        )}
      </summary>
      {b.text && (
        <div className="agent-step-result">
          <pre className="think-stream">{b.text}</pre>
        </div>
      )}
    </details>
  );
}

function ToolRow({ step }: { step: ToolStep }) {
  const running = step.running;
  return (
    <div className={`agent-step ${stepState(step)}`}>
      <div className="agent-step-head">
        <span className="agent-step-no">{step.order}</span>
        <Tag color={step.success === false ? 'red' : 'blue'} style={{ marginInlineEnd: 0 }}>
          {step.name || '…'}
        </Tag>
        <StepIcon success={step.success} />
        {!running && resultLine(step) && (
          <Text type="secondary" style={{ fontSize: 11 }}>{resultLine(step)}</Text>
        )}
      </div>
      <details className="agent-step-detail" open={running && !!step.args}>
        <summary>{running ? '参数（实时）' : '查看参数'}</summary>
        <pre>{pretty(step.args) || '…'}</pre>
      </details>
      {running ? (
        <div className="agent-step-result">
          <pre>执行中...</pre>
        </div>
      ) : (
        step.result !== '' && (
          <details className="agent-step-detail">
            <summary>查看结果</summary>
            <pre>{pretty(step.result)}</pre>
          </details>
        )
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
  const handleFrame = useChatStore((s) => s.handleFrame);
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
    turns[turns.length - 1]?.thinking.length,
    turns[turns.length - 1]?.steps.length,
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

  const streamWith = (sid: string | null, q: string) => {
    abortRef.current = agentChatStream(q, sid, {
      onSession: (actual) => {
        if (actual !== sid) {
          setSessionId(actual);
          localStorage.setItem(LS_KEY, actual);
        }
      },
      onFrame: (frame: AgentFrame) => {
        if (frame.type === 'TURN_DONE') {
          finishTurn(frame.data as TurnDoneData);
          refreshSessions();
          return;
        }
        handleFrame(frame);
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
    // session_id 为空时由服务端懒建会话，session 帧回传实际 id
    streamWith(sessionId, q);
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
