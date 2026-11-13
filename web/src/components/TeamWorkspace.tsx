import { For, Show, createEffect, createSignal, on, onCleanup } from 'solid-js';
import {
  getTeamEvents,
  getTeamPeerSettings,
  getTeamPeerThreads,
  getSubagents,
  getSession,
  type TeamEvent,
  type TeamPeerSettings,
  type TeamPeerThread,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import { getTeamCatalogEntryByAgentName } from '../utils/teamRegistry';
import { mergeCollaborationRecords } from '../utils/collaborationTasks';

const POLL_MS = 10000;

type TeamWorkspaceProps = {
  active?: boolean;
};

const eventTypeColor = (type: string) => {
  if (type === 'task_assigned') return 'text-[#58a6ff]';
  if (type === 'task_done') return 'text-[#3fb950]';
  if (type === 'task_failed') return 'text-[#ff7b72]';
  if (type === 'peer_question') return 'text-[#d2a8ff]';
  if (type === 'peer_question_copy') return 'text-[#ffa657]';
  if (type === 'peer_answer') return 'text-[#3fb950]';
  if (type === 'peer_answer_copy') return 'text-[#79c0ff]';
  if (type === 'peer_timeout') return 'text-[#ff7b72]';
  if (type === 'message') return 'text-[#c9d1d9]';
  return 'text-[#8b949e]';
};

const threadStatusClass = (status: string) => {
  if (status === 'pending') return 'text-[#f2cc60]';
  if (status === 'answered') return 'text-[#3fb950]';
  if (status === 'timed_out') return 'text-[#ff7b72]';
  return 'text-[#8b949e]';
};

const statusLabel = (status: string) => {
  const labels: Record<string, string> = {
    running: '运行中',
    busy: '忙碌',
    done: '已完成',
    idle: '空闲',
    failed: '失败',
    terminated: '已终止',
    pending: '待回答',
    answered: '已回答',
    timed_out: '已超时',
  };
  const normalized = String(status || '').trim().toLowerCase();
  return labels[normalized] || status;
};

const formatTs = (ts: number) => {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
};

const eventPreview = (ev: TeamEvent) => {
  const raw =
    ev.payload.task ||
    ev.payload.message ||
    ev.payload.result ||
    JSON.stringify(ev.payload);
  return String(raw || '').replace(/\s+/g, ' ').trim();
};

export default function TeamWorkspace(props: TeamWorkspaceProps) {
  const [events, setEvents] = createSignal<TeamEvent[]>([]);
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [peerSettings, setPeerSettings] = createSignal<TeamPeerSettings | null>(null);
  const [peerThreads, setPeerThreads] = createSignal<TeamPeerThread[]>([]);
  const [teamOwnerId, setTeamOwnerId] = createSignal<string | null>(null);
  const [teamOwnerAgentName, setTeamOwnerAgentName] = createSignal<string>('');
  const isActive = () => props.active ?? true;
  const pageVisible = createPageVisibleSignal();

  const teamId = () => teamOwnerId() || sessionStore.currentSessionId();
  const isProteinTeam = () =>
    getTeamCatalogEntryByAgentName(
      teamOwnerAgentName() || sessionStore.currentSession()?.agent_name || '',
    )?.key === 'protein';
  const shouldPoll = () => peerThreads().some((thread) => String(thread.status || '').toLowerCase() === 'pending');

  createEffect(on([isActive, pageVisible, () => sessionStore.currentSessionId()], ([active, visible, id]) => {
    if (!active || !visible || !id) {
      setTeamOwnerId(null);
      setTeamOwnerAgentName('');
      return;
    }

    let cancelled = false;
    void (async () => {
      const current = await getSession(id, { bustCache: true });
      if (cancelled) return;
      const parentId = String(current?.parent_id || '').trim();
      if (!parentId) {
        setTeamOwnerId(id);
        setTeamOwnerAgentName(current?.agent_name || sessionStore.currentSession()?.agent_name || '');
        return;
      }

      const parent = await getSession(parentId, { bustCache: true });
      if (cancelled) return;
      setTeamOwnerId(parentId);
      setTeamOwnerAgentName(parent?.agent_name || '');
    })();

    onCleanup(() => {
      cancelled = true;
    });
  }));

  const loadAll = async () => {
    if (!isActive() || !pageVisible()) {
      return;
    }
    const id = teamId();
    if (!id) {
      setEvents([]);
      setPeerSettings(null);
      setPeerThreads([]);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const [evs, peer, threads, subagents] = await Promise.all([
        getTeamEvents(id, 200),
        getTeamPeerSettings(id),
        getTeamPeerThreads(id),
        getSubagents(id),
      ]);
      setEvents(mergeCollaborationRecords(id, evs, subagents));
      setPeerSettings(peer);
      setPeerThreads(threads);
    } catch (e) {
      console.error('TeamWorkspace load error:', e);
      setError('协作信息加载失败，请刷新重试。');
    } finally {
      setLoading(false);
    }
  };

  createEffect(on([isActive, pageVisible, teamId], ([active, visible, id]) => {
    if (!active || !visible) return;
    if (!id) {
      setEvents([]);
      setPeerThreads([]);
      return;
    }
    void loadAll();
  }));

  createEffect(on([isActive, pageVisible, teamId, shouldPoll], ([active, visible, id, poll]) => {
    if (!active || !visible || !id || !poll) return;

    const timer = window.setInterval(() => {
      if (!isActive() || !pageVisible()) return;
      void loadAll();
    }, POLL_MS);
    onCleanup(() => window.clearInterval(timer));
  }));

  return (
    <div class="h-full min-w-0 flex flex-col bg-[#171c20]/92">
      <div class="border-b border-[#2a3136] px-4 py-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-sm font-semibold text-[#f5f7fa]">协作记录</div>
          </div>
          <div class="flex items-center gap-2">
          <button
            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            onClick={loadAll}
            title="刷新团队状态"
          >
            {loading() ? '刷新中...' : '刷新'}
          </button>
        </div>
      </div>
      </div>

      <Show when={error()}><p role="alert" class="px-4 py-2 text-xs text-[#ff7b72]">{error()}</p></Show>
      <Show when={isProteinTeam() && peerSettings()}>
        <div class="border-b border-[#2a3136] bg-[#10161a] px-4 py-3 text-[11px] text-[#8b949e]">
          蛋白团队同伴通信:
          <span class={peerSettings()?.enabled ? 'text-[#3fb950] ml-1' : 'text-[#ff7b72] ml-1'}>
            {peerSettings()?.enabled ? '已开启' : '已关闭'}
          </span>
          <span class="ml-2">
            白名单生效，原因: {(peerSettings()?.allowed_reasons || []).join(', ')}
          </span>
          <span class="ml-2">
            每对工作代理最多 {peerSettings()?.max_messages_per_pair_per_session} 次
          </span>
          <span class="ml-2">
            单轮超时 {peerSettings()?.round_timeout_seconds}s
          </span>
        </div>
      </Show>

      <div class="flex-1 overflow-y-auto flex flex-col gap-0">
        {/* Channel Events Feed */}
        <div class="border-b border-[#2a3136]">
          <div class="bg-[#10161a] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#7bd0ff]">
            委托与频道事件
          </div>
          <Show
            when={events().length > 0}
            fallback={<div class="px-4 py-3 text-xs text-[#6e7681]">暂无事件</div>}
          >
            <div class="tcode-scrollbar max-h-52 overflow-auto px-4 py-3 space-y-2 bg-[#13191d]">
              <For each={events()}>
                {(ev) => (
                  <div class="min-w-max flex items-start gap-2 rounded-2xl border border-[#2a3136] bg-[#171c20] px-3 py-2 text-[11px] whitespace-nowrap">
                    <span class="text-[#6e7681] shrink-0">{formatTs(ev.created_at)}</span>
                    <span class={`font-medium shrink-0 ${eventTypeColor(ev.type)}`}>[{ev.type}]</span>
                    <span class="text-[#8b949e] shrink-0">{ev.source}</span>
                    <Show when={ev.target}>
                      <span class="text-[#6e7681] shrink-0">→ {ev.target}</span>
                    </Show>
                    <span class="text-[#c9d1d9] whitespace-nowrap">
                      {eventPreview(ev)}
                    </span>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </div>

        <Show when={isProteinTeam()}>
          <div class="border-b border-[#2a3136]">
            <div class="bg-[#10161a] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#ffc176]">
              正式协作轮次
            </div>
            <Show
              when={peerThreads().length > 0}
              fallback={<div class="px-4 py-3 text-xs text-[#6e7681]">暂无协作轮次</div>}
            >
              <div class="tcode-scrollbar max-h-56 overflow-y-auto px-4 py-3 space-y-3 bg-[#13191d]">
                <For each={peerThreads().slice(0, 12)}>
                  {(thread) => (
                    <div class="rounded-2xl border border-[#2a3136] bg-[#171c20] p-3 text-[11px]">
                      <div class="flex items-center justify-between gap-2">
                        <div class="text-[#c9d1d9] truncate">
                          {thread.sender_worker} → {thread.target_worker} · {thread.reason}
                        </div>
                        <div class={`font-medium ${threadStatusClass(thread.status)}`}>{statusLabel(thread.status)}</div>
                      </div>
                      <div class="text-[#8b949e] mt-1 break-words">
                        问: {thread.question}
                      </div>
                      <Show when={thread.response?.summary}>
                        <div class="text-[#6e7681] mt-1 break-words">
                          答: {thread.response?.summary}
                        </div>
                      </Show>
                      <div class="text-[#6e7681] mt-1">
                        轮次 {thread.round_index}/{thread.max_rounds} · ID {thread.request_id.slice(0, 8)}
                      </div>
                    </div>
                  )}
                </For>
              </div>
            </Show>
          </div>
        </Show>

      </div>
    </div>
  );
}
