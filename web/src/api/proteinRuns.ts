const API_BASE = '';

export type ProteinRunSummary = {
  run_id: string;
  title: string;
  task_id: string;
  session_id: string;
  kind: string;
  domain: string;
  agent_name: string;
  worker_name: string;
  status: string;
  lifecycle_status: string;
  outcome: string;
  created_at: string;
  updated_at: string;
  finalized_at: string;
  input_count: number;
  output_count: number;
  tool_call_count: number;
  resume_count: number;
  retained_reason: string;
  error: string;
  sync_status?: string;
  stale?: boolean;
  deletable?: boolean;
  delete_block_reason?: string;
};

export type ProteinRunInput = {
  name?: string;
  kind?: string;
  relative_path?: string;
  container_path?: string;
  mode?: string;
  import_mode?: string;
  snapshot?: boolean;
  mounted?: boolean;
  read_only?: boolean;
  file_count?: number;
  size?: number;
  sha256?: string;
};

export type ProteinRunOutput = {
  kind?: string;
  relative_path: string;
  container_path?: string;
  size?: number;
  sha256?: string;
  artifact_url?: string;
};

export type ProteinRunToolCall = {
  tool_name?: string;
  call_id?: string;
  attempt?: number;
  state?: string;
  arguments?: Record<string, unknown>;
  arguments_sha256?: string;
  metadata?: Record<string, unknown>;
  error?: string;
};

export type ProteinRunModelAttempt = {
  invocation_id?: string;
  attempt?: number;
  agent_name?: string;
  provider?: string;
  model?: string;
  temperature?: number | null;
  top_p?: number | null;
  reasoning_effort?: string;
  execution_profile?: string;
  usage?: Record<string, number>;
  message_count?: number;
  outcome?: string;
  recorded_at?: string;
};

export type ProteinRunRecovery = {
  state: string;
  can_resume: boolean;
  requires_revalidation: boolean;
  checkpoint_status: string;
  checkpoint_created_at: string;
  checkpoint_updated_at: string;
  last_input_validation: {
    valid: boolean | null;
    checked_at: string;
  };
  pending_action: string;
  resume_count: number;
  completed_step_count: number;
};

export type ProteinRunAttempt = {
  attempt_id: string;
  issue_identifier: string;
  job_id: string;
  target_server: string;
  target_tool: string;
  status: string;
  phase: string;
  submitted_at: string;
  updated_at: string;
  finished_at: string;
  result_summary: string;
  error_summary: string;
  superseded_by: string;
  superseded_at: string;
  metadata: Record<string, unknown>;
  sync_status?: string;
  sync_retry_count?: number;
  sync_last_error_at?: string;
  sync_next_retry_at?: string;
  stale?: boolean;
  stale_seconds?: number;
  last_heartbeat_at?: string;
};

export type ProteinRunWatcherHealth = {
  enabled: boolean;
  running: boolean;
  started_at: string;
  last_poll_at: string;
  last_success_at: string;
  last_error_at: string;
  last_error: string;
  consecutive_failures: number;
  registered_count: number;
  pending_count: number;
  sync_failed_count: number;
  injected_cache_size: number;
  oldest_pending_at: string;
};

export type ProteinRunActionPolicy = {
  schema_version?: string;
  attempt_id?: string;
  retry?: {
    available?: boolean;
    reason?: string;
    requires_revalidation?: boolean;
    requires_biosafety_gate?: boolean;
  };
  cancel?: { available?: boolean; reason?: string };
  resume?: { available?: boolean; reason?: string };
};

export type ProteinRunDetail = {
  run: ProteinRunSummary;
  contract: Record<string, unknown>;
  biosafety: Record<string, unknown>;
  inputs: ProteinRunInput[];
  outputs: ProteinRunOutput[];
  tool_calls: ProteinRunToolCall[];
  model_attempts: ProteinRunModelAttempt[];
  reviews: Record<string, unknown>[];
  runtime: Record<string, unknown>;
  recovery: ProteinRunRecovery;
  attempts: ProteinRunAttempt[];
  active_attempt: ProteinRunAttempt | Record<string, never>;
  action_policy: ProteinRunActionPolicy;
  watcher: ProteinRunWatcherHealth;
};

function workspaceQuery(workspaceId?: string): string {
  const params = new URLSearchParams();
  if (workspaceId) params.set('workspace_id', workspaceId);
  const query = params.toString();
  return query ? `?${query}` : '';
}

async function proteinRunApi<T>(path: string, init?: RequestInit): Promise<T> {
  const response = init
    ? await fetch(`${API_BASE}${path}`, init)
    : await fetch(`${API_BASE}${path}`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(String(payload.detail || `读取 Protein Run 失败 (${response.status})`));
  }
  return payload as T;
}

export async function listProteinRuns(workspaceId?: string): Promise<{
  workspace_id: string;
  count: number;
  total: number;
  runs: ProteinRunSummary[];
  generated_at: string;
  watcher: ProteinRunWatcherHealth;
}> {
  return proteinRunApi(`/api/protein-runs${workspaceQuery(workspaceId)}`);
}

export async function getProteinRun(runId: string, workspaceId?: string): Promise<ProteinRunDetail> {
  return proteinRunApi(
    `/api/protein-runs/${encodeURIComponent(runId)}${workspaceQuery(workspaceId)}`,
  );
}

export async function deleteProteinRun(
  runId: string,
  workspaceId?: string,
): Promise<{
  status: 'deleted';
  run_id: string;
  deleted_at: string;
  removed_output_count: number;
  removed_attempt_count: number;
}> {
  return proteinRunApi(
    `/api/protein-runs/${encodeURIComponent(runId)}${workspaceQuery(workspaceId)}`,
    { method: 'DELETE' },
  );
}

export type ProteinRunActionResponse = {
  status: string;
  idempotent_replay?: boolean;
  run_id: string;
  action: 'retry' | 'cancel';
  source_attempt_id?: string;
  attempt_id?: string;
  attempt?: Partial<ProteinRunAttempt>;
  cancel_status?: string;
  cancellation?: Record<string, unknown>;
};

function actionKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `run-action-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function proteinRunAction(
  runId: string,
  attemptId: string,
  action: 'retry' | 'cancel',
  workspaceId?: string,
  reason = '',
): Promise<ProteinRunActionResponse> {
  const query = workspaceQuery(workspaceId);
  return proteinRunApi(
    `/api/protein-runs/${encodeURIComponent(runId)}/attempts/${encodeURIComponent(attemptId)}/${action}${query}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idempotency_key: actionKey(), reason }),
    },
  );
}

export async function retryProteinRunAttempt(
  runId: string,
  attemptId: string,
  workspaceId?: string,
  reason = '',
): Promise<ProteinRunActionResponse> {
  return proteinRunAction(runId, attemptId, 'retry', workspaceId, reason);
}

export async function cancelProteinRunAttempt(
  runId: string,
  attemptId: string,
  workspaceId?: string,
  reason = '',
): Promise<ProteinRunActionResponse> {
  return proteinRunAction(runId, attemptId, 'cancel', workspaceId, reason);
}

export async function reconcileProteinRunAttempt(
  runId: string,
  attemptId: string,
  workspaceId?: string,
): Promise<{
  status: string;
  run_id: string;
  attempt_id: string;
  action: 'reconcile';
  issue_state?: string;
  retry_count?: number;
  error?: string;
}> {
  return proteinRunApi(
    `/api/protein-runs/${encodeURIComponent(runId)}/attempts/${encodeURIComponent(attemptId)}/reconcile${workspaceQuery(workspaceId)}`,
    { method: 'POST' },
  );
}

export function proteinRunArtifactUrl(url: string, workspaceId?: string): string {
  const normalized = String(url || '').trim();
  if (!normalized || !workspaceId) return normalized;
  const separator = normalized.includes('?') ? '&' : '?';
  return `${normalized}${separator}workspace_id=${encodeURIComponent(workspaceId)}`;
}
