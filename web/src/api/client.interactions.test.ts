import { afterEach, describe, expect, it, vi } from 'vitest';
import { sendMessage, cancelMessage, getChatRuntimeStatus, respondPermission, respondQuestion, respondQuestions } from './client';
import { sessionStore } from '../stores/session';
import { markAsyncCompletionContinued } from './sessions';

afterEach(() => vi.unstubAllGlobals());

describe('session interaction failures and races', () => {
  it.each([409, 500])('does not consume a continuation without committed server acknowledgment (%s)', async status => {
    const sessionId = `continue-uncommitted-${status}`;
    sessionStore.unmarkAsyncJobContinued(sessionId, 'LOCAL-ORIGINAL');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'not committed' }), { status })));
    await expect(markAsyncCompletionContinued(sessionId, 'LOCAL-ORIGINAL')).rejects.toThrow();
    expect(sessionStore.isAsyncJobContinued(sessionId, 'LOCAL-ORIGINAL')).toBe(false);
  });
  it.each([404, 500])('rejects failed answers and permissions (%s)', async status => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'temporary failure' }), { status })));
    await expect(respondQuestion('q', 'my answer', 'a')).rejects.toThrow();
    await expect(respondPermission('p', 'once', 'a')).rejects.toThrow();
  });

  it('updates A only when cancellation completes after switching to B', async () => {
    let finish!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
    sessionStore.setCurrentSessionId('a');
    sessionStore.setSessionIsRunning('a', true);
    sessionStore.setSessionIsRunning('b', true);
    sessionStore.setSessionStatus('b', 'B running');
    const pending = cancelMessage();
    sessionStore.setCurrentSessionId('b');
    finish(new Response(JSON.stringify({ session_id: 'a', status: 'cancelled' })));
    await pending;
    expect(sessionStore.getSessionRuntimeState('a').isRunning).toBe(false);
    expect(sessionStore.getSessionRuntimeState('b')).toMatchObject({ isRunning: true, status: 'B running' });
  });
});

describe('pending question recovery', () => {
  const pending = (sessionId: string, requestId = 'q-recover') => ({
    request_id: requestId, session_id: sessionId,
    question: { question: '是否继续？', options: [] },
  });
  const status = (sessionId: string, question: unknown) => new Response(JSON.stringify({
    session_id: sessionId, running: true, question_request: question,
  }));

  it('recovers after refresh and preserves the form while polling', async () => {
    const id = 'recover-refresh';
    vi.stubGlobal('fetch', vi.fn(async () => status(id, pending(id))));
    await getChatRuntimeStatus(id);
    const original = sessionStore.getSessionRuntimeState(id).questionRequest;
    expect(original?.request_id).toBe('q-recover');
    await getChatRuntimeStatus(id);
    expect(sessionStore.getSessionRuntimeState(id).questionRequest).toBe(original);
    vi.stubGlobal('fetch', vi.fn(async () => status(id, null)));
    await getChatRuntimeStatus(id);
    expect(sessionStore.getSessionRuntimeState(id).questionRequest).toBeNull();
  });

  it('does not revive a question from a snapshot taken before its answer', async () => {
    const id = 'recover-answer';
    const question = pending(id, 'q-answered');
    sessionStore.setSessionQuestionRequest(id, question);
    sessionStore.addQuestionHistory(question);
    let finish!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
    const poll = getChatRuntimeStatus(id);
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')));
    await respondQuestion(question.request_id, '继续', id);
    finish(status(id, question));
    await poll;
    expect(sessionStore.getSessionRuntimeState(id).questionRequest).toBeNull();
  });

  it('does not erase a new socket question with an older empty snapshot', async () => {
    const id = 'recover-race';
    let finish!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
    const poll = getChatRuntimeStatus(id);
    const question = pending(id, 'q-new');
    sessionStore.setSessionQuestionRequest(id, question);
    finish(status(id, null));
    await poll;
    expect(sessionStore.getSessionRuntimeState(id).questionRequest).toBe(question);
  });

  it('restores every pending request after refresh and submits them in one POST', async () => {
    const id = 'recover-group';
    const first = pending(id,'group-first');
    const second = pending(id,'group-second');
    vi.stubGlobal('fetch',vi.fn(async () => new Response(JSON.stringify({
      session_id:id,running:true,question_request:first,question_requests:[first,second],
    }))));
    await getChatRuntimeStatus(id);
    expect(sessionStore.questionHistory().filter(q => q.session_id === id && q.status === 'pending')).toHaveLength(2);
    const post = vi.fn(async (_url: string, _init?: RequestInit) => new Response('{}'));
    vi.stubGlobal('fetch',post);
    await respondQuestions({'group-first':'第一答','group-second':'第二答'},id);
    expect(post).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(post.mock.calls[0][1]?.body))).toEqual({
      session_id:id,answers:{'group-first':'第一答','group-second':'第二答'},
    });
    expect(sessionStore.questionHistory().filter(q => q.session_id === id && q.status === 'pending')).toHaveLength(0);
  });
});

it('separates background orchestration from an answer that is still generating', async () => {
  const id = 'delivery-background';
  for (const direct of [false, true]) {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      session_id:id,running:true,direct_running:direct,harness_running:false,workflow_background_active:true,
    }))));
    await getChatRuntimeStatus(id);
    expect(sessionStore.getSessionRuntimeState(id).deliveryTurnRunning).toBe(direct);
  }
});

it('normalizes detached async work without claiming the chat composer', async () => {
  const id = 'chat-while-computing';
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    session_id: id, running: true, chat_running: false,
    background_running: true, child_session_active: true,
    direct_running: false, harness_running: false, pending_guidance_count: 0,
  }))));
  const runtime = await getChatRuntimeStatus(id);
  expect(runtime).toMatchObject({ running: false, chat_running: false, background_running: true, child_session_active: true });
  expect(sessionStore.getSessionRuntimeState(id).deliveryTurnRunning).toBe(false);
});

it('can send hi after async acceptance and keeps the manual continuation card', async () => {
  const id = 'async-chat-roundtrip';
  const sent: string[] = [];
  class Socket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;
    readyState = Socket.OPEN;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => Promise<void>) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    constructor() { queueMicrotask(() => this.onopen?.()); }
    send(value: string) {
      const request = JSON.parse(value);
      sent.push(request.message);
      queueMicrotask(() => void this.onmessage?.({ data: JSON.stringify(request.async_continuation_id ? {
        type: 'async_resume_rejected', session_id: id, error: 'async_resume_already_claimed', done: true,
      } : {
        type: 'message', session_id: id, role: 'assistant', done: true,
        content: request.message === 'hi' ? '你好' : '异步任务已受理',
        running: true, chat_running: false, child_session_active: true,
      }) }));
    }
    close() { this.readyState = Socket.CLOSED; queueMicrotask(() => this.onclose?.({ code: 1000 })); }
  }
  vi.stubGlobal('window', { location: { protocol: 'http:', host: 'localhost' } });
  vi.stubGlobal('WebSocket', Socket);
  sessionStore.setCurrentSessionId(id);
  sessionStore.setMessages([]);
  await sendMessage('提交后台任务');
  expect(sessionStore.isRunning()).toBe(false);
  sessionStore.addMessageForSession(id, {
    id: 'completion-card', role: 'assistant', content: '任务完成，点击继续',
    presentation: 'async_completion',
  });
  await expect(sendMessage('继续异步任务 LOCAL-ORIGINAL。', {
    async_continuation_id: 'LOCAL-ORIGINAL',
  })).rejects.toThrow('async_resume_already_claimed');
  expect(sessionStore.messages().some(message => message.content === '继续异步任务 LOCAL-ORIGINAL。')).toBe(false);
  await sendMessage('hi');
  expect(sent).toEqual(['提交后台任务', '继续异步任务 LOCAL-ORIGINAL。', 'hi']);
  expect(sessionStore.isRunning()).toBe(false);
  expect(sessionStore.messages().some(message => message.content === '你好')).toBe(true);
  expect(sessionStore.messages().find(message => message.id === 'completion-card')?.presentation).toBe('async_completion');
});


it('blocks continuation dispatch while the same session is switching harness', async () => {
  sessionStore.setCurrentSessionId('switching-session');
  sessionStore.setMessages([]);
  sessionStore.setHarnessSwitchPending('switching-session', true);
  try {
    expect(sessionStore.isHarnessSwitchPending('other-session')).toBe(false);
    await expect(sendMessage('继续异步任务')).rejects.toThrow('Harness 正在切换');
    expect(sessionStore.messages()).toHaveLength(0);
  } finally {
    sessionStore.setHarnessSwitchPending('switching-session', false);
  }
});
