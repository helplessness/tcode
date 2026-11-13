import { describe, expect, it } from 'vitest';

import type { HumanReviewItem, SessionTaskRuntimeSummary, SymphonyLocalIssue, WorkflowRun } from '../api/client';
import type { Session } from '../stores/session';
import {
  activeRuntimeNotices,
  taskRuntimeNoticeGroups,
  taskRuntimeItemTitle,
  taskRuntimeSessionOutcomeBadge,
} from './sessionRuntimeNotices';

const summary: SessionTaskRuntimeSummary = {
  session_id: 'session-1',
  total: 6,
  active_total: 5,
  counts: {
    waiting_user: 1,
    waiting_review: 1,
    waiting_external: 1,
    blocked: 1,
    running: 1,
    completed: 1,
  },
  active_counts: {
    waiting_user: 1,
    waiting_review: 1,
    waiting_external: 1,
    blocked: 1,
    running: 1,
  },
  items: [
    ['user-1', 'waiting_user'],
    ['review-1', 'waiting_review'],
    ['external-1', 'waiting_external'],
    ['blocked-1', 'blocked'],
    ['running-1', 'running'],
  ].map(([runtime_id, status]) => ({
    runtime_id,
    runtime_kind: 'test',
    status,
    phase: status,
    updated_at: 1,
  })),
  recent_events: [],
  updated_at: 1,
};

describe('session runtime notices', () => {
  it('separates an available result awaiting review from a genuine block', () => {
    expect(taskRuntimeSessionOutcomeBadge({ ...summary, session_outcome_status: 'partial' })?.label).toBe('待验收');
    expect(taskRuntimeSessionOutcomeBadge({ ...summary, session_outcome_status: 'blocked' })?.label).toBe('已阻塞');
    expect(taskRuntimeSessionOutcomeBadge({ ...summary, session_outcome_status: 'completed' })?.label).toBe('已完成');
  });
  it('routes temporary workflow proposals to the temporary run instead of the published catalog', () => {
    const session = { session_id: 's', workspace_id: 'ws', messages: [], created_at: 1, updated_at: 1 } as Session;
    const run = { run_id: 'wf-test', session_id: 's', status: 'blocked', version: 3,
      proposals: [{ status: 'pending', proposal_type: 'revision' }],
      metadata: { created_from: 'temporary_workflow_draft', template_snapshot: { display_name: '临时验收流程' } },
    } as unknown as WorkflowRun;
    const notices = activeRuntimeNotices([session], 'ws', [run]);
    expect(notices[0].page).toBe('runtime-workflow');
    expect(notices[0].title).toBe('临时验收流程');
    expect(notices[0].revision).toBe('3');
  });
  it('keeps all actionable groups and their records visible', () => {
    const groups = taskRuntimeNoticeGroups(summary);

    expect(groups.map(group => group.status)).toEqual([
      'waiting_user',
      'waiting_review',
      'waiting_external',
      'blocked',
      'running',
    ]);
    expect(groups.map(group => group.items[0]?.runtime_id)).toEqual([
      'user-1',
      'review-1',
      'external-1',
      'blocked-1',
      'running-1',
    ]);
  });

  it('uses a stable title fallback for runtime records', () => {
    expect(taskRuntimeItemTitle({
      runtime_id: 'runtime-1',
      runtime_kind: 'mcp_job',
      status: 'running',
      phase: 'execute',
      tool_name: 'score',
      updated_at: 1,
    })).toBe('score');
  });

  it('shows the parent outcome when no actionable runtime badge remains', () => {
    const recoveredSummary: SessionTaskRuntimeSummary = {
      ...summary,
      session_outcome_status: 'completed',
      active_total: 0,
      active_counts: {
        waiting_user: 0,
        waiting_review: 0,
        waiting_external: 0,
        blocked: 0,
        running: 0,
      },
      resolved_counts: { blocked: 1 },
      items: [],
      resolved_items: [summary.items![3]],
    };

    expect(taskRuntimeSessionOutcomeBadge(recoveredSummary)?.label).toBe('已完成');
  });

  it('shows only actionable jobs, workflows and reviews in the current workspace', () => {
    const items = [
      ['job-1', 'mcp_job', 'waiting_external'],
      ['flow-1', 'workflow_run', 'running'],
      ['flow-review', 'workflow_run', 'waiting_review'],
      ['review-1', 'human_review', 'waiting_review'],
      ['done-1', 'mcp_job', 'completed'],
      ['tool-1', 'agent_tool', 'running'],
    ].map(([runtime_id, runtime_kind, status]) => ({ runtime_id, runtime_kind, status, phase: status, updated_at: 1 }));
    const session = (session_id: string, workspace_id: string): Session => ({
      session_id, workspace_id, messages: [], created_at: 1, updated_at: 1,
      task_runtime: { ...summary, items },
    });
    const notices = activeRuntimeNotices([
      session('s1', 'ws1'), session('s2', 'ws1'), session('s3', 'ws2'),
    ], 'ws1');
    expect(notices.map(item => `${item.kind}:${item.runtimeId}`)).toEqual([
      'review:flow-review', 'review:review-1', 'workflow:flow-1', 'async:job-1',
    ]);
    expect(notices.every(item => item.sessionId !== 's3')).toBe(true);
  });

  it('uses the live workflow, review and issue lists when available', () => {
    const session: Session = {
      session_id: 's1', workspace_id: 'ws1', messages: [], created_at: 1, updated_at: 1,
      task_runtime: { ...summary, items: [
        { runtime_id: 'stale-flow', runtime_kind: 'workflow_run', status: 'running', phase: 'running', updated_at: 1 },
        { runtime_id: 'stale-job', runtime_kind: 'mcp_job', status: 'waiting_external', phase: 'running', external_id: 'j2', updated_at: 1 },
      ] },
    };
    const runs = [
      { run_id: 'stale-flow', session_id: 's1', status: 'completed', template_key: 'old', proposals: [{ status: 'pending' }] },
      { run_id: 'live-flow', session_id: 's1', status: 'running', template_key: 'design' },
      { run_id: 'needs-review', session_id: 's1', status: 'review_pending', template_key: 'dock' },
    ] as WorkflowRun[];
    const reviews = [{ id: 'r1', status: 'pending_review', tool_server: 'gnina', tool_name: 'dock', tool_args: {}, review_data: { session_id: 's1' } }] as HumanReviewItem[];
    const issues = [
      { id: 'j1', state: 'running', title: '对接任务', workspace: { session_id: 's1' } },
      { id: 'j2', state: 'done', tracker_status: 'claimed', title: '已完成', workspace: { session_id: 's1' } },
    ] as SymphonyLocalIssue[];
    expect(activeRuntimeNotices([session], 'ws1', runs, reviews, issues).map(item => `${item.kind}:${item.externalId}`)).toEqual([
      'review:needs-review', 'review:r1', 'workflow:live-flow', 'async:j1',
    ]);
  });
});
