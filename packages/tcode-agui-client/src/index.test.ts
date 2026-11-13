import { describe, expect, it, vi } from 'vitest';
import { EventType, type RunAgentInput } from '@ag-ui/core';
import { createTCodeAgent, TCodeGatewayClient } from './index.js';

describe('@tcode-ai/ag-ui-client', () => {
  it('adds the TCode runtime selection to every AG-UI run', async () => {
    let input: RunAgentInput | undefined;
    const agent = createTCodeAgent({
      baseUrl: 'http://tcode.local', workspaceId: 'workspace-1',
      runtimeId: 'opencode', modelId: 'deepseek/deepseek-v4-flash',
      fetch: async (_url, init) => {
        input = JSON.parse(String(init.body));
        return new Response(
          `data: ${JSON.stringify({ type: EventType.RUN_STARTED, threadId: 't', runId: 'r' })}\n\n` +
          `data: ${JSON.stringify({ type: EventType.RUN_FINISHED, threadId: 't', runId: 'r' })}\n\n`,
          { headers: { 'content-type': 'text/event-stream' } },
        );
      },
    });
    await new Promise<void>((resolve, reject) => agent.run({
      threadId: 't', runId: 'r', state: {}, messages: [], tools: [], context: [], forwardedProps: {},
    }).subscribe({ error: reject, complete: resolve }));
    expect(input?.forwardedProps).toMatchObject({
      tcode: { workspaceId: 'workspace-1', runtimeId: 'opencode', modelId: 'deepseek/deepseek-v4-flash' },
    });
  });

  it('uses the management endpoints without a UI dependency', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ runtimes: [{ runtimeId: 'tcode-native' }] }), { status: 200 }));
    const client = new TCodeGatewayClient({ baseUrl: 'http://tcode.local/', apiKey: 'secret', fetch });
    expect(await client.listRuntimes()).toEqual([{ runtimeId: 'tcode-native' }]);
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/ag-ui/v1/runtimes?'), expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer secret' }) }));
  });

  it('discovers public agents without assuming a TCode UI', async () => {
    const descriptor = {
      id: 'default', name: 'TCode', defaultRuntimeId: 'tcode-native',
      protocol: 'AG-UI', protocolVersion: '0.0.59',
    };
    const fetch = vi.fn()
      .mockResolvedValueOnce(Response.json({ agents: [descriptor] }))
      .mockResolvedValueOnce(Response.json({ ...descriptor, capabilities: { text: true } }));
    const client = new TCodeGatewayClient({ baseUrl: 'http://tcode.local', fetch });

    expect(await client.listAgents()).toEqual([descriptor]);
    expect(await client.getAgent()).toEqual({ ...descriptor, capabilities: { text: true } });
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      'http://tcode.local/api/ag-ui/v1/agents/default',
      expect.any(Object),
    );
  });

  it('exposes harness switching and artifact helpers to non-TCode frontends', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(Response.json({
        session_id: 'thread-1', runtime_id: 'tcode-native', model_id: 'model-a', switch_version: 2,
      }))
      .mockResolvedValueOnce(Response.json({
        session_id: 'thread-1', runtime_id: 'opencode', model_id: 'model-b', switch_version: 3,
      }))
      .mockResolvedValueOnce(Response.json({
        session_id: 'thread-1', messages: [{ content_blocks: [
          { type: 'artifact', artifact_id: 'a-1', title: '报告', uri: '/api/files/download?path=report.md', media_type: 'text/markdown' },
          { type: 'mcp_app', resource_uri: 'ui://demo' },
        ] }],
      }))
      .mockResolvedValueOnce(new Response('report', { status: 200 }));
    const client = new TCodeGatewayClient({ baseUrl: 'http://tcode.local', apiKey: 'secret', fetch });

    expect(await client.getHarnessState('thread-1', 'workspace-1')).toMatchObject({ switch_version: 2 });
    expect(await client.switchHarness('thread-1', 'workspace-1', {
      targetRuntimeId: 'opencode', modelId: 'model-b',
      expectedCurrentRuntimeId: 'tcode-native', expectedSwitchVersion: 2,
      idempotencyKey: 'switch-1',
    })).toMatchObject({ runtime_id: 'opencode', switch_version: 3 });
    const artifacts = await client.listArtifacts('thread-1', 'workspace-1');
    expect(artifacts).toHaveLength(1);
    expect(await (await client.downloadArtifact(artifacts[0])).text()).toBe('report');
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      'http://tcode.local/api/sessions/thread-1/harness-switch?workspace_id=workspace-1',
      expect.objectContaining({
        method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'switch-1' }),
      }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      'http://tcode.local/api/files/download?path=report.md',
      expect.objectContaining({ headers: { Authorization: 'Bearer secret' } }),
    );
  });

  it('submits and manages guidance with workspace scoping', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(Response.json({ status: 'queued', guidance_id: 'g-1' }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({ items: [{ id: 'g-1', message: '继续' }] }))
      .mockResolvedValueOnce(Response.json({ status: 'deleted', items: [] }));
    const client = new TCodeGatewayClient({ baseUrl: 'http://tcode.local', fetch });

    expect(await client.submitGuidance('thread-1', '继续', {
      workspaceId: 'workspace-1', mode: 'queue', clientRequestId: 'guide-1',
    })).toMatchObject({ guidance_id: 'g-1' });
    expect(await client.listGuidance('thread-1', 'workspace-1')).toEqual([{ id: 'g-1', message: '继续' }]);
    await expect(client.deleteGuidance('thread-1', 'g-1', 'workspace-1')).resolves.toBeUndefined();
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      'http://tcode.local/api/chat/guide?workspace_id=workspace-1',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('advertises optional client capabilities on a run', async () => {
    let input: RunAgentInput | undefined;
    const agent = createTCodeAgent({
      baseUrl: 'http://tcode.local', workspaceId: 'workspace-1',
      disconnectPolicy: 'detach', clientRequestId: 'client-1',
      clientCapabilities: { mcpApps: true, iframe: true, files: true, images: false },
      fetch: async (_url, init) => {
        input = JSON.parse(String(init.body));
        return new Response(
          `data: ${JSON.stringify({ type: EventType.RUN_STARTED, threadId: 't', runId: 'r' })}\n\n` +
          `data: ${JSON.stringify({ type: EventType.RUN_FINISHED, threadId: 't', runId: 'r' })}\n\n`,
          { headers: { 'content-type': 'text/event-stream' } },
        );
      },
    });
    await new Promise<void>((resolve, reject) => agent.run({
      threadId: 't', runId: 'r', state: {}, messages: [], tools: [], context: [], forwardedProps: {},
    }).subscribe({ error: reject, complete: resolve }));

    expect(input?.forwardedProps).toMatchObject({
      tcode: {
        disconnectPolicy: 'detach', clientRequestId: 'client-1',
        clientCapabilities: { mcpApps: true, iframe: true, files: true, images: false },
      },
    });
  });

  it('hydrates an agent with existing thread messages', () => {
    const agent = createTCodeAgent({
      baseUrl: 'http://tcode.local',
      workspaceId: 'workspace-1',
      threadId: 'thread-1',
      initialMessages: [
        { id: 'u1', role: 'user', content: '之前的问题' },
        { id: 'a1', role: 'assistant', content: '之前的回答' },
      ],
    });

    expect(agent.threadId).toBe('thread-1');
    expect(agent.messages).toEqual([
      { id: 'u1', role: 'user', content: '之前的问题' },
      { id: 'a1', role: 'assistant', content: '之前的回答' },
    ]);
  });

  it('replays persisted messages when CopilotKit reconnects an explicit thread', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      session_id: 'thread-1',
      messages: [
        { id: 'u1', role: 'user', content: '之前的问题' },
        { id: 'tool1', role: 'tool', content: '内部工具结果' },
        { id: 'a1', role: 'assistant', content: '之前的回答' },
      ],
    }), { status: 200 }));
    const agent = createTCodeAgent({
      baseUrl: 'http://tcode.local',
      workspaceId: 'workspace-1',
      threadId: 'thread-1',
      apiKey: 'secret',
      initialMessages: [{ id: 'stale', role: 'user', content: '旧快照' }],
      fetch,
    });

    // CopilotKit v2 clears the local transcript before connectAgent().
    agent.setMessages([]);
    await agent.connectAgent();

    expect(agent.messages).toEqual([
      { id: 'u1', role: 'user', content: '之前的问题' },
      { id: 'a1', role: 'assistant', content: '之前的回答' },
    ]);
    expect(fetch).toHaveBeenCalledWith(
      'http://tcode.local/api/sessions/thread-1?workspace_id=workspace-1',
      expect.objectContaining({
        method: 'GET',
        headers: expect.objectContaining({ Authorization: 'Bearer secret' }),
      }),
    );
  });

  it('treats a not-yet-created thread as an empty reconnect', async () => {
    const agent = createTCodeAgent({
      baseUrl: 'http://tcode.local',
      workspaceId: 'workspace-1',
      threadId: 'new-thread',
      fetch: vi.fn().mockResolvedValue(new Response('{}', { status: 404 })),
    });

    await expect(agent.connectAgent()).resolves.toEqual({ result: undefined, newMessages: [] });
    expect(agent.messages).toEqual([]);
  });

  it('lists and loads persisted TCode threads', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        sessions: [{
          session_id: 'thread-1', title: 'AG-UI session', agent_name: 'general',
          workspace_id: 'workspace-1', status: 'active', message_count: 2,
          message_preview: '之前的问题', created_at: 10, updated_at: 20,
        }],
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        session_id: 'thread-1', title: 'AG-UI session', agent_name: 'general',
        workspace_id: 'workspace-1', status: 'active', created_at: 10, updated_at: 20,
        messages: [
          { id: 'u1', role: 'user', content: '之前的问题' },
          { id: 'tool1', role: 'tool', content: '内部工具结果' },
          { id: 'a1', role: 'assistant', content: '之前的回答' },
        ],
      }), { status: 200 }));
    const client = new TCodeGatewayClient({ baseUrl: 'http://tcode.local', apiKey: '', fetch });

    expect(await client.listThreads('workspace-1')).toEqual([
      expect.objectContaining({ threadId: 'thread-1', messageCount: 2, messagePreview: '之前的问题' }),
    ]);
    expect((await client.getThread('thread-1', 'workspace-1')).messages).toEqual([
      { id: 'u1', role: 'user', content: '之前的问题' },
      { id: 'a1', role: 'assistant', content: '之前的回答' },
    ]);
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('/api/sessions?workspace_id=workspace-1'),
      expect.any(Object),
    );
  });

  it('submits deletion once and waits for cleanup with workspace and authentication', async () => {
    vi.useFakeTimers();
    try {
      const fetch = vi.fn()
        .mockResolvedValueOnce(Response.json({ deleted_session_id: 'thread/1', pending: true }))
        .mockResolvedValueOnce(Response.json({ session_id: 'thread/1', pending: true, running: true }))
        .mockResolvedValueOnce(Response.json({ session_id: 'thread/1', pending: false, running: false, error: '' }));
      const client = new TCodeGatewayClient({ baseUrl: 'http://tcode.local', apiKey: 'secret', fetch });
      let finished = false;
      const deletion = client.deleteThread('thread/1', 'workspace 1').then(() => { finished = true; });
      await vi.advanceTimersByTimeAsync(0);
      expect(finished).toBe(false);
      expect(fetch).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1000);
      await deletion;
      expect(finished).toBe(true);
      expect(fetch).toHaveBeenNthCalledWith(1,
        'http://tcode.local/api/sessions/thread%2F1?workspace_id=workspace+1',
        expect.objectContaining({ method: 'DELETE', headers: expect.objectContaining({ Authorization: 'Bearer secret' }) }),
      );
      expect(fetch).toHaveBeenNthCalledWith(3,
        'http://tcode.local/api/sessions/thread%2F1/delete-status?workspace_id=workspace+1',
        expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer secret' }) }),
      );
      expect(fetch.mock.calls.filter(([, init]) => init.method === 'DELETE')).toHaveLength(1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reports cleanup errors without claiming deletion or resubmitting it', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(Response.json({ deleted_session_id: 'thread-1', pending: true }))
      .mockResolvedValueOnce(Response.json({ session_id: 'thread-1', pending: false, running: false, error: 'workspace cleanup failed' }));
    const client = new TCodeGatewayClient({ baseUrl: '', fetch });
    await expect(client.deleteThread('thread-1', 'workspace-1')).rejects.toThrow('workspace cleanup failed');
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('does not poll or retry when deletion is denied', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({ detail: 'Forbidden' }, { status: 403 }));
    const client = new TCodeGatewayClient({ baseUrl: '', fetch });
    await expect(client.deleteThread('thread-1', 'workspace-1')).rejects.toThrow('Forbidden');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('bounds pending deletion without resending a destructive request', async () => {
    vi.useFakeTimers();
    try {
      const fetch = vi.fn()
        .mockResolvedValueOnce(Response.json({ deleted_session_id: 'thread-1', pending: true }))
        .mockImplementation(async () => Response.json({ session_id: 'thread-1', pending: true, running: true }));
      const client = new TCodeGatewayClient({ baseUrl: '', fetch });
      const result = expect(client.deleteThread('thread-1', 'workspace-1', { timeoutMs: 1500 }))
        .rejects.toThrow('删除结果尚未确认');
      await vi.advanceTimersByTimeAsync(1500);
      await result;
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(fetch.mock.calls.filter(([, init]) => init.method === 'DELETE')).toHaveLength(1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops polling when its caller leaves, without sending another deletion', async () => {
    const controller = new AbortController();
    const fetch = vi.fn()
      .mockResolvedValueOnce(Response.json({ deleted_session_id: 'thread-1', pending: true }))
      .mockImplementationOnce(async () => {
        controller.abort(new Error('page closed'));
        return Response.json({ session_id: 'thread-1', pending: true, running: true });
      });
    const client = new TCodeGatewayClient({ baseUrl: '', fetch });
    await expect(client.deleteThread('thread-1', 'workspace-1', { signal: controller.signal }))
      .rejects.toThrow('page closed');
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('rejects malformed status instead of treating it as successful cleanup', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(Response.json({ deleted_session_id: 'thread-1', pending: true }))
      .mockResolvedValueOnce(Response.json({}));
    const client = new TCodeGatewayClient({ baseUrl: '', fetch });
    await expect(client.deleteThread('thread-1', 'workspace-1')).rejects.toThrow('无效的会话删除状态');
  });
});
