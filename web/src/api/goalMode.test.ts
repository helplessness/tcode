import {afterEach, expect, it, vi} from 'vitest';
import {goalRequest} from './goalMode';
afterEach(() => vi.unstubAllGlobals());
it('changes only the exact goal review policy without resetting its runtime budget', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({goal:{id:'g',auto_approve_reviews:true}})));
  vi.stubGlobal('fetch', fetcher);
  await goalRequest('s','w',{action:'review_policy',goal_id:'g',auto_approve_reviews:true,max_auto_computations:3},true);
  expect(fetcher.mock.calls[0]).toEqual(['/api/sessions/s/goal/action',expect.objectContaining({
    body:JSON.stringify({action:'review_policy',goal_id:'g',auto_approve_reviews:true,max_auto_computations:3,workspace_id:'w'}),
  })]);
});
it('submits chat goals and guidance through scoped goal APIs, not ordinary chat', async () => {
  const fetcher = vi.fn(async (_url: string, _options: RequestInit) => new Response(JSON.stringify({goal: {id:'g',status:'ready'}})));
  vi.stubGlobal('fetch', fetcher);
  await goalRequest('session','workspace',{objective:'解释蛋白置信度',from_chat:true,max_rounds:8,max_minutes:30});
  expect(JSON.parse(fetcher.mock.calls[0][1].body as string)).toEqual({workspace_id:'workspace',objective:'解释蛋白置信度',from_chat:true,max_rounds:8,max_minutes:30});
  await goalRequest('session','workspace',{goal_id:'g',action:'guide',note:'面向初学者'},true);
  expect(fetcher.mock.calls[1][0]).toContain('/goal/action');
  expect(JSON.parse(fetcher.mock.calls[1][1].body as string).note).toBe('面向初学者');
});
it('scopes reads and explicit writes without changing ordinary chat', async () => {
  const fetcher = vi.fn(async (_url: string, _options: RequestInit) => new Response(JSON.stringify({goal:null})));
  vi.stubGlobal('fetch', fetcher);
  await goalRequest('session', 'workspace');
  expect(fetcher.mock.calls[0][0]).toBe('/api/sessions/session/goal?workspace_id=workspace');
  expect(fetcher.mock.calls[0][1].method).toBe('GET');
  await goalRequest('session', 'workspace', {action:'pause',goal_id:'g'}, true);
  expect(fetcher.mock.calls[1][0]).toBe('/api/sessions/session/goal/action');
  expect(JSON.parse(fetcher.mock.calls[1][1].body as string)).toEqual({workspace_id:'workspace',action:'pause',goal_id:'g'});
});
it('does not conceal a budget or ownership error', async () => {
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({detail:'预算已耗尽'}),{status:409}));
  await expect(goalRequest('session','workspace',{action:'resume'},true)).rejects.toThrow('预算已耗尽');
});
it('binds a late answer to its exact goal and question without submitting ordinary chat', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({goal:{id:'g',status:'ready'}})));
  vi.stubGlobal('fetch', fetcher);
  await goalRequest('s','w',{goal_id:'g',question_id:'q',action:'answer',note:'详细'},true);
  expect(fetcher.mock.calls[0]).toEqual(['/api/sessions/s/goal/action',expect.objectContaining({
    method:'POST',body:JSON.stringify({goal_id:'g',question_id:'q',action:'answer',note:'详细',workspace_id:'w'}),
  })]);
});
