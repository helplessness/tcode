import { describe, expect, it } from 'vitest';

import { mergeToolCalls } from './chat';

describe('mergeToolCalls', () => {
  it('does not regress a completed tool call to running', () => {
    const [toolCall] = mergeToolCalls(
      [{
        tool_name: 'task',
        call_id: 'call-1',
        arguments: { wait: false },
        state: 'completed',
        output: 'Background subagent started',
      }],
      [{
        tool_name: 'task',
        call_id: 'call-1',
        arguments: { wait: false },
        state: 'running',
      }],
    );

    expect(toolCall.state).toBe('completed');
    expect(toolCall.output).toBe('Background subagent started');
  });

  it('still advances a running tool call to completed', () => {
    const [toolCall] = mergeToolCalls(
      [{ tool_name: 'task', call_id: 'call-1', arguments: {}, state: 'running' }],
      [{ tool_name: 'task', call_id: 'call-1', arguments: {}, state: 'completed', output: 'done' }],
    );

    expect(toolCall.state).toBe('completed');
    expect(toolCall.output).toBe('done');
  });
});
