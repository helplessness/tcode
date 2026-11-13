import { describe, expect, it } from 'vitest';
import { normalizeQuestionOptions, questionAnswerValue, workflowQuestionSummary } from './questionPresentation';

describe('general Question answer contract', () => {
  it('accepts array, JSON and legacy options', () => {
    expect(normalizeQuestionOptions([' A ', 'B', ''])).toEqual(['A', 'B']);
    expect(normalizeQuestionOptions('["A, B", "C"]')).toEqual(['A, B', 'C']);
    expect(normalizeQuestionOptions('A, B')).toEqual(['A', 'B']);
    expect(normalizeQuestionOptions(null)).toEqual([]);
  });
  it('preserves single answers and safely encodes multi-choice labels with commas', () => {
    expect(questionAnswerValue(['A, B', 'C'], ['A, B'], '', false)).toBe('A, B');
    expect(questionAnswerValue(['A, B', 'C'], ['A, B', 'C'], '', true)).toBe('1, 2');
    expect(questionAnswerValue(['A'], [], ' 自定义内容 ', true)).toBe('自定义内容');
    expect(questionAnswerValue([], [], '', false)).toBe('');
  });
});

describe('workflow question presentation', () => {
  it.each([
    ['workflow_applicability_confirmation', 'workflow_applicability'],
    ['workflow_auto_route_confirmation', 'applicability'],
  ])('reads the existing %s assessment without another model call', (kind, field) => {
    const result = workflowQuestionSummary({
      question: '完整说明', metadata: { kind, [field]: {
        verdict: 'uncertain', reason: '尚缺反应物', gaps: ['输入不完整'], workflow_summary: '筛选酶',
      } },
    });
    expect(result).toMatchObject({ reason: '尚缺反应物', gaps: ['输入不完整'] });
    expect(result?.title).toContain('无法确认');
    expect(result?.title).not.toContain('不匹配');
  });

  it('distinguishes incompatibility from insufficient evidence', () => {
    expect(workflowQuestionSummary({ question: '是否继续？', metadata: { kind: 'workflow_applicability_confirmation',
      workflow_applicability: { verdict: 'incompatible', reason: '这是销售分析，不是酶探索' },
    } })?.title).toContain('不匹配');
  });

  it('does not turn general questions or final acceptance into preflight cards', () => {
    expect(workflowQuestionSummary({ question: '请选择底物' })).toBeNull();
    expect(workflowQuestionSummary({ question: '是否验收？', metadata: { kind: 'outcome_confirmation' } })).toBeNull();
  });

  it('falls back to the actual question when no structured reason is available', () => {
    expect(workflowQuestionSummary({ question: '请确认这个流程', metadata: {
      kind: 'workflow_auto_route_confirmation', applicability: {},
    } })?.reason).toBe('请确认这个流程');
  });

  it('does not repeat the same explanation as a limitation', () => {
    expect(workflowQuestionSummary({ question: '是否继续？', metadata: {
      kind: 'workflow_applicability_confirmation', workflow_applicability: {
        reason: '目标不明确', gaps: ['目标不明确', '缺少底物', '缺少底物'],
      },
    } })?.gaps).toEqual(['缺少底物']);
  });
});
