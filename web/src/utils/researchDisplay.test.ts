import {expect,it} from 'vitest';
import {researchStatusLabel,researchFieldLabel,researchDetailRows,researchValueText,userFacingTitle} from './userFacingLabels';
it('never falls back to raw enum codes and keeps unknown status explicit',()=>{
  expect(researchStatusLabel('under_review')).toBe('核对中');
  expect(researchStatusLabel('new_server_state')).toBe('状态未说明');
  expect(researchStatusLabel('model_inference')).toBe('模型推断');
  expect(researchFieldLabel('source_event_id')).toBe('补充信息');
});
it('keeps scientific identifiers and numbers but replaces platform identifiers',()=>{
  expect(userFacingTitle('hyp-abc…123','假说未命名')).toBe('假说未命名');
  expect(researchValueText('exp-abc123')).not.toContain('exp-');
  expect(researchValueText('10.1038/example','doi')).toBe('10.1038/example');
  expect(researchValueText('https://doi.org/10.1038/example','source_path')).toBe('https://doi.org/10.1038/example');
  expect(researchValueText('/private/work/output/result.csv','source_path')).toBe('result.csv');
  expect(researchValueText(0,'score')).toBe('0');
  expect(researchValueText(false)).toBe('否');
  expect(researchValueText('active','title')).toBe('active');
  expect(researchValueText('new_status','状态')).toBe('含义尚未说明');
});
it('projects nested records into readable bounded rows without raw IDs or field names',()=>{
  const record={experiment_id:'exp-123',status:'completed',metrics:{score:0.9,custom_metric:2},input_artifacts:[{title:'基线输入',source_event_id:'evt-123',path:'/work/input.csv'}]};
  const details=researchDetailRows(record);
  const text=JSON.stringify(details);
  expect(text).toContain('已完成');expect(text).toContain('评分');expect(text).toContain('0.9');expect(text).toContain('基线输入');expect(text).toContain('input.csv');
  expect(text).not.toMatch(/exp-|evt-|experiment_id|custom_metric|\/work\//);
  const many=researchDetailRows(Array.from({length:300},(_,i)=>({title:'记录',score:i})),20);
  expect(many.rows).toHaveLength(20);expect(many.omitted).toBeGreaterThan(0);
});
