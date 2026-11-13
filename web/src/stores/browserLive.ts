import { createRoot, createSignal } from 'solid-js';

export type BrowserLiveSession = {
  key: string;
  parentSessionId: string;
  agentSessionId: string;
  browserSessionId: string;
  workspaceId: string;
  status: string;
  action: string;
  url: string;
  title: string;
  frameSeq: number;
  error: string;
  updatedAt: number;
  hidden: boolean;
};

const normalizedText = (value: unknown) => String(value || '').trim();
const terminalBrowserStatuses = new Set(['closed', 'idle_timeout', 'stopped']);
const storageKey = 'tcode.browserLive.sessions.v1';

const loadSessions = (): BrowserLiveSession[] => {
  if (typeof sessionStorage === 'undefined') return [];
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
    return Array.isArray(value) ? value.slice(-12) : [];
  } catch {
    return [];
  }
};

const persistSessions = (sessions: BrowserLiveSession[]) => {
  if (typeof sessionStorage === 'undefined') return;
  try {
    const reconnectable = sessions.slice(-12).map((session) => ({
      ...session,
      url: '',
      title: '',
      error: '',
    }));
    sessionStorage.setItem(storageKey, JSON.stringify(reconnectable));
  } catch {
    // A live viewer can continue even when browser storage is unavailable.
  }
};

export const browserLiveStore = createRoot(() => {
  const [sessions, setSessions] = createSignal<BrowserLiveSession[]>(loadSessions());

  const upsert = (input: Partial<BrowserLiveSession> & {
    parentSessionId: string;
    browserSessionId: string;
  }) => {
    const parentSessionId = normalizedText(input.parentSessionId);
    const browserSessionId = normalizedText(input.browserSessionId);
    if (!parentSessionId || !browserSessionId) return;
    const key = `${parentSessionId}:${browserSessionId}`;
    setSessions((current) => {
      const index = current.findIndex((item) => item.key === key);
      const previous = index >= 0 ? current[index] : undefined;
      const status = normalizedText(input.status || previous?.status || 'starting');
      const terminal = terminalBrowserStatuses.has(status.toLowerCase());
      const next: BrowserLiveSession = {
        key,
        parentSessionId,
        agentSessionId: normalizedText(input.agentSessionId || previous?.agentSessionId),
        browserSessionId,
        workspaceId: normalizedText(input.workspaceId || previous?.workspaceId),
        status,
        action: normalizedText(input.action ?? previous?.action),
        url: terminal ? '' : normalizedText(input.url ?? previous?.url),
        title: terminal ? '' : normalizedText(input.title ?? previous?.title),
        frameSeq: terminal ? 0 : Math.max(0, Number(input.frameSeq ?? previous?.frameSeq ?? 0) || 0),
        error: terminal ? '' : normalizedText(input.error ?? previous?.error),
        updatedAt: Date.now(),
        hidden: input.hidden ?? previous?.hidden ?? current.find((item) => item.parentSessionId === parentSessionId)?.hidden ?? false,
      };
      if (index < 0) {
        const updated = [...current, next].slice(-12);
        persistSessions(updated);
        return updated;
      }
      const updated = [...current];
      updated[index] = next;
      persistSessions(updated);
      return updated;
    });
  };

  const upsertFromAgentEvent = (data: Record<string, any>, fallbackParentSessionId: string) => {
    upsert({
      parentSessionId: normalizedText(data.parent_session_id || fallbackParentSessionId),
      agentSessionId: normalizedText(data.session_id),
      browserSessionId: normalizedText(data.browser_session_id),
      workspaceId: normalizedText(data.workspace_id),
      status: normalizedText(data.status),
      action: normalizedText(data.action),
      url: normalizedText(data.url),
      title: normalizedText(data.title),
      frameSeq: Number(data.frame_seq || 0),
    });
  };

  const activeForParent = (parentSessionId: string) => {
    const normalized = normalizedText(parentSessionId);
    return sessions()
      .filter((item) => item.parentSessionId === normalized)
      .sort((left, right) => right.updatedAt - left.updatedAt)[0] || null;
  };

  const setHidden = (key: string, hidden: boolean) => {
    setSessions((current) => {
      const selected = current.find((item) => item.key === key);
      if (!selected) return current;
      const updated = current.map((item) => (
        item.parentSessionId === selected.parentSessionId ? { ...item, hidden } : item
      ));
      persistSessions(updated);
      return updated;
    });
  };

  return {
    sessions,
    upsert,
    upsertFromAgentEvent,
    activeForParent,
    setHidden,
  };
});
