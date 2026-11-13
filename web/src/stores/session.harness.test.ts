import { describe, expect, it } from 'vitest';

import { sessionStore } from './session';

function setHarnessSession() {
  sessionStore.setSessions([
    {
      session_id: 'session-harness',
      model: 'deepseek/deepseek-v4-flash',
      messages: [],
      metadata: {
        harness: {
          runtime_id: 'tcode-native',
          model_id: 'deepseek/deepseek-v4-flash',
          models: { 'tcode-native': 'deepseek/deepseek-v4-flash' },
          switch_version: 1,
        },
      },
    } as any,
  ]);
}

describe('server-authoritative Harness state', () => {
  it('applies a successful mid-session switch and its pending notice', () => {
    setHarnessSession();

    sessionStore.applyServerHarnessState('session-harness', {
      session_id: 'session-harness',
      previous_runtime_id: 'tcode-native',
      current_runtime_id: 'opencode',
      model_id: 'deepseek/deepseek-v4-flash',
      switch_version: 2,
      switched: true,
      mid_session: true,
      handoff: {
        handoff_id: 'handoff-2',
        summary_status: 'fallback',
        inherited_rounds: 8,
        truncated: true,
      },
      notice: {
        code: 'harness_mid_session_memory_risk',
        severity: 'warning',
        message: 'risk',
      },
    });

    expect(sessionStore.sessions()[0]?.metadata?.harness).toMatchObject({
      runtime_id: 'opencode',
      switch_version: 2,
      pending_handoff_id: 'handoff-2',
      handoff_notice_pending: true,
      summary_status: 'fallback',
      truncated: true,
      models: {
        'tcode-native': 'deepseek/deepseek-v4-flash',
        opencode: 'deepseek/deepseek-v4-flash',
      },
    });
  });

  it('preserves a pending handoff when only the current runtime model changes', () => {
    setHarnessSession();
    sessionStore.applyServerHarnessState('session-harness', {
      runtime_id: 'opencode',
      model_id: 'deepseek/deepseek-v4-flash',
      switch_version: 2,
      pending_handoff_id: 'handoff-pending',
      handoff_notice_pending: true,
      summary_status: 'generated',
    });

    sessionStore.applyServerHarnessState('session-harness', {
      session_id: 'session-harness',
      previous_runtime_id: 'opencode',
      current_runtime_id: 'opencode',
      model_id: 'deepseek/deepseek-v4-flash',
      switch_version: 2,
      switched: false,
      mid_session: false,
      handoff: null,
      notice: null,
    });

    expect(sessionStore.sessions()[0]?.metadata?.harness).toMatchObject({
      runtime_id: 'opencode',
      pending_handoff_id: 'handoff-pending',
      handoff_notice_pending: true,
      summary_status: 'generated',
    });
  });

  it('clears the pending notice when the refreshed server projection is delivered', () => {
    setHarnessSession();
    sessionStore.applyServerHarnessState('session-harness', {
      runtime_id: 'opencode',
      model_id: 'deepseek/deepseek-v4-flash',
      switch_version: 3,
      pending_handoff_id: '',
      handoff_notice_pending: false,
    });

    expect(sessionStore.sessions()[0]?.metadata?.harness).toMatchObject({
      runtime_id: 'opencode',
      pending_handoff_id: '',
      handoff_notice_pending: false,
    });
  });
});
