import ResearchActionCard from './ResearchActionCard';
import {researchActionReceipts,researchActionDisplayKey} from '../utils/researchAction';
import {hypothesisMapLinks} from '../api/hypothesisMap';
import {HypothesisEvidenceSummary} from './HypothesisEvidenceMap';
import { publicNarrationText, toolDisplayLabel } from '../utils/userFacingLabels';
import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, untrack, type JSX } from 'solid-js';
import { sessionStore, type Message } from '../stores/session';
import MessageItem, { ToolCallItem } from './MessageItem';
import HumanReviewCard, { reviewReferences } from './HumanReviewCard';
import MarkdownContent from './MarkdownContent';
import { getChatRuntimeStatus, getSession, getSymphonyLocalIssues, hasActiveChatSocket } from '../api/client';
import { collectProcessTools, type PresentedChatTurn } from '../utils/chatPresentation';
import { mergeAndStabilizeVisibleMessages } from '../utils/messageSnapshots';
import { chatPollDelayMs } from '../utils/chatSocketLifecycle';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import BrowserLivePanel from './BrowserLivePanel';
import InteractionPanel from './composer/InteractionPanel';
import ResearchSessionPanel, { type ResearchSessionTab } from './ResearchSessionPanel';
import { ResearchChangesSummary } from './ResearchChangesPanel';
import { researchChangesLinks } from '../api/researchChanges';

interface ChatViewProps {
  turns: PresentedChatTurn[];
  onStartConversation?: () => void;
  onInitializeWorkspace?: () => void;
  activeTab?: 'task' | ResearchSessionTab;
  onResearchTabChange?: (tab: ResearchSessionTab) => void;
  searchFocus?: {sessionId: string; messageId?: string; key: number};
}

export function MainProcessGroup(props: {
  messages: Message[];
  running: boolean;
  hasFinalResult?: boolean;
  preferenceId: string;
  allowSessionActions?: boolean;
  narrationTitle?: string;
  defaultToolsCollapsed?: boolean;
  includeOrphanOutputs?: boolean;
  renderTool?: (entry: ReturnType<typeof collectProcessTools>[number]) => JSX.Element;
}) {
  const commentaryText = (message: Message) => publicNarrationText(message.content,
    props.messages.flatMap(entry => (entry.tool_calls || []).map(tool => tool.tool_name)),
  ).trim().replace(
    /^(?:\*{0,2}(?:第[一二三四五六七八九十\d]+步|步骤\s*[一二三四五六七八九十\d]+|Step\s+\d+)\*{0,2})\s*[：:]\s*/iu,
    '',
  );
  const commentary = createMemo(() => {
    const entries = new Map<string, Message>();
    props.messages.forEach((message) => {
      if (message.role === 'assistant' && String(message.content || '').trim()) {
        entries.set(String(message.id || `${message.created_at || ''}:${message.content}`), message);
      }
    });
    const groups: { message: Message; content: string; count: number }[] = [];
    for (const message of entries.values()) {
      const text = commentaryText(message);
      const paragraphs = text.split(/\n\s*\n/).map(part => part.trim());
      const count = paragraphs.every(part => part === paragraphs[0]) ? paragraphs.length : 1;
      const content = count > 1 ? paragraphs[0] : text;
      const previous = groups[groups.length - 1];
      if (previous?.content === content) previous.count += count;
      else groups.push({ message, content, count });
    }
    return groups;
  });
  const tools = createMemo(() => collectProcessTools(props.messages, props.includeOrphanOutputs));
  // Completion starts a fresh disclosure state; an open live view must not
  // prevent auto-collapse, while reopening the completed view stays possible.
  const narrationKey = () => `commentary:${props.preferenceId}${props.hasFinalResult ? ':result' : ''}`;
  const toolsKey = () => `tools:${props.preferenceId}`;
  const narrationCollapsed = createMemo(() =>
    sessionStore.messageToolsCollapsePreference(narrationKey()) ?? Boolean(props.hasFinalResult)
  );
  const toolsCollapsed = createMemo(() =>
    sessionStore.messageToolsCollapsePreference(toolsKey()) ?? (props.defaultToolsCollapsed ?? true)
  );

  const sectionHeader = (
    title: string,
    collapsed: () => boolean,
    toggle: () => void,
    live = false,
    count?: number,
  ) => (
    <button
      type="button"
      class="process-disclosure flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#182027]"
      aria-expanded={!collapsed()}
      onClick={toggle}
    >
      <span class="flex items-center gap-2 text-xs font-semibold text-[#c7d2da]">
        <svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class={`h-4 w-4 text-[#9faeb7] transition-transform ${collapsed() ? '' : 'rotate-90'}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
        </svg>
        <Show when={live}><span class="h-2 w-2 rounded-full bg-[#38bdf8] animate-pulse" /></Show>
        {`${title}${count !== undefined ? ` · ${count}` : ''}`}
      </span>
      <span class="process-disclosure-hint">{collapsed() ? '展开' : '收起'}</span>
    </button>
  );

  return (
    <div class="space-y-3">
      <Show when={tools().length > 0}>
        <section class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0d1216]">
          {sectionHeader(
            '运行工具',
            toolsCollapsed,
            () => sessionStore.setMessageToolsCollapsed(toolsKey(), !toolsCollapsed()),
            false,
            tools().length,
          )}
          <Show when={!toolsCollapsed()}>
            <div class="process-scroll max-h-[min(36rem,60vh)] overflow-y-auto overscroll-x-contain overscroll-y-auto space-y-2 border-t border-[#2a3136] px-3 py-3"
              role="region" aria-label="工具调用记录" tabIndex={0}>
              <For each={tools()}>
                {(entry) => props.renderTool
                  ? props.renderTool(entry)
                  : <ToolCallItem messageId={entry.messageId} tool={entry.tool} index={entry.index} />}
              </For>
            </div>
          </Show>
        </section>
      </Show>
      <Show when={commentary().length > 0}>
        <section class="overflow-hidden rounded-2xl border border-[#31536a] bg-[#0e1820]">
          {sectionHeader(
            props.narrationTitle || (props.running ? '实时行动说明' : '行动说明'),
            narrationCollapsed,
            () => sessionStore.setMessageToolsCollapsed(narrationKey(), !narrationCollapsed()),
            props.running,
          )}
          <Show when={!narrationCollapsed()}>
            <div class="process-scroll max-h-[min(25rem,60vh)] overflow-y-auto overscroll-x-contain overscroll-y-auto divide-y divide-[#31536a]/70 border-t border-[#31536a]/80 px-4"
              role="region" aria-label="旁白内容" tabIndex={0}>
              <For each={commentary()}>
                {(entry) => (
                  <div class="flex min-w-0 items-start gap-2 py-3">
                    <MarkdownContent
                      content={entry.content}
                      role="assistant"
                      allowSessionActions={props.allowSessionActions}
                      renderKey={`commentary:${entry.message.id}`}
                      class="min-w-0 flex-1 text-sm text-[#d7e5ec]"
                    />
                    <Show when={entry.count > 1}>
                      <span class="shrink-0 rounded-full border border-current/20 px-2 py-0.5 text-xs opacity-70"
                        aria-label={`重复 ${entry.count} 次`}>{`×${entry.count}`}</span>
                    </Show>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </section>
      </Show>
    </div>
  );
}

export function MainChatTurn(props: { turn: PresentedChatTurn; latestAssistantMessageId: string }) {
  const reviewCards = createMemo(() => reviewReferences(collectProcessTools([
    ...props.turn.processMessages, ...(props.turn.result ? [props.turn.result] : []),
  ]).map(entry => entry.tool)));
  const actionCards=createMemo(()=>{
    const current=researchActionReceipts(collectProcessTools([...props.turn.processMessages,...(props.turn.result?[props.turn.result]:[])]).map(entry=>entry.tool));
    const latest=new Map(researchActionReceipts(collectProcessTools(sessionStore.messages()).map(entry=>entry.tool)).map(action=>[researchActionDisplayKey(action),action]));
    return current.map(action=>latest.get(researchActionDisplayKey(action))||action);
  });
  const evidenceLinks=createMemo(()=>hypothesisMapLinks(collectProcessTools([...props.turn.processMessages,...(props.turn.result?[props.turn.result]:[])]).map(entry=>entry.tool)));
  const changeLinks=createMemo(()=>researchChangesLinks(collectProcessTools([...props.turn.processMessages,...(props.turn.result?[props.turn.result]:[])]).map(entry=>entry.tool)));
  return <div class="flex flex-col gap-4" data-search-message-ids={[props.turn.user?.id, props.turn.result?.id, ...props.turn.processMessages.map(message=>message.id)].filter(Boolean).join(' ')}>
    <Show when={props.turn.user?.presentation !== 'async_hidden_prompt' ? props.turn.user : undefined}>
      {message => <div class="main-chat-row main-chat-row--user">
        <MessageItem message={message()} latestAssistantMessageId={props.latestAssistantMessageId}/>
      </div>}
    </Show>
    <Show when={props.turn.processMessages.some(message => message.content?.trim() || message.tool_calls?.length || message.content_blocks?.length)}>
      <div class="main-chat-row main-chat-row--agent main-chat-row--process">
        <MainProcessGroup messages={props.turn.processMessages} running={props.turn.running}
          hasFinalResult={Boolean(props.turn.result)} preferenceId={props.turn.key}/>
      </div>
    </Show>
    <Show when={props.turn.result}>
      {message => <div class="main-chat-row main-chat-row--agent">
        <MessageItem message={message()} latestAssistantMessageId={props.latestAssistantMessageId}/>
      </div>}
    </Show>
    <For each={reviewCards()}>{reference => <div class="main-chat-row main-chat-row--agent"><HumanReviewCard reference={reference} sessionId={sessionStore.currentSessionId() || ''}/></div>}</For>
    <Show when={!props.turn.running}><For each={actionCards()}>{action=><div class="main-chat-row main-chat-row--agent"><ResearchActionCard action={action}/></div>}</For></Show>
    <For each={evidenceLinks()}>{link=><div class="main-chat-row main-chat-row--agent"><HypothesisEvidenceSummary link={link}/></div>}</For>
    <For each={changeLinks()}>{link=><div class="main-chat-row main-chat-row--agent"><ResearchChangesSummary link={link} /></div>}</For>
  </div>;
}

export default function ChatView(props: ChatViewProps) {
  let chatContainer: HTMLDivElement | undefined;
  let contentResizeObserver: ResizeObserver | undefined;
  let pendingScrollFrame: number | undefined;
  const [messageListElement, setMessageListElement] = createSignal<HTMLDivElement>();
  const activeTab = () => props.activeTab || 'task';
  const currentSession = createMemo(() => sessionStore.currentSession());
  const researchId = createMemo(() => String(
    currentSession()?.research_id || currentSession()?.metadata?.research_id || '',
  ));
  const pageVisible = createPageVisibleSignal();
  const presentedTurns = () => props.turns;
  const latestAssistantMessageId = createMemo(() => {
    const messages = sessionStore.messages();
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'assistant') {
        return messages[index].id;
      }
    }
    return '';
  });
  const agentDisplayLabel = (agentName: string) => {
    const labels: Record<string, string> = {
      build: '构建模式',
      manager: '团队编排',
      paper_auto: '文献自动协作',
      protein_orchestrator: '蛋白设计团队',
    };
    return labels[agentName] || agentName;
  };

  // 离开底部即暂停跟随，避免小幅向上滚动后又被内容更新拉回。
  const [userScrolledUp, setUserScrolledUp] = createSignal(false);
  let appliedSearchKey: number | undefined;
  const searchTargetReady = createMemo(() => {
    const id = props.searchFocus?.messageId;
    return Boolean(id && presentedTurns().some(turn => turn.user?.id === id || turn.result?.id === id || turn.processMessages.some(message=>message.id===id)));
  });
  createEffect(() => {
    const request = props.searchFocus;
    const list = messageListElement();
    if (!request || !list || activeTab() !== 'task' || request.key === appliedSearchKey || request.sessionId !== sessionStore.currentSessionId()) return;
    if (request.messageId && !searchTargetReady()) return;
    const timer = window.setTimeout(() => {
      if (!request.messageId) {
        jumpToLatest();
        appliedSearchKey = request.key;
        return;
      }
      const target = [...list.querySelectorAll<HTMLElement>('[data-search-message-ids]')]
        .find(element => element.dataset.searchMessageIds?.split(' ').includes(request.messageId!));
      if (target) {
        setUserScrolledUp(true);
        target.scrollIntoView({block: 'start'});
        target.tabIndex = -1; target.focus({preventScroll: true});
        appliedSearchKey = request.key;
      }
    }, 60);
    onCleanup(() => window.clearTimeout(timer));
  });
  let scrollListenerAttached = false;

  const handleChatScroll = () => {
    if (!chatContainer) return;
    const distFromBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
    setUserScrolledUp(distFromBottom > 4);
  };

  const handleChatWheel = (event: WheelEvent) => {
    // Wheel intent arrives before scroll/ResizeObserver: cancel pending bottom
    // following immediately so freshly loaded content cannot pull history back.
    if (event.deltaY < 0) setUserScrolledUp(true);
  };

  const handleContentInteraction = (event: PointerEvent | KeyboardEvent) => {
    // Local diagram disclosures/forms can resize the last message. Reading or
    // editing them is explicit intent to keep position, even at the chat bottom.
    if (event.target instanceof Element && event.target.closest('.intelligent-canvas')) {
      setUserScrolledUp(true);
    }
  };

  const attachScrollListener = () => {
    if (scrollListenerAttached || !chatContainer) return;
    scrollListenerAttached = true;
    chatContainer.addEventListener('scroll', handleChatScroll, { passive: true });
  };

  const scrollToBottom = () => {
    if (chatContainer) {
      chatContainer.scrollTop = chatContainer.scrollHeight;
    }
  };

  const jumpToLatest = () => {
    scrollToBottom();
    setUserScrolledUp(false);
  };
  const JumpToLatestButton = () => (
    <button
      type="button"
      class="inline-flex min-h-11 shrink-0 cursor-pointer items-center gap-2 whitespace-nowrap rounded-full border border-[var(--tcode-border-strong)] bg-[var(--tcode-surface)] py-1.5 pl-2 pr-4 text-[13px] font-semibold text-[var(--tcode-text)] shadow-[0_3px_12px_#0002] hover:border-[var(--tcode-primary)] hover:bg-[var(--tcode-surface-soft)] active:bg-[var(--tcode-surface-muted)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--tcode-primary)] motion-safe:transition-colors"
      onClick={jumpToLatest}
    >
      <span aria-hidden="true" class="grid size-7 shrink-0 place-items-center rounded-full bg-[var(--tcode-surface-muted)] text-[var(--tcode-primary)]">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="size-4">
          <path d="M8 2.5V10m-3-3 3 3 3-3M4 13h8" />
        </svg>
      </span>
      跳转到最新回复
    </button>
  );

  // 只有在用户没有向上翻看时才自动滚到底
  const scrollToBottomIfAtBottom = () => {
    // 研究页复用外层滚动容器，但不属于聊天时间线。后台消息轮询和
    // ResizeObserver 不应改变用户正在阅读或填写的研究页位置。
    if (activeTab() !== 'task') return;
    if (!userScrolledUp()) {
      scrollToBottom();
    }
  };

  const scheduleScrollAfterContentResize = () => {
    if (pendingScrollFrame !== undefined) {
      window.cancelAnimationFrame(pendingScrollFrame);
    }
    pendingScrollFrame = window.requestAnimationFrame(() => {
      pendingScrollFrame = undefined;
      scrollToBottomIfAtBottom();
    });
  };

  const isActiveAsyncIssueState = (state?: string) => {
    const normalized = String(state || '').trim().toLowerCase();
    return (
      normalized === 'in progress'
      || normalized === 'running'
      || normalized === 'processing'
      || normalized === 'queued'
      || normalized === 'pending'
      || normalized === 'review_pending'
      || normalized === 'open'
    );
  };

  // 切换会话时重置滚动状态并直接滚到底
  createEffect(on(() => sessionStore.currentSessionId(), () => {
    setUserScrolledUp(false);
    const timer = window.setTimeout(scrollToBottomIfAtBottom, 30);
    onCleanup(() => window.clearTimeout(timer));
  }));

  // 新消息到来时，只在用户没有向上翻看历史的情况下才自动滚到底
  createEffect(on(() => sessionStore.messages().length, () => {
    setTimeout(scrollToBottomIfAtBottom, 10);
  }));

  // 挂载 scroll 监听（等容器 ref 就绪后）
  createEffect(() => {
    if (chatContainer) attachScrollListener();
  });

  // 流式内容会改变最后一条消息的高度，但不会改变消息数量。
  // 使用 ResizeObserver 跟随底部；用户向上阅读时则完全不干预当前位置。
  createEffect(() => {
    const list = messageListElement();
    if (!list || typeof ResizeObserver === 'undefined') return;
    contentResizeObserver?.disconnect();
    contentResizeObserver = new ResizeObserver(scheduleScrollAfterContentResize);
    contentResizeObserver.observe(list);
  });

  onCleanup(() => {
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
    const sessionId = sessionStore.currentSessionId();
    const visible = pageVisible();
    if (!sessionId || !visible) return;

    const existsInStore = untrack(() =>
      sessionStore.sessions().some((s) => s.session_id === sessionId)
    );
    if (!existsInStore) return;

    let disposed = false;
    let timer: number | undefined;
    let consecutiveFailures = 0;
    let polling = false;

    const syncRuntimeStatus = async (): Promise<boolean> => {
      try {
        const [runtime, localIssuesData] = await Promise.all([
          getChatRuntimeStatus(sessionId),
          getSymphonyLocalIssues(200, sessionId),
        ]);
        if (!runtime) return false;
        // A new user turn may have started while this background poll was in
        // flight. Its live socket owns foreground state until it finishes.
        if (hasActiveChatSocket(sessionId)) return true;

        const hasActiveAsyncIssue = (Array.isArray(localIssuesData?.issues) ? localIssuesData.issues : [])
          .some((issue) => isActiveAsyncIssueState(issue?.state));

        const waitingNextStep =
          runtime.pending_guidance_count > 0 ||
          Boolean(runtime.guidance_worker_active);
        const interruptGuidance = Boolean(runtime.pending_guidance_interrupt);
        // The status API separates chat ownership from detached descendants.
        const running = Boolean(runtime.running);

        sessionStore.setSessionIsRunning(sessionId, running);
        if (sessionStore.getSessionRuntimeState(sessionId).questionRequest) {
          sessionStore.setSessionStatus(sessionId, '需要回答问题');
        } else if (!running && waitingNextStep) {
          sessionStore.setSessionStatus(
            sessionId,
            interruptGuidance ? '待接入指导（单步）' : '待执行追加指导（单步）',
          );
        } else if (hasActiveAsyncIssue) {
          sessionStore.setSessionStatus(sessionId, '异步运行');
        } else if (!running) {
          sessionStore.setSessionStatus(sessionId, '');
        } else {
          if (runtime.workflow_background_active || runtime.child_session_active) {
            sessionStore.setSessionStatus(sessionId, '后台编排继续执行中...');
          } else {
            sessionStore.setSessionStatus(sessionId, '后台继续处理中...');
          }
        }
        return true;
      } catch (e) {
        console.error('Runtime status sync error:', e);
        return false;
      }
    };

    const scheduleNextPoll = () => {
      if (disposed || !pageVisible()) return;
      const delay = chatPollDelayMs(sessionStore.isRunning(), consecutiveFailures);
      timer = window.setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      if (disposed || polling || !pageVisible()) return;
      if (sessionStore.currentSessionId() !== sessionId) return;
      polling = true;
      let pollSucceeded = true;
      try {
        const hasLiveSocket = hasActiveChatSocket(sessionId);
        if (hasLiveSocket) {
          // A live socket does not guarantee receipt of an earlier question.
          pollSucceeded = Boolean(await getChatRuntimeStatus(sessionId));
        }
        if (!hasLiveSocket) {
          const runtimeSynced = await syncRuntimeStatus();
          const latest = await getSession(sessionId);
          const snapshotSynced = Boolean(latest && Array.isArray(latest.messages));
          pollSucceeded = runtimeSynced || snapshotSynced;

          if (snapshotSynced && latest) {
            const currentMessages = sessionStore.messages();
            const stableNext = mergeAndStabilizeVisibleMessages(currentMessages, latest.messages);
            if (stableNext !== currentMessages) sessionStore.setMessages(stableNext);
          }
        }
      } catch (e) {
        console.error('Session sync error:', e);
        pollSucceeded = false;
      } finally {
        consecutiveFailures = pollSucceeded ? 0 : consecutiveFailures + 1;
        polling = false;
        scheduleNextPoll();
      }
    };

    const handleOnline = () => {
      consecutiveFailures = 0;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = undefined;
      void poll();
    };

    // Refresh immediately so page reload or a newly visible tab recovers state.
    void poll();
    window.addEventListener('online', handleOnline);

    onCleanup(() => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      window.removeEventListener('online', handleOnline);
    });
  });

  return (
    <div
      ref={chatContainer}
      data-chat-timeline
      class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-5 [overflow-anchor:none] md:px-6"
      onWheel={handleChatWheel}
      onPointerDown={handleContentInteraction}
      onKeyDown={handleContentInteraction}
    >
      <Show
        when={activeTab() === 'task'}
        fallback={
          <ResearchSessionPanel
            tab={activeTab() as ResearchSessionTab}
            researchId={researchId()}
            workspaceId={String(currentSession()?.workspace_id || currentSession()?.workdir_name || '')}
            onNavigateTab={props.onResearchTabChange}
          />
        }
      >
      <Show
        when={sessionStore.messages().length > 0 || sessionStore.questionRequest() || sessionStore.questionHistory().some(item => item.session_id === sessionStore.currentSessionId())}
        fallback={
          <div class="studio-welcome flex min-h-full items-center justify-center py-10">
            <div class="w-full max-w-4xl">
              <div class="text-center">
                <div class="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-[28px] border border-[#3e484f] bg-[#171c20] shadow-[0_0_28px_rgba(56,189,248,0.12)]">
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-10 w-10 text-[#8ed5ff]">
                    <path d="M13 3a9 9 0 1 0 8.95 10H20a7 7 0 1 1-2.05-4.95l-3.95 3.95H21V5l-3.64 3.64A8.96 8.96 0 0 0 13 3Z" />
                  </svg>
                </div>
                <h2 class="text-2xl font-semibold tracking-tight text-[#f7fafc]">从一个问题，推进一项研究</h2>
                <p class="mx-auto mt-3 max-w-2xl text-sm leading-6 text-[#9faeb7]">
                  {sessionStore.currentSessionId()
                    ? '当前会话已经就绪，可以直接在下方输入新指令，或者把文件拖进输入区作为上下文。'
                    : '先创建一个任务，再用自然语言连接文献、文件与远程计算。'}
                </p>
              </div>

              <div class="mt-8 grid gap-4 md:grid-cols-2">
                <button
                  type="button"
                  class="group rounded-2xl border border-[#3e484f] bg-[#171c20]/86 p-6 text-left transition-colors hover:border-[#7bd0ff]/42 hover:bg-[#18232a]"
                  onClick={() => {
                    if (props.onStartConversation) {
                      props.onStartConversation();
                      return;
                    }
                    sessionStore.setStatus('在下方输入需求，按回车发送。');
                  }}
                >
                  <div class="flex items-center justify-between">
                    <span class="inline-flex h-11 w-11 items-center justify-center rounded-xl bg-[#38bdf8]/12 text-[#8ed5ff]">
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-6 w-6">
                        <path d="M4 4h16v10H5.17L4 15.17V4Zm0 14 4-4h12v4H4Z" />
                      </svg>
                    </span>
                    <span class="rounded-full bg-[#38bdf8]/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#8ed5ff]">推荐</span>
                  </div>
                  <div class="mt-5 text-lg font-semibold text-[#f5f7fa]">开启新对话</div>
                  <p class="mt-2 text-sm leading-6 text-[#9faeb7]">描述你的问题和预期结果，或附上文献与文件作为研究上下文。</p>
                  <div class="mt-6 flex items-center gap-2 text-sm font-semibold text-[#8ed5ff] transition-all group-hover:gap-3">
                    <span>立即开始</span>
                    <span>→</span>
                  </div>
                </button>

                <button
                  type="button"
                  class="group rounded-2xl border border-[#3e484f] bg-[#171c20]/86 p-6 text-left transition-colors hover:border-[#f1a02b]/42 hover:bg-[#241e17]"
                  onClick={() => {
                    if (props.onInitializeWorkspace) {
                      props.onInitializeWorkspace();
                      return;
                    }
                    sessionStore.setStatus('可以先在左侧创建会话并选择工作目录。');
                  }}
                >
                  <div class="flex items-center justify-between">
                    <span class="inline-flex h-11 w-11 items-center justify-center rounded-xl bg-[#f1a02b]/12 text-[#ffc176]">
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-6 w-6">
                        <path d="M12 2 1 7l11 5 9-4.09V17h2V7L12 2Zm0 13L5 11.82V17l7 3 7-3v-5.18L12 15Z" />
                      </svg>
                    </span>
                  </div>
                  <div class="mt-5 text-lg font-semibold text-[#f5f7fa]">准备研究空间</div>
                  <p class="mt-2 text-sm leading-6 text-[#9faeb7]">选择工作目录，整理文件与工具，让研究过程和成果留在同一个空间。</p>
                  <div class="mt-6 flex items-center gap-2 text-sm font-semibold text-[#ffc176] transition-all group-hover:gap-3">
                    <span>查看准备项</span>
                    <span>→</span>
                  </div>
                </button>
              </div>

              <div class="mt-8 flex flex-wrap justify-center gap-3 text-xs text-[#9faeb7]">
                <div class="rounded-full border border-[#3e484f] bg-[#171c20] px-4 py-2">
                  代理 {agentDisplayLabel(sessionStore.selectedAgentName())}
                </div>
                <Show when={sessionStore.currentSession()?.workdir_name}>
                  <div class="rounded-full border border-[#3e484f] bg-[#171c20] px-4 py-2">
                    工作目录 {sessionStore.currentSession()?.workdir_name}
                  </div>
                </Show>
              </div>
            </div>
          </div>
        }
      >
        <div ref={setMessageListElement} class="main-chat-transcript mx-auto flex w-full max-w-6xl flex-col gap-4 py-2">
          <For each={presentedTurns().map((turn) => turn.key)}>
            {(key) => {
              // Keep the container mounted while streamed message objects change.
              const turn = () => presentedTurns().find((item) => item.key === key)!;
              return <MainChatTurn turn={turn()} latestAssistantMessageId={latestAssistantMessageId()}/>;
            }}
          </For>
          <InteractionPanel mode="questions" />
        </div>
      </Show>

      <Show when={userScrolledUp() && (sessionStore.questionRequest() || (!sessionStore.isRunning() && presentedTurns().length > 0))}>
        <div class="pointer-events-none sticky bottom-0 z-10 flex justify-center py-3">
          <div class="pointer-events-auto">
            <Show when={sessionStore.questionRequest()} fallback={<JumpToLatestButton />}>
              <button type="button" class="rounded-full border border-[#f1a02b]/30 bg-[#211a0f] px-4 py-2 text-xs text-[#ffe1b3]" onClick={jumpToLatest}>
                有问题等待回答 · 查看
              </button>
            </Show>
          </div>
        </div>
      </Show>
      <Show when={sessionStore.isRunning() && !sessionStore.questionRequest()}>
        <div class="sticky bottom-0 z-10 pb-3 pt-4">
          <div class="mx-auto flex max-w-6xl gap-3 rounded-2xl border border-[#38b46a]/28 bg-[#102017]/92 px-4 py-3 shadow-[0_18px_40px_rgba(0,0,0,0.26)] backdrop-blur">
            <div class="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-[#38b46a]/14">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5 text-[#7ee787]">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
              </svg>
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#9ee6bb]">编排器运行中</span>
                <Show when={sessionStore.runningTool()}>
                  {(running) => (
                    <div class="inline-flex items-center gap-2 rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 px-2 py-1 text-[11px] font-semibold text-[#8ed5ff]">
                      <span class="inline-block h-1.5 w-1.5 rounded-full bg-current" />
                      {toolDisplayLabel(running().tool_name, running().tool_args)}
                    </div>
                  )}
                </Show>
                <Show when={userScrolledUp()}>
                  <div class="ml-auto"><JumpToLatestButton /></div>
                </Show>
              </div>
              <div class="mt-1 text-sm text-[#d8f3df] typing-cursor">
                {sessionStore.status() || '思考中...'}
              </div>
            </div>
          </div>
        </div>
      </Show>
      <BrowserLivePanel
        parentSessionId={sessionStore.currentSessionId() || ''}
        running={sessionStore.isRunning()}
        workspaceId={String(
          sessionStore.currentSession()?.workspace_id
          || sessionStore.currentSession()?.workdir_name
          || ''
        )}
      />
      </Show>
    </div>
  );
}
