import { describe, expect, it } from 'vitest';

import { groupResearchKnowledgeIssues } from './researchKnowledgeDisplay';

describe('research knowledge issue display', () => {
  it('groups repeated technical issues into one readable action', () => {
    const groups = groupResearchKnowledgeIssues([
      { severity: 'warning', code: 'knowledge_link_stale', message: '关联对象需要复核。', link_id: 'link-1' },
      { severity: 'warning', code: 'knowledge_link_stale', message: '关联对象需要复核。', link_id: 'link-2' },
    ], [
      { link_id: 'link-1', object_type: 'wiki_claim', object_id: 'claim-1', metadata: { statement: '结论 A' } },
      { link_id: 'link-2', object_type: 'wiki_claim', object_id: 'claim-2', metadata: { statement: '结论 B' } },
    ] as any);

    expect(groups).toHaveLength(1);
    expect(groups[0].title).toBe('旧来源关系待整理');
    expect(groups[0].count).toBe(2);
    expect(groups[0].sources).toEqual(['结论 A', '结论 B']);
  });

  it('explains missing sources without exposing only an internal code', () => {
    const [group] = groupResearchKnowledgeIssues([
      {
        severity: 'error',
        code: 'knowledge_object_missing',
        message: '关联的知识对象不存在。',
        object_type: 'paper',
        object_id: 'paper-demo',
      },
    ]);

    expect(group.title).toBe('来源对象不存在');
    expect(group.action).toContain('自动修复来源');
    expect(group.sources).toEqual(['论文（名称暂不可用）']);
  });
});

it('shows a missing export dependency as a readable reason and recovery action',()=>{
  const [issue]=groupResearchKnowledgeIssues([{severity:'error',code:'dependency_evidence_missing',message:''}]);
  expect(issue.title).toBe('研究经历引用的证据不存在');
  expect(issue.description).toContain('不存在');
  expect(issue.action).toContain('恢复');
  expect(issue.description).not.toContain('dependency_evidence_missing');
});
