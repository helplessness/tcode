import { withShortLivedGetCache } from './cache';
import type { JobWorkspace } from './client';

const API_BASE = '';

export type HumanReviewConfig = {
  enabled: boolean;
  mode: string;
  required_tools: string[];
  auto_review_rules: Record<string, any>;
};

export type HumanReviewData = {
  session_id?: string;
  parent_session_id?: string;
  workdir?: string;
  trigger?: string;
  proxy_source?: string;
  review_decision?: Record<string, any>;
  workspace?: JobWorkspace;
  timeline_ref?: Record<string, any>;
  [key: string]: any;
};

export type HumanReviewItem = {
  id: string;
  identifier?: string;
  task_id?: string;
  tool_server: string;
  tool_name: string;
  tool_args: Record<string, any>;
  reason?: string;
  review_data?: HumanReviewData;
  status: string;
  created_at?: string;
  updated_at?: string;
  reviewer?: string | null;
  reviewed_at?: string | null;
  comments?: string | null;
  rejection_reason?: string;
  approved_tool_args_override?: Record<string, any> | null;
  resume_instruction?: string | null;
  execution_disposition?: string;
  resume_status?: string;
  resume_result?: Record<string, any>;
};

export async function getHumanReviewConfig(): Promise<HumanReviewConfig> {
  try {
    return await withShortLivedGetCache('human-review-config', 5000, async () => {
      const resp = await fetch(`${API_BASE}/api/human-review/config`);
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail || '获取人工审核配置失败');
      }
      return {
        enabled: Boolean(data.enabled),
        mode: String(data.mode || 'disabled'),
        required_tools: Array.isArray(data.required_tools) ? data.required_tools : [],
        auto_review_rules: data.auto_review_rules && typeof data.auto_review_rules === 'object'
          ? data.auto_review_rules
          : {},
      };
    });
  } catch (e) {
    console.error('Human review config error:', e);
    return {
      enabled: false,
      mode: 'disabled',
      required_tools: [],
      auto_review_rules: {},
    };
  }
}

export async function getHumanReviewQueue(status = 'pending_review', limit = 50, target?: { reviewId?: string; reviewIdentifier?: string; sessionId?: string }): Promise<{
  count: number;
  total: number;
  reviews: HumanReviewItem[];
}> {
  try {
    const search = new URLSearchParams({ status, limit: String(limit) });
    if (target?.reviewId?.trim()) search.set('review_id', target.reviewId.trim());
    if (target?.reviewIdentifier?.trim()) search.set('review_identifier', target.reviewIdentifier.trim());
    if (target?.sessionId?.trim()) search.set('session_id', target.sessionId.trim());
    const resp = await fetch(`${API_BASE}/api/human-review/queue?${search}`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || '获取人工审核队列失败');
    }
    if (!Array.isArray(data.reviews)) throw new Error('审核记录响应无效，请重试');
    return {
      count: Number(data.count || 0),
      total: Number(data.total || 0),
      reviews: Array.isArray(data.reviews) ? data.reviews : [],
    };
  } catch (e) {
    console.error('Human review queue error:', e);
    throw e;
  }
}

export async function approveHumanReview(payload: {
  issue_id?: string;
  issue_identifier?: string;
  comments?: string;
  approved_tool_args_override?: Record<string, any>;
  resume_after_approval?: boolean;
  resume_instruction?: string;
  apply_feedback_before_execution?: boolean;
}): Promise<{
  status: string;
  message?: string;
  review?: HumanReviewItem;
  resume?: Record<string, any>;
}> {
  const resp = await fetch(`${API_BASE}/api/human-review/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '通过人工审核失败');
  }
  return data;
}

export async function rejectHumanReview(payload: {
  issue_id?: string;
  issue_identifier?: string;
  reason: string;
  comments?: string;
  resume_after_rejection?: boolean;
  resume_instruction?: string;
}): Promise<{
  status: string;
  message?: string;
  review?: HumanReviewItem;
  resume?: Record<string, any>;
}> {
  const resp = await fetch(`${API_BASE}/api/human-review/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '驳回人工审核失败');
  }
  return data;
}

export async function deleteHumanReview(payload: {
  issue_id?: string;
  issue_identifier?: string;
}): Promise<{
  status: string;
  message?: string;
  review?: HumanReviewItem;
}> {
  const resp = await fetch(`${API_BASE}/api/human-review/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '删除人工审核记录失败');
  }
  return data;
}
