/** Team workers, peer settings and conversations. */
import type { TeamWorkerCatalogEntry } from '../utils/teamRegistry';
import { withShortLivedGetCache } from './cache';
import type { SendMessageOptions } from './chat';

const API_BASE = '';

// ─── Team API ────────────────────────────────────────────────────────────────

export type WorkerItem = {
  session_id: string;
  team_id: string;
  name: string;
  role: string;
  preset: string;
  status: string; // 'idle' | 'running' | 'done' | 'failed'
  created_at: number;
  task_count: number;
  last_task?: string;
  profile?: Record<string, any>;
  team_worker?: TeamWorkerCatalogEntry | null;
  resident_worker?: boolean;
  visible?: boolean;
  enabled?: boolean;
};

export type TeamEvent = {
  id: string;
  team_id: string;
  type: string;
  target?: string;
  source: string;
  payload: Record<string, any>;
  created_at: number;
};

export type TeamPeerSettings = {
  team_id: string;
  enabled: boolean;
  enabled_default: boolean;
  allowed_reasons: string[];
  max_messages_per_pair_per_session: number;
  round_timeout_seconds: number;
};

export type TeamPeerThread = {
  request_id: string;
  team_id: string;
  sender_worker: string;
  target_worker: string;
  reason: string;
  question: string;
  status: string;
  created_at: number;
  updated_at: number;
  answered_at?: number;
  timeout_at?: number;
  round_index: number;
  max_rounds: number;
  response?: {
    summary?: string;
    confidence?: number;
    next_action?: string;
    blockers?: string[];
  };
};

export async function getTeamWorkers(teamId: string): Promise<WorkerItem[]> {
  try {
    return await withShortLivedGetCache(`team-workers:${teamId}`, 1200, async () => {
      const resp = await fetch(`${API_BASE}/api/team/${encodeURIComponent(teamId)}/workers`);
      if (!resp.ok) return [];
      const data = await resp.json();
      return (data.workers ?? []).map((worker: any) => ({
        ...worker,
        preset: worker.preset || worker.permission_preset || '',
      }));
    });
  } catch (e) {
    console.error('Get team workers error:', e);
    return [];
  }
}

export async function getTeamEvents(teamId: string, limit = 100): Promise<TeamEvent[]> {
  try {
    const resp = await fetch(
      `${API_BASE}/api/team/${encodeURIComponent(teamId)}/events?limit=${limit}`
    );
    if (!resp.ok) return [];
    const data = await resp.json();
    return (data.events ?? []).map((event: any) => ({
      id: event.id || event.event_id || '',
      team_id: event.team_id || teamId,
      type: event.type || event.event_type || 'message',
      source: event.source || '',
      target: event.target || '',
      payload: event.payload || {
        ...(event.metadata || {}),
        message: event.content || '',
      },
      created_at: event.created_at || event.timestamp || 0,
    }));
  } catch (e) {
    console.error('Get team events error:', e);
    return [];
  }
}

export async function getTeamPeerSettings(teamId: string): Promise<TeamPeerSettings | null> {
  try {
    const resp = await fetch(`${API_BASE}/api/team/${encodeURIComponent(teamId)}/peer-settings`);
    if (!resp.ok) return null;
    return resp.json();
  } catch (e) {
    console.error('Get team peer settings error:', e);
    return null;
  }
}

export async function updateTeamPeerSettings(
  teamId: string,
  enabled: boolean
): Promise<TeamPeerSettings | null> {
  try {
    const resp = await fetch(`${API_BASE}/api/team/${encodeURIComponent(teamId)}/peer-settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!resp.ok) return null;
    return resp.json();
  } catch (e) {
    console.error('Update team peer settings error:', e);
    return null;
  }
}

export async function getTeamPeerThreads(teamId: string): Promise<TeamPeerThread[]> {
  try {
    const resp = await fetch(`${API_BASE}/api/team/${encodeURIComponent(teamId)}/peer-threads`);
    if (!resp.ok) return [];
    const data = await resp.json();
    return data.threads ?? [];
  } catch (e) {
    console.error('Get team peer threads error:', e);
    return [];
  }
}

export async function teamChat(
  teamId: string,
  workerName: string,
  message: string,
  options: Pick<SendMessageOptions, 'output_format' | 'outcome_contract'> = {},
): Promise<{
  session_id: string;
  worker_name: string;
  response?: string;
  status?: string;
  outcome_status?: string;
  outcome_validation?: Record<string, any>;
  running?: boolean;
  pending_guidance_count?: number;
  guidance_worker_active?: boolean;
} | null> {
  const resp = await fetch(`${API_BASE}/api/team/${encodeURIComponent(teamId)}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      worker_name: workerName,
      message,
      output_format: options.output_format,
      outcome_contract: options.outcome_contract,
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data?.detail || data?.error || '与团队工作代理对话失败'));
  }

  return data;
}
