import { afterEach, expect, it, vi } from 'vitest';
import { controlWorkerExecution, getSideChats, getSubagents, injectSideChat } from './subagents';

afterEach(() => vi.unstubAllGlobals());

it('resumes the same task with explicit limits and surfaces budget rejection', async () => {
  const fetchMock = vi.fn().mockResolvedValue({ok:false, json:async()=>({detail:'预算已耗尽'})});
  vi.stubGlobal('fetch', fetchMock);
  await expect(controlWorkerExecution('worker/a', 'task/b', 'resume', {tokens:200})).rejects.toThrow('预算已耗尽');
  expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/subagents/worker%2Fa/tasks/task%2Fb/control', {
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'resume',tokens:200}),
  });
});

it('keeps side chats out of the main collaboration API projection', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ subagents: [
      { session_id: 'worker', side_question: false },
      { session_id: 'side', side_question: true },
    ] }),
  }));
  expect(await getSubagents('side-isolation-test')).toEqual([{ session_id: 'worker', side_question: false }]);
});

it('does not turn a temporary list failure into an empty set of saved tabs', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: false, json: async () => ({ detail: '暂时不可用' }),
  }));
  await expect(getSideChats('main')).rejects.toThrow('暂时不可用');
});

it('injects directly without changing the main composer draft or sending a new turn', async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true, json: async () => ({ status: 'injected', mode: 'context', message_count: 4, duplicate: false }),
  });
  vi.stubGlobal('fetch', fetchMock);
  expect((await injectSideChat('main', 'side')).mode).toBe('context');
  expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/sessions/main/side-chats/side/inject', { method: 'POST' });
});
