import { describe, expect, it } from 'vitest';

import type { WorkflowRun } from '../api/client';
import {
  resolveWorkflowRunSelection,
  refreshOpenWorkflowRecord,
  stabilizeWorkflowRuns,
  updateWorkflowDetailExpansion,
  workflowDetailExpansionKey,
  workflowDetailIsExpanded,
  workflowStageCanRerun,
  workflowRunAllowsHistoryMutation,
  workflowRunNeedsSync,
  workflowInputPayloadFromDraft,
  workflowFinalReport,
} from './WorkflowWorkspacePanel.helpers';

function workflowRun(status: string, stageStatus = 'committed'): WorkflowRun {
  return {
    run_id: `run-${status}`,
    template_key: 'protein-design-loop',
    session_id: 'session-1',
    status,
    current_stage_id: 'stage-1',
    input_payload: {},
    stage_states: [
      {
        stage_id: 'stage-1',
        label: 'Stage 1',
        owner: 'worker',
        call_type: 'agent',
        status: stageStatus,
        artifact_refs: {},
        attempt: 1,
        draft_result: {},
        committed_result: {},
        validation_report: {},
        metadata: {},
      },
    ],
    candidates: [],
    proposals: [],
    events: [],
    metadata: {},
    version: 1,
  };
}

describe('workflowRunNeedsSync', () => {
  it('polls runs that can still change from background execution', () => {
    expect(workflowRunNeedsSync(workflowRun('running'))).toBe(true);
    expect(workflowRunNeedsSync(workflowRun('queued'))).toBe(true);
    expect(workflowRunNeedsSync(workflowRun('waiting_external_job'))).toBe(true);
    expect(workflowRunNeedsSync(workflowRun('completed', 'collecting'))).toBe(true);
  });

  it('does not poll user-action or inactive workflow states forever', () => {
    expect(workflowRunNeedsSync(workflowRun('blocked'))).toBe(false);
    expect(workflowRunNeedsSync(workflowRun('draft'))).toBe(false);
    expect(workflowRunNeedsSync(workflowRun('review_pending'))).toBe(false);
    expect(workflowRunNeedsSync(workflowRun('completed'))).toBe(false);
  });
});

describe('workflowInputPayloadFromDraft', () => {
  it('keeps natural language as target_description', () => {
    expect(workflowInputPayloadFromDraft('分析这个目标')).toEqual({
      target_description: '分析这个目标',
    });
  });

  it('passes a structured JSON object through as input_payload', () => {
    const payload = workflowInputPayloadFromDraft(JSON.stringify({
      reactants: [{ smiles: 'CCO' }],
      products: [{ smiles: 'CC=O' }],
    }));
    expect(payload).toEqual({
      reactants: [{ smiles: 'CCO' }],
      products: [{ smiles: 'CC=O' }],
    });
  });

  it('rejects malformed JSON instead of silently treating it as prose', () => {
    expect(() => workflowInputPayloadFromDraft('{"reactants":')).toThrow(
      '输入以 { 开头时必须是有效的 JSON 对象',
    );
  });
});

describe('workflowFinalReport', () => {
  it('extracts the clean report from a committed final-report marker', () => {
    const run = workflowRun('completed');
    const report = '## 最终报告\n\n这里是完整正文。';
    run.stage_states[0].committed_result = {
      response: `阶段说明\n[workflow:final_report]${JSON.stringify({ summary: '摘要', final_report: report })}[/workflow:final_report]`,
    };

    expect(workflowFinalReport(run)).toBe(report);
  });

  it('extracts only the configured user summary from internal stage JSON', () => {
    const run = workflowRun('completed');
    run.stage_states[0].stage_id = 'critical_report';
    const response = [
      '```json',
      JSON.stringify({
        internal_receipt: { artifact: '07_final_report.json', detail: 'x'.repeat(1800) },
        user_summary: '## 候选酶结论\n\n优先考虑候选 A。',
      }),
      '```',
    ].join('\n');
    run.stage_states[0].result_summary = response.slice(0, 1200);
    run.stage_states[0].committed_result = { response };
    run.metadata.presentation = {
      stage_narration: 'status_only',
      completion: {
        source: 'stage_output_field',
        stage_id: 'critical_report',
        field: 'user_summary',
      },
    };

    const report = workflowFinalReport(run);

    expect(report).toContain('候选酶结论');
    expect(report).not.toContain('internal_receipt');
    expect(report).not.toContain('07_final_report.json');

    const requestedJson = '```json\n{"enzymes":["候选 A"]}\n```';
    run.stage_states[0].committed_result.response = '```json\n'
      + JSON.stringify({ user_summary: requestedJson }) + '\n```';
    expect(workflowFinalReport(run)).toBe(requestedJson);

    run.stage_states[0].committed_result.response = JSON.stringify({
      user_summary: { internal: 'secret' },
    });
    expect(workflowFinalReport(run)).toBe('');

    run.stage_states[0].status = 'validating';
    run.stage_states[0].committed_result.response = response;
    expect(workflowFinalReport(run)).toBe('');
  });
});

describe('resolveWorkflowRunSelection', () => {
  it('keeps the user-selected terminal run while another run is active', () => {
    const running = workflowRun('running');
    const completed = workflowRun('completed');

    expect(
      resolveWorkflowRunSelection(completed.run_id, [running, completed]),
    ).toBe(completed.run_id);
  });

  it('falls back only when the selected run disappeared', () => {
    const running = workflowRun('running');

    expect(resolveWorkflowRunSelection('deleted-run', [running])).toBe(running.run_id);
  });

  it('does not replace a manual selection made during a select-latest refresh', () => {
    const running = workflowRun('running');
    const completed = workflowRun('completed');

    expect(resolveWorkflowRunSelection(completed.run_id, [running, completed], {
      selectLatest: true,
      selectionAtRequestStart: '',
    })).toBe(completed.run_id);
  });
});

describe('workflowRunAllowsHistoryMutation', () => {
  it('allows delete and rerun only after execution is inactive', () => {
    expect(workflowRunAllowsHistoryMutation(workflowRun('terminated'))).toBe(true);
    expect(workflowRunAllowsHistoryMutation(workflowRun('completed'))).toBe(true);
    expect(workflowRunAllowsHistoryMutation(workflowRun('blocked'))).toBe(true);
    expect(workflowRunAllowsHistoryMutation(workflowRun('running'))).toBe(false);
    expect(workflowRunAllowsHistoryMutation(workflowRun('queued'))).toBe(false);
  });

  it('only enables a rerun checkpoint whose dependencies are committed', () => {
    const run = workflowRun('terminated', 'failed');
    const first = run.stage_states[0];
    const downstream = {
      ...first,
      stage_id: 'stage-2',
      status: 'pending',
      metadata: { depends_on: ['stage-1'] },
    };
    run.stage_states.push(downstream);

    expect(workflowRunAllowsHistoryMutation(run)).toBe(true);
    expect(workflowStageCanRerun(run, first)).toBe(true);
    expect(workflowStageCanRerun(run, downstream)).toBe(false);
  });
});

describe('workflow polling UI stability', () => {
  it('keeps the opened execution record visible across a temporarily empty refresh', () => {
    const opened = { runId: 'run-1', sessionId: 'agent-1', messages: ['old'] };
    expect(refreshOpenWorkflowRecord(opened, [], 'run-1')).toBe(opened);

    const updated = { ...opened, messages: ['new'] };
    expect(refreshOpenWorkflowRecord(opened, [updated], 'run-1')).toBe(updated);
  });

  it('keeps an explicit details choice across refreshes, including closing a default-open failure', () => {
    const key = workflowDetailExpansionKey('run-1', 'stage', 'stage-1');
    expect(workflowDetailIsExpanded({}, key, true)).toBe(true);

    const collapsed = updateWorkflowDetailExpansion({}, key, false);
    expect(workflowDetailIsExpanded(collapsed, key, true)).toBe(false);

    const expanded = updateWorkflowDetailExpansion(collapsed, key, true);
    expect(workflowDetailIsExpanded(expanded, key)).toBe(true);
  });

  it('reuses unchanged nested records when polling updates only the run version', () => {
    const previous = workflowRun('running');
    const unchanged = JSON.parse(JSON.stringify(previous)) as WorkflowRun;
    expect(stabilizeWorkflowRuns([previous], [unchanged])[0]).toBe(previous);

    const incoming = JSON.parse(JSON.stringify(previous)) as WorkflowRun;
    incoming.version += 1;

    const [stabilized] = stabilizeWorkflowRuns([previous], [incoming]);
    expect(stabilized).not.toBe(previous);
    expect(stabilized.stage_states[0]).toBe(previous.stage_states[0]);
  });
});
