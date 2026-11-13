/** Subagent conversations and session bootstrapping. */
import type { TeamWorkerCatalogEntry } from '../utils/teamRegistry';
import { invalidateSessionGetCache, withShortLivedGetCache } from './cache';
import type { SendMessageOptions } from './chat';

const API_BASE = '';

export async function controlWorkerExecution(sessionId: string, taskId: string,
  action: 'pause' | 'resume', limits: { tokens?: number; tool_calls?: number } = {}) {
  const response = await fetch(`/api/subagents/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}/control`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, ...limits }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '控制 Worker 失败');
  return data.task as Record<string, any>;
}

export async function getSideChats(parentSessionId: string): Promise<SubagentItem[]> {
  const resp = await fetch(`/api/sessions/${encodeURIComponent(parentSessionId)}/side-chats`, {cache:'no-store'});
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || '加载侧边聊天失败');
  return Array.isArray(data.side_chats) ? data.side_chats : [];
}

export async function injectSideChat(parentId: string, sideId: string): Promise<{status:string;mode:'guidance'|'context';message_count:number;duplicate:boolean}> {
  const resp = await fetch(`/api/sessions/${encodeURIComponent(parentId)}/side-chats/${encodeURIComponent(sideId)}/inject`, {method:'POST'});
  const data = await resp.json().catch(()=>({}));
  if (!resp.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '注入主会话失败');
  return data;
}
export async function askSideQuestion(
  parentSessionId: string,
  message: string,
  agentName = 'general',
  options: Pick<SendMessageOptions, 'output_format' | 'outcome_contract' | 'file_refs'> & { createOnly?: boolean; sourceMessageId?: string } = {},
): Promise<{
  status?: string;
  parent_session_id: string;
  session_id: string;
  agent_name: string;
  response?: string;
  outcome_status?: string;
  outcome_validation?: Record<string, any>;
  display_name?: string;
  last_response?: string;
  running?: boolean;
  pending_guidance_count?: number;
  guidance_worker_active?: boolean;
} | null> {
  const resp = await fetch(`${API_BASE}/api/subagents/side-question`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      parent_session_id: parentSessionId,
      message,
      agent_name: agentName,
      output_format: options.output_format,
      outcome_contract: options.outcome_contract,
      file_refs: options.file_refs,
      create_only: options.createOnly,
      source_message_id: options.sourceMessageId || '',
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = String(data?.detail || data?.error || '').trim();
    if (resp.status === 404 && detail === 'Not Found') {
      throw new Error('后端还没重启，旁路提问接口还不存在');
    }
    throw new Error(detail || '旁路提问失败');
  }

  return data;
}

export async function bootstrapSession(
  agentName: string,
  sessionId?: string,
  workdirName?: string,
): Promise<{ session_id: string; agent_name: string; workdir?: string; workdir_name?: string; workers_created: number }> {
  const resp = await fetch(`${API_BASE}/api/sessions/bootstrap`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      agent_name: agentName,
      session_id: sessionId,
      workdir_name: workdirName,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `Bootstrap failed: ${resp.status}`);
  }

  const data = await resp.json();
  const targetSessionId = String(data?.session_id || sessionId || '').trim();
  if (targetSessionId) {
    invalidateSessionGetCache(targetSessionId);
  }
  return data;
}

export type SubagentItem = {
  session_id: string;
  parent_session_id?: string;
  root_parent_session_id?: string;
  depth?: number;
  lineage?: string[];
  agent_name: string;
  display_name?: string;
  engine?: string;
  title: string;
  status: string;
  lifecycle_status?: 'available' | 'assigned' | 'running' | 'reporting' | 'paused' | 'blocked' | 'failed' | 'completed';
  created_at: number;
  message_count: number;
  last_message: string;
  tools_used?: string[];
  conversation_with?: string[];
  last_response?: string;
  context?: {
    message_count?: number;
    tool_call_count?: number;
    last_user?: string;
    last_assistant?: string;
  };
  contract?: {
    goal?: string;
    deliverable?: string;
    stop_when?: string;
    allowed_tools?: 'all' | string[];
  };
  deletable?: boolean;
  is_team_worker?: boolean;
  team_owner_session_id?: string;
  team_owner_display_name?: string;
  worker_name?: string;
  team_worker?: TeamWorkerCatalogEntry | null;
  resident_worker?: boolean;
  worker_role?: string;
  worker_preset?: string;
  worker_task_count?: number;
  worker_profile?: Record<string, any>;
  worker_visible?: boolean;
  worker_enabled?: boolean;
  current_runtime_task?: Record<string, any> | null;
  goal_ancestry?: string[];
  budget?: {
    limits?: { tokens?: number; tool_calls?: number; retries?: number };
    usage?: { tokens?: number; tool_calls?: number; retries?: number };
    state?: 'ok' | 'warning' | 'exhausted';
  };
  side_question?: boolean;
  source_parent_agent?: string;
};

export async function getSubagents(parentSessionId: string): Promise<SubagentItem[]> {
  try {
    return await withShortLivedGetCache(`subagents:${parentSessionId}`, 1200, async () => {
      const resp = await fetch(`${API_BASE}/api/subagents?parent_session_id=${encodeURIComponent(parentSessionId)}`);
      const data = await resp.json();
      return (data.subagents || []).filter((item: SubagentItem) => !item.side_question);
    });
  } catch (e) {
    console.error('Subagents error:', e);
    return [];
  }
}

export async function closeSessionSubagents(sessionId: string): Promise<{
  deleted_session_ids: string[];
  removed_worker_session_ids?: string[];
}> {
  const resp = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/subagents`, {
    method: 'DELETE',
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || '关闭子代理失败');
  }

  return resp.json();
}

export async function chatWithSubagent(
  parentSessionId: string,
  subagentSessionId: string,
  message: string,
  options: {
    integrateToParent?: boolean;
    output_format?: Record<string, any>;
    outcome_contract?: Record<string, any>;
    background?: boolean;
  } = {},
): Promise<{
  status?: string;
  session_id: string;
  agent_name: string;
  response?: string;
  outcome_status?: string;
  outcome_validation?: Record<string, any>;
  display_name?: string;
  engine?: string;
  tools_used?: string[];
  conversation_with?: string[];
  last_response?: string;
  context?: {
    message_count?: number;
    tool_call_count?: number;
    last_user?: string;
    last_assistant?: string;
  };
  contract?: {
    goal?: string;
    deliverable?: string;
    stop_when?: string;
    allowed_tools?: 'all' | string[];
  };
  integrated_to_parent?: boolean;
  parent_message_id?: string;
  integration_content?: string;
  running?: boolean;
  pending_guidance_count?: number;
  guidance_worker_active?: boolean;
}> {
  const resp = await fetch(`${API_BASE}/api/subagents/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      parent_session_id: parentSessionId,
      subagent_session_id: subagentSessionId,
      message,
      integrate_to_parent: options.integrateToParent,
      output_format: options.output_format,
      outcome_contract: options.outcome_contract,
      background: options.background,
    }),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || '与子代理对话失败');
  }

  return resp.json();
}
