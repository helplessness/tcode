/** Workspace creation, deletion, recovery and purge. */

const API_BASE = '';

export type WorkdirItem = {
  name: string;
  path: string;
  created_at?: number;
  updated_at?: number;
};

export type WorkdirListResponse = {
  root: string;
  template_root: string;
  max_workspaces_per_user: number;
  remaining_workspace_slots: number;
  workdirs: WorkdirItem[];
};

function workdirApiError(detail: unknown, fallback: string): string {
  const message = String(detail || '').trim();
  if (!message) return fallback;
  if (message === 'workdir is soft-deleted; restore it before reuse') {
    return '同名 Workspace 位于回收站，请点击“恢复”后继续使用。';
  }
  if (message === 'workdir already exists') {
    return '同名 Workspace 已存在，请直接选择该目录。';
  }
  if (message.startsWith('workspace limit reached:')) {
    return '已达到 Workspace 数量上限，请先删除或永久清理一个 Workspace。';
  }
  if (message.startsWith('workdir cleanup is incomplete')) {
    return '同名 Workspace 尚未清理完成，请先在回收站中恢复或继续永久删除。';
  }
  return message;
}

export async function getWorkdirs(): Promise<WorkdirListResponse> {
  const resp = await fetch(`${API_BASE}/api/workdirs`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '加载工作目录失败');
  }
  const workdirs = Array.isArray(data.workdirs) ? data.workdirs : [];
  const configuredMax = Number(data.max_workspaces_per_user);
  const maxWorkspaces = Number.isFinite(configuredMax) && configuredMax >= 1
    ? configuredMax
    : 5;
  const configuredRemaining = Number(data.remaining_workspace_slots);
  const remainingSlots = Number.isFinite(configuredRemaining) && configuredRemaining >= 0
    ? configuredRemaining
    : Math.max(0, maxWorkspaces - workdirs.length);
  return {
    root: String(data.root || ''),
    template_root: String(data.template_root || ''),
    max_workspaces_per_user: maxWorkspaces,
    remaining_workspace_slots: remainingSlots,
    workdirs,
  };
}

export async function createWorkdir(name: string): Promise<{ name: string; path: string; warning?: string }> {
  const resp = await fetch(`${API_BASE}/api/workdirs`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ name }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(workdirApiError(data.detail, '创建工作目录失败'));
  }
  return data.workdir || {};
}

export type DeleteWorkdirResult = {
  status: string;
  workspace_id: string;
  files_preserved: boolean;
  sessions?: {
    quiesced_session_ids?: string[];
    warnings?: Record<string, string>;
  };
  sandbox?: Record<string, unknown>;
};

export async function deleteWorkdir(name: string): Promise<DeleteWorkdirResult> {
  const resp = await fetch(`${API_BASE}/api/workdirs/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '删除工作目录失败');
  }
  return data as DeleteWorkdirResult;
}

export type DeletedWorkdirItem = {
  workspace_id: string;
  status: 'deleting' | 'deleted' | 'purging' | 'purge_failed' | 'purged';
  last_error?: string;
  deleted_at?: number;
  purge_started_at?: number;
  updated_at?: number;
  files_present?: boolean;
  residual_files?: boolean;
  purge_running?: boolean;
};

export async function getDeletedWorkdirs(): Promise<DeletedWorkdirItem[]> {
  const resp = await fetch(`${API_BASE}/api/workdirs/deleted`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '读取 Workspace 回收站失败');
  }
  return Array.isArray(data.workdirs) ? data.workdirs : [];
}

export async function restoreWorkdir(name: string): Promise<Record<string, unknown>> {
  const resp = await fetch(
    `${API_BASE}/api/workdirs/${encodeURIComponent(name)}/restore`,
    { method: 'POST' },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '恢复 Workspace 失败');
  }
  return data;
}

export async function purgeWorkdir(name: string): Promise<Record<string, unknown>> {
  const resp = await fetch(
    `${API_BASE}/api/workdirs/${encodeURIComponent(name)}/purge`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_workspace_id: name }),
    },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '永久删除 Workspace 失败');
  }
  return data;
}

export async function getWorkdirPurgeStatus(name: string): Promise<{
  status: string;
  running: boolean;
  error?: string;
}> {
  const resp = await fetch(
    `${API_BASE}/api/workdirs/${encodeURIComponent(name)}/purge`,
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || '读取 Workspace 清理状态失败');
  }
  return {
    status: String(data.status || ''),
    running: Boolean(data.running),
    error: String(data.error || ''),
  };
}
