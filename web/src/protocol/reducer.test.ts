import { describe, expect, it } from 'vitest';
import type { RuntimeEvent } from './generated';
import { createRuntimeProjection, reduceRuntimeEvent } from './reducer';
import { RuntimeWebSocketAdapter } from './websocketAdapter';

const envelope = (sequence: number, type: RuntimeEvent['type']) => ({
  protocol: 'tcode.agent.events' as const,
  protocol_version: '2.0.0' as const,
  type,
  event_id: `event-${sequence}`,
  session_id: 'session-1',
  run_id: 'run-1',
  sequence,
  timestamp: '2026-09-07T12:00:00.000Z',
  causation_id: '',
  metadata: {},
});

describe('runtime v2 reducer', () => {
  it('projects structured Artifact blocks into the live WebSocket message', () => {
    const adapter = new RuntimeWebSocketAdapter();
    adapter.consume({
      ...envelope(1, 'message.started'),
      type: 'message.started',
      message_id: 'message-artifact',
      role: 'assistant',
    });
    const result = adapter.consume({
      ...envelope(2, 'message.finished'),
      type: 'message.finished',
      message: {
        message_id: 'message-artifact',
        role: 'assistant',
        content: '已生成结果',
        content_blocks: [{
          type: 'artifact',
          artifact_id: 'art_0123456789abcdef01234567',
          uri: '/api/files/download?path=result.csv&workspace_id=workspace',
          media_type: 'text/csv',
          title: 'result.csv',
          fallback: 'Artifact: result.csv',
        }],
      },
    });
    expect(result.uiEvent?.content_blocks).toHaveLength(1);
    expect(result.uiEvent?.content_blocks[0].type).toBe('artifact');
  });

  it('rebuilds a streamed message and ignores an exact replay', () => {
    const started = {
      ...envelope(1, 'run.started'),
      type: 'run.started' as const,
      agent_id: 'build',
    };
    const messageStarted = {
      ...envelope(2, 'message.started'),
      type: 'message.started' as const,
      message_id: 'message-1',
      role: 'assistant' as const,
    };
    const delta = {
      ...envelope(3, 'message.delta'),
      type: 'message.delta' as const,
      message_id: 'message-1',
      delta: '你好',
    };
    let state = createRuntimeProjection();
    state = reduceRuntimeEvent(state, started);
    state = reduceRuntimeEvent(state, messageStarted);
    state = reduceRuntimeEvent(state, delta);
    const replayed = reduceRuntimeEvent(state, delta);
    expect(replayed).toBe(state);
    expect(state.messages['message-1'].content).toBe('你好');
    expect(state.needsSnapshot).toBe(false);
  });

  it('marks sequence gaps for snapshot recovery', () => {
    let state = createRuntimeProjection();
    state = reduceRuntimeEvent(state, {
      ...envelope(1, 'run.started'),
      type: 'run.started',
      agent_id: 'build',
    });
    state = reduceRuntimeEvent(state, {
      ...envelope(3, 'activity.delta'),
      type: 'activity.delta',
      activity_id: 'activity-1',
      phase: 'work',
      status: 'running',
      message: '跳过了一个事件',
    });
    expect(state.needsSnapshot).toBe(true);
    expect(state.lastSequence).toBe(1);
    expect(state.diagnostics[0]).toContain('expected 2, got 3');

    state = reduceRuntimeEvent(state, {
      ...envelope(8, 'messages.snapshot'),
      type: 'messages.snapshot',
      messages: [{ message_id: 'recovered', role: 'assistant', content: '已恢复' }],
    });
    expect(state.needsSnapshot).toBe(false);
    expect(state.lastSequence).toBe(8);
    expect(state.messages.recovered.content).toBe('已恢复');
  });

  it('applies RFC 6902 state patches', () => {
    let state = createRuntimeProjection();
    state = reduceRuntimeEvent(state, {
      ...envelope(1, 'state.snapshot'),
      type: 'state.snapshot',
      state: { progress: { current: 1 }, items: ['a'] },
    });
    state = reduceRuntimeEvent(state, {
      ...envelope(2, 'state.delta'),
      type: 'state.delta',
      patch: [
        { op: 'replace', path: '/progress/current', value: 2 },
        { op: 'add', path: '/items/-', value: 'b' },
      ],
    });
    expect(state.state).toEqual({ progress: { current: 2 }, items: ['a', 'b'] });
  });

  it('accepts a new sequence epoch after a server restart', () => {
    let state = createRuntimeProjection();
    state = reduceRuntimeEvent(state, {
      ...envelope(50, 'state.snapshot'),
      type: 'state.snapshot',
      state: { old: true },
      metadata: { tcode: { stream_id: 'stream-old' } },
    });
    state = reduceRuntimeEvent(state, {
      ...envelope(1, 'run.started'),
      type: 'run.started',
      agent_id: 'build',
      metadata: { tcode: { stream_id: 'stream-new' } },
    });
    expect(state.lastSequence).toBe(1);
    expect(state.streamId).toBe('stream-new');
    expect(state.needsSnapshot).toBe(false);
    expect(state.diagnostics[0]).toContain('runtime stream changed');
  });
});

describe('runtime WebSocket adapter', () => {
  it('keeps final message rendering separate from run termination', () => {
    const adapter = new RuntimeWebSocketAdapter();
    const message = adapter.consume({
      ...envelope(1, 'message.finished'),
      type: 'message.finished',
      message: { message_id: 'message-1', role: 'assistant', content: '完成' },
      metadata: {
        tcode: {
          legacy: { type: 'message', message_id: 'message-1', content: '完成', done: true },
        },
      },
    });
    expect(message.uiEvent?.done).toBe(false);

    const terminal = adapter.consume({
      ...envelope(2, 'run.finished'),
      type: 'run.finished',
      outcome: 'completed',
      metadata: {
        tcode: {
          legacy: { type: 'message', message_id: 'message-1', done: true },
        },
      },
    });
    expect(terminal.uiEvent).toMatchObject({ type: 'run_v2_terminal', done: true });
  });

  it('projects harness tools and permissions into the existing chat UI contract', () => {
    const adapter = new RuntimeWebSocketAdapter();
    adapter.consume({
      ...envelope(1, 'run.started'),
      type: 'run.started',
      agent_id: 'default',
      metadata: { tcode: { runtime_id: 'opencode' } },
    });
    const tool = adapter.consume({
      ...envelope(2, 'tool.started'),
      type: 'tool.started',
      tool_call_id: 'tool-1',
      tool_name: 'write',
      arguments: { path: 'README.md' },
    });
    expect(tool.uiEvent).toMatchObject({
      type: 'status',
      status: 'using_tool',
      tool_name: 'write',
      call_id: 'tool-1',
    });
    const permission = adapter.consume({
      ...envelope(3, 'interaction.requested'),
      type: 'interaction.requested',
      interaction_id: 'permission-1',
      kind: 'permission',
      prompt: '允许写文件吗？',
      payload: {
        toolCall: { toolCallId: 'tool-1', title: '写文件', rawInput: { path: 'README.md' } },
        options: [{ optionId: 'allow_once', kind: 'allow_once' }],
      },
    });
    expect(permission.uiEvent).toMatchObject({
      type: 'permission',
      request_id: 'permission-1',
      tool_name: '写文件',
      arguments: { path: 'README.md' },
    });
  });
});
