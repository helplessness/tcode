import { describe, expect, it } from 'vitest';
import { statementConditions, supportReason, SUPPORT_LABELS } from './ontologySupport';

describe('ontology support declarations', () => {
  it('preserves typed conditions and source metadata boundaries', () => {
    expect(statementConditions([{key:'pH',value:'7'},{key:'temperature',value:'"37 °C"'},{key:'verified',value:'true'}])).toEqual({pH:7,temperature:'37 °C',verified:true});
    expect(()=>statementConditions([{key:'pH',value:'null'}])).toThrow('空值');
    expect(()=>statementConditions([{key:'evidence_basis',value:'experimental'}])).toThrow('证据要求');
    expect(()=>statementConditions([{key:'pH',value:'7'},{key:'pH',value:'8'}])).toThrow('重复');
  });
  it('does not label structured support as scientific proof', () => {
    expect(SUPPORT_LABELS.supported).toContain('仅限结构化');
    expect(supportReason('required_conditions_missing')).toContain('不能假定');
    expect(supportReason('future_reason')).toContain('需要人工核对');
  });
});
