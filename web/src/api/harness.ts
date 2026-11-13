import { invalidateSessionGetCache } from './cache';

export interface HarnessSwitchRequest {
  target_runtime_id: string;
  model_id: string;
  expected_current_runtime_id: string;
  expected_switch_version: number;
}

export interface HarnessHandoffInfo {
  handoff_id: string;
  summary_status: 'generated' | 'fallback';
  inherited_rounds: number;
  truncated: boolean;
  warning_code?: string;
}

export interface HarnessSwitchNoticeValue {
  code: 'harness_mid_session_memory_risk';
  severity: 'warning';
  message: string;
}

export interface HarnessSwitchResponse {
  session_id: string;
  previous_runtime_id: string;
  current_runtime_id: string;
  model_id: string;
  switch_version: number;
  switched: boolean;
  mid_session: boolean;
  handoff: HarnessHandoffInfo | null;
  notice: HarnessSwitchNoticeValue | null;
}

export interface HarnessStateProjection {
  runtime_id: string;
  model_id: string;
  models?: Record<string, string>;
  switch_version: number;
  pending_handoff_id?: string;
  handoff_notice_pending?: boolean;
  summary_status?: 'generated' | 'fallback' | '';
  truncated?: boolean;
  warning_code?: string;
  previous_runtime_id?: string;
}

export class HarnessSwitchApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly currentState?: HarnessStateProjection,
  ) {
    super(message);
    this.name = 'HarnessSwitchApiError';
  }
}

export async function switchHarness(
  sessionId: string,
  request: HarnessSwitchRequest,
  idempotencyKey: string,
): Promise<HarnessSwitchResponse> {
  const response = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/harness-switch`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify(request),
    },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data?.detail && typeof data.detail === 'object' ? data.detail : data;
    throw new HarnessSwitchApiError(
      response.status,
      String(detail?.code || 'harness_switch_failed'),
      String(detail?.message || data?.detail || `Harness switch failed: ${response.status}`),
      detail?.current_state && typeof detail.current_state === 'object'
        ? detail.current_state as HarnessStateProjection
        : undefined,
    );
  }
  invalidateSessionGetCache(sessionId);
  return data as HarnessSwitchResponse;
}

export async function getHarnessState(sessionId: string): Promise<HarnessStateProjection> {
  const response = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/harness-state`,
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new HarnessSwitchApiError(
      response.status,
      String(data?.detail?.code || 'harness_state_failed'),
      String(data?.detail?.message || 'Harness state request failed'),
    );
  }
  return data as HarnessStateProjection;
}
