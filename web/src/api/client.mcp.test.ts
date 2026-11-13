import { afterEach, describe, expect, it, vi } from 'vitest';

import { getMCPServers } from './client';

describe('MCP status API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests reconnection of failed MCP servers on an active refresh', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ servers: [{ name: 'job-protocol-mcp', connected: true }] }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await getMCPServers({ refreshFailed: true });

    expect(fetchMock).toHaveBeenCalledWith('/api/mcp/status?refresh_failed=true');
    expect(result[0]).toMatchObject({ name: 'job-protocol-mcp', connected: true });
  });
});
