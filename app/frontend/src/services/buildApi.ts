import api from './api';

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export type Severity = 'ok' | 'warn' | 'error';

export interface ReadinessCheck {
  id: string;
  label: string;
  severity: Severity;
  detail: string;
  fix: string;
}

export interface Readiness {
  build_ready: boolean;
  embedding_ready: boolean;
  import_ready: boolean;
  checks: ReadinessCheck[];
}

export interface LLMConfigValues {
  api_base: string;
  model_name: string;
  api_key_set: boolean;
  api_key_hint: string;
  /** 仅写：保存时传入；读取接口不会返回明文 */
  api_key?: string;
  workers: number;
  timeout_secs: number;
  retries: number;
  rate_limit_qps: number;
  extra_throttle_sec: number;
  include_files: string[];
}

export interface StorageConfigValues {
  neo4j_uri: string;
  neo4j_username: string;
  neo4j_password_set: boolean;
  neo4j_password_hint: string;
  /** 仅写：保存时传入；读取接口不会返回明文 */
  neo4j_password?: string;
  neo4j_database: string;
  ollama_host: string;
  embedding_model: string;
  persist_directory: string;
  kg_file: string;
}

export type JobStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export type StepStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'skipped';

export interface JobStep {
  name: string;
  label: string;
  ordinal: number;
  selected: boolean | number;
  status: StepStatus;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  elapsed_sec: number | null;
  detail: string;
}

export interface BuildJob {
  id: string;
  kind: 'build' | 'import';
  status: JobStatus;
  source_kind: string;
  source_name: string;
  docx_path: string;
  options: Record<string, unknown>;
  current_step: string;
  exit_code: number | null;
  error: string;
  pid: number | null;
  cancel_requested: boolean;
  result: Record<string, unknown>;
  final_kg_path: string;
  final_kg_sha1: string;
  derived_from: string;
  imported_at: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  steps?: JobStep[];
}

export interface ConfigOverview {
  readiness: Readiness;
  llm: LLMConfigValues;
  storage: StorageConfigValues;
  active_job: BuildJob | null;
}

export interface ConfigFileMeta {
  id: string;
  label: string;
  path: string;
  exists: boolean;
  size: number;
  mtime: string | null;
  mode: 'form' | 'yaml';
  rebuild_from: string | null;
  critical: boolean;
}

export interface SchemaNodeType {
  name: string;
  pack: string;
  parent: string;
  description: string;
  examples: string[];
}

export interface SchemaRelation {
  name: string;
  group: string;
  direction: string;
  description: string;
  layers: string[];
}

export interface SchemaSummary {
  ontology_name: string;
  schema_version: string;
  node_types: SchemaNodeType[];
  relation_types: SchemaRelation[];
  layer_rules: Array<{ id: string; layer: string; confidence: number }>;
  counts: {
    node_types: number;
    core_types: number;
    domain_types: number;
    relation_types: number;
    layer_rules: number;
    packs: Record<string, number>;
  };
}

export interface ConfigFileDetail extends ConfigFileMeta {
  content?: string;
  sha1?: string;
  parsed?: unknown;
  summary?: SchemaSummary;
  values?: LLMConfigValues | StorageConfigValues;
}

export interface ValidationIssue {
  line: number | null;
  column: number | null;
  message: string;
}

export interface ValidationResult {
  ok: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  summary?: SchemaSummary;
}

export interface WriteConfigResult extends ValidationResult {
  backup?: string | null;
  requires_rebuild?: boolean;
  rebuild_from?: string | null;
  critical?: boolean;
  summary?: SchemaSummary;
}

export interface DocxItem {
  name: string;
  size: number;
  mtime: string;
}

export interface UploadResult {
  id: string;
  original_name: string;
  size: number;
  sha256: string;
}

export interface BuildJobOptions {
  check_only?: boolean;
  start?: string;
  until?: string;
  skip?: string;
  only?: string;
}

export interface BuildJobRequest extends BuildJobOptions {
  source?:
    | { kind: 'existing'; name: string }
    | { kind: 'upload'; upload_id: string }
    | null;
}

export interface BuildStreamEvent {
  type: string;
  seq?: number;
  ts?: string;
  [k: string]: unknown;
}

/** 后端 SSE 帧：data 里是事件对象，id 为 seq（心跳/snapshot 可能没有）。 */
export interface BuildStreamCallbacks {
  onEvent: (event: BuildStreamEvent) => void;
  onError: (message: string) => void;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

export const getBuildConfig = (deep = false): Promise<ConfigOverview> =>
  api.get('/build/config', { params: { deep: deep ? 1 : 0 } }).then(r => r.data);

export const putLLMConfig = (patch: Partial<LLMConfigValues>): Promise<WriteConfigResult> =>
  api.put('/build/config/llm', patch).then(r => r.data);

export const putStorageConfig = (patch: Partial<StorageConfigValues>): Promise<WriteConfigResult> =>
  api.put('/build/config/storage', patch).then(r => r.data);

export const listConfigFiles = (): Promise<ConfigFileMeta[]> =>
  api.get('/build/config/files').then(r => r.data.files);

export const getConfigFile = (fileId: string): Promise<ConfigFileDetail> =>
  api.get(`/build/config/files/${fileId}`).then(r => r.data);

export const saveConfigFile = (fileId: string, content: string): Promise<WriteConfigResult> =>
  api.put(`/build/config/files/${fileId}`, { content }).then(r => r.data);

export const validateConfigFile = (fileId: string, content: string): Promise<ValidationResult> =>
  api.post('/build/config/validate', { file_id: fileId, content }).then(r => r.data);

export const listDocx = (): Promise<DocxItem[]> =>
  api.get('/build/docx').then(r => r.data.items);

export const uploadDocx = (file: File): Promise<UploadResult> => {
  const form = new FormData();
  form.append('file', file);
  return api
    .post('/build/uploads', form, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 300000 })
    .then(r => r.data);
};

export const createBuildJob = (req: BuildJobRequest): Promise<{ job_id: string; job: BuildJob }> =>
  api.post('/build/jobs', req).then(r => r.data);

export const getActiveJob = (): Promise<BuildJob | null> =>
  api.get('/build/jobs/active').then(r => r.data.job);

export const listBuildJobs = (limit = 20): Promise<BuildJob[]> =>
  api.get('/build/jobs', { params: { limit } }).then(r => r.data.items);

export const getBuildJob = (jobId: string): Promise<BuildJob> =>
  api.get(`/build/jobs/${jobId}`).then(r => r.data);

export const cancelBuildJob = (jobId: string): Promise<{ ok: boolean }> =>
  api.post(`/build/jobs/${jobId}/cancel`).then(r => r.data);

export const retryBuildJob = (jobId: string, start?: string): Promise<{ job: BuildJob }> =>
  api.post(`/build/jobs/${jobId}/retry`, { start }).then(r => r.data);

export const importBuildJob = (
  jobId: string,
  expectedSha1?: string,
): Promise<{ job_id: string; job: BuildJob }> =>
  api.post(`/build/jobs/${jobId}/import`, { confirm: true, expected_sha1: expectedSha1 }).then(r => r.data);
