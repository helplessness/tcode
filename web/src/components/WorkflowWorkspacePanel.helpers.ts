import type { WorkflowProposal, WorkflowRun, WorkflowStageState } from '../api/client';

export type CandidateFieldRow = {
  id: string;
  key: string;
  value: string;
};

export type WorkflowDetailExpansionState = Record<string, boolean>;

export function workflowDetailExpansionKey(
  runId: string,
  section: 'stage' | 'candidate' | 'event',
  itemId: string,
) {
  return JSON.stringify([String(runId || ''), section, String(itemId || '')]);
}

export function workflowDetailIsExpanded(
  state: WorkflowDetailExpansionState,
  key: string,
  defaultExpanded = false,
) {
  return Object.prototype.hasOwnProperty.call(state, key) ? Boolean(state[key]) : defaultExpanded;
}

export function updateWorkflowDetailExpansion(
  state: WorkflowDetailExpansionState,
  key: string,
  expanded: boolean,
) {
  if (state[key] === expanded && Object.prototype.hasOwnProperty.call(state, key)) return state;
  return { ...state, [key]: expanded };
}

export function refreshOpenWorkflowRecord<T extends { runId: string; sessionId: string }>(
  opened: T | null,
  refreshed: T[],
  runId: string,
) {
  if (!opened || opened.runId !== runId) return opened;
  return refreshed.find(record => record.sessionId === opened.sessionId) || opened;
}

function sameJsonValue(left: unknown, right: unknown) {
  if (left === right) return true;
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function reuseUnchangedItems<T>(
  previous: T[],
  incoming: T[],
  itemKey: (item: T) => string,
) {
  const previousByKey = new Map(previous.map((item) => [itemKey(item), item]));
  return incoming.map((item) => {
    const oldItem = previousByKey.get(itemKey(item));
    return oldItem && sameJsonValue(oldItem, item) ? oldItem : item;
  });
}

/**
 * Polling returns fresh JSON objects every time. Reuse unchanged nested records so
 * Solid keeps their DOM nodes (and browser text selection/scroll anchors) intact.
 */
export function stabilizeWorkflowRuns(previous: WorkflowRun[], incoming: WorkflowRun[]) {
  const previousById = new Map(previous.map((run) => [run.run_id, run]));
  return incoming.map((run) => {
    const oldRun = previousById.get(run.run_id);
    if (!oldRun) return run;
    if (oldRun.version === run.version && oldRun.updated_at === run.updated_at) {
      return oldRun;
    }
    const stabilized: WorkflowRun = {
      ...run,
      stage_states: reuseUnchangedItems(
        oldRun.stage_states || [],
        run.stage_states || [],
        (stage) => stage.stage_id,
      ),
      candidates: reuseUnchangedItems(
        oldRun.candidates || [],
        run.candidates || [],
        (candidate) => candidate.candidate_id,
      ),
      proposals: reuseUnchangedItems(
        oldRun.proposals || [],
        run.proposals || [],
        (proposal) => proposal.proposal_id,
      ),
      events: reuseUnchangedItems(
        oldRun.events || [],
        run.events || [],
        (event) => event.event_id,
      ),
    };
    return stabilized;
  });
}

export function workflowInputPayloadFromDraft(value: string): Record<string, any> {
  const trimmed = String(value || '').trim();
  if (!trimmed) return {};
  if (!trimmed.startsWith('{')) {
    return { target_description: trimmed };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error('输入以 { 开头时必须是有效的 JSON 对象');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Workflow 输入 JSON 必须是对象');
  }
  return parsed as Record<string, any>;
}

function normalizeEpochMs(value?: number | null) {
  const numeric = Number(value || 0);
  if (!numeric) return 0;
  return numeric > 10_000_000_000 ? numeric : numeric * 1000;
}

export function formatTimestamp(value?: number | null) {
  const epochMs = normalizeEpochMs(value);
  if (!epochMs) return '-';
  return new Date(epochMs).toLocaleString();
}

export function statusClass(status?: string) {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'committed') {
    return 'border-[#238636]/30 bg-[#238636]/12 text-[#9ee6bb]';
  }
  if (normalized === 'running' || normalized === 'queued' || normalized === 'validating') {
    return 'border-[#1f6feb]/30 bg-[#1f6feb]/12 text-[#8ed5ff]';
  }
  if (normalized === 'waiting_external_job' || normalized === 'collecting' || normalized === 'review_pending') {
    return 'border-[#f1a02b]/30 bg-[#f1a02b]/12 text-[#ffe1b3]';
  }
  if (normalized === 'blocked' || normalized === 'failed' || normalized === 'error') {
    return 'border-[#da3633]/30 bg-[#da3633]/12 text-[#ffb6b0]';
  }
  return 'border-[#3e484f] bg-[#171c20] text-[#bdc8d1]';
}

export function stageStatusLabel(stage: WorkflowStageState) {
  return statusLabel(stage.status || 'pending');
}

export function statusLabel(status?: string) {
  const normalized = String(status || '').trim().toLowerCase();
  const labels: Record<string, string> = {
    pending: '待处理',
    running: '进行中',
    queued: '排队中',
    waiting_external_job: '等待异步任务',
    collecting: '收集中',
    review_pending: '等待审核',
    validating: '校验中',
    committed: '已提交',
    failed: '失败',
    error: '错误',
    blocked: '已阻塞',
    completed: '已完成',
    cancelled: '已取消',
    canceled: '已取消',
    terminated: '已终止',
    draft: '草稿',
  };
  return labels[normalized] || status || '待处理';
}

const WORKFLOW_HISTORY_MUTABLE_STATUSES = new Set([
  'blocked',
  'failed',
  'error',
  'completed',
  'cancelled',
  'canceled',
  'terminated',
]);

export function workflowRunAllowsHistoryMutation(run?: WorkflowRun | null) {
  if (!run) return false;
  const runStatusAllowsMutation = WORKFLOW_HISTORY_MUTABLE_STATUSES.has(
    String(run.status || '').trim().toLowerCase(),
  );
  if (!runStatusAllowsMutation) return false;
  const activeStatuses = new Set([
    'running',
    'validating',
    'waiting_external_job',
    'collecting',
    'review_pending',
    'revising',
    'recovering',
  ]);
  return !(Array.isArray(run.stage_states) ? run.stage_states : []).some((stage) => (
    activeStatuses.has(String(stage.status || '').trim().toLowerCase())
  ));
}

export function workflowStageCanRerun(
  run: WorkflowRun | null | undefined,
  stage: WorkflowStageState | null | undefined,
) {
  if (!run || !stage || !workflowRunAllowsHistoryMutation(run)) return false;
  const stageById = new Map(
    (Array.isArray(run.stage_states) ? run.stage_states : [])
      .map((item) => [item.stage_id, item] as const),
  );
  const dependencies = Array.isArray(stage.metadata?.depends_on)
    ? stage.metadata.depends_on.map((item: any) => String(item || '').trim()).filter(Boolean)
    : [];
  return dependencies.every((dependency: string) => (
    String(stageById.get(dependency)?.status || '').trim().toLowerCase() === 'committed'
  ));
}

export function resolveWorkflowRunSelection(
  currentRunId: string,
  runs: WorkflowRun[],
  options: { selectLatest?: boolean; selectionAtRequestStart?: string } = {},
) {
  const items = Array.isArray(runs) ? runs : [];
  const normalizedCurrent = String(currentRunId || '').trim();
  if (
    options.selectLatest
    && (options.selectionAtRequestStart === undefined
      || options.selectionAtRequestStart === currentRunId)
  ) return items[0]?.run_id || '';
  if (normalizedCurrent && items.some((item) => item.run_id === normalizedCurrent)) {
    return normalizedCurrent;
  }
  return items[0]?.run_id || '';
}

export function stageAsyncRefs(stage?: WorkflowStageState | null): Record<string, any>[] {
  const refs = stage?.metadata?.async_tool_refs;
  return Array.isArray(refs)
    ? refs.filter((item): item is Record<string, any> => item && typeof item === 'object')
    : [];
}

function stageAsyncHistoryRefs(stage?: WorkflowStageState | null): Record<string, any>[] {
  const completedRefs = stage?.metadata?.async_completed_refs;
  const rawRefs = Array.isArray(completedRefs) && completedRefs.length > 0
    ? completedRefs
    : stage?.metadata?.async_tool_refs;
  const refs = Array.isArray(rawRefs)
    ? rawRefs.filter((item): item is Record<string, any> => item && typeof item === 'object')
    : [];

  const seen = new Set<string>();
  return refs.filter((item) => {
    // 优先按任务唯一标识去重，避免 output 等字段不同导致同一任务重复出现
    const uniqueId = String(item.identifier || item.job_id || item.real_job_id || item.issue_id || '').trim();
    const key = uniqueId || [
      String(item.tool_name || ''),
      String(item.tool_server || ''),
      String(item.output || ''),
    ].join('::');
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function workflowAsyncHistory(run?: WorkflowRun | null) {
  const stages = Array.isArray(run?.stage_states) ? run!.stage_states : [];
  return stages
    .map((stage) => ({
      stage,
      refs: stageAsyncHistoryRefs(stage),
    }))
    .filter((item) => item.refs.length > 0);
}

export function stageInputGuardNotice(stage?: WorkflowStageState | null) {
  const guard = stage?.metadata?.llm_input_guard;
  if (!guard || typeof guard !== 'object') return '';
  if (!guard.oversize_handled) return '';

  const notes: string[] = ['检测到本阶段出现 LLM 输入过长，系统已自动保护'];
  if (guard.emergency_compaction) {
    notes.push('已执行紧急上下文压缩');
  }
  if (guard.degraded_no_tools) {
    notes.push('并以无工具降级模式重试');
  }
  return notes.join('；');
}

export function asyncRefLabel(ref: Record<string, any>) {
  return String(
    ref.identifier ||
    ref.job_id ||
    ref.real_job_id ||
    ref.issue_id ||
    ref.route_mode ||
    '异步任务'
  );
}

export function proposalTypeLabel(type?: string) {
  const normalized = String(type || '').trim().toLowerCase();
  const labels: Record<string, string> = {
    candidate: '候选提案',
    revision: '返工提案',
    final_report: '报告提案',
  };
  return labels[normalized] || type || '提案';
}

export function pendingWorkflowProposals(run?: WorkflowRun | null) {
  return (Array.isArray(run?.proposals) ? run!.proposals : [])
    .filter((proposal) => String(proposal.status || '').toLowerCase() === 'pending');
}

export function workflowFinalReport(run?: WorkflowRun | null) {
  if (!run) return '';
  const metadata = run.metadata && typeof run.metadata === 'object'
    ? run.metadata as Record<string, any>
    : {};

  const completion = metadata.presentation?.completion;
  if (completion?.source === 'stage_output_field') {
    const stageId = String(completion.stage_id || '').trim();
    const field = String(completion.field || '').trim();
    const stage = (Array.isArray(run.stage_states) ? run.stage_states : [])
      .find((item) => item.stage_id === stageId && item.status === 'committed');
    // The summary is capped at 1200 characters; the committed response is the
    // complete validated payload and remains available after a page reload.
    const raw = String(stage?.committed_result?.response || stage?.result_summary || '').trim();
    const fenced = raw.match(/^```(?:json)?\s*([\s\S]*)```\s*$/i);
    for (const candidate of [fenced?.[1] || '', raw].filter(Boolean)) {
      try {
        const payload = JSON.parse(candidate);
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) continue;
        const value = (payload as Record<string, unknown>)[field];
        if (typeof value === 'string' && value.trim()) return value.trim();
      } catch {
        // Invalid display output must not leak internal JSON as a fallback.
      }
    }
    return '';
  }

  const direct = String(metadata.final_report || '').trim();
  if (direct) return direct;

  const proposals = Array.isArray(run.proposals) ? run.proposals : [];
  for (let i = proposals.length - 1; i >= 0; i -= 1) {
    const proposal = proposals[i];
    const type = String(proposal?.proposal_type || '').trim().toLowerCase();
    const status = String(proposal?.status || '').trim().toLowerCase();
    if (type !== 'final_report' || status !== 'approved') continue;
    const payload = proposal.payload && typeof proposal.payload === 'object'
      ? proposal.payload as Record<string, any>
      : {};
    const text = String(payload.final_report || payload.summary || payload.report || '').trim();
    if (text) return text;
  }

  const committedStages = (Array.isArray(run.stage_states) ? run.stage_states : [])
    .filter((stage) => String(stage.status || '').toLowerCase() === 'committed');
  const lastResponse = String(
    committedStages[committedStages.length - 1]?.committed_result?.response || '',
  ).trim();
  if (lastResponse) {
    const marker = lastResponse.match(/\[workflow:final_report\](\{[\s\S]*?\})\[\/workflow:final_report\]/i);
    if (marker?.[1]) {
      try {
        const payload = JSON.parse(marker[1]);
        const report = String(payload?.final_report || '').trim();
        if (report) return report;
      } catch {
        // Fall through to the complete committed response.
      }
    }
    return lastResponse;
  }

  return '';
}

export function workflowRunNeedsSync(run?: WorkflowRun | null) {
  if (!run) return false;
  const runStatus = String(run.status || '').trim().toLowerCase();
  const activeRunStatuses = new Set([
    'pending',
    'running',
    'queued',
    'waiting_external_job',
    'collecting',
    'revising',
    'recovering',
    'validating',
  ]);
  if (activeRunStatuses.has(runStatus)) return true;

  return (Array.isArray(run.stage_states) ? run.stage_states : []).some((stage) => {
    const stageStatus = String(stage.status || '').trim().toLowerCase();
    return activeRunStatuses.has(stageStatus);
  });
}

export function latestWorkflowEvents(run?: WorkflowRun | null) {
  return (Array.isArray(run?.events) ? run!.events : []).slice().reverse();
}

export function proposalPreview(proposal: WorkflowProposal) {
  const summary = String(proposal.summary || '').trim();
  if (summary) return summary;
  const payload = proposal.payload || {};
  if (proposal.proposal_type === 'candidate') {
    const candidate = typeof payload.candidate === 'object' && payload.candidate
      ? payload.candidate as Record<string, any>
      : payload;
    return String(candidate.summary || candidate.description || candidate.candidate_id || '候选提案');
  }
  if (proposal.proposal_type === 'final_report') {
    return String(payload.summary || payload.final_report || payload.report || '最终报告提案');
  }
  return String(payload.summary || payload.reason || payload.required_revision || '流程返工提案');
}

export function formatList(values: string[]) {
  const normalized = (Array.isArray(values) ? values : [])
    .map((item) => String(item || '').trim())
    .filter(Boolean);
  return normalized.length > 0 ? normalized.join(' · ') : '-';
}

export function createCandidateFieldRow(key = '', value = ''): CandidateFieldRow {
  return {
    id: `candidate-row-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    key,
    value,
  };
}

export function normalizeOptionalNumber(value: string) {
  const text = String(value || '').trim();
  if (!text) return undefined;
  const numeric = Number(text);
  return Number.isFinite(numeric) ? numeric : undefined;
}

function coerceCandidateValue(value: string) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (/^(true|false)$/i.test(text)) {
    return text.toLowerCase() === 'true';
  }
  const numeric = Number(text);
  if (Number.isFinite(numeric) && /^-?\d+(\.\d+)?$/.test(text)) {
    return numeric;
  }
  return text;
}

export function buildCandidateFieldObject(
  rows: CandidateFieldRow[],
  options: { coerceValues?: boolean } = {},
) {
  const payload: Record<string, any> = {};
  for (const row of Array.isArray(rows) ? rows : []) {
    const key = String(row.key || '').trim();
    const value = String(row.value || '').trim();
    if (!key || !value) continue;
    payload[key] = options.coerceValues ? coerceCandidateValue(value) : value;
  }
  return payload;
}

export function suggestCandidateId(run: WorkflowRun | null | undefined) {
  if (!run) return '';
  return `${run.run_id}-cand-${(run.candidates?.length || 0) + 1}`;
}
