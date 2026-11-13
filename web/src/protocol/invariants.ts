import type { RuntimeEvent } from './generated';

export const AGENT_EVENTS_PROTOCOL_ID = 'tcode.agent.events' as const;
export const AGENT_EVENTS_PROTOCOL_VERSION = '2.0.0' as const;

export class ProtocolInvariantError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ProtocolInvariantError';
  }
}

export function isRuntimeEvent(value: unknown): value is RuntimeEvent {
  if (!value || typeof value !== 'object') return false;
  const event = value as Record<string, unknown>;
  return (
    event.protocol === AGENT_EVENTS_PROTOCOL_ID &&
    event.protocol_version === AGENT_EVENTS_PROTOCOL_VERSION &&
    typeof event.type === 'string' &&
    event.type.length > 0 &&
    typeof event.event_id === 'string' &&
    event.event_id.length > 0 &&
    typeof event.session_id === 'string' &&
    event.session_id.length > 0 &&
    typeof event.run_id === 'string' &&
    event.run_id.length > 0 &&
    Number.isInteger(event.sequence) &&
    Number(event.sequence) > 0
  );
}

export function requireRuntimeEvent(value: unknown): RuntimeEvent {
  if (!isRuntimeEvent(value)) {
    throw new ProtocolInvariantError('WebSocket frame is not a TCode Agent Events v2 envelope');
  }
  return value;
}

export function legacyPayload(event: RuntimeEvent): Record<string, unknown> | null {
  const tcode = event.metadata?.tcode;
  if (!tcode || typeof tcode !== 'object') return null;
  const legacy = (tcode as Record<string, unknown>).legacy;
  if (!legacy || typeof legacy !== 'object' || Array.isArray(legacy)) return null;
  return { ...(legacy as Record<string, unknown>) };
}
