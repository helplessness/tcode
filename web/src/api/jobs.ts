/** Job workspaces, execution, artifacts and previews. */
import type { SymphonyIssueCancelAction, SymphonyLocalIssue } from './symphony';

const API_BASE = '';

export type JobWorkspaceArtifact = {
  path: string;
  type: 'file' | 'directory';
};

export type JobWorkspaceArtifactSource = {
  kind?: string;
  root?: string;
  host?: string;
  browse_api?: string;
  available?: boolean;
  remote_base_url?: string;
};

export type JobWorkspacePreviewEntry = {
  label?: string;
  type?: string;
  port?: number | null;
  path?: string;
  open_url?: string;
};

export type JobTimelineEntry = {
  kind?: string;
  at?: string;
  title?: string;
  source?: string;
  details?: string;
  metadata?: Record<string, any>;
};

export type RuntimeJob = {
  job_id: string;
  provider: string;
  owner: {
    tenant_id: string;
    user_id: string;
    workspace_id: string;
  };
  session_id: string;
  external_id: string;
  status: string;
  phase: string;
  cursor: number;
  created_at: number;
  updated_at: number;
  cancellation_supported: boolean;
  metadata: Record<string, any>;
};

export type RuntimeJobEvent = {
  cursor: number;
  event_id: string;
  status: string;
  phase: string;
  created_at: number;
  metadata: Record<string, any>;
};

export type EffectiveConfigEntry = {
  key: string;
  value: any;
  source: string;
  reloadability: 'reloadable' | 'startup-only' | string;
  sensitive: boolean;
};

export type EffectiveConfigSnapshot = {
  workdir: string;
  config_root: string;
  sources: string[];
  entries: EffectiveConfigEntry[];
  fingerprint: string;
};

export type JobWorkspace = {
  artifact_recovery?: { total_files: number; original_status: string; recovered_at: string };
  output_abs_path?: string;
  cancellation?: { state?: string; requested_at?: string; error?: string; remote_status?: string };
  recovery?: { state?: string; error?: string; next_retry_at?: number };
  transfer_state?: 'pending' | 'downloading' | 'retry_wait' | 'ready';
  last_observed_at?: string;
  tenant_id?: string;
  user_id?: string;
  workspace_id?: string;
  job_id?: string;
  process_id?: string;
  execution_channel?: string;
  job_ref?: {
    job_id?: string;
    process_id?: string;
    id?: string;
    [key: string]: any;
  };
  tracker_status?: string;
  run_id?: string;
  tool?: string;
  task_type?: string;
  workdir?: string;
  log_file?: string;
  exitcode_file?: string;
  expected_artifacts?: string[];
  context_file?: string;
  session_id?: string;
  parent_session_id?: string;
  issue_identifier?: string;
  preview_ports?: number[];
  preview_urls?: string[];
  preview_entries?: JobWorkspacePreviewEntry[];
  artifacts?: string[];
  artifact_entries?: JobWorkspaceArtifact[];
  artifact_source?: JobWorkspaceArtifactSource;
  output_root?: string;
  host?: string;
  execution_host?: string;
  machine_label?: string;
  input_summary?: Record<string, any>;
  input_snapshot?: Record<string, any>;
  resume_supported?: boolean;
  resume_command?: string;
  retry_of?: string;
  resume_of?: string;
  review_lineage?: Record<string, any>;
  orchestration?: Record<string, any>;
  tool_contract?: Record<string, any>;
  action_policy?: Record<string, any>;
  timeline_ref?: Record<string, any>;
  submitted_at?: string;
  started_at?: string;
  completed_at?: string;
  cancelled_at?: string;
  status?: string;
  issue_state?: string;
  lifecycle_status?: string;
  result_summary?: string;
  tool_progress?: {
    message?: string;
    current?: number;
    total?: number;
    unit?: string;
    seq?: number;
    stream?: string;
    status?: string;
    observed_at?: string;
  };
  cancel_actions?: SymphonyIssueCancelAction[];
  timeline?: JobTimelineEntry[];
  retry_available?: boolean;
  retry_reason?: string;
  resubmit_available?: boolean;
  resubmit_reason?: string;
  resume_available?: boolean;
  resume_reason?: string;
};

export async function getJobWorkspace(jobId: string): Promise<{
  job_id: string;
  workspace: JobWorkspace;
}> {
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/workspace`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '获取任务工作区失败');
  }
  return {
    job_id: String(data.job_id || jobId),
    workspace: data.workspace && typeof data.workspace === 'object' ? data.workspace : {},
  };
}

export async function getJobTimeline(jobId: string): Promise<{
  job_id: string;
  lifecycle_status?: string;
  timeline: JobTimelineEntry[];
}> {
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/timeline`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '获取任务时间线失败');
  }
  return {
    job_id: String(data.job_id || jobId),
    lifecycle_status: typeof data.lifecycle_status === 'string' ? data.lifecycle_status : undefined,
    timeline: Array.isArray(data.timeline) ? data.timeline : [],
  };
}

/** 按 Session 与 Workspace Owner 边界读取统一 Job Runtime 投影。 */
export async function getRuntimeJob(
  jobId: string,
  sessionId: string,
  workspaceId: string,
  options: { trackerIdentifier?: string } = {},
): Promise<RuntimeJob | null> {
  const params = new URLSearchParams({ session_id: sessionId, workspace_id: workspaceId });
  if (options.trackerIdentifier) params.set('tracker_identifier', options.trackerIdentifier);
  const resp = await fetch(`${API_BASE}/api/runtime/jobs/${encodeURIComponent(jobId)}?${params}`);
  const data = await resp.json().catch(() => ({}));
  // Only the server can confirm that the owner-scoped tracker is authoritative.
  if (resp.ok && options.trackerIdentifier && data.job === null && data.projection_state === 'tracker_only') return null;
  if (!resp.ok || !data.job) {
    throw new Error(data.detail || '获取统一任务运行时失败');
  }
  return data.job as RuntimeJob;
}

/** Download already-produced artifacts only; never retry or resume computation. */
export async function recoverJobArtifacts(jobId: string, payload: {
  session_id: string; workspace_id: string; tracker_identifier: string; bundle_id: string;
}): Promise<{ recovery: NonNullable<JobWorkspace['artifact_recovery']>; workspace: JobWorkspace }> {
  const response = await fetch(`${API_BASE}/api/runtime/jobs/${encodeURIComponent(jobId)}/recover-artifacts`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.success || !data.recovery || !data.workspace) throw new Error(data.detail || '取回已有产物失败，原任务状态未改变');
  return data;
}

/** 从指定单调 Cursor 之后读取统一 Job Runtime 事件。 */
export async function getRuntimeJobEvents(
  jobId: string,
  sessionId: string,
  workspaceId: string,
  afterCursor = 0,
): Promise<{ events: RuntimeJobEvent[]; next_cursor: number }> {
  const params = new URLSearchParams({
    session_id: sessionId,
    workspace_id: workspaceId,
    after_cursor: String(Math.max(0, afterCursor)),
  });
  const resp = await fetch(`${API_BASE}/api/runtime/jobs/${encodeURIComponent(jobId)}/events?${params}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '获取统一任务事件失败');
  }
  return {
    events: Array.isArray(data.events) ? data.events : [],
    next_cursor: Number(data.next_cursor || afterCursor),
  };
}

/** 使用客户端最后观察到的 Cursor 请求 Owner 约束取消。 */
export async function cancelRuntimeJob(
  jobId: string,
  sessionId: string,
  workspaceId: string,
  expectedCursor: number,
): Promise<RuntimeJob> {
  const resp = await fetch(`${API_BASE}/api/runtime/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      workspace_id: workspaceId,
      expected_cursor: expectedCursor,
    }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.job) {
    throw new Error(data.detail || '取消统一任务失败');
  }
  return data.job as RuntimeJob;
}

/** 读取 Owner 可见工作区的脱敏 Effective Config 与来源说明。 */
export async function getEffectiveConfig(
  sessionId: string,
  workspaceId: string,
): Promise<EffectiveConfigSnapshot> {
  const params = new URLSearchParams({ session_id: sessionId, workspace_id: workspaceId });
  const resp = await fetch(`${API_BASE}/api/config/effective?${params}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.config) {
    throw new Error(data.detail || '获取有效配置失败');
  }
  return data.config as EffectiveConfigSnapshot;
}

/** 显式重载 Owner 可见工作区的 JSON 配置并返回脱敏差异。 */
export async function reloadEffectiveConfig(
  sessionId: string,
  workspaceId: string,
): Promise<{
  changed: boolean;
  changed_keys: string[];
  snapshot: EffectiveConfigSnapshot;
}> {
  const params = new URLSearchParams({ session_id: sessionId, workspace_id: workspaceId });
  const resp = await fetch(`${API_BASE}/api/config/reload?${params}`, { method: 'POST' });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.snapshot) {
    throw new Error(data.detail || '重载有效配置失败');
  }
  return data;
}

export async function getJobSubmission(jobId: string): Promise<{
  job_id: string;
  submit_input: Record<string, any>;
  retry_input?: Record<string, any>;
  resume_input?: Record<string, any> | null;
  resume_supported?: boolean;
}> {
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/submission`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '获取任务提交载荷失败');
  }
  return {
    job_id: String(data.job_id || jobId),
    submit_input: data.submit_input && typeof data.submit_input === 'object' ? data.submit_input : {},
    retry_input: data.retry_input && typeof data.retry_input === 'object' ? data.retry_input : undefined,
    resume_input: data.resume_input && typeof data.resume_input === 'object' ? data.resume_input : null,
    resume_supported: Boolean(data.resume_supported),
  };
}

export async function retryJob(jobId: string, payload?: {
  reuse_issue?: boolean;
  reason?: string;
}): Promise<{
  status?: string;
  action?: string;
  source_job_id?: string;
  job_id: string;
  snapshot_dir?: string;
  workspace?: JobWorkspace;
  issue?: SymphonyLocalIssue | null;
  submit_input?: Record<string, any>;
  notified_session_ids?: string[];
}> {
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '重试任务失败');
  }
  return {
    status: typeof data.status === 'string' ? data.status : undefined,
    action: typeof data.action === 'string' ? data.action : undefined,
    source_job_id: typeof data.source_job_id === 'string' ? data.source_job_id : undefined,
    job_id: String(data.job_id || ''),
    snapshot_dir: typeof data.snapshot_dir === 'string' ? data.snapshot_dir : undefined,
    workspace: data.workspace && typeof data.workspace === 'object' ? data.workspace : undefined,
    issue: data.issue && typeof data.issue === 'object' ? data.issue : null,
    submit_input: data.submit_input && typeof data.submit_input === 'object' ? data.submit_input : undefined,
    notified_session_ids: Array.isArray(data.notified_session_ids) ? data.notified_session_ids.map((item: any) => String(item || '')).filter(Boolean) : undefined,
  };
}

export async function resumeJob(jobId: string, payload?: {
  reuse_issue?: boolean;
  reason?: string;
}): Promise<{
  status?: string;
  action?: string;
  source_job_id?: string;
  job_id: string;
  snapshot_dir?: string;
  workspace?: JobWorkspace;
  issue?: SymphonyLocalIssue | null;
  submit_input?: Record<string, any>;
  notified_session_ids?: string[];
}> {
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '恢复任务失败');
  }
  return {
    status: typeof data.status === 'string' ? data.status : undefined,
    action: typeof data.action === 'string' ? data.action : undefined,
    source_job_id: typeof data.source_job_id === 'string' ? data.source_job_id : undefined,
    job_id: String(data.job_id || ''),
    snapshot_dir: typeof data.snapshot_dir === 'string' ? data.snapshot_dir : undefined,
    workspace: data.workspace && typeof data.workspace === 'object' ? data.workspace : undefined,
    issue: data.issue && typeof data.issue === 'object' ? data.issue : null,
    submit_input: data.submit_input && typeof data.submit_input === 'object' ? data.submit_input : undefined,
    notified_session_ids: Array.isArray(data.notified_session_ids) ? data.notified_session_ids.map((item: any) => String(item || '')).filter(Boolean) : undefined,
  };
}

export async function listJobArtifacts(jobId: string, params?: {
  path?: string;
  root?: string;
}): Promise<{
  job_id: string;
  path?: string;
  root?: string;
  artifact_source?: JobWorkspaceArtifactSource;
  items: Array<JobWorkspaceArtifact & { name: string; size?: number; preview_type?: string }>;
}> {
  const search = new URLSearchParams();
  if (params?.path) search.set('path', params.path);
  if (params?.root) search.set('root', params.root);
  const query = search.toString();
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/artifacts${query ? `?${query}` : ''}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '列出任务产物失败');
  }
  return {
    job_id: String(data.job_id || jobId),
    path: typeof data.path === 'string' ? data.path : undefined,
    root: typeof data.root === 'string' ? data.root : undefined,
    artifact_source: data.artifact_source && typeof data.artifact_source === 'object' ? data.artifact_source : undefined,
    items: Array.isArray(data.items) ? data.items : [],
  };
}

export async function getJobArtifactContent(jobId: string, params: {
  path: string;
  root?: string;
}): Promise<{
  job_id: string;
  path?: string;
  root?: string;
  content?: string;
  size?: number;
  content_type?: string;
  preview_type?: string;
  artifact_source?: JobWorkspaceArtifactSource;
}> {
  const search = new URLSearchParams({ path: params.path });
  if (params.root) search.set('root', params.root);
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/artifacts/content?${search.toString()}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '获取产物内容失败');
  }
  return {
    job_id: String(data.job_id || jobId),
    path: typeof data.path === 'string' ? data.path : undefined,
    root: typeof data.root === 'string' ? data.root : undefined,
    content: typeof data.content === 'string' ? data.content : undefined,
    size: typeof data.size === 'number' ? data.size : undefined,
    content_type: typeof data.content_type === 'string' ? data.content_type : undefined,
    preview_type: typeof data.preview_type === 'string' ? data.preview_type : undefined,
    artifact_source: data.artifact_source && typeof data.artifact_source === 'object' ? data.artifact_source : undefined,
  };
}

export function getJobArtifactDownloadUrl(jobId: string, params: {
  path: string;
  root?: string;
  inline?: boolean;
}): string {
  const search = new URLSearchParams({ path: params.path });
  if (params.root) search.set('root', params.root);
  if (params.inline) search.set('inline', '1');
  return `${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/artifacts/download?${search.toString()}`;
}

export async function createJobPreviewTicket(jobId: string, port: number): Promise<{
  job_id: string;
  port: number;
  ticket: string;
  expires_at?: string;
  open_url: string;
}> {
  const resp = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/preview-ticket`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '创建预览凭证失败');
  }
  return {
    job_id: String(data.job_id || jobId),
    port: Number(data.port || port),
    ticket: String(data.ticket || ''),
    expires_at: typeof data.expires_at === 'string' ? data.expires_at : undefined,
    open_url: String(data.open_url || ''),
  };
}
