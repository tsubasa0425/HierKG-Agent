import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Empty,
  Layout,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  BuildOutlined,
  CloseCircleOutlined,
  PlayCircleOutlined,
  RedoOutlined,
  SettingOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import ConfigEditor from '../components/build/ConfigEditor';
import ImportConfirmModal from '../components/build/ImportConfirmModal';
import LogPanel from '../components/build/LogPanel';
import ReadinessPanel from '../components/build/ReadinessPanel';
import SourcePicker from '../components/build/SourcePicker';
import StepTimeline from '../components/build/StepTimeline';
import {
  cancelBuildJob,
  createBuildJob,
  getBuildConfig,
  importBuildJob,
  listDocx,
  retryBuildJob,
  type BuildJob,
  type UploadResult,
} from '../services/buildApi';
import { useBuildStream } from '../hooks/useBuildStream';
import { isActiveJob, TERMINAL_STATUSES, useBuildStore } from '../stores/buildStore';

const { Sider, Content } = Layout;
const { Title, Text } = Typography;

const STEP_OPTIONS = [
  { value: 'textseg', label: '1 文本分割' },
  { value: 'extraction', label: '2 实体关系抽取' },
  { value: 'toc_graph', label: '3 目录图' },
  { value: 'evidence', label: '4 证据层构建' },
  { value: 'conv', label: '5 格式转换' },
  { value: 'aggr', label: '6 概念分层' },
  { value: 'embedding', label: '7 向量嵌入' },
  { value: 'dedup', label: '8 概念去重' },
  { value: 'pred', label: '9 关系预测' },
  { value: 'final', label: '10 四层装配' },
];

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  cancelling: '取消中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
};

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  cancelling: 'warning',
  succeeded: 'success',
  failed: 'error',
  cancelled: 'default',
  interrupted: 'warning',
};

const jobCounts = (job: BuildJob | null): Record<string, number> =>
  ((job?.result as { counts?: Record<string, number> } | undefined)?.counts) ?? {};

export default function BuildView() {
  const navigate = useNavigate();
  const [configOpen, setConfigOpen] = useState(false);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [deepLoading, setDeepLoading] = useState(false);

  const [sourceKind, setSourceKind] = useState<'existing' | 'upload'>('existing');
  const [docxName, setDocxName] = useState('');
  const [upload, setUpload] = useState<UploadResult | null>(null);
  const [checkOnly, setCheckOnly] = useState(false);
  const [startStep, setStartStep] = useState<string | undefined>();
  const [untilStep, setUntilStep] = useState<string | undefined>();
  const [skipSteps, setSkipSteps] = useState<string[]>([]);
  const [onlySteps, setOnlySteps] = useState<string[]>([]);

  const [starting, setStarting] = useState(false);
  const [busyAction, setBusyAction] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const readiness = useBuildStore((s) => s.readiness);
  const docxItems = useBuildStore((s) => s.docxItems);
  const job = useBuildStore((s) => s.job);
  const logs = useBuildStore((s) => s.logs);
  const lastSeq = useBuildStore((s) => s.lastSeq);
  const streamStatus = useBuildStore((s) => s.streamStatus);
  const autoScroll = useBuildStore((s) => s.autoScroll);
  const importPhase = useBuildStore((s) => s.importPhase);
  const notice = useBuildStore((s) => s.notice);

  const setOverview = useBuildStore((s) => s.setOverview);
  const setDocx = useBuildStore((s) => s.setDocx);
  const resetForNewJob = useBuildStore((s) => s.resetForNewJob);
  const clearLogs = useBuildStore((s) => s.clearLogs);
  const setAutoScroll = useBuildStore((s) => s.setAutoScroll);
  const setNotice = useBuildStore((s) => s.setNotice);

  useBuildStream();

  const loadOverview = useCallback(async (deep = false) => {
    if (deep) setDeepLoading(true);
    else setReadinessLoading(true);
    try {
      const overview = await getBuildConfig(deep);
      setOverview(overview);
    } catch {
      message.error('无法读取构建配置，请确认后端已启动');
    } finally {
      setReadinessLoading(false);
      setDeepLoading(false);
    }
  }, [setOverview]);

  const refreshAll = useCallback(async (deep = false) => {
    await Promise.all([
      loadOverview(deep),
      listDocx()
        .then((items) => {
          setDocx(items);
          if (items.length > 0) setDocxName((current) => current || items[0].name);
        })
        .catch(() => setDocx([])),
    ]);
  }, [loadOverview, setDocx]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 首屏拉取配置与 docx 列表
    void refreshAll();
  }, [refreshAll]);

  // 任务终态后刷新就绪状态（导入成功后 import_ready / 图谱统计会变化）
  const terminal = job && TERMINAL_STATUSES.includes(job.status);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 任务终态触发一次远端刷新
    if (terminal) void loadOverview(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [terminal, job?.id]);

  const active = isActiveJob(job);
  const buildSucceeded = job?.kind === 'build' && job.status === 'succeeded'
    && !(job.options as { check_only?: boolean })?.check_only;

  const handleStart = async () => {
    if (sourceKind === 'existing' && !docxName) {
      message.warning('请选择 docx 文件');
      return;
    }
    if (sourceKind === 'upload' && !upload) {
      message.warning('请先上传 docx 文件');
      return;
    }
    setStarting(true);
    try {
      const source = sourceKind === 'upload'
        ? { kind: 'upload' as const, upload_id: upload!.id }
        : { kind: 'existing' as const, name: docxName };
      const result = await createBuildJob({
        source,
        check_only: checkOnly,
        start: startStep,
        until: untilStep,
        skip: skipSteps.length > 0 ? skipSteps.join(',') : undefined,
        only: onlySteps.length > 0 ? onlySteps.join(',') : undefined,
      });
      resetForNewJob(result.job);
      setNotice('');
      message.success(checkOnly ? '预检任务已启动' : '构建任务已启动');
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      if (typeof detail === 'string') message.error(detail);
      else if (detail && typeof detail === 'object') {
        const d = detail as { message?: string; active_job_id?: string };
        message.error(d.active_job_id ? `${d.message}（${d.active_job_id}）` : (d.message || '启动失败'));
      } else message.error('启动任务失败');
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = () => {
    if (!job) return;
    Modal.confirm({
      title: '取消当前任务？',
      content: '将终止子进程树，已产生的中间产物仍留在 output/ 中，可由「重试」续跑。',
      okText: '取消任务',
      okButtonProps: { danger: true },
      cancelText: '继续运行',
      onOk: async () => {
        setBusyAction(true);
        try {
          await cancelBuildJob(job.id);
          message.warning('已请求取消');
        } catch (err: unknown) {
          const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
          message.error(detail || '取消失败');
        } finally {
          setBusyAction(false);
        }
      },
    });
  };

  const handleRetry = async () => {
    if (!job) return;
    setBusyAction(true);
    try {
      const result = await retryBuildJob(job.id);
      resetForNewJob(result.job);
      message.success('已重新启动任务');
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '重试失败');
    } finally {
      setBusyAction(false);
    }
  };

  const handleImport = async () => {
    if (!job) return;
    try {
      const result = await importBuildJob(job.id, job.final_kg_sha1 || undefined);
      setImportOpen(false);
      resetForNewJob(result.job);
      message.warning('导入已开始：Neo4j 将被清空重建');
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '导入请求被拒绝');
    }
  };

  const counts = jobCounts(job);

  return (
    <div style={{ margin: -24, height: 'calc(100vh - 64px)' }}>
      <Layout style={{ height: '100%' }}>
        <Sider
          width={380}
          theme="light"
          style={{ padding: 16, overflowY: 'auto', borderRight: '1px solid #f0f0f0' }}
        >
          <Title level={4} style={{ marginBottom: 16 }}>
            <BuildOutlined style={{ marginRight: 8 }} />
            图谱构建
          </Title>

          <ReadinessPanel
            readiness={readiness}
            loading={readinessLoading || deepLoading}
            onRefresh={() => loadOverview(false)}
            onDeepCheck={() => loadOverview(true)}
          />

          <div style={{ margin: '12px 0' }}>
            <Button icon={<SettingOutlined />} block onClick={() => setConfigOpen(true)}>
              编辑配置（LLM / 阶段 / L0 本体）
            </Button>
          </div>

          <SourcePicker
            sourceKind={sourceKind}
            onSourceKind={setSourceKind}
            docxName={docxName}
            onDocxName={setDocxName}
            docxItems={docxItems}
            upload={upload}
            onUpload={(value) => {
              setUpload(value);
              setSourceKind('upload');
            }}
            onRefresh={() => {
              listDocx().then(setDocx).catch(() => setDocx([]));
            }}
            disabled={active}
          />

          <div className="agent-panel" style={{ marginTop: 12 }}>
            <div className="agent-panel-header">高级选项</div>
            <div className="build-option-row">
              <Text style={{ fontSize: 12 }}>仅预检（不消耗 LLM / GPU）</Text>
              <Switch size="small" checked={checkOnly} onChange={setCheckOnly} disabled={active} />
            </div>
            <div className="build-option-row">
              <Text style={{ fontSize: 12 }}>起始步骤</Text>
              <Select size="small" allowClear style={{ width: 160 }} value={startStep}
                      onChange={setStartStep} options={STEP_OPTIONS} disabled={active}
                      placeholder="从头开始" />
            </div>
            <div className="build-option-row">
              <Text style={{ fontSize: 12 }}>结束步骤</Text>
              <Select size="small" allowClear style={{ width: 160 }} value={untilStep}
                      onChange={setUntilStep} options={STEP_OPTIONS} disabled={active}
                      placeholder="跑到最后" />
            </div>
            <div className="build-option-row">
              <Text style={{ fontSize: 12 }}>跳过步骤</Text>
              <Select size="small" mode="multiple" allowClear style={{ width: 160 }}
                      value={skipSteps} onChange={setSkipSteps} options={STEP_OPTIONS}
                      disabled={active} placeholder="不跳过" maxTagCount={1} />
            </div>
            <div className="build-option-row">
              <Text style={{ fontSize: 12 }}>只跑步骤</Text>
              <Select size="small" mode="multiple" allowClear style={{ width: 160 }}
                      value={onlySteps} onChange={setOnlySteps} options={STEP_OPTIONS}
                      disabled={active} placeholder="按上方范围" maxTagCount={1} />
            </div>
          </div>

          <Space direction="vertical" style={{ width: '100%', marginTop: 12 }}>
            <Button
              type="primary"
              block
              icon={<PlayCircleOutlined />}
              loading={starting}
              disabled={active || !readiness?.build_ready}
              onClick={handleStart}
            >
              {checkOnly ? '开始预检' : '开始构建'}
            </Button>
            {!readiness?.build_ready && (
              <Text type="danger" style={{ fontSize: 12 }}>
                配置未就绪，请先修正上方错误
              </Text>
            )}
            {active && (
              <Button danger block icon={<CloseCircleOutlined />}
                      loading={busyAction} onClick={handleCancel}>
                取消任务
              </Button>
            )}
            {job && TERMINAL_STATUSES.includes(job.status)
              && ['failed', 'cancelled', 'interrupted'].includes(job.status) && (
                <Button block icon={<RedoOutlined />} loading={busyAction} onClick={handleRetry}
                        disabled={active}>
                  从失败步骤重试
                </Button>
              )}
            {buildSucceeded && (
              <Button danger block icon={<UploadOutlined />} onClick={() => setImportOpen(true)}
                      disabled={active}>
                导入到 Neo4j…
              </Button>
            )}
          </Space>
        </Sider>

        <Content style={{ padding: 16, overflowY: 'auto' }}>
          {notice && (
            <Alert
              type={streamStatus === 'error' ? 'error' : 'warning'}
              showIcon
              closable
              style={{ marginBottom: 12 }}
              message={notice}
              onClose={() => setNotice('')}
            />
          )}

          {!job ? (
            <div className="build-empty">
              <Empty description="尚未开始构建：左侧选择数据源后点击「开始构建」" />
            </div>
          ) : (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Card size="small">
                <div className="build-job-head">
                  <Space size={8} wrap>
                    <Tag color={STATUS_COLOR[job.status]}>{STATUS_LABEL[job.status] ?? job.status}</Tag>
                    <Tag color={job.kind === 'import' ? 'orange' : 'blue'}>
                      {job.kind === 'import' ? '导入' : '构建'}
                    </Tag>
                    <Text strong>{job.source_name || job.id}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>#{job.id}</Text>
                  </Space>
                  <Space size={8}>
                    {streamStatus === 'connecting' && <Spin size="small" />}
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {job.started_at ? `开始 ${job.started_at}` : job.created_at}
                    </Text>
                  </Space>
                </div>
                <Space size={4} wrap style={{ marginTop: 8 }}>
                  {Object.entries(counts).map(([key, value]) => (
                    <Tag key={key}>{key}: {value}</Tag>
                  ))}
                  {job.final_kg_sha1 && <Tag>sha {job.final_kg_sha1.slice(0, 12)}</Tag>}
                  {job.error && <Text type="danger" style={{ fontSize: 12 }}>{job.error}</Text>}
                </Space>
                {job.kind === 'import' && job.status === 'succeeded' && (
                  <div style={{ marginTop: 8 }}>
                    <Button size="small" type="link" onClick={() => navigate('/graph')}>
                      图谱已更新，前往查看 →
                    </Button>
                  </div>
                )}
              </Card>

              {job.kind === 'build' && <StepTimeline job={job} />}

              {importPhase && (
                <Card size="small" title={`导入阶段：${importPhase.phase}`}>
                  <Progress
                    percent={importPhase.total > 0
                      ? Math.round((importPhase.done / importPhase.total) * 100) : 0}
                    status="active"
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {importPhase.done} / {importPhase.total || '?'}
                  </Text>
                </Card>
              )}

              <LogPanel
                logs={logs}
                autoScroll={autoScroll}
                onAutoScroll={setAutoScroll}
                onClear={clearLogs}
              />
              <Text type="secondary" style={{ fontSize: 11 }}>
                已接收 seq {lastSeq} · 流状态 {streamStatus}
              </Text>
            </Space>
          )}
        </Content>
      </Layout>

      <ConfigEditor
        open={configOpen}
        busy={active}
        onClose={() => setConfigOpen(false)}
        onSaved={() => void loadOverview(true)}
      />

      <ImportConfirmModal
        open={importOpen}
        job={job}
        onCancel={() => setImportOpen(false)}
        onConfirm={handleImport}
      />
    </div>
  );
}
