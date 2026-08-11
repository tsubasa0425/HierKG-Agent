import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Empty,
  Input,
  Layout,
  List,
  Space,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import {
  SendOutlined,
  StopOutlined,
  ClearOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  HistoryOutlined,
  CommentOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { agentChatStream, type ChatMessage } from '../services/api';
import { useChatStore, type ChatTurn } from '../stores/chatStore';
import type { TextAreaRef } from 'antd/es/input/TextArea';

const { TextArea } = Input;
const { Text, Title } = Typography;
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

function TurnBlock({ turn }: { turn: ChatTurn }) {
  const finished = turn.status === 'done';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 28 }}>
      {/* user question */}
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <div
          style={{
            maxWidth: '70%',
            background: '#1677ff',
            color: '#fff',
            borderRadius: 12,
            padding: '10px 14px',
            fontSize: 14,
            whiteSpace: 'pre-wrap',
          }}
        >
          {turn.question}
        </div>
      </div>

      {/* ReAct 过程：🤔 思考 → 🛠 工具执行 → 🤔 思考 → … → ✍ 回答 */}
      {(turn.steps.length > 0 || turn.thinking) && (
        <div style={{ paddingLeft: 8, borderLeft: '3px solid #e8e8e8' }}>
          <Text type="secondary" style={{ fontSize: 12, marginBottom: 6, display: 'block' }}>
            🤖 Agent ReAct 过程（{turn.steps.length} 次工具调用）
          </Text>
          <Timeline
            items={[
              // 每个工具步骤：推理耗时 + 工具执行耗时 + 可展开的参数/结果
              ...turn.steps.map((s) => ({
                color: s.success === false ? 'red' : s.success === true ? 'green' : 'blue',
                dot: <StepIcon success={s.success} />,
                children: (
                  <Collapse
                    size="small"
                    ghost
                    items={[
                      {
                        key: s.id,
                        label: (
                          <Space size={8} wrap>
                            <Tag color={s.success === false ? 'red' : 'blue'} style={{ marginInlineEnd: 0 }}>
                              {s.name}
                            </Tag>
                            {s.thinkingMs != null && (
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                🤔 推理 {(s.thinkingMs / 1000).toFixed(1)}s
                              </Text>
                            )}
                            {s.success === true && s.message && (
                              <Text type="secondary" style={{ fontSize: 12 }}>{s.message}</Text>
                            )}
                            {s.elapsedMs != null && (
                              <Text type="secondary" style={{ fontSize: 12 }}>⚡ {s.elapsedMs}ms</Text>
                            )}
                          </Space>
                        ),
                        children: (
                          <div>
                            <Text type="secondary" style={{ fontSize: 12 }}>参数</Text>
                            <pre style={preStyle}>{formatArgs(s.arguments)}</pre>
                            {s.dataSummary !== undefined && (
                              <>
                                <Text type="secondary" style={{ fontSize: 12 }}>结果摘要</Text>
                                <pre style={preStyle}>{formatSummary(s.dataSummary)}</pre>
                              </>
                            )}
                          </div>
                        ),
                      },
                    ]}
                  />
                ),
              })),
              // 当前正在进行的 LLM 推理（Think 阶段）
              ...(turn.thinking
                ? [
                    {
                      color: 'blue',
                      dot: <LoadingOutlined spin style={{ color: '#1677ff' }} />,
                      children: (
                        <Text type="secondary" style={{ fontSize: 13 }}>
                          {turn.thinking.message}
                        </Text>
                      ),
                    },
                  ]
                : []),
            ]}
          />
        </div>
      )}

      {/* answer */}
      <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
        <div style={{ maxWidth: '92%', width: '100%' }}>
          {/* 连接模型阶段（尚无任何 ReAct 输出）给一个提示 */}
          {turn.status === 'streaming' && !turn.thinking && turn.steps.length === 0 && (
            <Alert
              type="info"
              showIcon
              icon={<RobotOutlined spin />}
              message={turn.statusMessage || 'Agent 正在连接模型...'}
              style={{ marginBottom: 10 }}
            />
          )}
          {turn.answer ? (
            <div style={{ fontSize: 14, lineHeight: 1.7 }} className="markdown-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.answer}</ReactMarkdown>
            </div>
          ) : null}
          {turn.status === 'error' && (
            <Alert
              type="error"
              showIcon
              message={turn.error || '出错了，请重试'}
              style={{ marginTop: 8 }}
            />
          )}
          {finished && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              检索 {turn.toolRounds ?? 0} 轮 · {turn.toolCalls ?? 0} 次工具调用 ·{' '}
              {((turn.elapsedMs ?? 0) / 1000).toFixed(1)}s
            </Text>
          )}
        </div>
      </div>
    </div>
  );
}

const preStyle: React.CSSProperties = {
  background: '#f6f8fa',
  padding: 10,
  borderRadius: 6,
  fontSize: 12,
  overflowX: 'auto',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
  margin: '4px 0 12px',
  maxHeight: 200,
};

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

export default function AgentChat() {
  const turns = useChatStore((s) => s.turns);
  const streaming = useChatStore((s) => s.streaming);
  const startTurn = useChatStore((s) => s.startTurn);
  const setStatus = useChatStore((s) => s.setStatus);
  const setThinking = useChatStore((s) => s.setThinking);
  const addToolCall = useChatStore((s) => s.addToolCall);
  const setToolResult = useChatStore((s) => s.setToolResult);
  const appendChunk = useChatStore((s) => s.appendChunk);
  const finishTurn = useChatStore((s) => s.finishTurn);
  const failTurn = useChatStore((s) => s.failTurn);
  const clear = useChatStore((s) => s.clear);

  const [input, setInput] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<TextAreaRef | null>(null);

  // auto-scroll to bottom on new content
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [turns, turns[turns.length - 1]?.answer, turns[turns.length - 1]?.steps.length]);

  const buildHistory = (): ChatMessage[] => {
    const msgs: ChatMessage[] = [];
    for (const t of turns) {
      if (t.status === 'done' && t.answer) {
        msgs.push({ role: 'user', content: t.question });
        msgs.push({ role: 'assistant', content: t.answer });
      }
    }
    return msgs.slice(-12);
  };

  const handleSend = () => {
    if (streaming) return;
    const q = input.trim();
    if (!q) return;

    setInput('');
    startTurn(q);
    const messages: ChatMessage[] = [...buildHistory(), { role: 'user', content: q }];

    abortRef.current = agentChatStream(messages, {
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
      onDone: (ev) =>
        finishTurn(ev.answer, {
          toolRounds: ev.tool_rounds,
          toolCalls: ev.tool_calls,
          elapsedMs: ev.elapsed_ms,
        }),
      onError: (message) => failTurn(message),
    });
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    failTurn('已取消');
  };

  const handleClear = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    clear();
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
                      与 TreeKG 知识图谱 Agent 对话 —— 它会自主调用检索工具，引用图谱证据回答
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
                {streaming ? 'Agent 正在处理，可随时停止...' : '支持多轮追问，回答会标注 [ev_xxx] 证据'}
              </Text>
              <Space>
                <Button icon={<ClearOutlined />} onClick={handleClear} disabled={streaming}>
                  清空
                </Button>
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

        {/* ── 右侧：对话历史 ─────────────────────────────── */}
        <Sider
          width={260}
          theme="light"
          style={{
            padding: '16px',
            overflowY: 'auto',
            borderLeft: '1px solid #f0f0f0',
          }}
        >
          <Title level={5} style={{ marginBottom: 16 }}>
            <HistoryOutlined style={{ marginRight: 8 }} />
            对话历史
          </Title>
          <List
            size="small"
            dataSource={turns}
            locale={{ emptyText: '暂无对话' }}
            renderItem={(t) => (
              <List.Item style={{ padding: '8px 0' }}>
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Text strong ellipsis style={{ fontSize: 13, maxWidth: 200 }}>
                    {t.question}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t.status === 'done'
                      ? `${t.toolCalls ?? 0} 次工具调用 · ${((t.elapsedMs ?? 0) / 1000).toFixed(1)}s`
                      : t.status === 'error'
                        ? '失败'
                        : '进行中...'}
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        </Sider>
      </Layout>
    </div>
  );
}
