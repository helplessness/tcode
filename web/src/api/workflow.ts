import {
  invalidateWorkflowRunCache,
  invalidateWorkflowTemplateCache,
  withShortLivedGetCache,
} from './cache';

const API_BASE = '';

export type WorkflowStageTemplate = {
  stage_id: string;
  label: string;
  owner: string;
  call_type: string;
  description?: string;
  depends_on: string[];
  allowed_tools: string[];
  required_skills: string[];
  input_contract: Record<string, any>;
  output_contract: Record<string, any>;
  expected_artifacts: string[];
  success_checks: string[];
  validators: Record<string, any>[];
  retry_policy: Record<string, any>;
  timeout_policy: Record<string, any>;
  failure_policy: Record<string, any>;
  fallback_stage_ids: string[];
  review_gate: boolean;
  metadata: Record<string, any>;
};

export type WorkflowTemplate = {
  template_key: string;
  display_name: string;
  domain: string;
  description?: string;
  input_contract: Record<string, any>;
  output_contract: Record<string, any>;
  stage_defaults: Record<string, any>;
  stages: WorkflowStageTemplate[];
  metadata: Record<string, any>;
  enabled?: boolean;
};

export type WorkflowStageState = {
  diagnostic?: { category: string; title: string; message: string; suggestion: string };
  stage_id: string;
  label: string;
  owner: string;
  call_type: string;
  status: string;
  started_at?: number | null;
  completed_at?: number | null;
  artifact_refs: Record<string, any>;
  result_summary?: string;
  last_error?: string;
  attempt: number;
  draft_result: Record<string, any>;
  committed_result: Record<string, any>;
  validation_report: WorkflowValidationReport;
  idempotency_key?: string;
  metadata: Record<string, any>;
};

export type WorkflowValidationCheck = {
  validator: string;
  ok: boolean;
  code?: string;
  message?: string;
  evidence?: Record<string, any>;
  retryable?: boolean;
};

export type WorkflowValidationReport = {
  ok?: boolean | null;
  phase?: string;
  checks?: WorkflowValidationCheck[];
  summary?: string;
  diagnostic?: { category: string; title: string; message: string; suggestion: string };
};

export type WorkflowCandidate = {
  candidate_id: string;
  source_stage_id: string;
  status: string;
  rank?: number | null;
  score_card: Record<string, any>;
  artifact_refs: Record<string, any>;
  summary?: string;
  metadata: Record<string, any>;
};

export type WorkflowProposal = {
  proposal_id: string;
  proposal_type: 'candidate' | 'revision' | 'final_report' | string;
  source_stage_id: string;
  status: 'pending' | 'approved' | 'rejected' | string;
  proposed_by: string;
  payload: Record<string, any>;
  summary?: string;
  raw_marker?: string;
  metadata: Record<string, any>;
  created_at?: number;
  resolved_at?: number | null;
  resolved_by?: string;
  reason?: string;
};

export type WorkflowEvent = {
  event_id: string;
  kind: string;
  source: string;
  stage_id?: string;
  summary?: string;
  payload: Record<string, any>;
  created_at?: number;
};

export type WorkflowRun = {
  run_id: string;
  template_key: string;
  session_id: string;
  status: string;
  current_stage_id: string;
  input_payload: Record<string, any>;
  stage_states: WorkflowStageState[];
  candidates: WorkflowCandidate[];
  proposals: WorkflowProposal[];
  events: WorkflowEvent[];
  metadata: Record<string, any>;
  version: number;
  created_at?: number;
  updated_at?: number;
};

export type WorkflowScope = {
  session_id?: string;
  workdir_name?: string;
  workdir_path?: string;
};

export type WorkflowStageEvidence = {
  run_id: string;
  stage_id: string;
  stage_label: string;
  stage_status: string;
  evidence_state: 'verified' | 'missing' | 'pending';
  evidence: Array<Record<string, any>>;
  validation: { ok?: boolean | null; summary?: string; checks: WorkflowValidationCheck[] };
  artifacts: Record<string, any>;
  receipt: Record<string, any>;
  counts: { evidence: number; validation_checks: number; artifacts: number };
};

export type WorkflowDraft = {
  draft_id: string;
  owner_user_id: string;
  session_id: string;
  name: string;
  goal: string;
  status: 'editing' | 'confirmed' | 'running' | 'succeeded' | 'accepted' | 'saved' | 'published' | string;
  revision: number;
  model?: string;
  template: WorkflowTemplate;
  saved_template?: WorkflowTemplate;
  source_run_id?: string;
  created_at?: number;
  updated_at?: number;
};

async function workflowDraftRequest(path: string, init?: RequestInit) {
  const resp = await fetch(`${API_BASE}${path}`, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(workflowApiError(data, '临时 Workflow 操作失败'));
  return data;
}

function workflowScopeQuery(scope: WorkflowScope) {
  const search = new URLSearchParams();
  for (const key of ['session_id', 'workdir_name', 'workdir_path'] as const) {
    if (scope[key]) search.set(key, scope[key]!);
  }
  return search.toString();
}

export async function listWorkflowDrafts(scope: WorkflowScope = {}): Promise<{ drafts: WorkflowDraft[] }> {
  const query = workflowScopeQuery(scope);
  return workflowDraftRequest(`/api/workflow/drafts${query ? `?${query}` : ''}`);
}

export async function createWorkflowDraft(payload: WorkflowScope & {
  goal: string; model?: string; template?: Record<string, any>;
}): Promise<{ draft: WorkflowDraft }> {
  return workflowDraftRequest('/api/workflow/drafts', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

export async function updateWorkflowDraft(draftId: string, payload: WorkflowScope & {
  name: string; template: Record<string, any>;
}): Promise<{ draft: WorkflowDraft }> {
  return workflowDraftRequest(`/api/workflow/drafts/${encodeURIComponent(draftId)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

export async function confirmWorkflowDraft(draftId: string, payload: WorkflowScope & {
  input_payload?: Record<string, any>;
}): Promise<{ draft: WorkflowDraft; run: WorkflowRun; auto_continue_scheduled: boolean }> {
  invalidateWorkflowRunCache();
  return workflowDraftRequest(`/api/workflow/drafts/${encodeURIComponent(draftId)}/confirm`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

export async function restartWorkflowDraft(draftId: string, payload: WorkflowScope & {
  input_payload?: Record<string, any>;
}): Promise<{ draft: WorkflowDraft; run: WorkflowRun; auto_continue_scheduled: boolean }> {
  invalidateWorkflowRunCache();
  return workflowDraftRequest(`/api/workflow/drafts/${encodeURIComponent(draftId)}/restart`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

export async function deleteWorkflowDraft(draftId: string, scope: WorkflowScope = {}) {
  const query = workflowScopeQuery(scope);
  return workflowDraftRequest(`/api/workflow/drafts/${encodeURIComponent(draftId)}${query ? `?${query}` : ''}`, { method: 'DELETE' });
}

export async function saveWorkflowDraftAsTemplateDraft(draftId: string, payload: WorkflowScope & {
  template_key: string; display_name: string; description?: string;
}): Promise<{ draft: WorkflowDraft; template: WorkflowTemplate }> {
  return workflowDraftRequest(`/api/workflow/drafts/${encodeURIComponent(draftId)}/save-template-draft`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

export async function publishWorkflowDraft(draftId: string, payload: WorkflowScope & {
  template_key?: string; display_name?: string; overwrite?: boolean;
}): Promise<{ draft: WorkflowDraft; template: WorkflowTemplate; workflow_maturity?: 'experimental' | 'verified' }> {
  invalidateWorkflowTemplateCache();
  return workflowDraftRequest(`/api/workflow/drafts/${encodeURIComponent(draftId)}/publish`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

export type WorkflowApplicabilityAssessment = {
  verdict: 'applicable' | 'uncertain' | 'incompatible' | string;
  confidence: number;
  reason: string;
  workflow_summary?: string;
  use_when?: string[];
  do_not_use_when?: string[];
  matched_signals?: string[];
  gaps?: string[];
  source?: string;
  fingerprint: string;
  requires_confirmation?: boolean;
};

export class WorkflowApplicabilityConfirmationRequired extends Error {
  assessment: WorkflowApplicabilityAssessment;
  template: Record<string, any>;
  planHash: string;
  revision: number;

  constructor(message: string, assessment: WorkflowApplicabilityAssessment, template: Record<string, any>, planHash = '', revision = 1) {
    super(message);
    this.name = 'WorkflowApplicabilityConfirmationRequired';
    this.assessment = assessment;
    this.template = template;
    this.planHash = planHash;
    this.revision = revision;
  }
}

export async function getWorkflowTemplates(options: WorkflowScope & { includeDisabled?: boolean } = {}): Promise<{
  count: number;
  templates: WorkflowTemplate[];
}> {
  const includeDisabled = Boolean(options.includeDisabled);
  const search = new URLSearchParams();
  if (includeDisabled) search.set('include_disabled', 'true');
  for (const key of ['session_id', 'workdir_name', 'workdir_path'] as const) {
    if (options[key]) search.set(key, options[key]!);
  }
  const suffix = search.size ? `?${search.toString()}` : '';
  const cacheKey = `workflow:templates:${suffix}`;
  return withShortLivedGetCache(cacheKey, 0, async () => {
    const resp = await fetch(`${API_BASE}/api/workflow/templates${suffix}`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(workflowApiError(data, '获取 Workflow 失败'));
    }
    return {
      count: Number(data.count || 0),
      templates: Array.isArray(data.templates) ? data.templates : [],
    };
  });
}

export async function saveWorkflowTemplate(
  template: Record<string, any>,
  options: { overwrite?: boolean } = {},
): Promise<{ count: number; template: WorkflowTemplate }> {
  const resp = await fetch(`${API_BASE}/api/workflow/templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      template,
      overwrite: Boolean(options.overwrite ?? false),
    }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '保存 Workflow 失败'));
  }
  invalidateWorkflowTemplateCache();
  return {
    count: Number(data.count || 0),
    template: data.template as WorkflowTemplate,
  };
}

export async function deleteWorkflowTemplate(
  templateKey: string,
  options: { force?: boolean } = {},
): Promise<{ count: number; deleted: boolean; template_key: string; active_runs?: any[] }> {
  const suffix = options.force ? '?force=true' : '';
  const resp = await fetch(`${API_BASE}/api/workflow/templates/${encodeURIComponent(templateKey)}${suffix}`, {
    method: 'DELETE',
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '删除 Workflow 失败'));
  }
  invalidateWorkflowTemplateCache();
  return {
    count: Number(data.count || 0),
    deleted: Boolean(data.deleted),
    template_key: String(data.template_key || templateKey),
    active_runs: Array.isArray(data.active_runs) ? data.active_runs : [],
  };
}

function workflowApiError(data: any, fallback: string): string {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    if (detail.error === 'workflow_version_conflict') {
      return `流程运行已被更新，请刷新后重试（当前版本 ${detail.actual_version}）`;
    }
    if (typeof detail.message === 'string') return detail.message;
    if (typeof detail.error === 'string') return detail.error;
  }
  if (typeof data?.error === 'string') return data.error;
  return fallback;
}

export async function listWorkflowRuns(params: WorkflowScope & {
  template_key?: string;
  status?: string;
  sync?: boolean;
  auto_continue?: boolean;
} = {}): Promise<{
  count: number;
  workdir?: string;
  runs: WorkflowRun[];
}> {
  const search = new URLSearchParams();
  if (params.session_id) search.set('session_id', params.session_id);
  if (params.workdir_name) search.set('workdir_name', params.workdir_name);
  if (params.workdir_path) search.set('workdir_path', params.workdir_path);
  if (params.template_key) search.set('template_key', params.template_key);
  if (params.status) search.set('status', params.status);
  if (params.sync) search.set('sync', 'true');
  if (params.auto_continue) search.set('auto_continue', 'true');
  const cacheKey = `workflow:runs:${search.toString()}`;
  const cacheTtlMs = params.sync || params.auto_continue ? 0 : 1500;
  return withShortLivedGetCache(cacheKey, cacheTtlMs, async () => {
    const suffix = search.toString() ? `?${search.toString()}` : '';
    const resp = await fetch(`${API_BASE}/api/workflow/runs${suffix}`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(workflowApiError(data, '列出流程运行实例失败'));
    }
    return {
      count: Number(data.count || 0),
      workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
      runs: Array.isArray(data.runs) ? data.runs : [],
    };
  });
}

export async function createWorkflowRun(payload: WorkflowScope & {
  template_key: string;
  input_payload?: Record<string, any>;
  metadata?: Record<string, any>;
  run_id?: string;
}): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(`${API_BASE}/api/workflow/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data?.detail;
    if (
      resp.status === 409
      && detail?.error === 'workflow_applicability_confirmation_required'
      && detail?.assessment
    ) {
      throw new WorkflowApplicabilityConfirmationRequired(
        String(detail.message || '需要确认 Workflow 适用范围'),
        detail.assessment as WorkflowApplicabilityAssessment,
        (detail.template || {}) as Record<string, any>,
        String(detail.plan_hash || ''),
        Number(detail.revision || 1),
      );
    }
    throw new Error(workflowApiError(data, '创建流程运行实例失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function getWorkflowRun(runId: string, params: WorkflowScope & {
  sync?: boolean;
  auto_continue?: boolean;
} = {}): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const search = new URLSearchParams();
  if (params.session_id) search.set('session_id', params.session_id);
  if (params.workdir_name) search.set('workdir_name', params.workdir_name);
  if (params.workdir_path) search.set('workdir_path', params.workdir_path);
  if (params.sync) search.set('sync', 'true');
  if (params.auto_continue) search.set('auto_continue', 'true');
  const suffix = search.toString() ? `?${search.toString()}` : '';
  const resp = await fetch(`${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}${suffix}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '获取流程运行实例失败'));
  }
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function getWorkflowRunEvidence(
  runId: string,
  scope: WorkflowScope = {},
): Promise<{ run_id: string; stages: WorkflowStageEvidence[] }> {
  const query = workflowScopeQuery(scope);
  const resp = await fetch(`${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/evidence${query ? `?${query}` : ''}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(workflowApiError(data, '获取 Workflow 证据失败'));
  return { run_id: String(data.run_id || runId), stages: Array.isArray(data.stages) ? data.stages : [] };
}

export async function transitionWorkflowStage(
  runId: string,
  stageId: string,
  payload: WorkflowScope & {
    status: 'running' | 'committed' | 'failed' | string;
    expected_version?: number;
    result_summary?: string;
    artifact_refs?: Record<string, any>;
    error?: string;
    metadata?: Record<string, any>;
  },
): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/stages/${encodeURIComponent(stageId)}/transition`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '切换流程阶段失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function executeWorkflowStage(
  runId: string,
  stageId: string,
  payload: WorkflowScope & { expected_version?: number } = {},
): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/stages/${encodeURIComponent(stageId)}/execute`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '执行流程阶段失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function terminateWorkflowRun(
  runId: string,
  payload: WorkflowScope & {
    expected_version?: number;
    reason?: string;
    actor?: string;
  } = {},
): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/terminate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '终止流程运行实例失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function deleteWorkflowRun(
  runId: string,
  params: WorkflowScope & { expected_version?: number } = {},
): Promise<{
  deleted: boolean;
  run_id: string;
  status?: string;
  workdir?: string;
}> {
  const search = new URLSearchParams();
  if (params.session_id) search.set('session_id', params.session_id);
  if (params.workdir_name) search.set('workdir_name', params.workdir_name);
  if (params.workdir_path) search.set('workdir_path', params.workdir_path);
  if (params.expected_version !== undefined) {
    search.set('expected_version', String(params.expected_version));
  }
  const suffix = search.toString() ? `?${search.toString()}` : '';
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}${suffix}`,
    { method: 'DELETE' },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '删除流程运行实例失败'));
  }
  invalidateWorkflowRunCache();
  return {
    deleted: Boolean(data.deleted),
    run_id: String(data.run_id || runId),
    status: typeof data.status === 'string' ? data.status : undefined,
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
  };
}

export async function rerunWorkflowFromStage(
  runId: string,
  stageId: string,
  payload: WorkflowScope & {
    expected_version?: number;
    reason?: string;
    actor?: string;
    auto_continue?: boolean;
  } = {},
): Promise<{
  workdir?: string;
  run: WorkflowRun;
  auto_continue_scheduled: boolean;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/stages/${encodeURIComponent(stageId)}/rerun`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '从指定阶段重新运行失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
    auto_continue_scheduled: Boolean(data.auto_continue_scheduled),
  };
}

export async function attachWorkflowCandidate(
  runId: string,
  payload: WorkflowScope & {
    expected_version?: number;
    candidate: WorkflowCandidate | Record<string, any>;
  },
): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(`${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/candidates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '写入流程候选失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function approveWorkflowProposal(
  runId: string,
  proposalId: string,
  payload: WorkflowScope & {
    expected_version?: number;
    reason?: string;
    actor?: string;
  },
): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/proposals/${encodeURIComponent(proposalId)}/approve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '批准流程提案失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}

export async function rejectWorkflowProposal(
  runId: string,
  proposalId: string,
  payload: WorkflowScope & {
    expected_version?: number;
    reason?: string;
    actor?: string;
  },
): Promise<{
  workdir?: string;
  run: WorkflowRun;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workflow/runs/${encodeURIComponent(runId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workflowApiError(data, '拒绝流程提案失败'));
  }
  invalidateWorkflowRunCache();
  return {
    workdir: typeof data.workdir === 'string' ? data.workdir : undefined,
    run: data.run as WorkflowRun,
  };
}
