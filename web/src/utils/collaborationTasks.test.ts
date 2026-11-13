import {describe, expect, it} from 'vitest';
import {collaborationRunStatus, collaborationTaskTitle, isFinishedCollaborationTask, isWorkflowExecutionSession, mergeCollaborationRecords} from './collaborationTasks';
import {isCollaborationPage, studioNavigationPages, studioPageFromHash} from './studioLayout';
import type {SubagentItem} from '../api/client';
describe('collaboration entry and task summaries', () => {
  it('shows acknowledged pauses and pending safe pauses over stale live running events', () => {
    for (const status of ['paused', 'pause_requested']) {
      expect(collaborationRunStatus({status:'busy',current_runtime_task:{status}}, 'running')).toBe(status);
    }
  });
  it('keeps runtime review visible after the worker model has replied', () => {
    for (const status of ['running', 'reviewing', 'revising']) {
      const item: Parameters<typeof collaborationRunStatus>[0] = {
        status: 'completed',
        lifecycle_status: 'reporting',
        current_runtime_task: {status},
      };
      expect(collaborationRunStatus(item, 'completed')).toBe('running');
      expect(isFinishedCollaborationTask(collaborationRunStatus(item, 'completed'))).toBe(false);
    }
  });
  it('keeps legacy links but exposes only one entry', () => {
    expect(studioNavigationPages.filter(page => isCollaborationPage(page.id)).map(page => page.label)).toEqual(['协作']);
    for(const page of ['sidechat','subagents','team']) expect(isCollaborationPage(studioPageFromHash(`#/${page}`))).toBe(true);
  });
  it('keeps failures and running work visible while history is collapsed', () => {
    for(const status of ['failed','running','busy','pending','idle',undefined]) expect(isFinishedCollaborationTask(status)).toBe(false);
    for(const status of ['completed','done','cancelled','terminated']) expect(isFinishedCollaborationTask(status)).toBe(true);
  });
  it('labels the task before implementation details without dropping its goal', () => {
    const item = {title:'结合位点分析 · worker.a',agent_name:'worker.a'} as SubagentItem;
    expect(collaborationTaskTitle(item)).toBe('结合位点分析');
    expect(collaborationTaskTitle(item,' 检查配体附近残基 ')).toBe('检查配体附近残基');
  });
  it('includes ordinary subagent delegations in collaboration records without duplicates', () => {
    const subagents = [
      {session_id:'done-1',agent_name:'reviewer',title:'检查实现',status:'completed',created_at:20},
      {session_id:'run-1',agent_name:'builder',title:'继续开发',status:'running',created_at:30},
    ] as SubagentItem[];
    const recorded = [{
      id:'existing',team_id:'root',type:'task_done',source:'主代理',target:'reviewer',
      payload:{session_id:'done-1',message:'已有记录'},created_at:10,
    }];
    const events = mergeCollaborationRecords('root', recorded, subagents);
    expect(events.map(event => event.id)).toEqual(['subagent:run-1', 'existing']);
    expect(events[0]).toMatchObject({type:'task_assigned',target:'builder',payload:{message:'继续开发'}});
  });
  it('keeps workflow execution sessions out of global collaboration', () => {
    const workflow = {session_id:'workflow-agent-run-1-stage-a-worker',title:'Workflow · 阶段 A',display_name:'Workflow · 阶段 A',agent_name:'worker',status:'completed',created_at:20} as SubagentItem;
    expect(isWorkflowExecutionSession(workflow)).toBe(true);
    expect(mergeCollaborationRecords('root', [], [workflow])).toEqual([]);
  });
});
