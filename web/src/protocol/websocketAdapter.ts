import type { RuntimeEvent } from './generated';
import { isRuntimeEvent, legacyPayload } from './invariants';
import {
  createRuntimeProjection,
  reduceRuntimeEvent,
  type RuntimeProjection,
} from './reducer';

export type AdaptedWebSocketFrame = {
  runtimeEvent: RuntimeEvent | null;
  uiEvent: Record<string, any> | null;
  projection: RuntimeProjection | null;
};

export class RuntimeWebSocketAdapter {
  private projections = new Map<string, RuntimeProjection>();

  consume(raw: unknown): AdaptedWebSocketFrame {
    if (!isRuntimeEvent(raw)) {
      return {
        runtimeEvent: null,
        uiEvent: raw && typeof raw === 'object' && !Array.isArray(raw)
          ? raw as Record<string, any>
          : null,
        projection: null,
      };
    }

    const previous = this.projections.get(raw.session_id)
      || createRuntimeProjection(raw.session_id);
    const projection = reduceRuntimeEvent(previous, raw);
    this.projections.set(raw.session_id, projection);
    if (projection.diagnostics.length > previous.diagnostics.length) {
      console.warn('[tcode-protocol-v2] projection diagnostic', {
        sessionId: raw.session_id,
        runId: raw.run_id,
        eventId: raw.event_id,
        sequence: raw.sequence,
        diagnostic: projection.diagnostics[projection.diagnostics.length - 1],
      });
    }

    const legacy = legacyPayload(raw);
    let uiEvent: Record<string, any> | null = legacy;
    if (legacy && raw.type === 'message.finished') {
      uiEvent = { ...legacy, done: false };
    } else if (legacy && raw.type === 'run.finished') {
      uiEvent = { ...legacy, type: 'run_v2_terminal', done: true };
    } else if (!legacy) {
      uiEvent = this.projectCoreEvent(raw);
    }
    return { runtimeEvent: raw, uiEvent, projection };
  }

  projection(sessionId: string): RuntimeProjection | null {
    return this.projections.get(sessionId) || null;
  }

  reset(sessionId?: string): void {
    if (sessionId) this.projections.delete(sessionId);
    else this.projections.clear();
  }

  private projectCoreEvent(event: RuntimeEvent): Record<string, any> | null {
    switch (event.type) {
      case 'run.started':
        const runtimeMetadata = event.metadata?.tcode as Record<string, unknown> | undefined;
        return {
          type: 'status',
          status: 'thinking',
          session_id: event.session_id,
          content: `正在通过 ${String(runtimeMetadata?.runtime_id || 'Harness')} 执行`,
        };
      case 'run.finished':
        return {
          type: 'run_v2_terminal',
          session_id: event.session_id,
          done: true,
          outcome_status: event.outcome,
          ...event.result,
        };
      case 'run.error':
        return {
          type: 'error',
          session_id: event.session_id,
          error: event.message,
          code: event.code,
          retryable: event.retryable,
        };
      case 'run.cancelled':
        return {
          type: 'status',
          status: 'cancelled',
          session_id: event.session_id,
          content: event.reason,
        };
      case 'run.interrupted':
        return {
          type: 'status',
          status: 'cancelled',
          session_id: event.session_id,
          content: event.reason,
        };
      case 'message.delta':
        return {
          type: 'message',
          role: 'assistant',
          session_id: event.session_id,
          message_id: event.message_id,
          delta: event.delta,
          done: false,
        };
      case 'message.finished':
        return {
          type: 'message',
          role: event.message.role,
          session_id: event.session_id,
          message_id: event.message.message_id,
          content: event.message.content,
          content_blocks: event.message.content_blocks || [],
          done: false,
        };
      case 'tool.started':
        return {
          type: 'status',
          status: 'using_tool',
          session_id: event.session_id,
          tool_name: event.tool_name,
          tool_args: event.arguments || {},
          call_id: event.tool_call_id,
          message_id: event.message_id || '',
        };
      case 'tool.args_finished':
        return null;
      case 'tool.result':
        return {
          type: 'tool_result',
          session_id: event.session_id,
          message_id: event.message_id || '',
          tool_call: {
            tool_name: event.tool_name,
            call_id: event.tool_call_id,
            output: typeof event.result === 'string' ? event.result : JSON.stringify(event.result),
            error: event.error || undefined,
            state: event.state,
          },
        };
      case 'activity.snapshot':
      case 'activity.delta':
      case 'activity.finished':
        return {
          type: 'agent_event',
          kind: 'process_note',
          session_id: event.session_id,
          phase: event.phase,
          status: event.status,
          message: event.message || event.phase,
          tool_name: event.tool_name || undefined,
          call_id: event.tool_call_id || undefined,
          current: event.current,
          total: event.total,
          unit: event.unit,
          stream: event.stream,
        };
      case 'interaction.requested': {
        if (event.kind === 'permission') {
          const payload = event.payload || {};
          const toolCall = payload.toolCall && typeof payload.toolCall === 'object'
            ? payload.toolCall as Record<string, unknown>
            : {};
          return {
            type: 'permission',
            session_id: event.session_id,
            request_id: event.interaction_id,
            tool_name: String(payload.tool_name || toolCall.title || 'Harness 工具'),
            arguments: payload.arguments && typeof payload.arguments === 'object'
              ? payload.arguments
              : toolCall.rawInput || {},
          };
        }
        if (event.kind === 'question') {
          return {
            type: 'question',
            session_id: event.session_id,
            request_id: event.interaction_id,
            question: {
              ...(event.payload || {}),
              question: String(event.payload?.question || event.prompt || '请补充信息'),
            },
          };
        }
        return null;
      }
      case 'interaction.resolved':
      case 'interaction.cancelled':
      case 'interaction.expired':
        return null;
      default:
        return null;
    }
  }
}

export const runtimeWebSocketAdapter = new RuntimeWebSocketAdapter();
