import { sanitizeVisibleMessages, type Message } from '../stores/session';
import { invalidateSessionGetCache, resetShortLivedGetCache, withShortLivedGetCache } from './cache';
import type {
  AsyncCompletionEvent,
  SessionDetail,
  SessionSummary,
  SessionUsageSummary,
} from './generated/protocol';

export type {
  AsyncCompletionEvent,
  SessionDetail,
  SessionSummary,
  SessionTaskRuntimeSummary,
  SessionUsageNode,
  SessionUsageSummary,
  TaskRuntimeEvent,
  TaskRuntimeItem,
  TokenUsage,
} from './generated/protocol';

export type SessionDetailView = Omit<SessionDetail, 'messages' | 'metadata'> & {
  metadata?: Record<string, any>;
  messages: Message[];
};

export const resetSessionRequestCache = resetShortLivedGetCache;

const API_BASE = '';

export async function compactSession(sessionId: string): Promise<{ status: string; tokens_saved: number; original_tokens: number; compressed_tokens: number }> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, { method: 'POST' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '上下文压缩失败');
  invalidateSessionGetCache(sessionId);
  return data;
}


export async function listAsyncCompletions(options: {
  sessionId?: string;
  unreadOnly?: boolean;
} = {}): Promise<{ events: AsyncCompletionEvent[]; unread_count: number }> {
  const params = new URLSearchParams();
  if (options.sessionId) params.set('session_id', options.sessionId);
  if (options.unreadOnly) params.set('unread_only', 'true');
  const query = params.toString();
  const resp = await fetch(`${API_BASE}/api/async-completions${query ? `?${query}` : ''}`);
  if (!resp.ok) throw new Error(`Async completions request failed: ${resp.status}`);
  const data = await resp.json();
  return {
    events: Array.isArray(data?.events) ? data.events : [],
    unread_count: Number(data?.unread_count || 0),
  };
}


export async function markAsyncCompletionRead(eventId: string): Promise<void> {
  const resp = await fetch(
    `${API_BASE}/api/async-completions/${encodeURIComponent(eventId)}/read`,
    { method: 'POST' },
  );
  if (!resp.ok) throw new Error(`Read async completion failed: ${resp.status}`);
}

export async function markAllAsyncCompletionsRead(): Promise<void> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch(`${API_BASE}/api/async-completions/read-all`, { method: 'POST', signal: controller.signal });
    if (!response.ok) throw new Error(`全部标记已读失败：${response.status}`);
  } catch (error) {
    if (controller.signal.aborted) throw new Error('标记已读超时，请重试（不会影响任务和结果）');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}


export async function markSessionAsyncCompletionsRead(sessionId: string): Promise<void> {
  const resp = await fetch(
    `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/async-completions/read`,
    { method: 'POST' },
  );
  if (!resp.ok) throw new Error(`Read session async completions failed: ${resp.status}`);
}


export async function markAsyncCompletionContinued(
  sessionId: string,
  identifier: string,
): Promise<void> {
  const resp = await fetch(
    `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/async-completions/${encodeURIComponent(identifier)}/continued`,
    { method: 'POST' },
  );
  if (!resp.ok) throw new Error(`Continue async completion failed: ${resp.status}`);
}


export async function getSessions(
  options: { archived?: boolean; signal?: AbortSignal } = {},
): Promise<SessionSummary[]> {
  const params = new URLSearchParams();
  if (options.archived) params.set('archived', 'true');
  const query = params.toString();
  const resp = await fetch(`${API_BASE}/api/sessions${query ? `?${query}` : ''}`, { signal: options.signal });
  if (!resp.ok) throw new Error(`Sessions request failed: ${resp.status}`);
  const data = await resp.json();
  return data.sessions || [];
}


export async function getSessionUsageSummary(
  options: { workspaceId?: string; includeArchived?: boolean } = {},
): Promise<SessionUsageSummary | null> {
  try {
    const params = new URLSearchParams();
    if (options.workspaceId) params.set('workspace_id', options.workspaceId);
    if (options.includeArchived !== undefined) {
      params.set('include_archived', options.includeArchived ? 'true' : 'false');
    }
    const query = params.toString();
    const resp = await fetch(`${API_BASE}/api/usage/sessions${query ? `?${query}` : ''}`);
    if (!resp.ok) return null;
    return await resp.json() as SessionUsageSummary;
  } catch (error) {
    console.error('Session usage summary error:', error);
    return null;
  }
}

export interface UsageActivityDay {
  date: string;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export async function getUsageActivity(): Promise<UsageActivityDay[]> {
  const offset = -new Date().getTimezoneOffset();
  const response = await fetch(`/api/usage/activity?include_archived=true&utc_offset_minutes=${offset}`);
  if (!response.ok) throw new Error(`每日活动读取失败：${response.status}`);
  const data = await response.json();
  return Array.isArray(data?.days) ? data.days : [];
}


export async function getSession(
  sessionId: string,
  options: { includeToolMessages?: boolean; bustCache?: boolean } = {},
): Promise<SessionDetailView | null> {
  try {
    const includeToolMessages = Boolean(options.includeToolMessages);
    const cacheKey = `session:${sessionId}:${includeToolMessages ? 'with-tools' : 'default'}`;
    const ttlMs = options.bustCache ? 0 : 800;
    return await withShortLivedGetCache(cacheKey, ttlMs, async () => {
      const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`);
      if (!resp.ok) return null;
      const data = await resp.json();
      return {
        ...data,
        messages: sanitizeVisibleMessages(data?.messages || [], { includeToolMessages }),
      } as SessionDetailView;
    });
  } catch (error) {
    console.error('Get session error:', error);
    return null;
  }
}


export async function rewindUserMessage(sessionId: string, messageId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/rewind`, { method: 'POST' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '消息编辑失败');
}

export async function forkSession(
  sessionId: string,
  messageId: string,
): Promise<SessionDetailView> {
  const resp = await fetch(
    `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/fork`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: messageId }),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '分支会话失败');
  return {
    ...data.session,
    messages: sanitizeVisibleMessages(data.session?.messages || []),
  } as SessionDetailView;
}


export async function deleteSession(sessionId: string): Promise<void> {
  invalidateSessionGetCache(sessionId);
  const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || '删除会话失败');
  }
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const statusResp = await fetch(
      `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/delete-status`,
    );
    if (!statusResp.ok) {
      throw new Error('无法确认会话删除状态');
    }
    const status = await statusResp.json().catch(() => ({}));
    if (!status.pending && !status.running) {
      if (status.error) throw new Error(String(status.error));
      invalidateSessionGetCache(sessionId);
      const persisted = await fetch(
        `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`,
        { cache: 'no-store' },
      );
      if (persisted.status !== 404) {
        throw new Error('会话删除未持久化，请刷新后重试');
      }
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 400));
  }
  throw new Error('会话删除仍在后台执行，请稍后刷新确认');
}


export async function archiveSession(sessionId: string): Promise<SessionSummary> {
  invalidateSessionGetCache(sessionId);
  const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: 'POST',
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '归档会话失败');
  return data.session as SessionSummary;
}


export async function unarchiveSession(sessionId: string): Promise<SessionSummary> {
  invalidateSessionGetCache(sessionId);
  const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/unarchive`, {
    method: 'POST',
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '取消归档会话失败');
  return data.session as SessionSummary;
}


export async function renameSession(sessionId: string, title: string): Promise<SessionSummary> {
  invalidateSessionGetCache(sessionId);
  const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '重命名会话失败');
  return data.session as SessionSummary;
}

export async function bindSessionToResearch(
  sessionId: string,
  researchId: string,
): Promise<SessionSummary> {
  invalidateSessionGetCache(sessionId);
  const resp = await fetch(
    `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/research`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ research_id: researchId }),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '绑定研究课题失败');
  return data.session as SessionSummary;
}

export async function unbindSessionFromResearch(sessionId: string): Promise<SessionSummary> {
  invalidateSessionGetCache(sessionId);
  const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/research`, {
    method: 'DELETE',
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '转换普通会话失败');
  return data.session as SessionSummary;
}
