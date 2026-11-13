import type {
  RuntimeEvent,
  RuntimeMessage,
  RuntimeToolCall,
} from './generated';

export type RunProjection = {
  runId: string;
  status: string;
  outcome?: string;
  error?: string;
};

export type ActivityProjection = {
  activityId: string;
  phase: string;
  status: string;
  message: string;
  current?: number | null;
  total?: number | null;
  unit?: string;
  stream?: string;
  data: Record<string, unknown>;
};

export type InteractionProjection = {
  interactionId: string;
  kind: string;
  status: string;
  prompt?: string;
  payload: Record<string, unknown>;
};

export type RuntimeProjection = {
  sessionId: string;
  streamId: string;
  lastSequence: number;
  lastEventId: string;
  needsSnapshot: boolean;
  seenEventIds: string[];
  diagnostics: string[];
  runs: Record<string, RunProjection>;
  messages: Record<string, RuntimeMessage>;
  messageOrder: string[];
  tools: Record<string, RuntimeToolCall>;
  activities: Record<string, ActivityProjection>;
  interactions: Record<string, InteractionProjection>;
  subagents: Record<string, Record<string, unknown>>;
  state: Record<string, unknown>;
};

const MAX_SEEN_EVENTS = 4096;
const MAX_DIAGNOSTICS = 50;

export function createRuntimeProjection(sessionId = ''): RuntimeProjection {
  return {
    sessionId,
    streamId: '',
    lastSequence: 0,
    lastEventId: '',
    needsSnapshot: false,
    seenEventIds: [],
    diagnostics: [],
    runs: {},
    messages: {},
    messageOrder: [],
    tools: {},
    activities: {},
    interactions: {},
    subagents: {},
    state: {},
  };
}

function diagnostic(state: RuntimeProjection, message: string): RuntimeProjection {
  return {
    ...state,
    needsSnapshot: true,
    diagnostics: [...state.diagnostics, message].slice(-MAX_DIAGNOSTICS),
  };
}

function applyJsonPatch(
  source: Record<string, unknown>,
  operations: Array<Record<string, unknown>>,
): Record<string, unknown> {
  const document = structuredClone(source);
  const decode = (part: string) => part.replace(/~1/g, '/').replace(/~0/g, '~');
  const partsFor = (path: unknown) => {
    const value = String(path || '');
    if (!value.startsWith('/')) throw new Error(`invalid JSON pointer: ${value}`);
    return value.slice(1).split('/').map(decode);
  };
  const lookup = (parts: string[]) => {
    let node: unknown = document;
    for (const part of parts) {
      if (!node || typeof node !== 'object') throw new Error('JSON pointer parent is not an object');
      node = (node as Record<string, unknown>)[part];
    }
    return node;
  };
  const write = (parts: string[], value: unknown, remove = false) => {
    if (parts.length === 0) throw new Error('root JSON patch is not supported');
    const parent = lookup(parts.slice(0, -1));
    if (!parent || typeof parent !== 'object') throw new Error('JSON pointer parent is not an object');
    const key = parts[parts.length - 1];
    if (Array.isArray(parent)) {
      const index = key === '-' ? parent.length : Number(key);
      if (!Number.isInteger(index) || index < 0 || index > parent.length) {
        throw new Error('invalid JSON patch array index');
      }
      if (remove) parent.splice(index, 1);
      else if (index === parent.length) parent.push(value);
      else parent[index] = value;
      return;
    }
    if (remove) delete (parent as Record<string, unknown>)[key];
    else (parent as Record<string, unknown>)[key] = value;
  };

  for (const operation of operations) {
    const op = String(operation.op || '');
    const parts = partsFor(operation.path);
    if (op === 'add' || op === 'replace') write(parts, structuredClone(operation.value));
    else if (op === 'remove') write(parts, undefined, true);
    else if (op === 'copy' || op === 'move') {
      const from = partsFor(operation.from || operation.from_path);
      const value = structuredClone(lookup(from));
      if (op === 'move') write(from, undefined, true);
      write(parts, value);
    } else if (op === 'test') {
      if (JSON.stringify(lookup(parts)) !== JSON.stringify(operation.value)) {
        throw new Error('JSON patch test failed');
      }
    } else {
      throw new Error(`unsupported JSON patch operation: ${op}`);
    }
  }
  return document;
}

export function reduceRuntimeEvent(
  previous: RuntimeProjection,
  event: RuntimeEvent,
): RuntimeProjection {
  const tcodeMetadata = event.metadata?.tcode;
  const incomingStreamId = tcodeMetadata && typeof tcodeMetadata === 'object'
    ? String((tcodeMetadata as Record<string, unknown>).stream_id || '')
    : '';
  if (previous.streamId && incomingStreamId && previous.streamId !== incomingStreamId) {
    previous = {
      ...createRuntimeProjection(event.session_id),
      diagnostics: [
        ...previous.diagnostics,
        `runtime stream changed: ${previous.streamId} -> ${incomingStreamId}`,
      ].slice(-MAX_DIAGNOSTICS),
    };
  }
  if (previous.sessionId && previous.sessionId !== event.session_id) {
    return diagnostic(previous, `session mismatch: ${event.session_id}`);
  }
  if (previous.seenEventIds.includes(event.event_id)) return previous;
  const isResyncSnapshot = event.type === 'state.snapshot' || event.type === 'messages.snapshot';
  if (
    previous.lastSequence
    && event.sequence !== previous.lastSequence + 1
    && !isResyncSnapshot
  ) {
    return diagnostic(
      previous,
      `sequence gap: expected ${previous.lastSequence + 1}, got ${event.sequence}`,
    );
  }
  if (event.sequence <= previous.lastSequence) {
    return diagnostic(previous, `sequence moved backwards: ${event.sequence}`);
  }

  let next: RuntimeProjection = {
    ...previous,
    sessionId: event.session_id,
    streamId: incomingStreamId || previous.streamId,
    lastSequence: event.sequence,
    lastEventId: event.event_id,
    seenEventIds: [...previous.seenEventIds, event.event_id].slice(-MAX_SEEN_EVENTS),
  };

  switch (event.type) {
    case 'run.started':
      next = {
        ...next,
        runs: { ...next.runs, [event.run_id]: { runId: event.run_id, status: event.type } },
      };
      break;
    case 'run.finished':
      next = {
        ...next,
        runs: {
          ...next.runs,
          [event.run_id]: { runId: event.run_id, status: event.type, outcome: event.outcome },
        },
      };
      break;
    case 'run.error':
      next = {
        ...next,
        runs: {
          ...next.runs,
          [event.run_id]: { runId: event.run_id, status: event.type, error: event.message },
        },
      };
      break;
    case 'run.cancelled':
    case 'run.interrupted':
      next = {
        ...next,
        runs: { ...next.runs, [event.run_id]: { runId: event.run_id, status: event.type } },
      };
      break;
    case 'message.started': {
      const existing = next.messages[event.message_id];
      next = {
        ...next,
        messages: {
          ...next.messages,
          [event.message_id]: existing || {
            message_id: event.message_id,
            role: event.role,
            content: '',
          },
        },
        messageOrder: next.messageOrder.includes(event.message_id)
          ? next.messageOrder
          : [...next.messageOrder, event.message_id],
      };
      break;
    }
    case 'message.delta': {
      const message = next.messages[event.message_id];
      if (!message) return diagnostic(next, `message.delta without start: ${event.message_id}`);
      next = {
        ...next,
        messages: {
          ...next.messages,
          [event.message_id]: { ...message, content: `${message.content || ''}${event.delta}` },
        },
      };
      break;
    }
    case 'message.finished':
      next = {
        ...next,
        messages: { ...next.messages, [event.message.message_id]: event.message },
        messageOrder: next.messageOrder.includes(event.message.message_id)
          ? next.messageOrder
          : [...next.messageOrder, event.message.message_id],
      };
      break;
    case 'messages.snapshot':
      next = {
        ...next,
        needsSnapshot: false,
        messages: Object.fromEntries((event.messages || []).map((item) => [item.message_id, item])),
        messageOrder: (event.messages || []).map((item) => item.message_id),
      };
      break;
    case 'tool.started':
      next = {
        ...next,
        tools: {
          ...next.tools,
          [event.tool_call_id]: {
            tool_call_id: event.tool_call_id,
            tool_name: event.tool_name,
            arguments: event.arguments || {},
            state: 'running',
          },
        },
      };
      break;
    case 'tool.args':
      break;
    case 'tool.args_finished': {
      const tool = next.tools[event.tool_call_id];
      if (!tool) return diagnostic(next, `tool.args_finished without start: ${event.tool_call_id}`);
      next = {
        ...next,
        tools: {
          ...next.tools,
          [event.tool_call_id]: { ...tool, arguments: event.arguments || {} },
        },
      };
      break;
    }
    case 'tool.result': {
      const tool = next.tools[event.tool_call_id];
      if (!tool) return diagnostic(next, `tool.result without start: ${event.tool_call_id}`);
      next = {
        ...next,
        tools: {
          ...next.tools,
          [event.tool_call_id]: {
            ...tool,
            state: event.state,
            result: event.result,
            error: event.error,
          },
        },
      };
      break;
    }
    case 'activity.snapshot':
    case 'activity.delta':
    case 'activity.finished':
      next = {
        ...next,
        activities: {
          ...next.activities,
          [event.activity_id]: {
            activityId: event.activity_id,
            phase: event.phase || '',
            status: event.type === 'activity.finished' ? 'finished' : event.status || 'running',
            message: event.message || '',
            current: event.current,
            total: event.total,
            unit: event.unit,
            stream: event.stream,
            data: event.data || {},
          },
        },
      };
      break;
    case 'interaction.requested':
      next = {
        ...next,
        interactions: {
          ...next.interactions,
          [event.interaction_id]: {
            interactionId: event.interaction_id,
            kind: event.kind,
            status: 'requested',
            prompt: event.prompt,
            payload: event.payload || {},
          },
        },
      };
      break;
    case 'interaction.resolved':
    case 'interaction.cancelled':
    case 'interaction.expired': {
      const interactions = { ...next.interactions };
      delete interactions[event.interaction_id];
      next = { ...next, interactions };
      break;
    }
    case 'subagent.started':
    case 'subagent.finished':
    case 'subagent.error':
      next = {
        ...next,
        subagents: {
          ...next.subagents,
          [event.subagent_run_id]: event as unknown as Record<string, unknown>,
        },
      };
      break;
    case 'state.snapshot':
      next = { ...next, needsSnapshot: false, state: event.state || {} };
      break;
    case 'state.delta':
      try {
        next = {
          ...next,
          state: applyJsonPatch(
            next.state,
            (event.patch || []) as unknown as Array<Record<string, unknown>>,
          ),
        };
      } catch (error) {
        return diagnostic(next, `state.delta failed: ${String(error)}`);
      }
      break;
  }
  return next;
}
