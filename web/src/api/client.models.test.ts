import { afterEach, describe, expect, it, vi } from 'vitest';

import { getModels, selectModel } from './client';

describe('model API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends the active workspace together with the session id', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        current_model: 'deepseek/deepseek-v4-flash',
        models: [
          {
            id: 'deepseek/deepseek-v4-flash',
            provider_id: 'deepseek',
            model_id: 'deepseek-v4-flash',
            name: 'DeepSeek V4 Flash',
          },
        ],
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await getModels('session-ws1', 'ws1');

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/models?session_id=session-ws1&workspace_id=ws1',
    );
    expect(result.models).toHaveLength(1);
  });

  it('surfaces a non-2xx response instead of returning an empty model list', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 410,
      json: async () => ({ detail: 'workspace is purged' }),
    }));

    await expect(getModels('session-ws1', 'workspace-1')).rejects.toThrow(
      'workspace is purged',
    );
  });

  it('selects a model in the active workspace scope', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ model: 'local-qwen/qwen3.5-plus' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await selectModel(
      'local-qwen/qwen3.5-plus',
      'session-ws1',
      'ws1',
    );

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/models/select?workspace_id=ws1',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          model: 'local-qwen/qwen3.5-plus',
          session_id: 'session-ws1',
        }),
      }),
    );
  });
});
