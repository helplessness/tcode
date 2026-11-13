import { expect, it } from 'vitest';
import { latestDeliveredAssistant, readableVerificationText, verificationFeedback } from './verificationFeedback';

it('uses short Chinese explanations and never displays raw ids or diagnostics', () => {
  expect(readableVerificationText('criterion_secret')).toBe('现有信息还不足以确认这一点。');
  expect(readableVerificationText('semantic outcome timed out limit=5')).toBe('检查等待时间过长，暂时无法确认。');
  expect(readableVerificationText('final_response is not valid JSON')).toBe('回答的格式与要求不一致。');
  expect(readableVerificationText('报告缺少计算依据')).toBe('报告缺少计算依据');
});

it('retains Chinese explanations with internal keys and hides local task identifiers', () => {
  expect(readableVerificationText('尚未取得 job_id，无法确认开始运行')).toBe('尚未取得 任务编号，无法确认开始运行');
  expect(readableVerificationText('保留旧记录 LOCAL-20261020014400887133，不恢复、不重提。')).not.toContain('LOCAL-');
  const rows = verificationFeedback({verification_status:'PARTIAL',confidence:.86,findings:[
    {status:'unverified',title:'未满足的要求',reason:'现有信息还不足以确认这一点。'},
    {status:'unverified',title:'暂未核实的内容',reason:'现有信息还不足以确认这一点。'},
  ]});
  expect(rows).toHaveLength(1);
  expect(rows[0].reason).toContain('没有保存具体原因');
});

it('ignores notifications but not newer delivered answers', () => {
  const answer = {id:'answer',role:'assistant'};
  expect(latestDeliveredAssistant([answer,{id:'notice',role:'assistant',presentation:'async_completion'}])).toBe(answer);
  expect(latestDeliveredAssistant([answer,{id:'new',role:'assistant'}])?.id).toBe('new');
});
it('keeps discovered failures separate from information not yet verified', () => {
  const rows = verificationFeedback({verification_status:'PARTIAL',confidence:.2,findings:[
    {status:'failed',title:'报告完整性',reason:'报告缺少计算依据'},
    {status:'unverified',title:'结论依据',reason:'尚未提供来源支持'},
  ]});
  expect(rows.map(row => row.status)).toEqual(['failed','unverified']);
  expect(verificationFeedback({verification_status:'PARTIAL',confidence:.2})[0].reason).toContain('没有足够');
});
