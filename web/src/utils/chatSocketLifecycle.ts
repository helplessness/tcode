export type ChatSocketCloseDisposition = 'unsettled' | 'terminal' | 'background';

/** Background task progress must not claim the foreground chat composer. */
export function isForegroundChatRunning(runtime: {
  chat_running?: boolean;
  running?: boolean;
  workflow_background_active?: boolean;
  child_session_active?: boolean;
}): boolean {
  return runtime.chat_running ?? Boolean(
    runtime.running || runtime.workflow_background_active || runtime.child_session_active,
  );
}

export type ChatSocketCloseKind =
  | 'intentional'
  | 'offline'
  | 'auth'
  | 'tenant'
  | 'server'
  | 'network';

export function shouldKeepRunningAfterSocketClose(
  disposition: ChatSocketCloseDisposition,
  hasBackground: boolean,
  hasGuidance: boolean,
): boolean {
  if (disposition === 'terminal') return false;
  if (disposition === 'background') return true;
  return hasBackground || hasGuidance;
}

export function shouldAdoptSocketSession(
  currentSessionId: string | null | undefined,
  initiatingSessionId: string | null | undefined,
  incomingSessionId: string | null | undefined,
): boolean {
  const incoming = String(incomingSessionId || '').trim();
  if (!incoming) return false;
  const current = String(currentSessionId || '').trim();
  const initiating = String(initiatingSessionId || '').trim();
  // A late event from a request running in another known session must never
  // steal the user's current selection. Server-side session remapping is only
  // adopted while the initiating session is still selected.
  return !current || current === initiating;
}

export function classifyChatSocketClose(
  code: number,
  online: boolean,
  disposition: ChatSocketCloseDisposition,
): ChatSocketCloseKind {
  if (disposition !== 'unsettled') return 'intentional';
  if (!online) return 'offline';
  if (code === 4401) return 'auth';
  if (code === 4423) return 'tenant';
  if (code === 1011 || code === 1012 || code === 1013) return 'server';
  return 'network';
}

export function chatSocketCloseStatusText(kind: ChatSocketCloseKind): string {
  if (kind === 'offline') return '网络已断开，恢复联网后将同步任务状态';
  if (kind === 'auth') return '登录状态已失效，请重新登录';
  if (kind === 'tenant') return '当前租户不可用，请切换租户或联系管理员';
  if (kind === 'server') return '服务连接中断，正在确认任务状态';
  if (kind === 'network') return '连接已中断，正在确认任务状态';
  return '';
}

export function chatPollDelayMs(
  running: boolean,
  consecutiveFailures: number,
  randomValue = Math.random(),
): number {
  const baseDelay = running ? 3_000 : 15_000;
  const failureCount = Math.max(0, Math.min(5, Math.floor(consecutiveFailures || 0)));
  const backedOffDelay = Math.min(60_000, baseDelay * (2 ** failureCount));
  const normalizedRandom = Math.max(0, Math.min(1, Number(randomValue) || 0));
  const jitter = 0.9 + normalizedRandom * 0.2;
  return Math.min(60_000, Math.round(backedOffDelay * jitter));
}
