import { describe, expect, it, vi } from 'vitest';

import { sessionStore } from './session';

describe('session model state', () => {
  it('drops locally cached active sessions missing from the authoritative list', () => {
    sessionStore.setSessions([
      {
        session_id: 'orphaned-session',
        messages: [{ id: 'message-1', role: 'user', content: 'stale' }],
        is_archived: false,
        updated_at: 2,
      } as any,
      {
        session_id: 'archived-session',
        messages: [],
        is_archived: true,
        updated_at: 1,
      } as any,
    ]);
    sessionStore.setCurrentSessionId('orphaned-session');

    sessionStore.mergeSessions([], 'active');

    expect(sessionStore.sessions().map((item) => item.session_id)).toEqual([
      'archived-session',
    ]);
    expect(sessionStore.currentSessionId()).toBeNull();
    expect(sessionStore.messages()).toEqual([]);
  });

  it('does not publish a new session list when the model is unchanged', () => {
    sessionStore.setSessions([
      {
        session_id: 'session-ws1',
        model: 'local-qwen/qwen3.5-plus',
        messages: [],
      } as any,
    ]);
    const before = sessionStore.sessions();

    sessionStore.updateSessionModel(
      'session-ws1',
      'local-qwen/qwen3.5-plus',
    );

    expect(sessionStore.sessions()).toBe(before);
  });

  it('publishes the selected model when it actually changes', () => {
    sessionStore.setSessions([
      {
        session_id: 'session-ws1',
        model: 'local-qwen/qwen3.5-plus',
        messages: [],
      } as any,
    ]);
    const before = sessionStore.sessions();

    sessionStore.updateSessionModel(
      'session-ws1',
      'deepseek/deepseek-v4-flash',
    );

    expect(sessionStore.sessions()).not.toBe(before);
    expect(sessionStore.sessions()[0]?.model).toBe(
      'deepseek/deepseek-v4-flash',
    );
  });

  it('does not change activity time when a session is only selected', () => {
    sessionStore.setSessions([
      { session_id: 'session-a', messages: [], updated_at: 20 } as any,
      { session_id: 'session-b', messages: [], updated_at: 10 } as any,
    ]);

    sessionStore.switchSession('session-b');

    expect(sessionStore.sessions().find(item => item.session_id === 'session-b')?.updated_at).toBe(10);
  });

  it('uses the session id as a stable tie-breaker for equal timestamps', () => {
    sessionStore.setSessions([]);
    sessionStore.mergeSessions([
      { session_id: 'session-b', messages: [], updated_at: 10 } as any,
      { session_id: 'session-a', messages: [], updated_at: 10 } as any,
    ]);

    expect(sessionStore.sessions().map(item => item.session_id)).toEqual(['session-a', 'session-b']);
  });

  it('starts a new task with the saved agent and selected execution defaults', () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === 'tcode.settings.defaultAgent.v2' ? 'paper_auto' : null,
      setItem: () => undefined,
    });
    sessionStore.setSessions([]);
    sessionStore.setSelectedAgentName('research');
    sessionStore.setSelectedSchedulerMode('chain');
    sessionStore.setSelectedReasoningEffort('high');

    const created = sessionStore.createSession();

    expect(created.agent_name).toBe('paper_auto');
    expect(created.scheduler_mode).toBe('supervisor');
    expect(created.reasoning_effort).toBe('high');
    expect(sessionStore.selectedAgentName()).toBe('paper_auto');
  });
});
