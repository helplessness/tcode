/** Symphony issue creation, updates and cancellation. */
import type { JobTimelineEntry, JobWorkspace } from './jobs';

const API_BASE = '';

export async function createSymphonyIssue(payload: {
  title?: string;
  description: string;
  project_slug?: string;
}): Promise<{ id: string; identifier?: string; title?: string; url?: string }> {
  const resp = await fetch(`${API_BASE}/api/symphony/issue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '创建 Symphony 任务失败');
  }
  return data;
}

export async function createSymphonyLocalIssue(payload: {
  title?: string;
  description: string;
}): Promise<{ id: string; identifier?: string; title?: string; status?: string; tracker?: string }> {
  const resp = await fetch(`${API_BASE}/api/symphony/local-issue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '创建本地 Symphony 任务失败');
  }
  return data;
}

export type SymphonyIssueCancelAction = {
  kind?: string;
  pid?: number;
  job_id?: string;
  process_id?: string;
  terminated?: boolean;
  reason?: string;
};

export type SymphonyIssueCancellationResult = {
  cancelled?: boolean;
  actions?: SymphonyIssueCancelAction[];
};

export type SymphonyLocalIssue = {
  id: string;
  identifier?: string;
  title?: string;
  description?: string;
  state?: string;
  tracker_status?: string;
  labels?: string[];
  created_at?: string;
  updated_at?: string;
  last_turn_id?: string;
  result_summary?: string;
  error_summary?: string;
  execution_channel?: string;
  claim_owner?: string;
  claim_expires_at?: string;
  fingerprint?: string;
  resume_data?: Record<string, any>;
  cancelled_at?: string;
  execution_pid?: number | null;
  execution_started_at?: string;
  cancel_actions?: SymphonyIssueCancelAction[];
  workspace?: JobWorkspace;
  timeline?: JobTimelineEntry[];
};

export async function getSymphonyLocalIssues(
  limit = 20,
  sessionId = '',
  filters: {
    sessionIds?: Iterable<string>;
    workspaceId?: string;
    query?: string;
  } = {},
): Promise<{
  count: number;
  total: number;
  filters?: {
    session_ids: string[];
    workspace_id: string;
  };
  issues: SymphonyLocalIssue[];
}> {
  try {
    const search = new URLSearchParams({
      limit: String(limit),
    });
    if (String(sessionId || '').trim()) {
      search.set('session_id', String(sessionId || '').trim());
    }
    const filterSessionIds = Array.from(filters.sessionIds || [])
      .map((item) => String(item || '').trim())
      .filter(Boolean);
    if (filterSessionIds.length > 0) {
      search.set('filter_session_ids', filterSessionIds.join(','));
    }
    if (String(filters.workspaceId || '').trim()) {
      search.set('workspace_id', String(filters.workspaceId || '').trim());
    }
    if (filters.query) search.set('q', filters.query);
    const resp = await fetch(`${API_BASE}/api/symphony/local-issues?${search.toString()}`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || '获取本地 Symphony 任务失败');
    }
    return {
      count: Number(data.count || 0),
      total: Number(data.total || 0),
      filters: data.filters && typeof data.filters === 'object'
        ? {
            session_ids: Array.isArray(data.filters.session_ids) ? data.filters.session_ids : [],
            workspace_id: String(data.filters.workspace_id || ''),
          }
        : undefined,
      issues: Array.isArray(data.issues) ? data.issues : [],
    };
  } catch (e) {
    console.error('Symphony local issues error:', e);
    return {
      count: 0,
      total: 0,
      issues: [],
    };
  }
}

export async function deleteSymphonyLocalIssue(payload: {
  id?: string;
  identifier?: string;
  forget_unresolved_submission?: boolean;
}): Promise<{
  status: string;
  issue?: SymphonyLocalIssue;
}> {
  const resp = await fetch(`${API_BASE}/api/symphony/local-issue`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '删除本地 Symphony 任务失败');
  }
  return data;
}

export type SymphonyLocalIssueBatchDeleteResult = {
  id: string;
  identifier: string;
  status: 'deleted' | 'already_deleted' | 'duplicate' | 'failed' | string;
  status_code?: number;
  error?: string;
  cancellation?: SymphonyIssueCancellationResult | null;
};

export async function batchDeleteSymphonyLocalIssues(
  issues: Array<{ id?: string; identifier?: string; forget_unresolved_submission?: boolean }>,
  cancelActive = true,
): Promise<{
  status: 'deleted' | 'partial' | 'failed' | string;
  requested_count: number;
  deleted_count: number;
  failed_count: number;
  results: SymphonyLocalIssueBatchDeleteResult[];
}> {
  const resp = await fetch(`${API_BASE}/api/symphony/local-issues/batch-delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ issues, cancel_active: cancelActive }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '批量删除本地 Symphony 任务失败');
  }
  return {
    status: String(data.status || ''),
    requested_count: Number(data.requested_count || 0),
    deleted_count: Number(data.deleted_count || 0),
    failed_count: Number(data.failed_count || 0),
    results: Array.isArray(data.results) ? data.results : [],
  };
}

export async function updateSymphonyLocalIssue(payload: {
  id?: string;
  identifier?: string;
  title?: string;
  description?: string;
  state?: string;
  labels?: string[];
}): Promise<{
  status: string;
  refresh_triggered?: boolean;
  issue?: SymphonyLocalIssue;
  cancellation?: SymphonyIssueCancellationResult | null;
}> {
  const resp = await fetch(`${API_BASE}/api/symphony/local-issue`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '更新本地 Symphony 任务失败');
  }
  return data;
}
