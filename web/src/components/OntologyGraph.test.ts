import { describe, expect, it } from 'vitest';
import { conflictText, relationshipColor, graphCompletenessText, readableLocator, readablePredicate } from './OntologyGraph';

describe('ontology graph human-readable details', () => {
  it('renders known and unknown predicates without opaque formatting', () => {
    expect(readablePredicate('has_participant')).toBe('参与者');
    expect(readablePredicate('candidate_relation')).toBe('candidate relation');
  });

  it('renders page and line ranges for evidence', () => {
    expect(readableLocator({ page: 1, line_start: 1, line_end: 32 })).toBe('第 1 页 · 第 1–32 行');
    expect(readableLocator({})).toBe('');
  });
});


describe('graph completeness feedback', () => {
  it('distinguishes empty, complete and partial results', () => {
    expect(graphCompletenessText()).toBe('');
    expect(graphCompletenessText({status:'not_found',truncated:false})).toBe('未找到匹配实体');
    expect(graphCompletenessText({status:'complete',truncated:false})).toBe('当前范围已完整展示');
    expect(graphCompletenessText({status:'truncated',truncated:true})).toContain('部分实体未显示');
    expect(graphCompletenessText({status:'truncated',truncated:true,edges_truncated:true})).toContain('关系达到展示上限');
  });
});


describe('condition-aware conflict display', () => {
  it('distinguishes opposition, differing conditions, uncertainty and source disputes', () => {
    expect(conflictText({conflict:true,conflict_categories:['same_conditions']})).toContain('同记录条件下相反');
    expect(conflictText({conflict:false,conflict_categories:['different_conditions']})).toBe('实验条件不同');
    expect(conflictText({conflict:false,conflict_categories:['unknown_conditions','source_dispute']})).toContain('条件不足');
    expect(conflictText({conflict:false,conflict_categories:['unchecked']})).toContain('尚未核对完整');
    expect(conflictText({conflict:false})).toBe('本次范围未发现相反主张');
    expect(relationshipColor({scope:'public',conflict:true})).not.toBe(relationshipColor({scope:'public',conflict:false,conflict_categories:['different_conditions']}));
  });
});
