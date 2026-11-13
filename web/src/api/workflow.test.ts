import { afterEach, describe, expect, it, vi } from 'vitest';
import { getWorkflowTemplates } from './workflow';

afterEach(() => vi.unstubAllGlobals());

describe('workflow template workspace scope', () => {
  it('sends session/workspace and does not deduplicate requests across workspaces', async () => {
    const fetcher = vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ templates: [], count: 0 }) }));
    vi.stubGlobal('fetch', fetcher);
    await Promise.all([
      getWorkflowTemplates({ includeDisabled: true, session_id: 'a', workdir_name: '中文 空间' }),
      getWorkflowTemplates({ includeDisabled: true, session_id: 'b', workdir_name: 'other' }),
    ]);
    expect(fetcher).toHaveBeenCalledTimes(2);
    const url = new URL(String(fetcher.mock.calls[0][0]), 'http://localhost');
    expect(url.searchParams.get('session_id')).toBe('a');
    expect(url.searchParams.get('workdir_name')).toBe('中文 空间');
    expect(url.searchParams.get('include_disabled')).toBe('true');
  });
});
