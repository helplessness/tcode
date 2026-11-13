import { For, Show, createSignal, onCleanup, onMount } from 'solid-js';
import {
  getSession,
  listAsyncCompletions,
  markAsyncCompletionRead,
  markAllAsyncCompletionsRead,
  type AsyncCompletionEvent,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { formatAsyncCompletionNotice } from '../utils/asyncResultSummary';
import { desktop } from '../desktop';
import { composerIdentity } from '../stores/composer';
import { desktopNotificationLedger } from '../utils/desktopNotifications';


export default function AsyncCompletionNotifications() {
  const [events, setEvents] = createSignal<AsyncCompletionEvent[]>([]);
  const [dismissed, setDismissed] = createSignal<Set<string>>(new Set());
  const [markingRead, setMarkingRead] = createSignal<Set<string>>(new Set());
  const [readErrors, setReadErrors] = createSignal<Record<string, string>>({});
  const [markingAllRead, setMarkingAllRead] = createSignal(false);
  const [bulkReadError, setBulkReadError] = createSignal('');
  const [preferencesVersion, setPreferencesVersion] = createSignal(0);
  let polling = false;
  let disposed = false;
  let notificationRevision = 0;
  const identity = composerIdentity();
  const isCurrent = () => !disposed && composerIdentity() === identity;
  const ledger = desktop ? desktopNotificationLedger(identity, localStorage) : undefined;
  const nativeEvents = new Map<string, AsyncCompletionEvent>();
  const notice = (event: AsyncCompletionEvent) => formatAsyncCompletionNotice(
    event, sessionStore.sessions().find(session => session.session_id === event.session_id)?.title,
  );
  const timestampMs = (value: unknown, fallback: number) => {
    const parsed = Number(value || 0);
    if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
    return parsed < 1_000_000_000_000 ? parsed * 1000 : parsed;
  };

  const sync = async () => {
    if (polling || markingAllRead()) return;
    polling = true;
    const revision = notificationRevision;
    try {
      const result = await listAsyncCompletions({ unreadOnly: true });
      if (!isCurrent() || revision !== notificationRevision) return;
      const unread = Array.isArray(result.events) ? result.events : [];
      setEvents(unread);
      const counts = new Map<string, number>();
      for (const event of unread) {
        const sessionId = String(event.session_id || '').trim();
        if (sessionId) counts.set(sessionId, (counts.get(sessionId) || 0) + 1);
      }
      sessionStore.setSessions((previous) => previous.map((session) => ({
        ...session,
        async_completion_unread_count: counts.get(session.session_id) || 0,
      })));
      if (desktop && ledger && localStorage.getItem('tcode.settings.notify') !== '0') {
        for (const event of unread) nativeEvents.set(JSON.stringify([identity, event.id]), event);
        // Bound the initial catch-up after reopening, retaining all unread cards in-app.
        const fresh = unread.filter(event => !ledger.has(event.id));
        for (const event of fresh.slice(3)) ledger.remember(event.id);
        for (const event of fresh.slice(0, 3)) {
          if (!isCurrent()) return;
          const id = JSON.stringify([identity, event.id]);
          nativeEvents.set(id, event);
          if (nativeEvents.size > 1000) nativeEvents.delete(nativeEvents.keys().next().value!);
          try {
            const display = notice(event);
            await desktop.notify({ id, title: display.title, body: display.body });
            if (isCurrent()) ledger.remember(event.id);
          } catch { /* Keep in-app cards available if native notifications fail. */ }
        }
      }
    } catch (error) {
      console.error('Failed to load async completion notifications:', error);
    } finally {
      polling = false;
    }
  };

  onMount(() => {
    void sync();
    const timer = window.setInterval(() => void sync(), 3000);
    const unsubscribe = desktop?.onNotification(id => {
      const event = nativeEvents.get(id);
      if (event && isCurrent()) void openSession(event);
    });
    const settingsChanged = () => setPreferencesVersion(value => value + 1);
    window.addEventListener('tcode:settings-changed', settingsChanged);
    onCleanup(() => { disposed = true; window.clearInterval(timer); unsubscribe?.(); nativeEvents.clear(); window.removeEventListener('tcode:settings-changed', settingsChanged); });
  });

  const visibleEvents = () => {
    preferencesVersion();
    if (localStorage.getItem('tcode.settings.inAppNotify') === '0') return [];
    return events().filter((event) => !dismissed().has(event.id)).slice(0, 3);
  };

  const dismiss = (eventId: string) => {
    setDismissed((previous) => new Set([...previous, eventId]));
  };

  const markRead = async (event: AsyncCompletionEvent) => {
    if (!isCurrent() || markingAllRead() || markingRead().has(event.id)) return;
    setMarkingRead(previous => new Set([...previous, event.id]));
    setReadErrors(previous => ({ ...previous, [event.id]: '' }));
    try {
      await markAsyncCompletionRead(event.id);
      if (!isCurrent()) return;
      notificationRevision++;
      dismiss(event.id);
      const remaining = events().filter(item => item.id !== event.id);
      setEvents(remaining);
      sessionStore.setSessions(previous => previous.map(session => session.session_id === event.session_id
        ? { ...session, async_completion_unread_count: remaining.filter(item => item.session_id === event.session_id).length }
        : session));
    } catch (error) {
      if (isCurrent()) setReadErrors(previous => ({ ...previous, [event.id]: error instanceof Error ? error.message : '标记已读失败，请重试' }));
    } finally {
      if (isCurrent()) setMarkingRead(previous => new Set([...previous].filter(id => id !== event.id)));
    }
  };

  const markAllRead = async () => {
    if (!isCurrent() || markingAllRead() || markingRead().size > 0) return;
    setMarkingAllRead(true);
    setBulkReadError('');
    notificationRevision++;
    try {
      await markAllAsyncCompletionsRead();
      if (!isCurrent()) return;
      setEvents([]);
      setReadErrors({});
      sessionStore.setSessions(previous => previous.map(session => ({ ...session, async_completion_unread_count: 0 })));
    } catch (error) {
      if (isCurrent()) setBulkReadError(error instanceof Error ? error.message : '全部标记已读失败，请重试');
    } finally {
      if (isCurrent()) setMarkingAllRead(false);
    }
  };

  const openSession = async (event: AsyncCompletionEvent) => {
    const sessionId = String(event.session_id || '').trim();
    if (!sessionId) return;
    try {
      const detail = await getSession(sessionId, { bustCache: true });
      if (!isCurrent()) return;
      if (detail) {
        await markAsyncCompletionRead(event.id);
        if (!isCurrent()) return;
        notificationRevision++;
        const existing = sessionStore.sessions().find((item) => item.session_id === sessionId);
        sessionStore.upsertSession({
          ...existing,
          session_id: sessionId,
          title: detail.title || existing?.title,
          messages: detail.messages || [],
          created_at: timestampMs(detail.created_at, existing?.created_at || Date.now()),
          updated_at: timestampMs(detail.updated_at, Date.now()),
          agent_name: detail.agent_name || existing?.agent_name,
          status: detail.status || existing?.status,
          workspace_id: detail.workspace_id || existing?.workspace_id,
          workdir: detail.workdir || existing?.workdir,
          workdir_name: detail.workdir_name || existing?.workdir_name,
          metadata: detail.metadata || existing?.metadata,
          async_completion_unread_count: 0,
        });
        sessionStore.switchSession(sessionId);
        sessionStore.setMessages(detail.messages || []);
      }
      setEvents((previous) => previous.filter((item) => item.id !== event.id));
      dismiss(event.id);
    } catch (error) {
      if (!isCurrent()) return;
      sessionStore.setStatus(
        error instanceof Error ? error.message : '打开异步任务原会话失败',
      );
    }
  };

  return (
    <Show when={visibleEvents().length > 0}>
      <div class="fixed bottom-3 right-3 z-[110] flex w-[min(340px,calc(100vw-1.5rem))] flex-col gap-1.5">
        <div class="rounded-lg border px-2.5 py-1.5" style={{ background: 'var(--tcode-surface)', 'border-color': 'var(--tcode-border)', color: 'var(--tcode-text)' }}>
          <div class="flex items-center justify-between gap-3">
            <span class="text-[11px]">异步任务 · {events().length} 条未读</span>
            <button type="button" class="shrink-0 rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:cursor-wait disabled:opacity-60"
              style={{ 'border-color': 'var(--tcode-border-strong)' }} disabled={markingAllRead() || markingRead().size > 0}
              onClick={() => void markAllRead()}>{markingAllRead() ? '标记中…' : '全部已读'}</button>
          </div>
          <Show when={bulkReadError()}><div role="alert" class="mt-2 text-xs" style={{ color: 'var(--tcode-danger)' }}>{bulkReadError()}，可以重试。</div></Show>
        </div>
        <For each={visibleEvents()}>
          {(event) => (
            <div class="tcode-result-notice overflow-hidden rounded-lg border" data-status={event.status} role="status">
              <div class="px-3 py-2">
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0 flex-1">
                    <div class="tcode-result-notice-title">
                      {notice(event).title}
                    </div>
                    <div class="tcode-result-notice-body mt-1 line-clamp-2 whitespace-pre-line break-words" title={notice(event).body}>
                      {notice(event).body}
                    </div>
                    <details class="tcode-result-notice-hint mt-1">
                      <summary class="cursor-pointer">技术详情</summary>
                      <div class="mt-2 break-all">任务编号：{event.identifier}</div>
                      <Show when={event.target_tool}><div class="break-all">工具：{event.target_tool}</div></Show>
                      <Show when={notice(event).details}>
                        <pre class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all">{notice(event).details}</pre>
                      </Show>
                    </details>
                  </div>
                </div>
                <div class="mt-2 flex flex-wrap items-center gap-1.5">
                  <button
                    type="button"
                    class="rounded-full border border-[#8ff0c8]/38 bg-[#8ff0c8]/12 px-3 py-1.5 text-[11px] font-semibold text-[#c9ffe9]"
                    onClick={() => void openSession(event)}
                  >
                    查看原会话
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#9faeb7]"
                    onClick={() => dismiss(event.id)}
                  >
                    稍后处理
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#dee3e8] disabled:cursor-wait disabled:opacity-60"
                    disabled={markingAllRead() || markingRead().has(event.id)}
                    onClick={() => void markRead(event)}
                  >
                    {markingRead().has(event.id) ? '标记中…' : '已读'}
                  </button>
                </div>
                <Show when={readErrors()[event.id]}><div role="alert" class="mt-2 text-xs text-[var(--tcode-danger)]">标记已读失败：{readErrors()[event.id]}。可以重试。</div></Show>
              </div>
            </div>
          )}
        </For>
      </div>
    </Show>
  );
}
