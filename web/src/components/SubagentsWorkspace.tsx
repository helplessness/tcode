import { For, Show, createEffect, createMemo, createSignal, on, onCleanup } from 'solid-js';
import {
  cancelSessionRun,
  controlWorkerExecution,
  chatWithSubagent,
  deleteSession as deleteSessionApi,
  getChatRuntimeStatus,
  getSession,
  getSubagents,
  getTeamWorkers,
  getTeamPeerSettings,
  getTeamPeerThreads,
  guideSession,
  teamChat,
  updateTeamPeerSettings,
  type SubagentItem,
  type TeamPeerSettings,
  type TeamPeerThread,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { generateId } from '../utils/id';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import { getToolTextDisplayStats } from '../utils/toolTextDisplay';
import { getToolRuntimeSummary, getToolVerificationLabel } from '../utils/toolRuntime';
import MarkdownContent from './MarkdownContent';
import { collaborationTaskTitle, collaborationRunStatus, isFinishedCollaborationTask, isWorkflowExecutionSession } from '../utils/collaborationTasks';
import { collaborationBudgetNotice } from '../utils/runBudget';
import SubagentDelegationTree from './SubagentDelegationTree';
import CollaborationOffice from './CollaborationOffice';
import {
  getTeamCatalogEntryByAgentName,
  getTeamCatalogEntries,
  isFixedTeamBootstrapAgent,
  type TeamCatalogEntry,
} from '../utils/teamRegistry';

type SubagentMessage = {
  id?: string;
  role: 'user' | 'assistant' | 'system' | string;
  content: string;
  tool_calls?: Array<{
    tool_name: string;
    state?: string;
    arguments?: Record<string, any>;
    output?: string;
    error?: string;
    call_id?: string;
    metadata?: Record<string, any>;
  }>;
};

type SubagentToolCall = NonNullable<SubagentMessage['tool_calls']>[number];

type SubagentAction = 'ask' | 'guide' | 'interrupt-guide' | 'reask';

type SubagentsWorkspaceProps = {
  active?: boolean;
  compact?: boolean;
  office?: boolean;
};

export default function SubagentsWorkspace(props: SubagentsWorkspaceProps) {
  let panelDisposed = false;
  onCleanup(() => {panelDisposed = true;});
  const PAGE_SIZE = 10;
  const [items, setItems] = createSignal<SubagentItem[]>([]);
  const [workerLimits, setWorkerLimits] = createSignal<Record<string, { tokens?: number; tool_calls?: number }>>({});
  const [loading, setLoading] = createSignal(false);
  const [autoRefresh, setAutoRefresh] = createSignal(Boolean(props.compact || props.office));
  const [currentPage, setCurrentPage] = createSignal(1);
  const [showCompleted, setShowCompleted] = createSignal(false);
  const [viewMode, setViewMode] = createSignal<'tree' | 'list'>('tree');
  const [collapsedCards, setCollapsedCards] = createSignal<Record<string, boolean>>({});
  const [drafts, setDrafts] = createSignal<Record<string, string>>({});
  const [sending, setSending] = createSignal<Record<string, boolean>>({});
  const [deleting, setDeleting] = createSignal<Record<string, boolean>>({});
  const [peerSettings, setPeerSettings] = createSignal<TeamPeerSettings | null>(null);
  const [peerThreads, setPeerThreads] = createSignal<TeamPeerThread[]>([]);
  const [updatingPeer, setUpdatingPeer] = createSignal(false);
  const [subagentMessages, setSubagentMessages] = createSignal<Record<string, SubagentMessage[]>>({});
  const inputRefs: Record<string, HTMLInputElement | undefined> = {};
  const detailSyncAt = new Map<string, number>();
  const detailSignatures = new Map<string, string>();
  let queuedStructuralSync = false;
  let structuralSyncTimer: number | undefined;
  let lastStructuralSyncAt = 0;
  let itemsSignature = '';
  const isActive = () => props.active ?? true;
  const pageVisible = createPageVisibleSignal();
  const STRUCTURAL_SYNC_INTERVAL_MS = 10000;
  const currentTeamEntry = createMemo(
    () => getTeamCatalogEntryByAgentName(sessionStore.selectedAgentName()),
  );
  const currentTeamKey = createMemo(() => currentTeamEntry()?.key || '');

  const workerNamesForTeam = (entry?: TeamCatalogEntry) => {
    const names = new Set<string>();
    for (const worker of entry?.workers || []) {
      const normalized = String(worker?.name || '').trim().toLowerCase();
      if (!normalized || worker.enabled === false || worker.visible === false) continue;
      names.add(normalized);
    }
    for (const name of [
      ...(entry?.worker_names || []),
      ...(entry?.resident_worker_names || []),
    ]) {
      const normalized = String(name || '').trim().toLowerCase();
      if (normalized) names.add(normalized);
    }
    return names;
  };

  const residentWorkerNamesForTeam = (entry?: TeamCatalogEntry) => {
    const names = new Set<string>();
    for (const worker of entry?.workers || []) {
      const normalized = String(worker?.name || '').trim().toLowerCase();
      if (!normalized || worker.enabled === false || worker.visible === false || !worker.resident) continue;
      names.add(normalized);
    }
    for (const name of entry?.resident_worker_names || []) {
      const normalized = String(name || '').trim().toLowerCase();
      if (normalized) names.add(normalized);
    }
    return names;
  };

  const allTeamSessionPrefixes = () =>
    getTeamCatalogEntries()
      .flatMap((entry) => entry.session_prefixes || [])
      .map((prefix) => String(prefix || '').trim().toLowerCase())
      .filter(Boolean);

  const allTeamWorkerNames = () => {
    const names = new Set<string>();
    for (const entry of getTeamCatalogEntries()) {
      for (const worker of entry.workers || []) {
        const normalized = String(worker?.name || '').trim().toLowerCase();
        if (normalized) names.add(normalized);
      }
      for (const name of workerNamesForTeam(entry)) {
        names.add(name);
      }
      for (const name of entry.disabled_worker_names || []) {
        const normalized = String(name || '').trim().toLowerCase();
        if (normalized) names.add(normalized);
      }
    }
    return names;
  };

  const isRelevantToCurrentTeam = (
    item: Pick<SubagentItem, 'session_id' | 'agent_name' | 'worker_name'>,
  ) => {
    const teamKey = String(currentTeamKey() || '').trim().toLowerCase();
    const teamEntry = currentTeamEntry();
    const sessionId = String(item.session_id || '').trim().toLowerCase();
    const agentName = String(item.agent_name || '').trim().toLowerCase();
    const workerName = String(item.worker_name || '').trim().toLowerCase();
    const agentTeamKey = getTeamCatalogEntryByAgentName(agentName)?.key || '';
    const currentPrefixes = (teamEntry?.session_prefixes || [])
      .map((prefix) => String(prefix || '').trim().toLowerCase())
      .filter(Boolean);
    const allPrefixes = allTeamSessionPrefixes();
    const workerNames = workerNamesForTeam(teamEntry);
    const knownWorkerNames = allTeamWorkerNames();
    const workerAgentName = agentName.startsWith('worker_')
      ? agentName.replace(/^worker_/, '')
      : '';
    const looksLikeFixedWorker =
      (Boolean(workerName) && knownWorkerNames.has(workerName)) ||
      (Boolean(workerAgentName) && knownWorkerNames.has(workerAgentName)) ||
      (Boolean(agentName) && knownWorkerNames.has(agentName)) ||
      allPrefixes.some((prefix) => sessionId.startsWith(prefix));

    if (!teamKey) return !looksLikeFixedWorker;
    if (agentTeamKey && agentTeamKey !== teamKey) return false;
    if (!looksLikeFixedWorker) return true;
    if (currentPrefixes.some((prefix) => sessionId.startsWith(prefix))) return true;
    if (workerName) return workerNames.has(workerName);
    if (workerAgentName) {
      return workerNames.has(workerAgentName);
    }
    return workerNames.has(agentName);
  };

  const applySubagentMessages = (subagentSessionId: string, messages: SubagentMessage[]) => {
    const normalized = Array.isArray(messages) ? messages : [];
    const signature = JSON.stringify(normalized);
    if (detailSignatures.get(subagentSessionId) === signature) return false;
    detailSignatures.set(subagentSessionId, signature);
    setSubagentMessages(prev => ({
      ...prev,
      [subagentSessionId]: normalized,
    }));
    return true;
  };

  const applySubagentItems = (nextItems: SubagentItem[]) => {
    const signature = JSON.stringify(nextItems);
    if (signature === itemsSignature) return;
    itemsSignature = signature;
    setItems(nextItems);
  };

  const formatToolArguments = (args?: Record<string, any>) => {
    if (!args || typeof args !== 'object' || Object.keys(args).length === 0) return '';
    try {
      return JSON.stringify(args, null, 2);
    } catch {
      return '';
    }
  };

  const formatToolState = (state?: string) => {
    switch (String(state || '').trim().toLowerCase()) {
      case 'running':
        return '运行中';
      case 'completed':
        return '已完成';
      case 'error':
        return '错误';
      case 'skipped':
        return '已跳过';
      case 'interrupted':
        return '已中断';
      case 'pending':
        return '等待中';
      default:
        return state || '未知状态';
    }
  };

  const subagentMessageKey = (subagentSessionId: string, msg: SubagentMessage, index: number) => {
    const messageId = String(msg.id || '').trim();
    return `subagent:${subagentSessionId}:${messageId || `${msg.role || 'message'}:${index}`}`;
  };

  const subagentToolDetailKey = (tool: SubagentToolCall, index: number) => {
    const callId = String(tool.call_id || '').trim();
    if (callId) return callId;
    return `${tool.tool_name || 'tool'}:${index}`;
  };

  const messageRoleLabel = (msg: SubagentMessage) => {
    if (msg.role === 'assistant') return '代理';
    if (msg.role === 'user') return '用户';
    if (msg.role === 'system') return '系统';
    if (msg.role === 'tool') return '工具';
    return msg.role;
  };

  const messageCardClass = (msg: SubagentMessage) => {
    if (msg.role === 'tool') {
      return 'rounded-2xl border border-[#3a4a1f] bg-[#11160f] p-3';
    }
    if (msg.role === 'system') {
      return 'rounded-2xl border border-[#2a3136] bg-[#11161a] p-3';
    }
    return 'rounded-2xl border border-[#2a3136] bg-[#11161a] p-3';
  };

  const SubagentToolCallItem = (props: {
    messageKey: string;
    tool: SubagentToolCall;
    index: number;
  }) => {
    const detailKey = createMemo(() => subagentToolDetailKey(props.tool, props.index));
    const isCollapsed = createMemo(() =>
      sessionStore.isMessageToolDetailsCollapsed(props.messageKey, detailKey())
    );
    const argumentsText = createMemo(() => formatToolArguments(props.tool.arguments));
    const argumentStats = createMemo(() => getToolTextDisplayStats(argumentsText()));
    const resultText = createMemo(() => getToolVerificationLabel(props.tool)
      ? `操作已发送；此条记录不代表任务完成，实际效果请查看后续观察。\n${props.tool.output || ''}`.trim()
      : String(props.tool.error || props.tool.output || '').trim());
    const resultStats = createMemo(() => getToolTextDisplayStats(resultText() || '（无返回内容）'));
    const hasResult = createMemo(() =>
      Boolean(resultText() || ['completed', 'error', 'skipped', 'interrupted'].includes(String(props.tool.state || '')))
    );
    const runtimeSummary = createMemo(() => getToolRuntimeSummary(props.tool));
    const detailLabel = createMemo(() => {
      if (getToolVerificationLabel(props.tool)) return '动作回执';
      if (props.tool.error) return '错误返回';
      if (props.tool.output || props.tool.state === 'completed') return '返回结果';
      if (props.tool.state === 'error') return '错误返回';
      if (props.tool.state === 'skipped') return '未执行（已被新指导替代）';
      if (props.tool.state === 'interrupted') return '中断结果';
      return '调用详情';
    });

    return (
      <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d]">
        <button
          class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#171f24]"
          onClick={() => sessionStore.setMessageToolDetailsCollapsed(props.messageKey, detailKey(), !isCollapsed())}
        >
          <span class="min-w-0 flex items-center gap-2">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class={`w-4 h-4 flex-shrink-0 transition-transform ${isCollapsed() ? '' : 'rotate-90'}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
            <span class="text-[#8ed5ff] font-semibold text-xs break-all">{props.tool.tool_name || 'tool'}</span>
            <Show when={runtimeSummary()}>
              <span class={`flex-shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${runtimeSummary()!.badgeClass}`}>
                {runtimeSummary()!.badge}
              </span>
            </Show>
          </span>
          <span class="flex-shrink-0 rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#9faeb7]">{getToolVerificationLabel(props.tool) || formatToolState(props.tool.state)}</span>
        </button>

        <Show when={!isCollapsed()}>
          <div class="px-4 pb-4 space-y-3 text-xs">
            <Show when={runtimeSummary()}>
              <div class={`rounded-xl border p-3 ${runtimeSummary()!.panelClass}`}>
                <div class="mb-2 text-[11px] uppercase tracking-[0.18em] text-[#7f8b93]">运行环境</div>
                <div class="grid gap-2 sm:grid-cols-2">
                  <For each={runtimeSummary()!.rows}>
                    {(row) => (
                      <div class="min-w-0">
                        <div class="text-[10px] uppercase tracking-[0.12em] text-[#6f7d87]">{row.label}</div>
                        <div class="break-all text-[#d6e3ea]">{row.value}</div>
                      </div>
                    )}
                  </For>
                </div>
              </div>
            </Show>

            <Show when={argumentsText()}>
              <div>
                <div class="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.18em] text-[#7f8b93]">
                  <span>调用参数</span>
                  <span class="normal-case tracking-normal text-[#6f7d87]">{argumentStats().label}</span>
                </div>
                <pre class="tcode-scrollbar max-h-44 overflow-auto rounded-xl bg-[#0d1216] border border-[#2a3136] p-3 text-[#c9dfe7] whitespace-pre-wrap break-words">{argumentsText()}</pre>
              </div>
            </Show>

            <Show when={hasResult()}>
              <div>
                <div class="mb-2 flex items-center justify-between gap-3">
                  <div class={`text-[11px] uppercase tracking-[0.18em] ${props.tool.error && !getToolVerificationLabel(props.tool) ? 'text-[#ffb6b0]' : 'text-[#7f8b93]'}`}>
                    {detailLabel()}
                  </div>
                  <div class="flex flex-shrink-0 items-center gap-2 text-[11px] text-[#6f7d87]">
                    <Show when={resultStats().isLong}>
                      <span>限制高度</span>
                    </Show>
                    <span>{resultStats().label}</span>
                  </div>
                </div>
                <pre class={`tcode-scrollbar max-h-64 overflow-auto rounded-xl border p-3 whitespace-pre-wrap break-words ${props.tool.error && !getToolVerificationLabel(props.tool) ? 'border-[#5a1e23] bg-[#2d1418] text-[#ffc3be]' : 'border-[#2a3136] bg-[#0d1216] text-[#c9dfe7]'}`}>
                  {resultText() || '（无返回内容）'}
                </pre>
              </div>
            </Show>
          </div>
        </Show>
      </div>
    );
  };

  const SubagentToolResultMessage = (props: {
    messageKey: string;
    msg: SubagentMessage;
  }) => {
    const resultKey = createMemo(() => `${props.messageKey}:tool-result`);
    const isCollapsed = createMemo(() =>
      sessionStore.isMessageToolDetailsCollapsed(props.messageKey, resultKey())
    );
    const content = createMemo(() => String(props.msg.content || '').trim());
    const contentStats = createMemo(() => getToolTextDisplayStats(content() || '（无返回内容）'));

    return (
      <div class="overflow-hidden rounded-2xl border border-[#3a4a1f] bg-[#11160f]">
        <button
          class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#151c12]"
          onClick={() => sessionStore.setMessageToolDetailsCollapsed(props.messageKey, resultKey(), !isCollapsed())}
        >
          <span class="min-w-0 flex items-center gap-2">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class={`w-4 h-4 flex-shrink-0 transition-transform ${isCollapsed() ? '' : 'rotate-90'}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
            <span class="text-[#7ee787] font-semibold text-xs">工具返回结果</span>
          </span>
          <span class="flex-shrink-0 rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#9faeb7]">{isCollapsed() ? '已折叠' : '已展开'}</span>
        </button>

        <Show when={!isCollapsed()}>
          <div class="px-4 pb-4">
            <div class="mb-2 flex items-center justify-between gap-3 text-[11px] text-[#6f7d87]">
              <span>返回内容</span>
              <span>{contentStats().label}</span>
            </div>
            <pre class="tcode-scrollbar max-h-64 overflow-auto rounded-xl bg-[#0d1216] border border-[#3a4a1f] p-3 text-[#d2f4b3] text-xs whitespace-pre-wrap break-words">
              {content() || '（无返回内容）'}
            </pre>
          </div>
        </Show>
      </div>
    );
  };

  const SubagentMessageItem = (props: {
    subagentSessionId: string;
    msg: SubagentMessage;
    index: number;
    hideContent?: boolean;
  }) => {
    const messageKey = createMemo(() => subagentMessageKey(props.subagentSessionId, props.msg, props.index));
    const toolCalls = createMemo(() => props.msg.tool_calls || []);
    const isToolMessage = createMemo(() => props.msg.role === 'tool');
    const isToolsCollapsed = createMemo(() => sessionStore.isMessageToolsCollapsed(messageKey()));

    return (
      <div class={messageCardClass(props.msg)}>
        <div class="text-[11px] font-medium mb-1 text-[#8b949e]">
          {messageRoleLabel(props.msg)}
        </div>

        <Show
          when={isToolMessage()}
          fallback={
            <Show when={!props.hideContent && props.msg.content?.trim()}>
              <div class="text-xs whitespace-pre-wrap break-words text-[#c9d1d9]">
                {props.msg.content}
              </div>
            </Show>
          }
        >
          <SubagentToolResultMessage messageKey={messageKey()} msg={props.msg} />
        </Show>

        <Show when={toolCalls().length > 0}>
          <div class="mt-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3">
            <button
              class="w-full text-xs text-[#9faeb7] hover:text-[#e6edf3] flex items-center justify-between gap-2"
              onClick={() => sessionStore.setMessageToolsCollapsed(messageKey(), !isToolsCollapsed())}
            >
              <span class="flex items-center gap-1">
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  class={`w-4 h-4 transition-transform ${isToolsCollapsed() ? '' : 'rotate-90'}`}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                </svg>
                工具过程 ({toolCalls().length})
              </span>
              <span>{isToolsCollapsed() ? '展开' : '折叠'}</span>
            </button>

            <Show when={!isToolsCollapsed()}>
              <div class="mt-2 space-y-2">
                <For each={toolCalls()}>
                  {(tc, tcIndex) => (
                    <SubagentToolCallItem
                      messageKey={messageKey()}
                      tool={tc}
                      index={tcIndex()}
                    />
                  )}
                </For>
              </div>
            </Show>
          </div>
        </Show>
      </div>
    );
  };

  const syncStateFromSessionSnapshot = (
    item: SubagentItem,
    messages: SubagentMessage[],
    parentSessionId: string,
    options: {
      preserveRunning?: boolean;
      messageOverride?: string;
    } = {},
  ) => {
    const previousState = stateMap().get(item.session_id);
    const normalizedMessages = Array.isArray(messages) ? messages : [];
    const lastAssistant = [...normalizedMessages]
      .reverse()
      .find((msg) => msg.role === 'assistant' && (msg.content || '').trim());
    const lastUser = [...normalizedMessages]
      .reverse()
      .find((msg) => msg.role === 'user' && (msg.content || '').trim());
    const lastSystem = [...normalizedMessages]
      .reverse()
      .find((msg) => msg.role === 'system' && (msg.content || '').trim());
    const toolCallKeys = new Set<string>();
    for (const msg of normalizedMessages) {
      for (const tool of msg.tool_calls || []) {
        const callId = String(tool.call_id || '').trim();
        const fallbackKey = `${tool.tool_name || 'tool'}:${JSON.stringify(tool.arguments || {})}`;
        toolCallKeys.add(callId || fallbackKey);
      }
    }
    const toolCallCount = toolCallKeys.size;
    const itemStatus = String(item.lifecycle_status || item.status || '').trim().toLowerCase();
    const itemIsRunning = ['running', 'busy', 'reporting'].includes(itemStatus);

    let status: 'running' | 'completed' | 'failed' | 'idle' =
      itemIsRunning
        ? 'running'
          : ['failed', 'blocked'].includes(itemStatus)
          ? 'failed'
          : itemStatus === 'completed'
            ? 'completed'
            : 'idle';
    let message =
      (itemIsRunning ? String(previousState?.message || '').trim() : '')
      || String(item.last_message || '').trim()
      || (status === 'running' ? '运行中...' : status === 'completed' ? '已回复' : status === 'failed' ? '执行失败' : '空闲');

    if ((lastSystem?.content || '').includes('[Subagent Error]')) {
      status = 'failed';
      message = lastSystem?.content || '子代理执行失败';
    } else if ((lastSystem?.content || '').includes('[Guidance Cancelled]')) {
      status = 'idle';
      message = '已中止';
    } else if (status === 'running') {
      status = 'running';
      message = message || '运行中...';
    } else if (!lastAssistant) {
      status = status === 'failed' ? 'failed' : 'idle';
      message = message || (status === 'failed' ? '执行失败' : '空闲');
    } else if (lastAssistant && status !== 'failed' && itemStatus !== 'paused') {
      status = 'completed';
      message = '已回复';
    }

    if (options.preserveRunning) {
      status = 'running';
      message = options.messageOverride || '运行中...';
    }

    const nextState = {
      parent_session_id: parentSessionId,
      session_id: item.session_id,
      agent_name: item.agent_name,
      display_name: item.display_name,
      engine: item.engine,
      status,
      message,
      tools_used: item.tools_used,
      conversation_with: item.conversation_with,
      last_response: item.last_response || lastAssistant?.content,
      context: {
        ...(item.context || {}),
        message_count: normalizedMessages.length,
        tool_call_count: toolCallCount,
        last_user: item.context?.last_user || lastUser?.content,
        last_assistant: item.context?.last_assistant || lastAssistant?.content,
      },
      contract: item.contract,
    };
    const currentComparable = previousState ? {
      parent_session_id: previousState.parent_session_id,
      session_id: previousState.session_id,
      agent_name: previousState.agent_name,
      display_name: previousState.display_name,
      engine: previousState.engine,
      status: previousState.status,
      message: previousState.message,
      tools_used: previousState.tools_used,
      conversation_with: previousState.conversation_with,
      last_response: previousState.last_response,
      context: previousState.context,
      contract: previousState.contract,
    } : null;
    if (JSON.stringify(currentComparable) === JSON.stringify(nextState)) return;
    sessionStore.upsertSubagentState(nextState);
  };

  const loadSubagents = async () => {
    const parentSessionId = sessionStore.currentSessionId();
    if (!isActive() || !pageVisible()) {
      return;
    }
    if (!parentSessionId) {
      setItems([]);
      return;
    }
    if (loading()) return;
    setLoading(true);
    try {
      const [subagentsResult, teamWorkersResult, peerSettingsResult, peerThreadsResult] = await Promise.allSettled([
        getSubagents(parentSessionId),
        getTeamWorkers(parentSessionId),
        currentTeamKey() ? getTeamPeerSettings(parentSessionId) : Promise.resolve(null),
        currentTeamKey() ? getTeamPeerThreads(parentSessionId) : Promise.resolve([]),
      ]);
      if (panelDisposed || parentSessionId !== sessionStore.currentSessionId()) return;
      const data = (subagentsResult.status === 'fulfilled' ? subagentsResult.value : [])
        .filter(item => !isWorkflowExecutionSession(item));
      const workers = teamWorkersResult.status === 'fulfilled' ? teamWorkersResult.value : [];
      setPeerSettings(peerSettingsResult.status === 'fulfilled' ? peerSettingsResult.value : null);
      setPeerThreads(peerThreadsResult.status === 'fulfilled' ? peerThreadsResult.value : []);
      const delegatedTeams = data.filter(item => isFixedTeamBootstrapAgent(item.agent_name));
      const nestedTeamWorkers = await Promise.all(
        delegatedTeams.map(async (item) => {
          try {
            const nested = await getTeamWorkers(item.session_id);
            return {
              ownerSessionId: item.session_id,
              ownerDisplayName: item.display_name || item.title || item.agent_name,
              workers: nested,
            };
          } catch (error) {
            console.error('Failed to load delegated team workers:', error);
            return {
              ownerSessionId: item.session_id,
              ownerDisplayName: item.display_name || item.title || item.agent_name,
              workers: [],
            };
          }
        })
      );
      if (panelDisposed || parentSessionId !== sessionStore.currentSessionId()) return;
      const merged = [...data.filter(isRelevantToCurrentTeam)];
      const seen = new Set(merged.map(item => item.session_id));
      for (const worker of workers) {
        if (!isRelevantToCurrentTeam({
          session_id: worker.session_id,
          agent_name: `worker_${worker.name}`,
          worker_name: worker.name,
        })) continue;
        if (seen.has(worker.session_id)) continue;
        merged.push({
          session_id: worker.session_id,
          agent_name: `worker_${worker.name}`,
          display_name: `工作代理：${worker.name}`,
          title: `[固定] ${worker.role}`,
          status: worker.status,
          created_at: worker.created_at,
          message_count: 0,
          last_message: worker.last_task || '',
          team_worker: worker.team_worker,
          resident_worker: Boolean(worker.resident_worker),
          worker_role: worker.role,
          worker_preset: worker.preset,
          worker_task_count: worker.task_count,
          worker_profile: worker.profile,
          worker_visible: worker.visible,
          worker_enabled: worker.enabled,
          worker_name: worker.name,
        });
        seen.add(worker.session_id);
      }
      for (const group of nestedTeamWorkers) {
        for (const worker of group.workers) {
          if (!isRelevantToCurrentTeam({
            session_id: worker.session_id,
            agent_name: `worker_${worker.name}`,
            worker_name: worker.name,
          })) continue;
          if (seen.has(worker.session_id)) continue;
          merged.push({
            session_id: worker.session_id,
            agent_name: `worker_${worker.name}`,
            display_name: `${group.ownerDisplayName} / ${worker.name}`,
            title: `[${group.ownerDisplayName}] ${worker.role}`,
            status: worker.status,
            created_at: worker.created_at,
            message_count: 0,
            last_message: worker.last_task || '',
            deletable: false,
            is_team_worker: true,
            team_owner_session_id: group.ownerSessionId,
            team_owner_display_name: group.ownerDisplayName,
            worker_name: worker.name,
            team_worker: worker.team_worker,
            resident_worker: Boolean(worker.resident_worker),
            worker_role: worker.role,
            worker_preset: worker.preset,
            worker_task_count: worker.task_count,
            worker_profile: worker.profile,
            worker_visible: worker.visible,
            worker_enabled: worker.enabled,
          });
          seen.add(worker.session_id);
        }
      }
      applySubagentItems(merged);
      const detailList = await Promise.allSettled(
        merged.map(async (item) => {
          const session = await getSession(item.session_id, {
            includeToolMessages: true,
          });
          return {
            item,
            sessionId: item.session_id,
            messages: ((session?.messages || []) as SubagentMessage[]),
          };
        })
      );

      if (panelDisposed || parentSessionId !== sessionStore.currentSessionId()) return;
      for (const detail of detailList) {
        if (detail.status !== 'fulfilled') {
          console.error('Failed to load subagent session detail:', detail.reason);
          continue;
        }
        applySubagentMessages(detail.value.sessionId, detail.value.messages);
        syncStateFromSessionSnapshot(detail.value.item, detail.value.messages, parentSessionId);
      }
    } catch (error) {
      console.error('Failed to load subagents:', error);
    } finally {
      setLoading(false);
      if (!panelDisposed && queuedStructuralSync && isActive() && pageVisible()) {
        queuedStructuralSync = false;
        window.clearTimeout(structuralSyncTimer);
        structuralSyncTimer = window.setTimeout(() => {
          structuralSyncTimer = undefined;
          lastStructuralSyncAt = Date.now();
          void loadSubagents();
        }, STRUCTURAL_SYNC_INTERVAL_MS);
      }
    }
  };

  const scheduleStructuralSync = (options: { immediate?: boolean } = {}) => {
    if (!isActive() || !pageVisible()) return;
    const parentSessionId = sessionStore.currentSessionId();
    if (!parentSessionId) {
      setItems([]);
      return;
    }

    if (loading()) {
      queuedStructuralSync = true;
      return;
    }

    const elapsed = Date.now() - lastStructuralSyncAt;
    const delay = options.immediate ? 0 : Math.max(0, STRUCTURAL_SYNC_INTERVAL_MS - elapsed);

    if (structuralSyncTimer !== undefined) {
      if (delay === 0) {
        window.clearTimeout(structuralSyncTimer);
      } else {
        return;
      }
    }

    structuralSyncTimer = window.setTimeout(() => {
      structuralSyncTimer = undefined;
      lastStructuralSyncAt = Date.now();
      void loadSubagents();
    }, delay);
  };

  onCleanup(() => {
    if (structuralSyncTimer !== undefined) {
      window.clearTimeout(structuralSyncTimer);
      structuralSyncTimer = undefined;
    }
  });

  createEffect(on([isActive, () => sessionStore.currentSessionId(), () => sessionStore.subagentsVersion()], ([active, parentSessionId]) => {
    if (!active || !pageVisible()) return;
    if (!parentSessionId) {
      setItems([]);
      return;
    }

    scheduleStructuralSync({ immediate: true });
  }));

  createEffect(on([isActive, pageVisible, autoRefresh, () => sessionStore.currentSessionId()], ([active, visible, refreshEnabled, parentSessionId]) => {
    if (!active || !visible || !refreshEnabled || !parentSessionId) return;

    const timer = window.setInterval(() => {
      scheduleStructuralSync();
    }, STRUCTURAL_SYNC_INTERVAL_MS);

    onCleanup(() => window.clearInterval(timer));
  }, { defer: true }));

  const stateMap = createMemo(() => {
    const map = new Map<string, ReturnType<typeof sessionStore.subagentStates>[number]>();
    const parentSessionId = sessionStore.currentSessionId();
    for (const state of sessionStore.subagentStates()) {
      if (state.parent_session_id === parentSessionId) map.set(state.session_id, state);
    }
    return map;
  });
  const mergedItems = createMemo(() => {
    const parentSessionId = sessionStore.currentSessionId();
    const base = [...items()].filter(isRelevantToCurrentTeam);
    const seen = new Set(base.map(item => item.session_id));

    for (const state of sessionStore.subagentStates()) {
      if (state.parent_session_id !== parentSessionId) continue;
      if (isWorkflowExecutionSession({ session_id: state.session_id, title: state.display_name || state.agent_name, display_name: state.display_name })) continue;
      if (
        !isRelevantToCurrentTeam({
          session_id: state.session_id,
          agent_name: state.agent_name,
        })
      ) continue;
      if (seen.has(state.session_id)) continue;

      base.push({
        session_id: state.session_id,
        agent_name: state.agent_name,
        display_name: state.display_name,
        title: state.agent_name,
        status: state.status,
        created_at: state.updated_at,
        message_count: 0,
        last_message: state.message || '',
        tools_used: state.tools_used,
        conversation_with: state.conversation_with,
        last_response: state.last_response,
        contract: state.contract,
      });
      seen.add(state.session_id);
    }

    return base.sort((a, b) => b.created_at - a.created_at);
  });

  const visibleItems = createMemo(() => !props.compact || showCompleted() ? mergedItems() : mergedItems().filter(item => !isFinishedCollaborationTask(collaborationRunStatus(item, stateMap().get(item.session_id)?.status))));
  const totalPages = createMemo(() => {
    const total = visibleItems().length;
    return Math.max(1, Math.ceil(total / PAGE_SIZE));
  });

  const pagedItems = createMemo(() => {
    const page = Math.min(Math.max(currentPage(), 1), totalPages());
    const start = (page - 1) * PAGE_SIZE;
    return visibleItems().slice(start, start + PAGE_SIZE);
  });

  const workerNameForItem = (item: SubagentItem) => {
    const workerName = String(item.worker_name || item.team_worker?.name || '').trim().toLowerCase();
    if (workerName) return workerName;
    const agentName = String(item.agent_name || '').trim().toLowerCase();
    return agentName.startsWith('worker_') ? agentName.replace(/^worker_/, '') : agentName;
  };

  const residentSessionIds = createMemo(() => {
    const residentNames = residentWorkerNamesForTeam(currentTeamEntry());
    const ids: string[] = [];
    if (residentNames.size === 0) return ids;
    for (const item of mergedItems()) {
      const workerName = workerNameForItem(item);
      if (item.resident_worker || residentNames.has(workerName)) {
        ids.push(item.session_id);
      }
    }
    return ids;
  });

  const isCardCollapsed = (subagentSessionId: string) =>
    collapsedCards()[subagentSessionId] !== false;

  const toggleCardCollapsed = (subagentSessionId: string) => {
    setCollapsedCards((prev) => ({
      ...prev,
      [subagentSessionId]: !isCardCollapsed(subagentSessionId),
    }));
  };

  createEffect(() => {
    const ids = new Set(mergedItems().map((item) => item.session_id));
    setCollapsedCards((prev) => {
      const next: Record<string, boolean> = {};
      for (const id of ids) {
        next[id] = Object.prototype.hasOwnProperty.call(prev, id) ? prev[id] : true;
      }
      return next;
    });
  });

  let lastResidentSyncKey = '';
  createEffect(() => {
    const residents = residentSessionIds();
    const teamKey = currentTeamKey();
    const syncKey = `${teamKey}:${residents.join('|')}`;
    if (!teamKey || residents.length === 0 || syncKey === lastResidentSyncKey) return;
    lastResidentSyncKey = syncKey;

    const residentSet = new Set(residents);
    const visibleItems = mergedItems();
    setCollapsedCards(() => {
      const next: Record<string, boolean> = {};
      for (const item of visibleItems) {
        next[item.session_id] = !residentSet.has(item.session_id);
      }
      return next;
    });

    const firstResidentIndex = visibleItems.findIndex((item) => residentSet.has(item.session_id));
    if (firstResidentIndex >= 0) {
      setCurrentPage(Math.floor(firstResidentIndex / PAGE_SIZE) + 1);
    }
  });

  createEffect(() => {
    setCurrentPage((prev) => {
      const page = Math.max(1, prev);
      const maxPage = totalPages();
      return page > maxPage ? maxPage : page;
    });
  });

  createEffect(on(() => sessionStore.currentSessionId(), () => {
    setCurrentPage(1);
  }));

  const isSubagentRunning = (subagentSessionId: string) =>
    stateMap().get(subagentSessionId)?.status === 'running';

  const getSessionMessages = (subagentSessionId: string) => subagentMessages()[subagentSessionId] || [];

  const getAgentResponses = (subagentSessionId: string) =>
    getSessionMessages(subagentSessionId)
      .filter((msg) => msg.role === 'assistant' && String(msg.content || '').trim());

  const getToolTraceMessages = (subagentSessionId: string) => {
    const seenCallIds = new Set<string>();
    const trace: SubagentMessage[] = [];
    for (const msg of getSessionMessages(subagentSessionId)) {
      const toolCalls = msg.tool_calls || [];
      if (msg.role !== 'tool' && toolCalls.length > 0) {
        trace.push(msg);
        for (const tool of toolCalls) {
          const callId = String(tool.call_id || '').trim();
          if (callId) seenCallIds.add(callId);
        }
        continue;
      }
      if (msg.role !== 'tool') continue;
      const callIds = toolCalls
        .map((tool) => String(tool.call_id || '').trim())
        .filter(Boolean);
      if (callIds.length > 0 && callIds.every((callId) => seenCallIds.has(callId))) {
        continue;
      }
      trace.push(msg);
    }
    return trace;
  };

  const describeSubagentActivity = (
    subagentSessionId: string,
    status: string,
    fallback: string,
  ) => {
    const runtimeTask = items().find(item => item.session_id === subagentSessionId)?.current_runtime_task;
    if (runtimeTask?.status === 'reviewing') return '执行已结束，等待 Critic / Verifier 审查';
    if (fallback.includes('压缩上下文') || fallback.includes('上下文压缩')) {
      return fallback;
    }
    const messages = getSessionMessages(subagentSessionId);
    const tools = messages.flatMap((msg) => msg.tool_calls || []);
    const activeTool = [...tools].reverse().find((tool) => {
      const state = String(tool.state || '').trim().toLowerCase();
      return state === 'running' || state === 'pending';
    });
    if (activeTool && status === 'running') return `正在调用 ${activeTool.tool_name || '工具'}`;

    const latestTool = tools[tools.length - 1];
    if (status === 'running' && latestTool) {
      const failed = Boolean(latestTool.error) || String(latestTool.state || '').toLowerCase() === 'error';
      return failed
        ? `${latestTool.tool_name || '工具'} 返回错误，正在调整执行方案`
        : `${latestTool.tool_name || '工具'} 已返回，正在分析结果`;
    }
    if (status === 'running') return fallback || '正在分析任务';
    if (status === 'partial') return '部分完成，仍有未完成项';
    if (status === 'cancelled') return '当前任务已中止，已有结果保留';
    if (status === 'paused') return '已暂停，等待补充输入';
    if (status === 'blocked') return '任务受阻，等待处理';
    if (status === 'completed') return '任务已完成';
    if (status === 'failed') return fallback || '执行失败';
    return fallback || '等待新任务';
  };

  const getLatestUserPrompt = (subagentSessionId: string) => {
    const live = [...sessionStore.getSubagentChats(subagentSessionId)]
      .reverse()
      .find((entry) => entry.role === 'user' && (entry.content || '').trim());
    if (live?.content?.trim()) {
      return live.content.trim();
    }

    const persisted = [...getSessionMessages(subagentSessionId)]
      .reverse()
      .find((msg) => msg.role === 'user' && (msg.content || '').trim());
    return String(persisted?.content || '').trim();
  };

  const resolveSubagentMessage = (subagentSessionId: string, action: SubagentAction) => {
    const draft = String(drafts()[subagentSessionId] || '').trim();
    if (action === 'reask') {
      return draft || getLatestUserPrompt(subagentSessionId);
    }
    return draft;
  };

  const clearDraft = (subagentSessionId: string) => {
    setDrafts(prev => ({ ...prev, [subagentSessionId]: '' }));
    if (inputRefs[subagentSessionId]) {
      inputRefs[subagentSessionId]!.value = '';
    }
  };

  const isBusyConflictError = (error: unknown) => {
    const text = error instanceof Error ? error.message : String(error);
    return /already (running|busy)/i.test(text) && /guidance/i.test(text);
  };

  const reaskStatusMessage = (pendingGuidanceCount: number) => {
    if (pendingGuidanceCount > 0) {
      return `已请求重新提问，队列中还有 ${pendingGuidanceCount} 条待执行`;
    }
    return '已请求重新提问，将切换到新的问题';
  };

  const runningMessage = (pendingGuidanceCount: number, interrupt = false) => {
    if (pendingGuidanceCount > 0) {
      return interrupt
        ? `已请求在当前位置接入新指导，队列中还有 ${pendingGuidanceCount} 条待执行`
        : `已追加指导，队列中还有 ${pendingGuidanceCount} 条待执行`;
    }
    return interrupt ? '已请求中断当前步骤，并在当前位置接入新指导' : '已追加指导，等待当前步骤结束';
  };

  const refreshSubagentSessionDetail = async (
    subagentSessionId: string,
    parentSessionId = sessionStore.currentSessionId() || '',
    options: {
      preserveRunning?: boolean;
      messageOverride?: string;
    } = {},
  ) => {
    try {
      const sessionDetail = await getSession(subagentSessionId, {
        includeToolMessages: true,
        bustCache: true,
      });
      const messages = (sessionDetail?.messages || []) as SubagentMessage[];
      applySubagentMessages(subagentSessionId, messages);

      const currentItem = mergedItems().find(item => item.session_id === subagentSessionId) || {
        session_id: subagentSessionId,
        agent_name: stateMap().get(subagentSessionId)?.agent_name || 'subagent',
        display_name: stateMap().get(subagentSessionId)?.display_name,
        engine: stateMap().get(subagentSessionId)?.engine,
        title: stateMap().get(subagentSessionId)?.display_name || stateMap().get(subagentSessionId)?.agent_name || 'subagent',
        status: stateMap().get(subagentSessionId)?.status || 'idle',
        created_at: Date.now(),
        message_count: messages.length,
        last_message: stateMap().get(subagentSessionId)?.message || '',
        tools_used: stateMap().get(subagentSessionId)?.tools_used,
        conversation_with: stateMap().get(subagentSessionId)?.conversation_with,
        last_response: stateMap().get(subagentSessionId)?.last_response,
        context: stateMap().get(subagentSessionId)?.context,
        contract: stateMap().get(subagentSessionId)?.contract,
      } satisfies SubagentItem;

      syncStateFromSessionSnapshot(currentItem, messages, parentSessionId, options);
    } catch (error) {
      console.error('Failed to refresh subagent session detail:', error);
    }
  };

  createEffect(on([isActive, pageVisible, () => sessionStore.currentSessionId()], ([active, visible, parentSessionId]) => {
    if (!active || !visible || !parentSessionId) return;

    let disposed = false;
    let inFlight = false;
    const STATUS_POLL_INTERVAL_MS = 2500;
    const DETAIL_SYNC_INTERVAL_MS = 5000;

    const syncRunningSubagents = async () => {
      if (disposed || inFlight) return;
      const runningIds = Array.from(stateMap().values())
        .filter(state => state.parent_session_id === parentSessionId && state.status === 'running')
        .map(state => state.session_id);
      if (runningIds.length === 0 || !pageVisible()) return;

      inFlight = true;
      try {
        for (const subagentSessionId of runningIds) {
          const runtime = await getChatRuntimeStatus(subagentSessionId);
          if (disposed || !runtime) continue;

          const stillRunning =
            runtime.running ||
            runtime.pending_guidance_count > 0 ||
            Boolean(runtime.guidance_worker_active);

          if (stillRunning) {
            const runtimeMessage = runtime.running
              ? '运行中...'
              : runtime.pending_guidance_count > 0 || Boolean(runtime.guidance_worker_active)
                ? (runtime.pending_guidance_interrupt
                  ? '等待新指导接入...'
                  : `后台继续处理中，队列中还有 ${runtime.pending_guidance_count} 条`)
                : '运行中...';
            const currentState = stateMap().get(subagentSessionId);
            if (currentState?.status !== 'running' || currentState?.message !== runtimeMessage) {
              sessionStore.upsertSubagentState({
                parent_session_id: parentSessionId,
                session_id: subagentSessionId,
                status: 'running',
                message: runtimeMessage,
              });
            }
            const lastDetailSyncAt = Number(detailSyncAt.get(subagentSessionId) || 0);
            const shouldSyncDetail =
              Date.now() - lastDetailSyncAt >= DETAIL_SYNC_INTERVAL_MS;
            if (shouldSyncDetail) {
              detailSyncAt.set(subagentSessionId, Date.now());
              await refreshSubagentSessionDetail(subagentSessionId, parentSessionId, {
                preserveRunning: true,
                messageOverride: runtimeMessage,
              });
            }
            continue;
          }

          detailSyncAt.delete(subagentSessionId);
          await refreshSubagentSessionDetail(subagentSessionId, parentSessionId);
        }
      } finally {
        inFlight = false;
      }
    };

    const timer = window.setInterval(() => {
      void syncRunningSubagents();
    }, STATUS_POLL_INTERVAL_MS);

    void syncRunningSubagents();
    onCleanup(() => {
      disposed = true;
      window.clearInterval(timer);
    });
  }, { defer: true }));

  const sendToSubagent = async (subagentSessionId: string, action: SubagentAction = 'ask') => {
    const parentSessionId = sessionStore.currentSessionId();
    if (sending()[subagentSessionId]) return;
    const currentlyRunning = isSubagentRunning(subagentSessionId);
    const text = resolveSubagentMessage(subagentSessionId, action);
    if (!parentSessionId || !text) return;
    const targetItem = mergedItems().find(item => item.session_id === subagentSessionId);
    const teamOwnerSessionId = targetItem?.team_owner_session_id;
    const teamWorkerName = targetItem?.worker_name;
    const draftText = String(drafts()[subagentSessionId] || '').trim();
    const interrupt = currentlyRunning && (action === 'reask' || action === 'interrupt-guide');

    sessionStore.appendSubagentChat({
      session_id: subagentSessionId,
      role: 'user',
      content: text,
      timestamp: Date.now(),
    });

    clearDraft(subagentSessionId);
    setSending(prev => ({ ...prev, [subagentSessionId]: true }));

    try {
      if (currentlyRunning) {
        const resp = await guideSession(subagentSessionId, text, {
          interrupt,
          cancel_descendants: interrupt,
        });
        sessionStore.upsertSubagentState({
          parent_session_id: parentSessionId,
          session_id: subagentSessionId,
          status: 'running',
          message: action === 'reask'
            ? reaskStatusMessage(resp.pending_guidance_count)
            : runningMessage(resp.pending_guidance_count, interrupt),
          context: {
            ...(stateMap().get(subagentSessionId)?.context || {}),
            last_user: text,
          },
        });
        sessionStore.setStatus(
          action === 'reask'
            ? '已请求子代理重新提问'
            : interrupt
              ? '已请求子代理在当前位置接收新指导'
              : '已向子代理追加指导',
        );
        return;
      }

      sessionStore.upsertSubagentState({
        parent_session_id: parentSessionId,
        session_id: subagentSessionId,
        status: 'running',
        message: action === 'reask' ? '重新提问中...' : '运行中...',
        context: {
          ...(stateMap().get(subagentSessionId)?.context || {}),
          last_user: text,
        },
      });

      const resp = teamOwnerSessionId && teamWorkerName
        ? await teamChat(teamOwnerSessionId, teamWorkerName, text)
        : await chatWithSubagent(parentSessionId, subagentSessionId, text);
      if (!resp) {
        throw new Error('子代理没有返回结果');
      }
      if (resp.status === 'cancelled') {
        const stillRunning =
          Boolean(resp.running) ||
          Math.max(0, Number(resp.pending_guidance_count || 0)) > 0 ||
          Boolean(resp.guidance_worker_active);
        sessionStore.upsertSubagentState({
          parent_session_id: parentSessionId,
          session_id: subagentSessionId,
          status: stillRunning ? 'running' : 'idle',
          message: stillRunning ? '已中断，等待新指导接入...' : '已中止',
        });
        if (!stillRunning) {
          sessionStore.appendSubagentChat({
            session_id: subagentSessionId,
            role: 'system',
            content: '[已中止] 当前子代理回合已停止。',
            timestamp: Date.now(),
          });
        }
        return;
      }

      if (resp.response) {
        sessionStore.appendSubagentChat({
          session_id: subagentSessionId,
          role: 'assistant',
          content: resp.response,
          timestamp: Date.now(),
        });
      }

      sessionStore.upsertSubagentState({
        parent_session_id: parentSessionId,
        session_id: subagentSessionId,
        agent_name: (resp as any).agent_name,
        display_name: (resp as any).display_name,
        status: 'completed',
        message: '已回复',
        tools_used: (resp as any).tools_used,
        conversation_with: (resp as any).conversation_with,
        last_response: (resp as any).last_response || resp.response,
        context: (resp as any).context,
        contract: (resp as any).contract,
      });

      if ((resp as any).integrated_to_parent && sessionStore.currentSessionId() === parentSessionId) {
        sessionStore.addMessage({
          id: (resp as any).parent_message_id || generateId(),
          role: 'system',
          content: (resp as any).integration_content || '[子代理反馈已合并]',
        });
      }

      await refreshSubagentSessionDetail(subagentSessionId, parentSessionId);
    } catch (error) {
      if (isBusyConflictError(error)) {
        try {
          const resp = await guideSession(subagentSessionId, text, {
            interrupt: action === 'reask' || action === 'interrupt-guide',
            cancel_descendants: action === 'reask' || action === 'interrupt-guide',
          });
          sessionStore.upsertSubagentState({
            parent_session_id: parentSessionId,
            session_id: subagentSessionId,
            status: 'running',
            message: action === 'reask'
              ? reaskStatusMessage(resp.pending_guidance_count)
              : runningMessage(resp.pending_guidance_count, action === 'interrupt-guide'),
            context: {
              ...(stateMap().get(subagentSessionId)?.context || {}),
              last_user: text,
            },
          });
          sessionStore.setStatus(
            action === 'reask'
              ? '子代理已切换为重新提问'
              : action === 'interrupt-guide'
                ? '子代理已自动转为当前位置直接指导'
                : '子代理已自动转为继续指导',
          );
          return;
        } catch (fallbackError) {
          error = fallbackError;
        }
      }

      const msg = error instanceof Error ? error.message : String(error);
      sessionStore.appendSubagentChat({
        session_id: subagentSessionId,
        role: 'system',
        content: `错误：${msg}`,
        timestamp: Date.now(),
      });
      if (draftText) {
        setDrafts(prev => ({ ...prev, [subagentSessionId]: draftText }));
        if (inputRefs[subagentSessionId]) {
          inputRefs[subagentSessionId]!.value = draftText;
        }
      }
      sessionStore.upsertSubagentState({
        parent_session_id: parentSessionId,
        session_id: subagentSessionId,
        status: 'failed',
        message: msg,
      });
    } finally {
      setSending(prev => ({ ...prev, [subagentSessionId]: false }));
    }
  };

  const stopSubagentRun = async (subagentSessionId: string, autoReplan = false) => {
    const parentSessionId = sessionStore.currentSessionId();
    if (!parentSessionId) return;
    try {
      const resp = await cancelSessionRun(subagentSessionId, {
        notify_parent: true,
        auto_replan: autoReplan,
        controller_session_id: parentSessionId,
        clear_guidance: true,
      });
      const stillBusy =
        Boolean(resp.running) ||
        Math.max(0, Number(resp.pending_guidance_count || 0)) > 0 ||
        Boolean(resp.guidance_worker_active);
      const parentNotified = Array.isArray(resp.parent_notified_session_ids)
        && resp.parent_notified_session_ids.length > 0;
      const replanQueued = Boolean(resp.replan_queued);
      const stopMessage = stillBusy
        ? '已请求中止，等待当前步骤停止'
        : replanQueued
          ? '已中止，并已通知主代理重新规划'
          : parentNotified
            ? '已中止，并已通知父会话'
            : '已中止';
      sessionStore.upsertSubagentState({
        parent_session_id: parentSessionId,
        session_id: subagentSessionId,
        status: stillBusy ? 'running' : 'idle',
        message: stopMessage,
      });
      sessionStore.appendSubagentChat({
        session_id: subagentSessionId,
        role: 'system',
        content: stillBusy
          ? '[已请求中止] 当前子代理回合将尽快停止。'
          : replanQueued
            ? '[已中止] 当前子代理回合已停止，主代理已收到中止事件并会重新规划。'
            : parentNotified
              ? '[已中止] 当前子代理回合已停止，父会话已收到中止事件。'
              : '[已中止] 当前子代理回合已停止。',
        timestamp: Date.now(),
      });
      if (replanQueued && resp.replan_session_id === parentSessionId) {
        sessionStore.setIsRunning(true);
        sessionStore.setStatus('主代理已收到子代理中止事件，正在重新规划...');
      } else if (parentNotified) {
        sessionStore.setStatus('已中止子代理，并已通知父会话');
      }
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      sessionStore.setStatus(`停止子代理失败: ${msg}`);
    }
  };

  const deleteSubagent = async (subagentSessionId: string) => {
    setDeleting(prev => ({ ...prev, [subagentSessionId]: true }));
    try {
      await deleteSessionApi(subagentSessionId);

      setItems(prev => prev.filter(item => item.session_id !== subagentSessionId));
      detailSignatures.delete(subagentSessionId);
      setSubagentMessages(prev => {
        const next = { ...prev };
        delete next[subagentSessionId];
        return next;
      });
      setDrafts(prev => {
        const next = { ...prev };
        delete next[subagentSessionId];
        return next;
      });
      setSending(prev => {
        const next = { ...prev };
        delete next[subagentSessionId];
        return next;
      });
      sessionStore.removeSubagentSession(subagentSessionId);
      sessionStore.setStatus('子代理已停止并移除');
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      sessionStore.setStatus(`停止并移除子代理失败: ${msg}`);
    } finally {
      setDeleting(prev => ({ ...prev, [subagentSessionId]: false }));
    }
  };

  const statusClass = (status?: string) => {
    if (status === 'running') return 'text-[#f2cc60]';
    if (status === 'completed') return 'text-[#3fb950]';
    if (status === 'failed') return 'text-[#ff7b72]';
    return 'text-[#8b949e]';
  };

  const togglePeerCommunication = async (enabled: boolean) => {
    const teamId = sessionStore.currentSessionId();
    if (!teamId || updatingPeer()) return;
    setUpdatingPeer(true);
    try {
      const next = await updateTeamPeerSettings(teamId, enabled);
      if (next) setPeerSettings(next);
    } finally {
      setUpdatingPeer(false);
    }
  };
  const changeWorkerExecution = async (item: SubagentItem, action: 'pause' | 'resume') => {
    setSending(prev => ({ ...prev, [item.session_id]: true }));
    try {
      const task = await controlWorkerExecution(item.session_id, item.current_runtime_task!.task_id,
        action, action === 'resume' ? workerLimits()[item.session_id] : undefined);
      setItems(prev => prev.map(row => row.session_id === item.session_id
        ? { ...row, current_runtime_task: task, budget: task.budget } : row));
      setWorkerLimits(prev => ({ ...prev, [item.session_id]: {} }));
    } catch (error) {
      sessionStore.setStatus(error instanceof Error ? error.message : '控制 Worker 失败');
    } finally {
      setSending(prev => ({ ...prev, [item.session_id]: false }));
    }
  };
  const statusLabel = (status?: string) => {
    const labels: Record<string, string> = {
      running: '运行中',
      completed: '已完成',
      partial: '部分完成',
      cancelled: '已中止',
      failed: '失败',
      idle: '空闲',
      busy: '忙碌',
      available: '可接任务',
      assigned: '已委托',
      reporting: '汇报中',
      paused: '已暂停',
      pause_requested: '等待安全暂停',
      blocked: '受阻',
      done: '已完成',
      terminated: '已终止',
    };
    const normalized = String(status || '').trim().toLowerCase();
    return labels[normalized] || status || '未知';
  };

  const openSideChat = (item: SubagentItem) => {
    const parentSessionId = sessionStore.currentSessionId();
    if (!parentSessionId || !item.session_id) return;
    window.dispatchEvent(new CustomEvent('tcode-open-side-chat', {
      detail: {
        parentSessionId,
        sessionId: item.session_id,
        discussion: !item.side_question,
      },
    }));
  };

  const openFromTree = (item: SubagentItem) => {
    const index = visibleItems().findIndex(entry => entry.session_id === item.session_id);
    if (index >= 0) setCurrentPage(Math.floor(index / PAGE_SIZE) + 1);
    setCollapsedCards(prev => ({ ...prev, [item.session_id]: false }));
    setViewMode('list');
    window.setTimeout(() => {
      document.querySelector(`[data-subagent-session="${CSS.escape(item.session_id)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  };

  return (
    <Show when={!props.office} fallback={<CollaborationOffice
      items={mergedItems()} sessionId={sessionStore.currentSessionId() || ''}
      peerThreads={peerThreads()}
      manager={sessionStore.selectedAgentName() || 'Manager'} active={isActive() && pageVisible()}
      statusOf={item => collaborationRunStatus(item, stateMap().get(item.session_id)?.status)}
      toolsOf={item => stateMap().get(item.session_id)?.tools_used || item.tools_used || []}
      contractOf={item => stateMap().get(item.session_id)?.contract || item.contract}
      activityOf={item => describeSubagentActivity(item.session_id, collaborationRunStatus(item, stateMap().get(item.session_id)?.status), stateMap().get(item.session_id)?.message || item.last_message || '')}
      onDiscuss={openSideChat}
    />}>
    <div class="h-full min-w-0 flex flex-col bg-[#171c20]/92" classList={{'collaboration-tasks': Boolean(props.compact)}}>
      <div class="border-b border-[#2a3136] px-4 py-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-[11px] font-semibold text-[#7bd0ff]">{props.compact ? '当前会话任务' : '子代理'}</div>
            <Show when={!props.compact}><div class="mt-1 text-sm font-semibold text-[#f5f7fa]">运行状态与回复</div></Show>
          </div>
          <div class="flex items-center gap-2">
          <Show when={props.compact && peerSettings()}>
            <label class="flex items-center gap-1.5 rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[10px] text-[#9faeb7]" title="允许团队成员在受控白名单内互相提问">
              <input
                type="checkbox"
                class="checkbox checkbox-xs"
                checked={Boolean(peerSettings()?.enabled)}
                disabled={updatingPeer()}
                onChange={event => void togglePeerCommunication(event.currentTarget.checked)}
              />
              <span>成员互助</span>
            </label>
          </Show>
          <Show when={!props.compact}>
            <div class="flex rounded-full border border-[#3e484f] bg-[#10161a] p-0.5" aria-label="子代理视图">
              <button class={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${viewMode() === 'tree' ? 'bg-[#38bdf8]/16 text-[#8ed5ff]' : 'text-[#8b949e]'}`} onClick={() => setViewMode('tree')}>委托树</button>
              <button class={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${viewMode() === 'list' ? 'bg-[#38bdf8]/16 text-[#8ed5ff]' : 'text-[#8b949e]'}`} onClick={() => setViewMode('list')}>详情</button>
            </div>
          </Show>
          <button
            class={`rounded-full border px-2.5 py-1.5 text-[11px] font-semibold transition-colors ${autoRefresh() ? 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]' : 'border-[#3e484f] bg-[#171c20] text-[#8b949e] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]'}`}
            onClick={() => setAutoRefresh((prev) => !prev)}
            title="切换自动刷新"
          >
            {autoRefresh() ? '自动刷新 10s' : '自动刷新 关'}
          </button>
          <button
            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            onClick={() => scheduleStructuralSync({ immediate: true })}
            disabled={loading()}
            title="刷新子代理"
          >
            {loading() ? '刷新中...' : '刷新'}
          </button>
        </div>
      </div>
      </div>

      <div class="tcode-scrollbar flex-1 overflow-y-auto p-4 space-y-4">
        <p class="text-xs text-[#9faeb7]">Worker 暂停保留当前进度，在下一次模型或工具调用前停止推进；恢复继续原任务，不重置用量。预算耗尽后禁止新调用，需明确增加额度后恢复。已发出的请求允许返回，不取消远端作业；原地恢复要求当前服务进程仍在运行。</p>
        <Show when={props.compact}><button class="collaboration-history-toggle" aria-expanded={showCompleted()} onClick={() => {setShowCompleted(!showCompleted());setCurrentPage(1);}}>{showCompleted() ? '收起已结束任务' : '查看已结束任务'}（{mergedItems().filter(item => isFinishedCollaborationTask(collaborationRunStatus(item, stateMap().get(item.session_id)?.status))).length}）</button></Show>
        <Show when={props.compact && !showCompleted() && !loading() && visibleItems().length === 0}><p>当前没有待处理或运行中的协作任务。</p></Show>
        <Show when={loading() && mergedItems().length === 0}>
          <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] px-4 py-4 text-xs text-[#8b949e]">加载子代理中...</div>
        </Show>

        <Show
          when={visibleItems().length > 0}
          fallback={<Show when={!props.compact}><div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] px-4 py-6 text-sm text-[#8b949e]">暂无活跃子代理</div></Show>}
        >
          <Show when={!props.compact && viewMode() === 'tree'}>
            <SubagentDelegationTree
              items={visibleItems()}
              rootLabel={sessionStore.selectedAgentName() || '当前主代理'}
              statusOf={item => collaborationRunStatus(item, stateMap().get(item.session_id)?.status)}
              activityOf={item => describeSubagentActivity(item.session_id, collaborationRunStatus(item, stateMap().get(item.session_id)?.status), stateMap().get(item.session_id)?.message || item.last_message || '')}
              onSelect={openFromTree}
              onDiscuss={openSideChat}
            />
          </Show>

          <Show when={props.compact || viewMode() === 'list'}>
          <div class="rounded-2xl border border-[#2a3136] bg-[#10161a] px-3 py-2 text-[11px] text-[#8b949e]">
            共 {visibleItems().length} 个运行实例 · 第 {Math.min(currentPage(),totalPages())} / {totalPages()} 页（每页 {PAGE_SIZE} 条）
          </div>

          <For each={pagedItems()}>
            {(item) => {
              const state = () => stateMap().get(item.session_id);
              const status = () => collaborationRunStatus(item, state()?.status);
              const held = () => ['paused', 'pause_requested'].includes(item.current_runtime_task?.status);
              const note = () => held() ? item.current_runtime_task?.next_action : status() === 'cancelled' ? '当前任务已中止，已有结果和已记录用量保留。' : state()?.message || item.last_message || '';
              const displayName = () => state()?.display_name || item.display_name || item.title || item.agent_name;
              const lastResponse = () => state()?.last_response || item.last_response || '';
              const messages = () => subagentMessages()[item.session_id] || [];
              const responses = () => getAgentResponses(item.session_id);
              const toolTraceMessages = () => getToolTraceMessages(item.session_id);
              const activity = () => held() ? note() : describeSubagentActivity(item.session_id, status(), note());
              const ctx = () => state()?.context || item.context || {};
              const engine = () => state()?.engine || item.engine || '';
              const contract = () => state()?.contract || item.contract;
              const lastUserPrompt = () => ctx().last_user || getLatestUserPrompt(item.session_id);
              const canSendFreshQuestion = () => !held() && Boolean(resolveSubagentMessage(item.session_id, 'ask'));
              const canReask = () => !held() && Boolean(resolveSubagentMessage(item.session_id, 'reask'));

              return (
                <div class="collaboration-task overflow-hidden rounded-3xl border border-[#2a3136] bg-[#13191d] shadow-[0_14px_34px_rgba(0,0,0,0.16)]" data-collapsed={isCardCollapsed(item.session_id)} data-subagent-session={item.session_id}>
                  <div class="border-b border-[#2a3136] bg-[#10161a] px-4 py-4">
                    <div class="flex items-center justify-between gap-2">
                      <button
                        class="min-w-0 flex flex-1 items-center gap-2 text-left"
                        onClick={() => toggleCardCollapsed(item.session_id)}
                        aria-expanded={!isCardCollapsed(item.session_id)}
                        title={isCardCollapsed(item.session_id) ? '展开实例详情' : '折叠实例详情'}
                      >
                        <svg
                          xmlns="http://www.w3.org/2000/svg"
                          class={`h-4 w-4 flex-shrink-0 transition-transform ${isCardCollapsed(item.session_id) ? '' : 'rotate-90'}`}
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                        >
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                        </svg>
                        <div class="min-w-0">
                          <div class="text-sm text-[#f5f7fa] font-semibold truncate" title={contract()?.goal || displayName()}>{props.compact ? collaborationTaskTitle(item, contract()?.goal) : displayName()}</div>
                          <div class="text-xs text-[#8b949e] truncate">{item.agent_name}{props.compact ? ' · AI 执行者' : ` · ${item.session_id}`}</div>
                        </div>
                      </button>
                      <div class="collaboration-extra flex items-center gap-2">
                        <Show when={item.resident_worker}>
                          <span class="rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-2 py-1 text-[10px] font-semibold text-[#9ee6bb]">
                            常驻
                          </span>
                        </Show>
                        <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#9faeb7]">
                          {isCardCollapsed(item.session_id) ? '已折叠' : '已展开'}
                        </span>
                      </div>
                      <div class="flex items-center gap-2">
                        <button
                          class="rounded-full border border-[#7b61ff]/28 bg-[#7b61ff]/12 px-2.5 py-1.5 text-[10px] font-semibold text-[#d8ceff] transition-colors hover:bg-[#7b61ff]/18"
                          onClick={() => openSideChat(item)}
                          title="在右侧打开讨论，不切换主聊天"
                        >
                          与它讨论
                        </button>
                        <span class={`rounded-full border px-3 py-1 text-[11px] font-semibold ${status() === 'running' ? 'border-[#f1a02b]/28 bg-[#f1a02b]/12 text-[#ffe1b3]' : status() === 'completed' ? 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]' : status() === 'failed' ? 'border-[#ff9c96]/28 bg-[#ff9c96]/12 text-[#ffd0cb]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`}>{statusLabel(status())}</span>
                        <Show when={item.deletable !== false && (!props.compact || !isCardCollapsed(item.session_id))}>
                          <button
                            class="rounded-full border border-[#ff9c96]/24 bg-[#ff9c96]/10 px-2.5 py-1.5 text-[10px] font-semibold text-[#ffcbc7]"
                            onClick={() => deleteSubagent(item.session_id)}
                            disabled={deleting()[item.session_id]}
                            title="停止并删除该子代理"
                          >
                            {deleting()[item.session_id] ? '处理中...' : '删除'}
                          </button>
                        </Show>
                      </div>
                    </div>
                    <Show when={note()}>
                      <div class="mt-2 line-clamp-2 text-xs text-[#9faeb7]">{note()}</div>
                    </Show>
                    <Show when={!props.compact || !isCardCollapsed(item.session_id)}>
                    <Show when={item.budget}><p class="mt-2 text-xs text-[#9faeb7]">{collaborationBudgetNotice(item.budget)}</p></Show>
                    <Show when={item.current_runtime_task && (held() || ['running', 'reviewing', 'revising', 'recovering', 'queued'].includes(item.current_runtime_task.status))}>
                      <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
                        <Show when={held() && item.budget?.state === 'exhausted'}>
                          <For each={(['tokens', 'tool_calls'] as const).filter(key => (item.budget?.limits?.[key] || 0) > 0)}>{key =>
                            <label>{key === 'tokens' ? 'Token' : '工具'}新总额度
                              <input type="number" min={(item.budget?.limits?.[key] || 0) + 1} step="1"
                                class="input input-sm ml-1 w-28" aria-label={`${key}新总额度`}
                                value={workerLimits()[item.session_id]?.[key] ?? ''}
                                onInput={event => setWorkerLimits(prev => ({ ...prev, [item.session_id]: {
                                  ...prev[item.session_id], [key]: event.currentTarget.value === '' ? undefined : Number(event.currentTarget.value),
                                } }))} />
                            </label>
                          }</For>
                        </Show>
                        <button class="rounded-full border border-[#3e484f] px-3 py-1.5 disabled:opacity-50"
                          disabled={sending()[item.session_id]}
                          onClick={() => void changeWorkerExecution(item, held() ? 'resume' : 'pause')}>
                          {held() ? '恢复原任务' : '保留进度并暂停'}
                        </button>
                      </div>
                    </Show>
                    <Show when={engine()}>
                      <div class="text-[11px] text-[#8b949e] mt-1 truncate">
                        执行引擎：{engine()}
                      </div>
                    </Show>
                    <Show when={item.worker_role || item.worker_preset || item.worker_profile}>
                      <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[#8b949e]">
                        <Show when={item.worker_role}><span>角色：{item.worker_role}</span></Show>
                        <Show when={item.worker_preset}><span>预设：{item.worker_preset}</span></Show>
                        <Show when={item.worker_task_count !== undefined}><span>已执行：{item.worker_task_count} 个任务</span></Show>
                        <Show when={item.worker_profile?.success_rate !== undefined}><span>成功率：{item.worker_profile?.success_rate}</span></Show>
                        <Show when={item.worker_profile?.avg_latency_sec !== undefined}><span>平均耗时：{Number(item.worker_profile?.avg_latency_sec || 0).toFixed(1)} 秒</span></Show>
                      </div>
                    </Show>
                    <Show when={lastUserPrompt()}>
                      <div class="text-[11px] text-[#8b949e] mt-1 line-clamp-2">最近问题：{lastUserPrompt()}</div>
                    </Show>
                    <Show when={status() === 'running'}>
                      <div class="mt-3 rounded-2xl border border-[#f1a02b]/22 bg-[#20190f] px-3 py-2 text-[11px] text-[#ffe1b3]">
                        运行中的子代理不允许旁路提问。请使用继续指导、重新提问或中止当前回合。
                      </div>
                    </Show>
                    <Show when={contract() && (contract()?.goal || contract()?.deliverable || contract()?.stop_when)}>
                      <div class="mt-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] p-3 text-[11px] text-[#9faeb7] whitespace-pre-wrap">
                        目标: {contract()?.goal || '-'}{'\n'}
                        交付物: {contract()?.deliverable || '-'}{'\n'}
                        停止条件: {contract()?.stop_when || '-'}
                      </div>
                    </Show>
                    <div class="text-[11px] text-[#8b949e] mt-1">
                      上下文：{ctx().message_count || messages().length} 条消息 / {ctx().tool_call_count || 0} 次工具
                    </div>
                    </Show>
                  </div>

                  <Show when={!isCardCollapsed(item.session_id)}>
                    <div class="tcode-scrollbar h-96 overflow-y-auto border-t border-[#2a3136] bg-[#11171b] px-4 py-4">
                      <div class={`mb-4 rounded-lg border px-3 py-3 ${status() === 'running' ? 'border-[#f1a02b]/28 bg-[#20190f]' : status() === 'failed' ? 'border-[#ff9c96]/28 bg-[#241517]' : 'border-[#2a3136] bg-[#0d1216]'}`}>
                        <div class="text-[10px] font-semibold uppercase tracking-[0.14em] text-[#7f8b93]">执行动态</div>
                        <div class={`mt-1 text-xs font-medium ${statusClass(status())}`}>{activity()}</div>
                      </div>

                      <section>
                        <div class="mb-2 text-[11px] font-semibold text-[#c9d1d9]">代理回复</div>
                        <Show
                          when={responses().length > 0}
                          fallback={
                            <Show
                              when={lastResponse()}
                              fallback={<div class="rounded-lg border border-dashed border-[#3e484f] px-3 py-4 text-xs text-[#6e7681]">尚未产生文字回复</div>}
                            >
                              <div class="rounded-lg border border-[#2a3136] bg-[#13191d] p-3">
                                <MarkdownContent content={lastResponse()} role="assistant" renderKey={`subagent-fallback:${item.session_id}`} />
                              </div>
                            </Show>
                          }
                        >
                          <div class="space-y-2">
                            <For each={responses()}>
                              {(msg, responseIndex) => (
                                <div class="rounded-lg border border-[#2a3136] bg-[#13191d] p-3">
                                  <div class="mb-2 text-[10px] font-medium text-[#7bd0ff]">
                                    {responseIndex() === responses().length - 1 ? '最新回复' : `阶段回复 ${responseIndex() + 1}`}
                                  </div>
                                  <MarkdownContent
                                    content={msg.content}
                                    role="assistant"
                                    renderKey={`subagent-response:${item.session_id}:${msg.id || responseIndex()}`}
                                  />
                                </div>
                              )}
                            </For>
                          </div>
                        </Show>
                      </section>

                      <Show when={toolTraceMessages().length > 0}>
                        <section class="mt-5 border-t border-[#2a3136] pt-4">
                          <div class="mb-2 text-[11px] font-semibold text-[#9faeb7]">工具轨迹</div>
                          <div class="space-y-2">
                            <For each={toolTraceMessages()}>
                              {(msg, msgIndex) => (
                                <SubagentMessageItem
                                  subagentSessionId={item.session_id}
                                  msg={msg}
                                  index={msgIndex()}
                                  hideContent={msg.role === 'assistant'}
                                />
                              )}
                            </For>
                          </div>
                        </section>
                      </Show>
                    </div>

                    <div class="space-y-3 border-t border-[#2a3136] bg-[#13191d] p-4">
                      <input
                        ref={(el) => {
                          inputRefs[item.session_id] = el;
                        }}
                        type="text"
                        class="input input-sm flex-1 bg-[#0f1418] border-[#2a3136] text-[#e6edf3]"
                        placeholder={status() === 'running' ? '直接指导这个子代理；如需整轮重问，可留空后点击“重问”...' : '直接提问这个子代理...'}
                        value={drafts()[item.session_id] || ''}
                        onInput={(e) => setDrafts(prev => ({ ...prev, [item.session_id]: e.currentTarget.value }))}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            void sendToSubagent(item.session_id, status() === 'running' ? 'interrupt-guide' : 'ask');
                          }
                        }}
                      />
                      <div class="flex flex-wrap items-center gap-2">
                        <button
                          class="rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 px-3 py-1.5 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50"
                          onClick={() => void sendToSubagent(item.session_id, status() === 'running' ? 'interrupt-guide' : 'ask')}
                          disabled={sending()[item.session_id] || !canSendFreshQuestion()}
                          title={status() === 'running' ? '直接在当前位置指导这个子代理' : '向这个子代理发送新问题'}
                        >
                          {sending()[item.session_id] ? '...' : status() === 'running' ? '指导' : '提问'}
                        </button>
                        <button
                          class="rounded-full border border-[#7b61ff]/28 bg-[#7b61ff]/12 px-3 py-1.5 text-xs font-semibold text-[#d8ceff] disabled:opacity-50"
                          onClick={() => void sendToSubagent(item.session_id, 'reask')}
                          disabled={sending()[item.session_id] || !canReask()}
                          title={status() === 'running'
                            ? '优先使用当前输入；如果输入为空，则复用上一条用户问题并切换当前回合'
                            : '优先使用当前输入；如果输入为空，则复用上一条用户问题重新执行'}
                        >
                          重问
                        </button>
                        <button
                          class="rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-3 py-1.5 text-xs font-semibold text-[#ffd0cb] disabled:opacity-50"
                          onClick={() => stopSubagentRun(item.session_id)}
                          disabled={sending()[item.session_id]}
                          title="仅中止当前子代理回合"
                        >
                          中止
                        </button>
                        <button
                          class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/12 px-3 py-1.5 text-xs font-semibold text-[#ffe1b3] disabled:opacity-50"
                          onClick={() => stopSubagentRun(item.session_id, true)}
                          disabled={sending()[item.session_id]}
                          title="中止当前子代理回合，并通知主代理重新规划"
                        >
                          中止并重规划
                        </button>
                      </div>
                      <div class="text-[11px] text-[#8b949e]">
                        {status() === 'running'
                          ? '运行中不会创建旁路子会话。直接指导会中断当前步骤并在当前位置继续，重新提问会优先使用当前输入，否则复用上一条问题。'
                          : canReask()
                            ? '可直接发送新问题，也可点击“重新提问”复用上一条用户问题。'
                            : '先发送一条问题，之后这里会支持一键重新提问。'}
                      </div>
                    </div>
                  </Show>
                </div>
              );
            }}
          </For>

          <Show when={totalPages() > 1}>
            <div class="flex items-center justify-between rounded-2xl border border-[#2a3136] bg-[#10161a] px-3 py-2 text-[11px] text-[#9faeb7]">
              <div class="flex items-center gap-2">
                <button
                  class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50"
                  onClick={() => setCurrentPage((prev) => Math.max(1, prev - 1))}
                  disabled={currentPage() <= 1}
                >
                  上一页
                </button>
                <button
                  class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50"
                  onClick={() => setCurrentPage((prev) => Math.min(totalPages(), prev + 1))}
                  disabled={currentPage() >= totalPages()}
                >
                  下一页
                </button>
              </div>
            </div>
          </Show>
          </Show>
        </Show>
      </div>
    </div>
    </Show>
  );
}
