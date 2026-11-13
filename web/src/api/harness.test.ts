import { afterEach, describe, expect, it, vi } from 'vitest';

import { HarnessSwitchApiError, switchHarness } from './harness';

describe('Harness switch API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends the expected state and stable idempotency key', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        session_id: 'session-1',
        previous_runtime_id: 'tcode-native',
        current_runtime_id: 'opencode',
        model_id: 'deepseek/deepseek-v4-flash',
        switch_version: 3,
        switched: true,
        mid_session: true,
        handoff: { handoff_id: 'handoff-1', summary_status: 'generated' },
        notice: { code: 'harness_mid_session_memory_risk', severity: 'warning' },
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await switchHarness(
      'session/1',
      {
        target_runtime_id: 'opencode',
        model_id: 'deepseek/deepseek-v4-flash',
        expected_current_runtime_id: 'tcode-native',
        expected_switch_version: 2,
      },
      'switch-fixed-key',
    );

    expect(result.current_runtime_id).toBe('opencode');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/session%2F1/harness-switch',
      expect.objectContaining({
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': 'switch-fixed-key',
        },
      }),
    );
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({
      target_runtime_id: 'opencode',
      model_id: 'deepseek/deepseek-v4-flash',
      expected_current_runtime_id: 'tcode-native',
      expected_switch_version: 2,
    });
  });

  it('exposes the authoritative state from a 409 conflict', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        detail: {
          code: 'harness_state_conflict',
          message: 'state changed',
          current_state: {
            runtime_id: 'opencode',
            model_id: 'deepseek/deepseek-v4-flash',
            switch_version: 4,
          },
        },
      }),
    }));

    let caught: unknown;
    try {
      await switchHarness(
        'session-1',
        {
          target_runtime_id: 'opencode',
          model_id: 'deepseek/deepseek-v4-flash',
          expected_current_runtime_id: 'tcode-native',
          expected_switch_version: 2,
        },
        'switch-conflict',
      );
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(HarnessSwitchApiError);
    expect((caught as HarnessSwitchApiError).code).toBe('harness_state_conflict');
    expect((caught as HarnessSwitchApiError).currentState).toMatchObject({
      runtime_id: 'opencode',
      switch_version: 4,
    });
  });
});
