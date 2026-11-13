/** Kanban cards and dismissal requests. */
import type { HumanReviewItem } from './humanReview';
import type { JobWorkspace } from './jobs';

const API_BASE = '';

export type KanbanStatus = 'todo' | 'in_progress' | 'completed' | 'failed' | 'cancelled';

export type KanbanCard = {
  id: string;
  source_type: 'todo' | 'subagent' | 'async' | string;
  sources?: string[];
  title: string;
  status: KanbanStatus;
  agent: string;
  workspace_id: string;
  session_id?: string;
  root_session_id?: string;
  async_task_id?: string;
  issue_identifier?: string;
  tracker_status?: string;
  execution_channel?: string;
  claim_owner?: string;
  workspace?: JobWorkspace;
  human_review?: HumanReviewItem;
  current_step?: string;
  progress_percent: number;
  progress_label: string;
  created_at?: string;
  updated_at?: string;
  result_summary?: string;
  error_summary?: string;
};

export type KanbanResponse = {
  tenant_id: string;
  user_id: string;
  filters?: {
    workspace_id: string;
    session_id: string;
  };
  poll_interval_seconds: number;
  counts: Record<KanbanStatus, number>;
  cards: KanbanCard[];
};

export async function getKanban(filters: {
  workspaceId?: string;
  sessionId?: string;
} = {}): Promise<KanbanResponse> {
  const search = new URLSearchParams();
  if (String(filters.workspaceId || '').trim()) {
    search.set('workspace_id', String(filters.workspaceId || '').trim());
  }
  if (String(filters.sessionId || '').trim()) {
    search.set('session_id', String(filters.sessionId || '').trim());
  }
  const suffix = search.size > 0 ? `?${search.toString()}` : '';
  const resp = await fetch(`${API_BASE}/api/kanban${suffix}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || '获取任务看板失败'));
  }
  return data as KanbanResponse;
}

export async function deleteKanbanCard(cardId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/api/kanban/cards/${encodeURIComponent(cardId)}`, {
    method: 'DELETE',
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || '删除任务卡片失败'));
  }
}
