import { expect, it } from 'vitest';
import { questionGroup } from './questionGroup';

it('keeps independent and nested answers tied to the right tool requests', () => {
  const first = {request_id:'first',session_id:'s',question:{question:'目标？',required:true}};
  const second = {request_id:'second',session_id:'s',question:{question:'选项组',questions:[
    {id:'platform',question:'平台？',type:'single',options:['macOS','Linux'],required:true},
    {id:'note',question:'补充？',required:false},
  ]}};
  const original = questionGroup([first]);
  const group = questionGroup([first,second]);
  expect(group.questions).toHaveLength(3);
  expect(group.questions[0].id).toBe(original.questions[0].id);
  const raw = JSON.stringify(Object.fromEntries(group.questions.map((q,index) => [q.id,['研究目标','2',''][index]])));
  expect(group.answers(raw)).toEqual({first:'研究目标',second:JSON.stringify({platform:'2',note:''})});
});
