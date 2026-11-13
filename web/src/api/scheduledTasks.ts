/** Scheduled tasks, confirmations and notifications. */

const API_BASE = '';

export type ScheduleType = 'at' | 'delay' | 'daily' | 'weekly' | 'monthly' | 'weekdays' | 'cron';

export type ScheduledTask = {
  id: string;
  tenant_id: string;
  user_id: string;
  workspace_id: string;
  name: string;
  instruction: string;
  schedule_type: ScheduleType;
  schedule: Record<string, any>;
  timezone: string;
  status: 'active' | 'paused' | 'completed' | 'failed' | 'deleted';
  next_run_at?: number | null;
  next_run_at_iso?: string | null;
  next_run_at_local?: string | null;
  last_run_at?: number | null;
  last_status?: string | null;
  last_error?: string | null;
  source_session_id?: string;
  agent_name?: string;
  workdir?: string;
  created_via?: string;
  claimed?: boolean;
  created_at: number;
  updated_at: number;
};

export type ScheduledTaskRun = {
  id: string;
  task_id: string;
  scheduled_for: number;
  scheduled_for_iso?: string;
  trigger: string;
  status: 'running' | 'succeeded' | 'failed';
  output?: string;
  error?: string;
  started_at: number;
  started_at_iso?: string;
  finished_at?: number;
  finished_at_iso?: string;
};

export type ScheduledNotification = {
  id: string;
  task_id?: string;
  run_id?: string;
  kind: string;
  title: string;
  content: string;
  is_read: boolean;
  created_at: number;
  created_at_iso?: string;
  source_session_id?: string;
  execution_session_id?: string;
};

export type ScheduledTaskInput = {
  name?: string;
  instruction: string;
  schedule_type: ScheduleType;
  schedule: Record<string, any>;
  timezone: string;
  source_session_id?: string;
  workspace_id?: string;
  workdir?: string;
};

export type ScheduledTaskPreview = {
  status: 'requires_confirmation';
  requires_confirmation: true;
  confirmation_token: string;
  confirmation_expires_at: number;
  preview: ScheduledTaskInput & { next_run_at?: number; next_run_at_iso?: string; next_run_at_local?: string };
};

async function scheduledApi<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data?.detail || data?.error || '定时任务操作失败'));
  }
  return data as T;
}

export async function listScheduledTasks(admin = false): Promise<ScheduledTask[]> {
  const data = await scheduledApi<{ tasks: ScheduledTask[] }>(
    admin ? '/api/admin/scheduled-tasks' : '/api/scheduled-tasks',
  );
  return data.tasks || [];
}

export async function getScheduledTaskPreferences(): Promise<{ timezone: string; updated_at?: number | null }> {
  return scheduledApi('/api/scheduled-tasks/preferences');
}

export async function updateScheduledTaskPreferences(timezone: string): Promise<{ timezone: string; updated_at: number }> {
  return scheduledApi('/api/scheduled-tasks/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ timezone }),
  });
}

export async function prepareScheduledTask(input: ScheduledTaskInput): Promise<ScheduledTaskPreview> {
  return scheduledApi<ScheduledTaskPreview>('/api/scheduled-tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}

export async function confirmScheduledTask(confirmationToken: string): Promise<ScheduledTask> {
  const data = await scheduledApi<{ task: ScheduledTask }>('/api/scheduled-tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmation_token: confirmationToken }),
  });
  return data.task;
}

export async function updateScheduledTask(
  taskId: string,
  input: ScheduledTaskInput,
  admin = false,
): Promise<ScheduledTask> {
  const prefix = admin ? '/api/admin/scheduled-tasks' : '/api/scheduled-tasks';
  const data = await scheduledApi<{ task: ScheduledTask }>(`${prefix}/${encodeURIComponent(taskId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return data.task;
}

export async function deleteScheduledTask(taskId: string, admin = false): Promise<void> {
  const prefix = admin ? '/api/admin/scheduled-tasks' : '/api/scheduled-tasks';
  await scheduledApi(`${prefix}/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
}

export async function scheduledTaskAction(
  taskId: string,
  action: 'pause' | 'resume' | 'run' | 'retry',
  admin = false,
): Promise<{ task?: ScheduledTask; run_id?: string; status: string }> {
  const prefix = admin ? '/api/admin/scheduled-tasks' : '/api/scheduled-tasks';
  return scheduledApi(`${prefix}/${encodeURIComponent(taskId)}/${action}`, { method: 'POST' });
}

export async function listScheduledTaskRuns(taskId: string, admin = false): Promise<ScheduledTaskRun[]> {
  const prefix = admin ? '/api/admin/scheduled-tasks' : '/api/scheduled-tasks';
  const data = await scheduledApi<{ runs: ScheduledTaskRun[] }>(
    `${prefix}/${encodeURIComponent(taskId)}/runs`,
  );
  return data.runs || [];
}

export async function listScheduledNotifications(): Promise<{
  notifications: ScheduledNotification[];
  unread_count: number;
}> {
  return scheduledApi('/api/notifications');
}

export async function markScheduledNotificationRead(notificationId: string): Promise<void> {
  await scheduledApi(`/api/notifications/${encodeURIComponent(notificationId)}/read`, { method: 'POST' });
}

export async function markAllScheduledNotificationsRead(): Promise<void> {
  await scheduledApi('/api/notifications/read-all', { method: 'POST' });
}
