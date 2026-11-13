import { For, Show, createEffect, createMemo, createSignal, on, onCleanup } from 'solid-js';
import {
  askSideQuestion,
  cancelSessionRun,
  chatWithSubagent,
  getChatRuntimeStatus,
  getSession,
  getSubagents,
  getSideChats,
  injectSideChat,
  getTeamWorkers,
  teamChat,
  type SubagentItem,
} from '../api/client';
import { sessionStore, type Message, type QuestionRequest, type ToolCall } from '../stores/session';
import { composerDrafts, type AttachedFile } from '../stores/composer';
import FileUpload from './FileUpload';
import { buildFinalMessage } from './composer/messagePayload';
import { applySideChatText, readSideChatSelection, saveSideChatSelection, sideChatDraftKey } from '../utils/sideChat';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import { sanitizeMessageContentBlocks } from '../utils/contentBlocks';
import { getToolTextDisplayStats } from '../utils/toolTextDisplay';
import { toolDisplayLabel } from '../utils/userFacingLabels';
import { getToolRuntimeSummary, getToolVerificationLabel } from '../utils/toolRuntime';
import MarkdownContent from './MarkdownContent';
import StructuredContentBlocks from './StructuredContentBlocks';
import SideChatTranscript from './SideChatTranscript';
import QuestionCard from './QuestionCard';
import { isWorkflowExecutionSession } from '../utils/collaborationTasks';

type SideChatWorkspaceProps = {
  active?: boolean;
  selectedSessionId?: string;
  pending?: boolean;
  onSelectedSessionChange?: (sessionId: string) => void;
  onPendingHandled?: () => void;
  onClose?: () => void;
  embedded?: boolean;
  onQuote?: (text: string) => void;
  sideOnly?: boolean;
  sourceMessageId?: string;
  onDeleted?: () => void;
};

type RuntimeSnapshot = {
  running?: boolean;
  pending_guidance_count?: number;
  guidance_worker_active?: boolean;
  status?: string;
};

type SideMessageScrollMode = 'force-bottom' | 'follow-if-near-bottom';

const isSideQuestionItem = (item: SubagentItem) => {
  const title = String(item.title || '').trim();
  const displayName = String(item.display_name || '').trim();
  return Boolean(item.side_question) || title.includes('旁路提问') || displayName === '旁路提问';
};

const statusLabel = (status?: string) => {
  const normalized = String(status || '').trim().toLowerCase();
  const labels: Record<string, string> = {
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    idle: '空闲',
    busy: '忙碌',
    done: '已完成',
    cancelled: '已取消',
  };
  return labels[normalized] || status || '未知';
};

const messageRoleLabel = (role?: string) => {
  switch (String(role || '').trim().toLowerCase()) {
    case 'user':
      return '';
    case 'system':
      return '系统';
    case 'tool':
      return '工具';
    default:
      return '助手';
  }
};

const messageRoleClass = (role?: string) => {
  switch (String(role || '').trim().toLowerCase()) {
    case 'user':
      return 'border-[#2f4552] bg-[#101920]';
    case 'system':
      return 'border-[#4f3f20] bg-[#19160f]';
    case 'tool':
      return 'border-[#38424b] bg-[#10161a]';
    default:
      return 'border-[#2a3136] bg-[#13191d]';
  }
};

const formatToolArguments = (argumentsValue?: Record<string, any>) => {
  try {
    return JSON.stringify(argumentsValue || {}, null, 2);
  } catch (error) {
    console.error('Tool arguments stringify failed:', error);
    return String(argumentsValue ?? '');
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

const getToolDetailKey = (tool: ToolCall, index: number) => {
  if (tool.call_id) return tool.call_id;
  return `${tool.tool_name}:${index}`;
};

function SideToolCall(props: { messageId: string; tool: ToolCall; index: number }) {
  const detailKey = createMemo(() => getToolDetailKey(props.tool, props.index));
  const isCollapsed = createMemo(() =>
    sessionStore.isMessageToolDetailsCollapsed(props.messageId, detailKey())
  );
  const hasArguments = createMemo(() => Object.keys(props.tool.arguments || {}).length > 0);
  const hasResult = createMemo(
    () => Boolean(props.tool.output || props.tool.error || ['completed', 'error', 'skipped', 'interrupted'].includes(String(props.tool.state || '')))
  );
  const runtimeSummary = createMemo(() => getToolRuntimeSummary(props.tool));
  const argumentsText = createMemo(() => formatToolArguments(props.tool.arguments));
  const argumentStats = createMemo(() => getToolTextDisplayStats(argumentsText()));
  const resultText = createMemo(() => getToolVerificationLabel(props.tool)
    ? `操作已发送；此条记录不代表任务完成，实际效果请查看后续观察。\n${props.tool.output || ''}`.trim()
    : props.tool.error || props.tool.output || '（无返回内容）');
  const resultStats = createMemo(() => getToolTextDisplayStats(resultText()));
  const detailLabel = createMemo(() => {
    if (getToolVerificationLabel(props.tool)) return '动作回执';
    if (props.tool.error) return '错误返回';
    if (props.tool.output) return '返回结果';
    if (props.tool.state === 'completed') return '返回结果';
    if (props.tool.state === 'error') return '错误返回';
    if (props.tool.state === 'skipped') return '未执行（已被新指导替代）';
    if (props.tool.state === 'interrupted') return '中断结果';
    return '调用详情';
  });

  return (
    <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d] shadow-[0_10px_24px_rgba(0,0,0,0.14)]">
      <button
        type="button"
        class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#182027]"
        aria-expanded={!isCollapsed()}
        onClick={() => sessionStore.setMessageToolDetailsCollapsed(props.messageId, detailKey(), !isCollapsed())}
      >
        <span class="min-w-0 flex items-center gap-2">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            class={`h-4 w-4 flex-shrink-0 text-[#9faeb7] transition-transform ${isCollapsed() ? '' : 'rotate-90'}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>
          <span class="break-all text-xs font-semibold text-[#8ed5ff]">{toolDisplayLabel(props.tool.tool_name)}</span>
          <Show when={runtimeSummary()}>
            <span class={`flex-shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${runtimeSummary()!.badgeClass}`}>
              {runtimeSummary()!.badge}
            </span>
          </Show>
        </span>
        <span class="flex-shrink-0 rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#9faeb7]">{getToolVerificationLabel(props.tool) || formatToolState(props.tool.state)}</span>
      </button>

      <Show when={!isCollapsed()}>
        <div class="space-y-3 px-4 pb-4 text-xs">
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

          <Show when={hasArguments()}>
            <div>
              <div class="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.18em] text-[#7f8b93]">
                <span>调用参数</span>
                <span class="normal-case tracking-normal text-[#6f7d87]">{argumentStats().label}</span>
              </div>
              <pre class="tcode-scrollbar max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#2a3136] bg-[#0d1216] p-3 text-[#cfe1e9]">{argumentsText()}</pre>
            </div>
          </Show>

          <Show when={hasResult()}>
            <div>
              <div class="mb-2 flex items-center justify-between gap-3">
                <div class={`text-[11px] uppercase tracking-[0.18em] ${props.tool.error && !getToolVerificationLabel(props.tool) ? 'text-[#ff9c96]' : 'text-[#7f8b93]'}`}>
                  {detailLabel()}
                </div>
                <div class="flex flex-shrink-0 items-center gap-2 text-[11px] text-[#6f7d87]">
                  <Show when={resultStats().isLong}>
                    <span>限制高度</span>
                  </Show>
                  <span>{resultStats().label}</span>
                </div>
              </div>
              <pre class={`tcode-scrollbar max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-xl border p-3 ${props.tool.error && !getToolVerificationLabel(props.tool) ? 'border-[#5a1e23] bg-[#2d1418] text-[#ffc3be]' : 'border-[#2a3136] bg-[#0d1216] text-[#cfe1e9]'}`}>
                {resultText()}
              </pre>
            </div>
          </Show>
        </div>
      </Show>
    </div>
  );
}

function SideToolProcessGroup(props: { messageId: string; toolCalls: ToolCall[] }) {
  const isCollapsed = createMemo(() => sessionStore.isMessageToolsCollapsed(props.messageId));

  return (
    <div class="mt-4 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3">
      <button
        type="button"
        class="flex w-full items-center justify-between gap-2 text-xs text-[#9faeb7] hover:text-[#e6edf3]"
        onClick={() => sessionStore.setMessageToolsCollapsed(props.messageId, !isCollapsed())}
      >
        <span class="flex items-center gap-1">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            class={`h-4 w-4 transition-transform ${isCollapsed() ? '' : 'rotate-90'}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>
          工具过程 ({props.toolCalls.length})
        </span>
        <span class="text-[11px] text-[#6f7d87]">
          {isCollapsed() ? '已折叠' : '展开中'}
        </span>
      </button>

      <Show when={!isCollapsed()}>
        <div class="mt-2 space-y-2">
          <For each={props.toolCalls}>
            {(tool, index) => (
              <SideToolCall
                messageId={props.messageId}
                tool={tool}
                index={index()}
              />
            )}
          </For>
        </div>
      </Show>
    </div>
  );
}

function SideChatMessage(props: { message: Message; index: number; isolated?: boolean }) {
  const role = createMemo(() => String(props.message.role || '').trim().toLowerCase());
  const renderKey = createMemo(() => props.message.id || `side-message-${props.index}`);
  const messageId = createMemo(() => props.message.id || `side-message-${props.index}`);
  const [processContentCollapsed, setProcessContentCollapsed] = createSignal(true);
  const isProcessContent = createMemo(() => role() === 'tool');
  const isJsonText = createMemo(() => {
    const text = String(props.message.content || '').trim();
    if (!text || !text.startsWith('{')) return false;
    try {
      JSON.parse(text);
      return true;
    } catch {
      return false;
    }
  });

  return (
    <div class={`rounded-2xl border shadow-[0_10px_24px_rgba(0,0,0,0.16)] ${messageRoleClass(role())}`}>
      <div class="flex gap-4 p-4">
        <div class={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border ${role() === 'user' ? 'border-[#38bdf8]/35 bg-[#38bdf8]/12 text-[#8ed5ff]' : 'border-[#3e484f] bg-[#171c20] text-[#8ed5ff]'}`}>
          <Show
            when={role() === 'user'}
            fallback={
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4.5 w-4.5">
                <path d="M12 2 4 6v6c0 5.55 3.84 10.74 8 12 4.16-1.26 8-6.45 8-12V6l-8-4Zm0 2.18 5.82 2.91v4.77c0 4.24-2.79 8.59-5.82 9.82-3.03-1.23-5.82-5.58-5.82-9.82V7.09L12 4.18Z" />
              </svg>
            }
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4.5 w-4.5">
              <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4Zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4Z" />
            </svg>
          </Show>
        </div>
        <div class="min-w-0 flex-1">
          <div style={role() === 'user' ? {display:'none'} : undefined} class="mb-2 flex flex-wrap items-center gap-2">
            <span class="text-sm font-semibold text-[#f5f7fa]">{messageRoleLabel(role())}</span>
            <span class={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] ${role() === 'user' ? 'border-[#38bdf8]/35 bg-[#38bdf8]/10 text-[#8ed5ff]' : role() === 'system' ? 'border-[#f1a02b]/30 bg-[#f1a02b]/10 text-[#ffc176]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`}>
              {role() || 'assistant'}
            </span>
          </div>
          <Show when={props.message.content}>
            <Show
              when={isProcessContent()}
              fallback={
                <div class={role() === 'user' ? 'text-sm text-[#dee3e8]' : 'rounded-2xl border border-[#2a3136] bg-[#10161a] px-4 py-3.5'}>
                  <Show
                    when={isJsonText()}
                    fallback={
                      <MarkdownContent
                        content={String(props.message.content || '')}
                        role={props.message.role}
                        allowSessionActions={!props.isolated}
                        renderKey={renderKey()}
                        class="text-sm text-[#dee3e8]"
                      />
                    }
                  >
                    <pre class="max-h-[70vh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[#080d10] p-3 text-xs leading-relaxed text-[#dee3e8]">
                      {String(props.message.content || '')}
                    </pre>
                  </Show>
                </div>
              }
            >
              <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0d1216]">
                <button
                  type="button"
                  class="flex w-full items-center justify-between gap-2 px-4 py-3 text-left text-xs text-[#9faeb7] transition-colors hover:bg-[#182027] hover:text-[#e6edf3]"
                  onClick={() => setProcessContentCollapsed((prev) => !prev)}
                >
                  <span class="flex min-w-0 items-center gap-1">
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      class={`h-4 w-4 shrink-0 transition-transform ${processContentCollapsed() ? '' : 'rotate-90'}`}
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                    >
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                    </svg>
                    <span class="truncate">工具 / Skill 输出</span>
                  </span>
                  <span class="shrink-0 text-[11px] text-[#6f7d87]">
                    {processContentCollapsed() ? '已折叠' : '展开中'}
                  </span>
                </button>
                <Show when={!processContentCollapsed()}>
                  <div class="border-t border-[#2a3136] px-4 py-3.5">
                    <MarkdownContent
                      content={String(props.message.content || '')}
                      role={props.message.role}
                      allowSessionActions={!props.isolated}
                      renderKey={renderKey()}
                      class="text-sm text-[#dee3e8]"
                    />
                  </div>
                </Show>
              </div>
            </Show>
          </Show>
          <StructuredContentBlocks
            blocks={props.message.content_blocks}
            allowSessionActions={!props.isolated}
            class={props.message.content ? 'mt-3' : ''}
          />
          <Show when={props.message.tool_calls && props.message.tool_calls.length > 0}>
            <SideToolProcessGroup messageId={messageId()} toolCalls={props.message.tool_calls!} />
          </Show>
        </div>
      </div>
    </div>
  );
}

export default function SideChatWorkspace(props: SideChatWorkspaceProps) {
  let chatContainer: HTMLDivElement | undefined;
  let contentResizeObserver: ResizeObserver | undefined;
  let pendingScrollFrame: number | undefined;
  let messageLoadRequestId = 0;
  let disposed = false;
  const pageVisible = createPageVisibleSignal();
  const [messageListElement, setMessageListElement] = createSignal<HTMLDivElement>();
  const [items, setItems] = createSignal<SubagentItem[]>([]);
  const [messages, setMessages] = createSignal<Message[]>([]);
  const [localSelectedId, setLocalSelectedId] = createSignal('');
  const [loadingItems, setLoadingItems] = createSignal(false);
  const [loadingMessages, setLoadingMessages] = createSignal(false);
  const [sending, setSending] = createSignal(false);
  const [draft, setDraft] = createSignal('');
  const [attachedFiles, setAttachedFiles] = createSignal<AttachedFile[]>([]);
  const [showUpload, setShowUpload] = createSignal(false);
  const [uploading, setUploading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [runtime, setRuntime] = createSignal<RuntimeSnapshot | null>(null);
  const [question, setQuestion] = createSignal<QuestionRequest | null>(null);
  const [stopping, setStopping] = createSignal(false);
  const [injecting, setInjecting] = createSignal(false);
  const [injectionFeedback, setInjectionFeedback] = createSignal('');
  const [sourceMessageId, setSourceMessageId] = createSignal(props.sourceMessageId || '');
  const [lastFailedText, setLastFailedText] = createSignal('');
  const liveTextIds = new Set<string>();
  createEffect(on(() => props.sourceMessageId, id => setSourceMessageId(id || '')));
  const [userScrolledUp, setUserScrolledUp] = createSignal(false);
  let scrollListenerAttached = false;

  const isActive = () => props.active ?? true;
  const parentSessionId = () => String(sessionStore.currentSessionId() || '').trim();
  const requestedSelectedId = () => String(props.selectedSessionId ?? localSelectedId()).trim();
  const draftKey = () => sideChatDraftKey(parentSessionId(), requestedSelectedId());
  const updateDraft = (text: string) => {
    setDraft(text); composerDrafts.update(draftKey(), {text});
  };
  const updateFiles = (files: AttachedFile[]) => {
    setAttachedFiles(files); composerDrafts.update(draftKey(), {files});
  };
  createEffect(on(draftKey, key => {
    setAttachedFiles(composerDrafts.get(key).files); setShowUpload(false);
    setDraft(composerDrafts.get(key).text); setRuntime(null); setMessages([]); setError(''); setQuestion(null); setInjectionFeedback(''); liveTextIds.clear(); messageLoadRequestId++;
  }));
  const orderedItems = createMemo(() =>
    [...items()].sort((left, right) => {
      const leftSide = isSideQuestionItem(left) ? 0 : 1;
      const rightSide = isSideQuestionItem(right) ? 0 : 1;
      if (leftSide !== rightSide) return leftSide - rightSide;
      return Number(right.created_at || 0) - Number(left.created_at || 0);
    }),
  );
  const selectedItem = createMemo(() =>
    orderedItems().find((item) => item.session_id === requestedSelectedId()) || null,
  );
  const selectedRunning = createMemo(() => {
    const currentRuntime = runtime();
    if (currentRuntime) return Boolean(currentRuntime.running || currentRuntime.guidance_worker_active);
    const status = String(selectedItem()?.status || '').trim().toLowerCase();
    return status === 'running' || status === 'busy';
  });

  const scrollToBottom = () => {
    if (!chatContainer) return;
    chatContainer.scrollTop = chatContainer.scrollHeight;
  };

  const handleChatScroll = () => {
    if (!chatContainer) return;
    const distFromBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
    setUserScrolledUp(distFromBottom > 120);
  };

  const attachScrollListener = () => {
    if (scrollListenerAttached || !chatContainer) return;
    scrollListenerAttached = true;
    chatContainer.addEventListener('scroll', handleChatScroll, { passive: true });
  };

  const scheduleScrollToBottom = () => {
    if (pendingScrollFrame !== undefined) {
      window.cancelAnimationFrame(pendingScrollFrame);
    }
    pendingScrollFrame = window.requestAnimationFrame(() => {
      pendingScrollFrame = undefined;
      scrollToBottom();
    });
  };

  const scheduleFollowIfNearBottom = () => {
    if (!userScrolledUp()) scheduleScrollToBottom();
  };

  const normalizeMessages = (raw: any[]): Message[] =>
    (Array.isArray(raw) ? raw : [])
      .filter((message) => {
        if (message?.metadata?.side_chat_context || String(message?.content || '').startsWith('[Side Chat Context]')) return false;
        const role = String(message?.role || '').trim().toLowerCase();
        return role === 'user' || role === 'assistant' || role === 'system' || role === 'tool';
      })
      .map((message, index) => {
        const contentBlocks = sanitizeMessageContentBlocks(message?.content_blocks);
        return {
          id: String(message?.id || `side-message-${index}`),
          role: (message?.role || 'assistant') as Message['role'],
          content: String(message?.content || ''),
          content_blocks: contentBlocks.length > 0 ? contentBlocks : undefined,
          tool_calls: Array.isArray(message?.tool_calls) ? message.tool_calls : undefined,
          presentation: message?.presentation || undefined,
          created_at: message?.created_at,
          usage: message?.usage || undefined,
        };
      });

  const stabilizeMessages = (current: Message[], next: Message[]) => {
    const currentById = new Map(
      current.map((message) => [String(message.id || ''), message]),
    );
    return next.map((message) => {
      const previous = currentById.get(String(message.id || ''));
      if (!previous) return message;
      const unchanged =
        String(message.role || '') === String(previous.role || '') &&
        String(message.content || '') === String(previous.content || '') &&
        message.presentation === previous.presentation &&
        JSON.stringify(message.content_blocks || []) === JSON.stringify(previous.content_blocks || []) &&
        JSON.stringify(message.tool_calls || []) === JSON.stringify(previous.tool_calls || []) &&
        JSON.stringify(message.usage || null) === JSON.stringify(previous.usage || null);
      return unchanged ? previous : message;
    });
  };
  const reconcileLiveMessages = (current: Message[], next: Message[], running = selectedRunning()) => {
    const received = new Set(next.map(message=>message.id));
    for (const id of received) liveTextIds.delete(id);
    if (!running) liveTextIds.clear();
    return stabilizeMessages(current, [...next, ...current.filter(message=>
      !received.has(message.id) && (liveTextIds.has(message.id) || (running && message.id.startsWith('local-side-user-') && !next.some(m=>m.role==='user' && m.content===message.content)))
    )]);
  };

  const selectSession = (sessionId: string, load = true) => {
    const normalized = String(sessionId || '').trim();
    setLocalSelectedId(normalized);
    if (props.sideOnly) saveSideChatSelection(parentSessionId(), normalized);
    props.onSelectedSessionChange?.(normalized);
    props.onPendingHandled?.();
    if (load && normalized) {
      void loadSessionMessages(normalized, true, 'force-bottom');
    }
  };

  const loadSessionMessages = async (
    sessionId = requestedSelectedId(),
    bustCache = false,
    scrollMode: SideMessageScrollMode = 'follow-if-near-bottom',
  ) => {
    const normalized = String(sessionId || '').trim();
    const requestId = ++messageLoadRequestId;
    if (!normalized) {
      setMessages([]);
      return;
    }
    setLoadingMessages(true);
    setError('');
    try {
      const detail = await getSession(normalized, {
        includeToolMessages: true,
        bustCache,
      });
      if (disposed || requestId !== messageLoadRequestId || normalized !== requestedSelectedId()) return;
      if (!detail) {
        // getSession also returns null on network failures; only an
        // authoritative deleted event/list refresh may remove a saved tab.
        setError('暂时无法加载这段聊天，请稍后重试。');
        return;
      }
      const nextMessages = normalizeMessages(detail?.messages || []);
      setMessages((current) => props.sideOnly ? reconcileLiveMessages(current,nextMessages) : stabilizeMessages(current, nextMessages));
      if (scrollMode === 'force-bottom') {
        setUserScrolledUp(false);
        scheduleScrollToBottom();
      } else {
        scheduleFollowIfNearBottom();
      }
    } catch (err) {
      if (requestId !== messageLoadRequestId) return;
      console.error('Failed to load side chat messages:', err);
      setError(err instanceof Error ? err.message : '加载旁支消息失败');
    } finally {
      if (requestId === messageLoadRequestId) {
        setLoadingMessages(false);
      }
    }
  };

  const loadRuntime = async (sessionId = requestedSelectedId()) => {
    const normalized = String(sessionId || '').trim();
    if (!normalized) {
      setRuntime(null);
      return;
    }
    try {
      const snapshot = await getChatRuntimeStatus(normalized);
      if (disposed || normalized !== requestedSelectedId()) return;
      setRuntime(snapshot || null);
    } catch (err) {
      console.error('Failed to load side chat runtime:', err);
    }
  };

  const loadItems = async () => {
    const parentId = parentSessionId();
    if (!isActive() || !parentId) return;
    setLoadingItems(true);
    setError('');
    try {
      const [subagents, workers] = await Promise.all([
        props.sideOnly ? getSideChats(parentId) : getSubagents(parentId),
        props.sideOnly ? Promise.resolve([]) : getTeamWorkers(parentId),
      ]);
      const nextItems = subagents.filter(item => !isWorkflowExecutionSession(item) && (!props.sideOnly || isSideQuestionItem(item)));
      const seen = new Set(nextItems.map(item => item.session_id));
      for (const worker of workers) {
        if (props.sideOnly) break;
        if (seen.has(worker.session_id) || worker.visible === false || worker.enabled === false) continue;
        nextItems.push({
          session_id: worker.session_id,
          parent_session_id: parentId,
          agent_name: `worker_${worker.name}`,
          display_name: worker.name,
          title: worker.role || worker.name,
          status: worker.status,
          created_at: worker.created_at,
          message_count: 0,
          last_message: worker.last_task || '',
          is_team_worker: true,
          team_owner_session_id: parentId,
          worker_name: worker.name,
          team_worker: worker.team_worker,
          resident_worker: Boolean(worker.resident_worker),
        });
        seen.add(worker.session_id);
      }
      if (disposed || parentId !== parentSessionId()) return;
      const pendingItem = props.sideOnly ? items().find(item=>item.session_id===requestedSelectedId()) : undefined;
      setItems(pendingItem && !nextItems.some(item=>item.session_id===pendingItem.session_id) ? [...nextItems,pendingItem] : nextItems);
      const currentId = requestedSelectedId();
      if (currentId) {
        await loadSessionMessages(currentId, true);
        await loadRuntime(currentId);
        return;
      }
      const fallback = props.sideOnly && readSideChatSelection(parentId) !== null ? undefined : nextItems.find(isSideQuestionItem) || nextItems[0];
      if (props.pending) return;
      if (fallback?.session_id) {
        selectSession(fallback.session_id, false);
        await loadSessionMessages(fallback.session_id, true, 'force-bottom');
        await loadRuntime(fallback.session_id);
      } else {
        setMessages([]);
      }
    } catch (err) {
      console.error('Failed to load side chat items:', err);
      setError(err instanceof Error ? err.message : '加载旁支列表失败');
    } finally {
      setLoadingItems(false);
    }
  };

  const refreshAll = async () => {
    await loadItems();
  };

  const sendSideMessage = async () => {
    const originalDraft = draft();
    const files = attachedFiles();
    const text = props.sideOnly ? buildFinalMessage(originalDraft, files, []) : originalDraft.trim();
    const parentId = parentSessionId();
    let subSessionId = requestedSelectedId();
    if (!text || !parentId || (!props.sideOnly && !subSessionId) || sending() || uploading() || selectedRunning()) return;

    if (props.sideOnly) {
      const originalKey = draftKey();
      setSending(true); setError(''); setLastFailedText(''); setInjectionFeedback('');
      try {
        if (!subSessionId) {
          const created = await askSideQuestion(parentId, text, sessionStore.currentSession()?.agent_name || 'general', {
            createOnly: true, sourceMessageId: sourceMessageId(),
          });
          if (!created) throw new Error('创建侧聊失败');
          subSessionId = created.session_id;
          if (disposed || parentId !== parentSessionId()) return;
          selectSession(subSessionId, false);
          setItems(current=>[...current,{session_id:subSessionId,parent_session_id:parentId,agent_name:created.agent_name,title:`侧边聊天 · ${text.slice(0,36)}`,status:'idle',created_at:Date.now()/1000,message_count:0,last_message:text,side_question:true}]);
        }
        await chatWithSubagent(parentId, subSessionId, text, {integrateToParent:false, background:true});
        composerDrafts.update(originalKey, {text:'', files:[]});
        composerDrafts.update(sideChatDraftKey(parentId, subSessionId), {text:'', files:[]});
        if (disposed || parentId !== parentSessionId() || subSessionId !== requestedSelectedId()) return;
        setDraft(''); setAttachedFiles([]); setShowUpload(false); setRuntime({running:true});
        setMessages(current => current.some(m=>m.role==='user' && m.content===text) ? current : [...current,{id:`local-side-user-${Date.now()}`,role:'user',content:text}]);
        setUserScrolledUp(false); scheduleScrollToBottom();
        sessionStore.refreshSubagents();
      } catch (err) {
        composerDrafts.update(sideChatDraftKey(parentId, subSessionId), {text:originalDraft, files});
        if (!disposed && parentId === parentSessionId() && subSessionId === requestedSelectedId()) {
          setDraft(originalDraft); setAttachedFiles(files); setRuntime({running:false}); setLastFailedText(originalDraft);
          setError(err instanceof Error ? err.message : '侧聊发送失败');
        }
      } finally { setSending(false); }
      return;
    }

    setSending(true);
    setError('');
    setDraft('');
    setMessages((prev) => [
      ...prev,
      {
        id: `local-side-user-${Date.now()}`,
        role: 'user',
        content: text,
      },
    ]);
    setUserScrolledUp(false);
    scheduleScrollToBottom();

    try {
      const target = selectedItem();
      const resp = target?.is_team_worker && target.worker_name
        ? await teamChat(parentId, target.worker_name, text)
        : await chatWithSubagent(parentId, subSessionId, text, { integrateToParent: false });
      if (!resp) throw new Error('团队成员没有返回结果');
      if (disposed || parentId !== parentSessionId() || subSessionId !== requestedSelectedId()) return;
      sessionStore.upsertSubagentState({
        parent_session_id: parentId,
        session_id: resp.session_id || subSessionId,
        agent_name: ('agent_name' in resp ? resp.agent_name : '') || selectedItem()?.agent_name || 'subagent',
        display_name: ('display_name' in resp ? resp.display_name : '') || selectedItem()?.display_name || selectedItem()?.title,
        status: resp.status === 'completed' ? 'completed' : (resp.status as any) || 'idle',
        message: resp.status === 'completed' ? '旁支回复已完成' : '旁支聊天已更新',
        last_response: ('last_response' in resp ? resp.last_response : '') || resp.response,
        tools_used: 'tools_used' in resp ? resp.tools_used : undefined,
        conversation_with: 'conversation_with' in resp ? resp.conversation_with : undefined,
        context: 'context' in resp ? resp.context : undefined,
        contract: 'contract' in resp ? resp.contract : undefined,
      });
      sessionStore.refreshSubagents();
      await loadSessionMessages(subSessionId, true, 'force-bottom');
      await loadRuntime(subSessionId);
      await loadItems();
    } catch (err) {
      if (disposed || parentId !== parentSessionId() || subSessionId !== requestedSelectedId()) return;
      console.error('Failed to send side chat message:', err);
      const detail = err instanceof Error ? err.message : '旁支聊天发送失败';
      setError(detail);
      setDraft(text);
    } finally {
      setSending(false);
    }
  };

  const toggleSideUpload = async () => {
    if (sending() || uploading() || selectedRunning() || !parentSessionId()) return;
    if (showUpload()) { setShowUpload(false); return; }
    const parentId = parentSessionId();
    setSending(true); setError('');
    try {
      if (!requestedSelectedId()) {
        const originalKey = draftKey();
        const saved = composerDrafts.get(originalKey);
        const created = await askSideQuestion(parentId, '', sessionStore.currentSession()?.agent_name || 'build', {
          createOnly:true, sourceMessageId:sourceMessageId(),
        });
        if (!created) throw new Error('创建侧聊失败');
        if (disposed || parentId !== parentSessionId()) return;
        composerDrafts.update(sideChatDraftKey(parentId, created.session_id), saved);
        selectSession(created.session_id, false);
        setItems(current=>[...current,{session_id:created.session_id,parent_session_id:parentId,agent_name:created.agent_name,title:'侧边聊天',status:'idle',created_at:Date.now()/1000,message_count:0,last_message:'',side_question:true}]);
      }
      setShowUpload(true);
    } catch (err) {
      if (!disposed && parentId === parentSessionId()) setError(err instanceof Error ? err.message : '打开附件失败');
    } finally { setSending(false); }
  };

  const stopSideChat = async () => {
    const id=requestedSelectedId(); if (!id || stopping()) return;
    setStopping(true);
    try { await cancelSessionRun(id, {clear_guidance:true}); await loadRuntime(id); await loadSessionMessages(id,true); }
    catch (err) { setError(err instanceof Error ? err.message : '停止失败'); }
    finally { setStopping(false); }
  };
  const injectIntoMain = async () => {
    const parent=parentSessionId(), id=requestedSelectedId();
    if (!parent || !id || injecting() || selectedRunning()) return;
    setInjecting(true); setError(''); setInjectionFeedback('');
    try {
      await injectSideChat(parent,id);
      if (!disposed && parent===parentSessionId() && id===requestedSelectedId()) setInjectionFeedback('注入成功');
      window.dispatchEvent(new CustomEvent('tcode-side-chat-injected',{detail:{parentSessionId:parent}}));
    } catch(err) {
      if (!disposed && parent===parentSessionId() && id===requestedSelectedId()) setError(err instanceof Error ? err.message : '注入失败');
    } finally { if (!disposed) setInjecting(false); }
  };
  createEffect(() => {
    const id=requestedSelectedId();
    if (!props.sideOnly || !isActive() || !pageVisible() || !id) return;
    const stream=new EventSource(`/api/subagents/${encodeURIComponent(id)}/events`);
    stream.onmessage=event=>{
      if (disposed || id!==requestedSelectedId()) return;
      try {
        const payload=JSON.parse(event.data);
        if (payload.type==='side_chat_snapshot') {
          const next=normalizeMessages(payload.messages);
          setMessages(current=>reconcileLiveMessages(current,next,Boolean(payload.runtime?.running)));
          setRuntime(payload.runtime || {running:false}); setQuestion(payload.question || null); setLoadingMessages(false);
        } else if (payload.type==='side_chat_deleted') { stream.close(); props.onDeleted?.(); }
        else if (payload.type==='question') setQuestion(payload);
        else if (payload.type==='question_timeout') setQuestion(null);
        else if (payload.kind?.startsWith('model_output')) {
          if(payload.message_id) liveTextIds.add(payload.message_id);
          setMessages(current=>applySideChatText(current,payload));
        }
        else if (payload.type==='side_chat_result') {
          setRuntime({running:false,status:payload.status}); setQuestion(null);
          if(payload.status==='failed') {setError(payload.error || '侧聊运行失败');setLastFailedText([...messages()].reverse().find(m=>m.role==='user')?.content || '');}
          void loadItems();
        }
        scheduleFollowIfNearBottom();
      } catch { setError('侧聊状态解析失败，请重新打开面板同步历史。'); }
    };
    stream.onerror=()=>{ if (!disposed && id===requestedSelectedId()) void loadRuntime(id); };
    onCleanup(()=>stream.close());
  });

  createEffect(on([
    () => isActive(),
    () => parentSessionId(),
    () => sessionStore.subagentsVersion(),
  ], ([active]) => {
    if (!active) return;
    void loadItems();
  }));

  createEffect(on(() => props.selectedSessionId, (sessionId) => {
    const normalized = String(sessionId || '').trim();
    if (!isActive() || !normalized) return;
    setLocalSelectedId(normalized);
    void loadSessionMessages(normalized, true, 'force-bottom');
    void loadRuntime(normalized);
  }));

  createEffect(() => {
    if (chatContainer) attachScrollListener();
  });

  createEffect(() => {
    const list = messageListElement();
    if (!list || typeof ResizeObserver === 'undefined') return;
    contentResizeObserver?.disconnect();
    contentResizeObserver = new ResizeObserver(scheduleFollowIfNearBottom);
    contentResizeObserver.observe(list);
  });

  onCleanup(() => {
    disposed = true; messageLoadRequestId++;
    contentResizeObserver?.disconnect();
    contentResizeObserver = undefined;
    if (pendingScrollFrame !== undefined) {
      window.cancelAnimationFrame(pendingScrollFrame);
      pendingScrollFrame = undefined;
    }
    if (scrollListenerAttached && chatContainer) {
      chatContainer.removeEventListener('scroll', handleChatScroll);
      scrollListenerAttached = false;
    }
  });

  createEffect(() => {
    const sessionId = requestedSelectedId();
    if (props.sideOnly || !isActive() || !pageVisible() || !sessionId) return;
    const timer = window.setInterval(() => {
      void loadRuntime(sessionId);
      if (selectedRunning()) {
        void loadSessionMessages(sessionId, true);
      }
    }, 3000);
    onCleanup(() => window.clearInterval(timer));
  });

  const selectedTitle = createMemo(() => {
    const item = selectedItem();
    if (!item) return '旁支聊天';
    return item.display_name || item.title || item.agent_name || '旁支聊天';
  });

  return (
    <Show when={props.sideOnly} fallback={
    <section class="flex min-w-0 flex-1 overflow-hidden bg-[#0f1418]/76" classList={{'collaboration-sidechat': Boolean(props.embedded)}}>
      <Show when={!props.embedded}>
      <aside class="flex h-full w-[320px] shrink-0 flex-col border-r border-[#2a3136] bg-[#171c20]/92">
        <div class="border-b border-[#2a3136] px-4 py-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#b7a8ff]">Side Chats</div>
              <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">旁支聊天</div>
            </div>
            <button
              type="button"
              class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
              onClick={() => void refreshAll()}
              disabled={loadingItems()}
            >
              {loadingItems() ? '刷新中' : '刷新'}
            </button>
          </div>
          <div class="mt-3 rounded-xl border border-[#2a3136] bg-[#10161a] px-3 py-2 text-[11px] text-[#8b949e]">
            当前主会话：{parentSessionId() ? parentSessionId().slice(0, 8).toUpperCase() : '--'}
          </div>
        </div>

        <div class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto p-3">
          <Show when={props.pending}>
            <div class="mb-3 rounded-2xl border border-[#7b61ff]/28 bg-[#18122a] px-3 py-3 text-xs text-[#d8ceff]">
              正在创建新的旁支聊天...
            </div>
          </Show>
          <Show
            when={orderedItems().length > 0}
            fallback={
              <div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] px-4 py-6 text-sm text-[#8b949e]">
                暂无旁支或子会话
              </div>
            }
          >
            <div class="space-y-2">
              <For each={orderedItems()}>
                {(item) => {
                  const selected = () => requestedSelectedId() === item.session_id;
                  const sideQuestion = () => isSideQuestionItem(item);
                  return (
                    <button
                      type="button"
                      class={`w-full rounded-2xl border p-3 text-left transition-colors ${selected() ? 'border-[#7b61ff]/45 bg-[#1a1530]' : 'border-[#2a3136] bg-[#13191d] hover:border-[#7bd0ff]/35 hover:bg-[#18212a]'}`}
                      onClick={() => selectSession(item.session_id)}
                    >
                      <div class="flex items-start justify-between gap-2">
                        <div class="min-w-0">
                          <div class="truncate text-sm font-semibold text-[#f5f7fa]">
                            {item.display_name || item.title || item.agent_name}
                          </div>
                          <div class="mt-1 truncate font-mono text-[10px] text-[#6f7d87]">{item.session_id}</div>
                        </div>
                        <span class={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${sideQuestion() ? 'border-[#7b61ff]/35 bg-[#7b61ff]/12 text-[#d8ceff]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`}>
                          {sideQuestion() ? '旁支' : statusLabel(item.status)}
                        </span>
                      </div>
                      <Show when={item.context?.last_user || item.last_message}>
                        <div class="mt-2 line-clamp-2 text-[11px] leading-5 text-[#9faeb7]">
                          {item.context?.last_user || item.last_message}
                        </div>
                      </Show>
                    </button>
                  );
                }}
              </For>
            </div>
          </Show>
        </div>
      </aside>
      </Show>

      <div class="flex min-w-0 flex-1 flex-col">
        <Show when={props.embedded}>
          <header class="collaboration-chat-header">
            <div><b>侧边讨论</b><button aria-label="关闭侧边讨论" onClick={() => props.onClose?.()}>×</button></div>
            <label>讨论对象<select aria-label="选择侧边讨论对象" value={requestedSelectedId()} onChange={event => selectSession(event.currentTarget.value)}>
              <option value="">选择执行者或独立讨论</option>
              <For each={orderedItems()}>{item => <option value={item.session_id} selected={requestedSelectedId() === item.session_id}>{item.display_name || item.title || item.agent_name} · {item.session_id.match(/wf-([a-z0-9]+)/)?.[1] || item.session_id.slice(0,8)}</option>}</For>
            </select></label>
            <small>所属主会话：{sessionStore.currentSession()?.title || parentSessionId().slice(0, 8)}</small>
            <Show when={props.pending}><p role="status">正在创建独立讨论…</p></Show>
            <Show when={selectedRunning()}><p>执行者正在运行，暂不能追问。请展开上方任务详情进行指导或中止。</p></Show>
            <Show when={props.onQuote && messages().some(message => message.role === 'assistant' && message.content)}>
              <button onClick={() => {
                const message = [...messages()].reverse().find(message => message.role === 'assistant' && message.content);
                if (message) props.onQuote?.(`来自「${selectedTitle()}」的讨论结论：\n${message.content}`);
              }}>引用最新回复到主聊天草稿</button>
            </Show>
          </header>
        </Show>
        <Show when={!props.embedded}>
        <div class="border-b border-[#2a3136] bg-[#0d1216]/88 px-5 py-4 backdrop-blur">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-mono text-[11px] text-[#b7a8ff]">
                  {requestedSelectedId() ? requestedSelectedId().slice(0, 8).toUpperCase() : '未选择'}
                </span>
                <Show when={selectedItem()}>
                  {(item) => (
                    <>
                      <span class={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${selectedRunning() ? 'border-[#f1a02b]/30 bg-[#f1a02b]/12 text-[#ffc176]' : 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]'}`}>
                        {selectedRunning() ? '运行中' : statusLabel(item().status)}
                      </span>
                      <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-0.5 text-[10px] font-semibold text-[#bdc8d1]">
                        {item().agent_name}
                      </span>
                    </>
                  )}
                </Show>
              </div>
              <h1 class="mt-3 line-clamp-2 text-xl font-semibold tracking-tight text-[#f7fafc]">{selectedTitle()}</h1>
              <p class="mt-1 max-w-3xl text-sm text-[#9faeb7]">
                这里是当前主会话派生出来的独立旁支聊天，主线控制台保持原样。
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <button
                type="button"
                class="inline-flex h-8 shrink-0 items-center justify-center whitespace-nowrap rounded-full border border-[#303539] bg-[#171c20] px-3 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                onClick={() => void refreshAll()}
              >
                同步
              </button>
              <Show when={props.onClose}>
                <button
                  type="button"
                  class="inline-flex h-8 shrink-0 items-center justify-center whitespace-nowrap rounded-full border border-[#303539] bg-[#171c20] px-3 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                  onClick={() => props.onClose?.()}
                >
                  返回控制台
                </button>
              </Show>
            </div>
          </div>
        </div>
        </Show>

        <div ref={chatContainer} class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto px-4 py-5 [overflow-anchor:none] md:px-6">
          <Show when={error()}>
            <div class="mx-auto mb-4 max-w-5xl rounded-2xl border border-[#ef6767]/35 bg-[#251315] px-4 py-3 text-sm text-[#ffd0cb]">
              {error()}
            </div>
          </Show>
          <Show
            when={requestedSelectedId()}
            fallback={
              <div class="flex min-h-full items-center justify-center py-10">
                <div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] px-6 py-8 text-center text-sm text-[#8b949e]">
                  选择一个旁支聊天，或在主任务运行时用“旁路提问”创建新的旁支。
                </div>
              </div>
            }
          >
            <Show
              when={!loadingMessages() || messages().length > 0}
              fallback={<div class="mx-auto max-w-5xl rounded-2xl border border-[#2a3136] bg-[#13191d] px-4 py-4 text-sm text-[#8b949e]">加载旁支消息中...</div>}
            >
              <Show
                when={messages().length > 0}
                fallback={<div class="mx-auto max-w-5xl rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] px-4 py-8 text-center text-sm text-[#8b949e]">这个旁支还没有消息。</div>}
              >
                <div ref={setMessageListElement} class="mx-auto flex w-full max-w-5xl flex-col gap-4 py-2">
                  <For each={messages()}>
                    {(message, index) => <SideChatMessage message={message} index={index()} />}
                  </For>
                </div>
              </Show>
            </Show>
          </Show>
        </div>

        <div class="border-t border-[#2a3136] bg-[#0d1216]/92 px-4 py-4">
          <div class="mx-auto flex max-w-5xl flex-col gap-3 rounded-2xl border border-[#2a3136] bg-[#13191d] p-3 shadow-[0_16px_38px_rgba(0,0,0,0.22)]">
            <Show when={props.embedded}><small>发送给：{requestedSelectedId() ? selectedTitle() : '尚未选择讨论对象'}</small></Show>
            <textarea
              class="tcode-scrollbar min-h-[86px] w-full resize-none rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-3 text-sm leading-6 text-[#e6edf3] outline-none transition-colors placeholder:text-[#6f7d87] focus:border-[#7bd0ff]/55"
              placeholder={selectedRunning() ? '这个旁支正在运行，完成后可以继续追问...' : '继续问这个旁支...'}
              value={draft()}
              disabled={!requestedSelectedId() || selectedRunning() || sending()}
              onInput={(event) => updateDraft(event.currentTarget.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                  event.preventDefault();
                  void sendSideMessage();
                }
              }}
            />
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="text-xs text-[#8b949e]">
                {selectedRunning() ? '旁支运行中，正在自动同步状态。' : 'Enter 发送，Shift+Enter 换行。'}
              </div>
              <button
                type="button"
                class="inline-flex h-9 items-center justify-center rounded-full border border-[#7b61ff]/28 bg-[#7b61ff]/12 px-4 text-sm font-semibold text-[#d8ceff] transition-colors hover:bg-[#7b61ff]/18 disabled:cursor-not-allowed disabled:opacity-50"
                disabled={!draft().trim() || !requestedSelectedId() || selectedRunning() || sending()}
                onClick={() => void sendSideMessage()}
              >
                {sending() ? '发送中...' : '发送到旁支'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
    }>
      <section class="side-chat-panel" aria-label="侧边聊天" data-testid="side-chat-panel">
        <header class="side-chat-header">
          <div><b>侧边聊天</b><span>{selectedItem()?.agent_name || sessionStore.currentSession()?.agent_name || ''}{selectedRunning() ? ' · 运行中' : ''}</span>
            <button aria-label="注入主会话" title="注入整段侧聊的提问、回复及执行证据" disabled={injecting() || selectedRunning() || sending() || !messages().length} onClick={()=>void injectIntoMain()}>{injecting() ? '注入中…' : '注入主会话'}</button>
          </div>
          <Show when={injectionFeedback()}><small role="status">{injectionFeedback()}</small></Show>
        </header>
        <div ref={chatContainer} class="side-chat-messages tcode-scrollbar" role="log" aria-label="侧聊消息">
          <Show when={error()}><div class="side-chat-error" role="alert">{error()}<Show when={lastFailedText()}><button disabled={selectedRunning() || sending()} onClick={()=>{updateDraft(lastFailedText());void sendSideMessage();}}>重试</button></Show></div></Show>
          <Show when={messages().length} fallback={<div class="side-chat-empty"><b>继续讨论</b><p>有什么想进一步聊聊的？</p></div>}>
            <div ref={setMessageListElement} class="side-chat-transcript">
              <SideChatTranscript messages={messages()} running={selectedRunning()} sessionId={requestedSelectedId()}
                renderTool={entry=><SideToolCall messageId={`side:${requestedSelectedId()}:${entry.messageId}`} tool={entry.tool} index={entry.index}/>}/>
            </div>
          </Show>
          <Show when={selectedRunning()}><p class="side-chat-progress" role="status">{question() ? '等待你的回答' : '正在回复…'}</p></Show>
          <Show when={question()} keyed>{request=><QuestionCard request={request}/>}</Show>
        </div>
        <form class="side-chat-composer" onSubmit={event=>{event.preventDefault();void sendSideMessage();}}>
          <Show when={attachedFiles().length}><ul class="side-chat-attachments" aria-label="侧聊附件">
            <For each={attachedFiles()}>{file=><li><span title={file.path}>{file.name}</span><button type="button" aria-label={`移除附件 ${file.name}`} disabled={sending() || uploading() || selectedRunning()} onClick={()=>updateFiles(attachedFiles().filter(item=>item.id!==file.id))}>×</button></li>}</For>
          </ul></Show>
          <Show when={showUpload() && requestedSelectedId()}><div class="side-chat-upload">
            <FileUpload sessionId={requestedSelectedId()} allowFolders={false} disabled={sending() || selectedRunning()} onUploadingChange={setUploading}
              onUpload={files=>updateFiles([...attachedFiles(), ...files.map(file=>({id:crypto.randomUUID(),name:file.filename,path:file.path,file:file.file,mode:'path' as const,source:'upload' as const,sessionId:file.sessionId}))])}/>
          </div></Show>
          <label for={`side-chat-input-${requestedSelectedId()}`}>侧聊问题</label>
          <textarea id={`side-chat-input-${requestedSelectedId()}`} aria-label="侧聊问题" placeholder="讨论思路、解释结果，或提出追问…" value={draft()} disabled={!parentSessionId() || sending() || selectedRunning()}
            onInput={event=>updateDraft(event.currentTarget.value)} onKeyDown={event=>{if(event.key==='Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();void sendSideMessage();}}}/>
          <div class="side-chat-composer-actions">
            <button type="button" aria-label="添加侧聊附件" title="上传文件到当前侧聊" aria-expanded={showUpload()} disabled={sending() || uploading() || selectedRunning() || !parentSessionId()} onClick={()=>void toggleSideUpload()}
              class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-[#3e484f] bg-[#171c20] text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
            </button>
            <small>{selectedRunning() ? '正在运行 · 可中止当前回复' : 'Enter 发送 · Shift+Enter 换行'}</small>
            <Show when={selectedRunning()} fallback={<button type="submit" disabled={(!draft().trim() && !attachedFiles().length) || uploading() || sending() || !parentSessionId()}
              class="inline-flex shrink-0 items-center justify-center gap-2 rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-3 py-2 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-3.5 w-3.5"><path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z"/></svg>{sending() ? '发送中…' : '发送'}</button>}>
              <button type="button" disabled={stopping()} aria-busy={stopping()} title="中止当前回合及其执行中的工作" onClick={()=>void stopSideChat()}
                class="inline-flex shrink-0 items-center justify-center gap-2 rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-3 py-2 text-xs font-semibold text-[#ffd0cb] disabled:opacity-50">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-3.5 w-3.5"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm5 11H7v-2h10v2z"/></svg>{stopping() ? '正在中止…' : '中止'}</button>
            </Show>
          </div>
        </form>
      </section>
    </Show>
  );
}
