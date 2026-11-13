import { expect, it } from 'vitest';
import type { SubagentItem } from '../api/subagents';
import { collaborationRunStatus, isFinishedCollaborationTask } from './collaborationTasks';
import { normalizeSubagentStatus } from './subagentTree';

it('does not let an assistant reply turn partial, cancelled or waiting work into success', () => {
  for (const status of ['partial','cancelled','blocked','failed','waiting_for_user']) {
    const item: Pick<SubagentItem, 'status' | 'lifecycle_status' | 'current_runtime_task'>={status:'completed',lifecycle_status:'completed',current_runtime_task:{status}};
    expect(collaborationRunStatus(item,'completed')).toBe(status==='waiting_for_user'?'paused':status);
    expect(collaborationRunStatus(item,'running')).toBe('running');
    if(status!=='waiting_for_user') expect(normalizeSubagentStatus(collaborationRunStatus(item,'completed'))).toBe('failed');
  }
  expect(isFinishedCollaborationTask('partial')).toBe(true);
  expect(collaborationRunStatus({status:'completed',current_runtime_task:{status:'partial',last_error:'task_cancelled'}},'completed')).toBe('cancelled');
});
