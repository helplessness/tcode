import { describe, expect, it } from 'vitest';

import {
  researchActionDisplayIndexes,
  researchActionFromTool,
  researchSourceCategoryLabel,
} from './researchAction';


describe('researchActionFromTool', () => {
  it('accepts separate research record proposals and completed receipts', () => {
    for (const status of ['ready', 'completed'] as const) {
      const action = researchActionFromTool({tool_name:'research_workspace', arguments:{}, metadata:{research_action:{schema:'tcode.research-action.v1',kind:'research_step',status,title:'仅创建实验记录',details:{影响:'不启动计算'}}}});
      expect(action?.kind).toBe('research_step'); expect(action?.status).toBe(status);
    }
  });
  it('accepts only the versioned research action envelope', () => {
    const action = researchActionFromTool({
      tool_name: 'research_workspace',
      arguments: {},
      metadata: {
        research_action: {
          schema: 'tcode.research-action.v1',
          kind: 'candidate_confirmation',
          status: 'ready',
          title: '长程记忆 Agent',
        },
      },
    });

    expect(action?.kind).toBe('candidate_confirmation');
    expect(action?.title).toBe('长程记忆 Agent');
  });

  it('rejects unrelated tools and malformed actions', () => {
    expect(researchActionFromTool({
      tool_name: 'research_read',
      arguments: {},
      metadata: { research_action: { schema: 'tcode.research-action.v1' } },
    })).toBeNull();
    expect(researchActionFromTool({
      tool_name: 'research_workspace',
      arguments: {},
      metadata: {
        research_action: {
          schema: 'tcode.research-action.v0',
          kind: 'pack_download',
          status: 'ready',
          title: '包',
        },
      },
    })).toBeNull();
  });

  it('accepts durable commit receipts including cancellation', () => {
    const action = researchActionFromTool({
      tool_name: 'research_action_commit',
      arguments: {},
      metadata: {
        research_action: {
          schema: 'tcode.research-action.v1',
          kind: 'pack_build',
          status: 'cancelled',
          title: '长程记忆研究包',
          action_id: 'research-action-1',
        },
      },
    });

    expect(action?.status).toBe('cancelled');
    expect(action?.action_id).toBe('research-action-1');
  });

  it('accepts candidate rename and dismissal confirmation cards', () => {
    for (const kind of ['candidate_rename', 'candidate_dismissal'] as const) {
      const action = researchActionFromTool({
        tool_name: 'research_workspace',
        arguments: {},
        metadata: {
          research_action: {
            schema: 'tcode.research-action.v1',
            kind,
            status: 'ready',
            title: kind === 'candidate_rename' ? '重命名候选课题' : '忽略候选课题',
            candidate_id: 'candidate-1',
            action_id: `action-${kind}`,
          },
        },
      });
      expect(action?.kind).toBe(kind);
    }
  });

  it('uses user-facing labels for source categories', () => {
    expect(researchSourceCategoryLabel('message')).toBe('对话');
    expect(researchSourceCategoryLabel('agent')).toBe('子 Agent');
  });

  it('shows only the latest card for duplicate durable action receipts', () => {
    const duplicate = {
      tool_name: 'research_action_commit',
      arguments: {},
      metadata: {
        research_action: {
          schema: 'tcode.research-action.v1',
          kind: 'pack_download',
          status: 'completed',
          title: 'BetB ResearchPack',
          action_id: 'research-action-pack-1',
          pack_id: 'pack-1',
        },
      },
    } as const;
    const indexes = researchActionDisplayIndexes([
      duplicate,
      { tool_name: 'research_read', arguments: {} },
      duplicate,
    ]);

    expect(Array.from(indexes)).toEqual([2]);
  });
});

it('keeps the latest successful confirmation receipt and rejects failed receipts', async () => {
  const {researchActionReceipts}=await import('./researchAction');
  const base={schema:'tcode.research-action.v1',kind:'research_step',title:'关联假说与证据',action_id:'a'};
  const tools=[
    {tool_name:'research_workspace',arguments:{},state:'completed',metadata:{research_action:{...base,status:'ready'}}},
    {tool_name:'research_action_commit',arguments:{},state:'completed',metadata:{research_action:{...base,status:'completed'}}},
    {tool_name:'research_action_commit',arguments:{},state:'error',error:'失败',metadata:{research_action:{...base,status:'ready'}}},
  ];
  expect(researchActionReceipts(tools as any)).toHaveLength(1);
  expect(researchActionReceipts(tools as any)[0].status).toBe('completed');
});
