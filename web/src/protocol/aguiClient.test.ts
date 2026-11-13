import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  EventSchemas,
  EventType,
  RunAgentInputSchema,
  type BaseEvent,
  type RunAgentInput,
} from '@ag-ui/core';
import { createTCodeAGUIAgent } from './aguiClient';

const repoRoot = path.resolve(process.cwd(), '..');

function mappedProtocolExamples(): unknown[] {
  const program = [
    'import json',
    'from tcode_agent.protocol.v2 import protocol_event_examples, parse_runtime_event',
    'from tcode_agent.agui.mapper import runtime_event_to_agui',
    'print(json.dumps([runtime_event_to_agui(parse_runtime_event(v)) for v in protocol_event_examples().values()]))',
  ].join(';');
  return JSON.parse(execFileSync('python', ['-c', program], {
    cwd: repoRoot,
    encoding: 'utf8',
  }));
}

describe('AG-UI 0.0.59 interoperability', () => {
  it('validates every TCode v2 mapping with the official event schemas', () => {
    const events = mappedProtocolExamples();
    expect(events.length).toBeGreaterThan(20);
    for (const event of events) expect(EventSchemas.safeParse(event).success).toBe(true);
  });

  it('accepts TCode forwardedProps in the official RunAgentInput schema', () => {
    const input = {
      threadId: 'thread-1',
      runId: 'run-1',
      state: {},
      messages: [{ id: 'user-1', role: 'user', content: '你好' }],
      tools: [],
      context: [],
      forwardedProps: {
        tcode: { workspaceId: 'workspace-1', runtimeId: 'tcode-native' },
      },
    };
    expect(RunAgentInputSchema.safeParse(input).success).toBe(true);
  });

  it('streams through the official HttpAgent implementation', async () => {
    const source: BaseEvent[] = [
      { type: EventType.RUN_STARTED, threadId: 'thread-1', runId: 'run-1' },
      { type: EventType.TEXT_MESSAGE_START, messageId: 'assistant-1', role: 'assistant' },
      { type: EventType.TEXT_MESSAGE_CONTENT, messageId: 'assistant-1', delta: '完成' },
      { type: EventType.TEXT_MESSAGE_END, messageId: 'assistant-1' },
      { type: EventType.RUN_FINISHED, threadId: 'thread-1', runId: 'run-1', outcome: { type: 'success' } },
    ];
    let submitted: RunAgentInput | null = null;
    const agent = createTCodeAGUIAgent({
      baseUrl: 'http://tcode.local',
      workspaceId: 'workspace-1',
      runtimeId: 'opencode',
      modelId: 'deepseek/deepseek-v4-flash',
      fetch: async (_url, init) => {
        submitted = JSON.parse(String(init.body || '{}'));
        return new Response(
          source.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''),
          { headers: { 'content-type': 'text/event-stream' } },
        );
      },
    });
    const input: RunAgentInput = {
      threadId: 'thread-1',
      runId: 'run-1',
      state: {},
      messages: [{ id: 'user-1', role: 'user', content: '测试' }],
      tools: [],
      context: [],
      forwardedProps: {},
    };
    const received = await new Promise<BaseEvent[]>((resolve, reject) => {
      const values: BaseEvent[] = [];
      agent.run(input).subscribe({
        next: (event) => values.push(event),
        error: reject,
        complete: () => resolve(values),
      });
    });
    expect(received.map((event) => event.type)).toEqual(source.map((event) => event.type));
    const posted = submitted as RunAgentInput | null;
    expect(posted && posted.forwardedProps).toMatchObject({
      tcode: {
        workspaceId: 'workspace-1',
        runtimeId: 'opencode',
        modelId: 'deepseek/deepseek-v4-flash',
      },
    });
  });
});
