import type { SubagentItem, TeamEvent } from '../api/client';
export const isFinishedCollaborationTask = (status?: string) => ['completed', 'partial', 'done', 'cancelled', 'terminated'].includes(String(status || '').toLowerCase());
export function collaborationRunStatus(item: Pick<SubagentItem, 'current_runtime_task' | 'lifecycle_status' | 'status'>, liveStatus?: string): string {
  if (item.current_runtime_task?.status === 'paused') return 'paused';
  if (item.current_runtime_task?.status === 'pause_requested') return 'pause_requested';
  if (liveStatus === 'running') return 'running';
  if (item.current_runtime_task?.last_error === 'task_cancelled') return 'cancelled';
  const taskStatus = item.current_runtime_task?.status;
  if (['running', 'reviewing', 'revising'].includes(taskStatus)) return 'running';
  if (['partial', 'cancelled', 'blocked', 'failed', 'waiting_for_user', 'awaiting_input'].includes(taskStatus)) {
    return ['waiting_for_user', 'awaiting_input'].includes(taskStatus) ? 'paused' : taskStatus;
  }
  return liveStatus || item.lifecycle_status || item.status || 'idle';
}
export const isWorkflowExecutionSession = (item: Pick<SubagentItem, 'session_id' | 'title' | 'display_name'>) =>
  String(item.session_id || '').startsWith('workflow-agent-')
  || /^Workflow\s*·/.test(String(item.title || item.display_name || ''));
export function collaborationTaskTitle(item: SubagentItem, goal?: string) {
  if (goal?.trim()) return goal.trim();
  return String(item.title || item.display_name || item.agent_name || '执行任务')
    .replace(new RegExp(`\\s*·\\s*${item.agent_name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`), '');
}

export function mergeCollaborationRecords(
  teamId: string,
  channelEvents: TeamEvent[],
  subagents: SubagentItem[],
): TeamEvent[] {
  const recordedSessions = new Set(
    channelEvents.map(event => String(event.payload?.session_id || event.payload?.task_id || '')).filter(Boolean),
  );
  const delegated = subagents
    .filter(item => !isWorkflowExecutionSession(item) && !recordedSessions.has(item.session_id))
    .map((item): TeamEvent => {
      const status = String(item.lifecycle_status || item.status || '').toLowerCase();
      const type = ['failed', 'error'].includes(status)
        ? 'task_failed'
        : isFinishedCollaborationTask(status)
          ? 'task_done'
          : 'task_assigned';
      const goal = collaborationTaskTitle(item, item.contract?.goal);
      return {
        id: `subagent:${item.session_id}`,
        team_id: teamId,
        type,
        source: item.source_parent_agent || '主代理',
        target: item.display_name || item.agent_name || '子代理',
        payload: {
          message: goal,
          result: item.last_response || item.last_message || '',
          session_id: item.session_id,
        },
        created_at: Number(item.created_at || 0),
      };
    });

  return [...channelEvents, ...delegated].sort((a, b) => b.created_at - a.created_at);
}
