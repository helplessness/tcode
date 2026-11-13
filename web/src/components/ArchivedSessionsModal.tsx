import { For, Show, createMemo, createSignal, onMount } from 'solid-js';
import {
  getSession,
  getSessions,
  deleteSession as deleteSessionApi,
  unarchiveSession as unarchiveSessionApi,
  bootstrapSession,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { isFixedTeamBootstrapAgent } from '../utils/teamRegistry';

interface ArchivedSessionsModalProps {
  onClose?: () => void;
}

export default function ArchivedSessionsModal(props: ArchivedSessionsModalProps) {
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [busySessionId, setBusySessionId] = createSignal('');

  const loadArchivedSessions = async () => {
    setLoading(true);
    setError('');
    try {
      const sessions = await getSessions({ archived: true });
      sessionStore.mergeSessions(
        sessions.map((session) => ({
          session_id: session.session_id,
          title: session.title,
          status: session.status,
          is_archived: true,
          archived_at: session.archived_at ? session.archived_at * 1000 : undefined,
          messages: [],
          created_at: session.created_at * 1000,
          updated_at: session.updated_at * 1000,
          agent_name: session.agent_name,
          workdir: session.workdir,
          workdir_name: session.workdir_name,
          scheduler_mode: 'supervisor' as const,
        })),
        'archived',
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载归档历史失败');
    } finally {
      setLoading(false);
    }
  };

  onMount(() => {
    void loadArchivedSessions();
  });

  const archivedSessions = createMemo(() =>
    sessionStore
      .sessions()
      .filter((session) => session.is_archived)
      .sort(
        (a, b) =>
          Number(b.archived_at || b.updated_at || b.created_at || 0) -
          Number(a.archived_at || a.updated_at || a.created_at || 0),
      ),
  );

  const formatTime = (timestamp?: number) => {
    if (!timestamp) return '未知时间';
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return '刚刚';
    if (diffMins < 60) return `${diffMins} 分钟前`;
    if (diffHours < 24) return `${diffHours} 小时前`;
    if (diffDays < 7) return `${diffDays} 天前`;
    return date.toLocaleDateString();
  };

  const handleOpenSession = async (sessionId: string) => {
    setBusySessionId(sessionId);
    setError('');
    try {
      const sessionData = await getSession(sessionId, { bustCache: true });
      if (!sessionData) {
        throw new Error('会话不存在');
      }

      sessionStore.upsertSession({
        session_id: sessionId,
        title: sessionData.title,
        status: sessionData.status,
        is_archived: Boolean(sessionData.is_archived),
        archived_at: sessionData.archived_at
          ? sessionData.archived_at * 1000
          : undefined,
        messages: sessionData.messages,
        created_at: Number(sessionData.created_at || Date.now()),
        updated_at: Number(sessionData.updated_at || Date.now()),
        agent_name: sessionData.agent_name || '',
        workdir: String(sessionData.workdir || ''),
        workdir_name: String(sessionData.workdir_name || ''),
        scheduler_mode: 'supervisor',
      });
      sessionStore.switchSession(sessionId);

      const sessionAgentName = sessionData.agent_name || '';
      if (sessionAgentName && isFixedTeamBootstrapAgent(sessionAgentName)) {
        try {
          const resp = await bootstrapSession(sessionAgentName, sessionId);
          sessionStore.updateSessionAgent(
            sessionId,
            resp.agent_name || sessionAgentName,
          );
          sessionStore.refreshSubagents();
        } catch (e) {
          console.error('Failed to bootstrap fixed team from archive view:', e);
          sessionStore.setStatus('固定团队初始化失败');
        }
      }
      props.onClose?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '打开归档会话失败');
    } finally {
      setBusySessionId('');
    }
  };

  const handleUnarchive = async (event: Event, sessionId: string) => {
    event.stopPropagation();
    setBusySessionId(sessionId);
    setError('');
    try {
      await unarchiveSessionApi(sessionId);
      sessionStore.markSessionUnarchived(sessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : '取消归档失败');
    } finally {
      setBusySessionId('');
    }
  };

  const handleDelete = async (event: Event, sessionId: string) => {
    event.stopPropagation();
    setBusySessionId(sessionId);
    setError('');
    try {
      await deleteSessionApi(sessionId);
      sessionStore.deleteSession(sessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除归档会话失败');
    } finally {
      setBusySessionId('');
    }
  };

  return (
    <div
      class="fixed inset-0 z-[120] flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
      onClick={() => props.onClose?.()}
    >
      <div
        class="flex h-[min(82vh,760px)] w-full max-w-5xl flex-col overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.5)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div class="flex items-center justify-between border-b border-[#2a3136] px-6 py-5">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.24em] text-[#7bd0ff]">归档历史</div>
            <div class="mt-1 text-lg font-semibold text-[#f5f7fa]">归档历史会话</div>
            <div class="mt-1 text-sm text-[#8b949e]">查看已归档会话，或执行取消归档与删除。</div>
          </div>
          <button
            class="inline-flex h-9 items-center justify-center rounded-xl border border-[#303539] bg-[#171c20] px-4 text-sm text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            onClick={() => props.onClose?.()}
          >
            关闭
          </button>
        </div>

        <div class="flex-1 overflow-y-auto px-6 py-5">
          <Show when={!loading()} fallback={<div class="py-16 text-center text-sm text-[#8b949e]">正在加载归档历史...</div>}>
            <Show when={!error()} fallback={<div class="rounded-2xl border border-[#5a2d32] bg-[#2d1418]/70 px-4 py-3 text-sm text-[#ffb4ae]">{error()}</div>}>
              <Show
                when={archivedSessions().length > 0}
                fallback={
                  <div class="rounded-3xl border border-dashed border-[#374148] bg-[#161d22]/78 px-6 py-14 text-center text-[#8b949e]">
                    <div class="text-base font-semibold text-[#dee3e8]">还没有归档会话</div>
                    <div class="mt-2 text-sm">先在当前会话卡片或主视图里执行归档，历史记录会出现在这里。</div>
                  </div>
                }
              >
                <div class="space-y-4">
                  <For each={archivedSessions()}>
                    {(session) => {
                      const isBusy = () => busySessionId() === session.session_id;
                      return (
                        <div
                          role="button"
                          tabindex="0"
                          class="group rounded-3xl border border-[#2a3136] bg-[#141a1f] p-4 text-left transition-colors hover:border-[#3e484f] hover:bg-[#182128]"
                          onClick={() => void handleOpenSession(session.session_id)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault();
                              void handleOpenSession(session.session_id);
                            }
                          }}
                        >
                          <div class="flex items-start justify-between gap-4">
                            <div class="min-w-0 flex-1">
                              <div class="flex flex-wrap items-center gap-2">
                                <div class="truncate text-base font-semibold text-[#f5f7fa]">
                                  {session.title || session.messages[0]?.content || '归档会话'}
                                </div>
                                <span class="rounded-full border border-[#7bd0ff]/30 bg-[#7bd0ff]/10 px-2 py-0.5 text-[10px] font-semibold text-[#8ed5ff]">
                                  已归档
                                </span>
                              </div>
                              <div class="mt-2 flex flex-wrap items-center gap-3 text-xs text-[#8b949e]">
                                <span>归档于 {formatTime(session.archived_at)}</span>
                                <Show when={session.workdir_name}>
                                  <span>{session.workdir_name}</span>
                                </Show>
                              </div>
                              <Show when={session.messages[0]?.content || session.title}>
                                <div class="mt-3 line-clamp-2 text-sm text-[#bdc8d1]">
                                  {session.messages[0]?.content || session.title}
                                </div>
                              </Show>
                            </div>

                            <div class="flex flex-shrink-0 flex-wrap items-center gap-2">
                              <button
                                class="inline-flex h-9 items-center justify-center rounded-xl border border-[#303539] bg-[#171c20] px-3 text-sm font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  void handleOpenSession(session.session_id);
                                }}
                                disabled={isBusy()}
                              >
                                查看
                              </button>
                              <button
                                class="inline-flex h-9 items-center justify-center rounded-xl border border-[#38b46a]/30 bg-[#38b46a]/10 px-3 text-sm font-semibold text-[#9ee6bb] transition-colors hover:bg-[#38b46a]/16 disabled:cursor-not-allowed disabled:opacity-50"
                                onClick={(event) => void handleUnarchive(event, session.session_id)}
                                disabled={isBusy()}
                              >
                                取消归档
                              </button>
                              <button
                                class="inline-flex h-9 items-center justify-center rounded-xl border border-[#5a2d32] bg-[#2d1418] px-3 text-sm font-semibold text-[#ffb4ae] transition-colors hover:bg-[#3a171c] disabled:cursor-not-allowed disabled:opacity-50"
                                onClick={(event) => void handleDelete(event, session.session_id)}
                                disabled={isBusy()}
                              >
                                删除
                              </button>
                            </div>
                          </div>
                        </div>
                      );
                    }}
                  </For>
                </div>
              </Show>
            </Show>
          </Show>
        </div>
      </div>
    </div>
  );
}
