import { create } from 'zustand';
import type {
  BuildJob,
  BuildStreamEvent,
  ConfigFileMeta,
  DocxItem,
  JobStep,
  LLMConfigValues,
  Readiness,
  StorageConfigValues,
  UploadResult,
} from '../services/buildApi';

export type StreamStatus = 'idle' | 'connecting' | 'live' | 'closed' | 'error';

export interface LogLine {
  seq: number;
  text: string;
  truncated?: boolean;
}

export interface ImportPhase {
  phase: string;
  done: number;
  total: number;
}

const MAX_LOGS = 2000;
export const ACTIVE_STATUSES = ['queued', 'running', 'cancelling'];
export const TERMINAL_STATUSES = ['succeeded', 'failed', 'cancelled', 'interrupted'];

export const isActiveJob = (job: BuildJob | null): boolean =>
  !!job && ACTIVE_STATUSES.includes(job.status);

const toLog = (event: BuildStreamEvent): LogLine => ({
  seq: Number(event.seq ?? 0),
  text: String(event.text ?? ''),
  truncated: Boolean(event.truncated),
});

const patchStep = (job: BuildJob, name: string, patch: Partial<JobStep>): BuildJob => ({
  ...job,
  steps: (job.steps ?? []).map((step) => (step.name === name ? { ...step, ...patch } : step)),
});

interface BuildState {
  readiness: Readiness | null;
  llm: LLMConfigValues | null;
  storage: StorageConfigValues | null;
  files: ConfigFileMeta[];
  docxItems: DocxItem[];
  upload: UploadResult | null;
  job: BuildJob | null;
  logs: LogLine[];
  lastSeq: number;
  streamStatus: StreamStatus;
  autoScroll: boolean;
  importPhase: ImportPhase | null;
  notice: string;

  setOverview: (overview: {
    readiness: Readiness;
    llm: LLMConfigValues;
    storage: StorageConfigValues;
    active_job: BuildJob | null;
  }) => void;
  setFiles: (files: ConfigFileMeta[]) => void;
  setDocx: (items: DocxItem[]) => void;
  setUpload: (upload: UploadResult | null) => void;
  resetForNewJob: (job: BuildJob) => void;
  setJob: (job: BuildJob | null) => void;
  applyEvent: (event: BuildStreamEvent) => void;
  clearLogs: () => void;
  setStreamStatus: (status: StreamStatus) => void;
  setAutoScroll: (value: boolean) => void;
  setNotice: (notice: string) => void;
}

export const useBuildStore = create<BuildState>((set) => ({
  readiness: null,
  llm: null,
  storage: null,
  files: [],
  docxItems: [],
  upload: null,
  job: null,
  logs: [],
  lastSeq: 0,
  streamStatus: 'idle',
  autoScroll: true,
  importPhase: null,
  notice: '',

  setOverview: ({ readiness, llm, storage, active_job }) =>
    set((state) => ({
      readiness,
      llm,
      storage,
      job: state.job ?? active_job,
      streamStatus: state.job ? state.streamStatus : active_job ? 'connecting' : 'idle',
    })),
  setFiles: (files) => set({ files }),
  setDocx: (docxItems) => set({ docxItems }),
  setUpload: (upload) => set({ upload }),
  resetForNewJob: (job) =>
    set({ job, logs: [], lastSeq: 0, importPhase: null, streamStatus: 'connecting', autoScroll: true }),
  setJob: (job) => set({ job }),
  clearLogs: () => set({ logs: [] }),
  setStreamStatus: (streamStatus) => set({ streamStatus }),
  setAutoScroll: (autoScroll) => set({ autoScroll }),
  setNotice: (notice) => set({ notice }),

  applyEvent: (event) =>
    set((state) => {
      const next: Partial<BuildState> = {};
      if (typeof event.seq === 'number' && event.seq > state.lastSeq) {
        next.lastSeq = event.seq;
      }

      switch (event.type) {
        case 'snapshot': {
          if (event.job) next.job = event.job as BuildJob;
          const recent = (event.events as BuildStreamEvent[] | undefined) ?? [];
          next.logs = recent.filter((e) => e.type === 'log').map(toLog);
          next.lastSeq = Number(event.last_seq ?? 0);
          next.streamStatus = 'live';
          break;
        }
        case 'gap':
          next.notice = '部分历史日志已被截断';
          break;
        case 'heartbeat':
          next.streamStatus = 'live';
          break;
        case 'log': {
          const line = toLog(event);
          const logs = [...state.logs, line];
          next.logs = logs.length > MAX_LOGS ? logs.slice(logs.length - MAX_LOGS) : logs;
          break;
        }
        case 'step_start':
          if (state.job) {
            next.job = patchStep(
              { ...state.job, current_step: String(event.step ?? '') },
              String(event.step ?? ''),
              { status: 'running', started_at: String(event.ts ?? ''), detail:
                `第 ${event.index ?? '?'}/${event.total ?? '?'} 步` },
            );
          }
          break;
        case 'step_end':
          if (state.job) {
            next.job = patchStep(
              { ...state.job, current_step: '' },
              String(event.step ?? ''),
              {
                status: event.ok ? 'succeeded' : 'failed',
                exit_code: Number(event.exit_code ?? 0),
                elapsed_sec: Number(event.elapsed_sec ?? 0),
                finished_at: String(event.ts ?? ''),
              },
            );
          }
          break;
        case 'run_start':
          if (state.job) next.job = { ...state.job, current_step: '' };
          break;
        case 'job_status':
          if (state.job && event.status) {
            next.job = { ...state.job, status: String(event.status) as BuildJob['status'] };
          }
          break;
        case 'job_done': {
          if (state.job) {
            const result = (event.result as Record<string, unknown>) ?? {};
            next.job = {
              ...state.job,
              status: 'succeeded',
              result,
              current_step: '',
              // 导入确认需要 build 产物路径与 SHA-1
              final_kg_path: typeof result.final_kg_path === 'string'
                ? result.final_kg_path : state.job.final_kg_path,
              final_kg_sha1: typeof result.final_kg_sha1 === 'string'
                ? result.final_kg_sha1 : state.job.final_kg_sha1,
            };
          }
          next.streamStatus = 'closed';
          next.importPhase = null;
          break;
        }
        case 'job_failed':
          if (state.job) {
            next.job = {
              ...state.job,
              status: 'failed',
              error: String(event.error ?? '任务失败'),
            };
          }
          next.streamStatus = 'closed';
          next.importPhase = null;
          break;
        case 'job_cancelled':
          if (state.job) next.job = { ...state.job, status: 'cancelled' };
          next.streamStatus = 'closed';
          next.importPhase = null;
          break;
        case 'import_phase_start':
          next.importPhase = {
            phase: String(event.phase ?? ''),
            done: 0,
            total: Number(event.total ?? 0),
          };
          break;
        case 'import_progress':
          next.importPhase = {
            phase: String(event.phase ?? ''),
            done: Number(event.done ?? 0),
            total: Number(event.total ?? 0),
          };
          break;
        case 'import_phase_end':
          next.importPhase = null;
          break;
        case 'preflight':
          next.notice = event.ok ? '' : '预检未通过';
          break;
        default:
          break;
      }
      return next;
    }),
}));
