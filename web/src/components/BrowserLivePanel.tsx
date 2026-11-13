import { Show, batch, createEffect, createMemo, createSignal, onCleanup, untrack } from 'solid-js';
import { browserLiveStore } from '../stores/browserLive';
import { desktop, type BrowserCommand, type BrowserState } from '../desktop';
import BrowserToolbar from '../desktop-app/BrowserToolbar';
import { Portal } from 'solid-js/web';

type BrowserLivePanelProps = {
  parentSessionId: string;
  workspaceId: string;
  running: boolean;
};

const terminalBrowserStatuses = new Set(['closed', 'idle_timeout', 'stopped']);

const statusLabel = (status: string, connected: boolean) => {
  const normalizedStatus = String(status || '').trim().toLowerCase();
  if (!connected && !terminalBrowserStatuses.has(normalizedStatus)) return '正在连接';
  switch (normalizedStatus) {
    case 'starting': return '正在启动';
    case 'running': return '正在操作';
    case 'paused': return '等待确认';
    case 'ready': return '实时';
    case 'closed': return '已关闭';
    case 'idle_timeout': return '空闲结束';
    case 'stopped': return '已停止';
    case 'error': return '异常';
    default: return status || '等待画面';
  }
};

export default function BrowserLivePanel(props: BrowserLivePanelProps) {
  const [frameUrl, setFrameUrl] = createSignal('');
  const [connected, setConnected] = createSignal(false);
  const [desktopBrowser, setDesktopBrowser] = createSignal(false);
  const [expanded, setExpanded] = createSignal(false);
  const [sidePanelOpen, setSidePanelOpen] = createSignal(false);
  const [question, setQuestion] = createSignal('');
  const [manualSessions, setManualSessions] = createSignal<string[]>([]);
  const manualOpen = () => manualSessions().length > 0;
  const [selectedBrowser, setSelectedBrowser] = createSignal('');
  const [takenOverSession, setTakenOverSession] = createSignal('');
  const manualSession = () => manualSessions().includes(selectedBrowser()) ? selectedBrowser() : manualSessions()[manualSessions().length - 1] || '';
  const notifyPanel = (open: boolean, session: string) => window.dispatchEvent(new CustomEvent('tcode:browser-panel', { detail: {open, session} }));
  const manualItem = () => ({ key: 'manual', parentSessionId: props.parentSessionId, browserSessionId: manualSession(), workspaceId: props.workspaceId, agentSessionId: '', status: 'ready', action: '', title: '', url: '', frameSeq: 0, hidden: true, error: '', updatedAt: 0 });
  const [browserState, setBrowserState] = createSignal<BrowserState>({ url: '', title: '', loading: false, canGoBack: false, canGoForward: false, zoom: 1 });
  const [nativeHost, setNativeHost] = createSignal<HTMLDivElement>();
  const [connectionError, setConnectionError] = createSignal('');
  const [controlMode, setControlMode] = createSignal<'agent' | 'human' | 'paused'>('agent');
  const [pendingConfirmation, setPendingConfirmation] = createSignal<{ action?: string; summary?: string } | null>(null);
  let liveSocket: WebSocket | null = null;
  const active = createMemo(() => {
    const item = props.running || takenOverSession() ? browserLiveStore.activeForParent(props.parentSessionId) : null;
    if (item && !terminalBrowserStatuses.has(String(item.status || '').trim().toLowerCase())) return item;
    return manualOpen() ? manualItem() : null;
  });
  const displayingManual = createMemo(() => manualOpen() && (manualSessions().includes(selectedBrowser()) || !takenOverSession()));
  const panelItem = createMemo(() => displayingManual() ? manualItem() : active());
  const panelHuman = createMemo(() => displayingManual() || (!!takenOverSession() && controlMode() === 'human'));
  const nativePanel = createMemo(() => !!desktop?.browserPanel && (displayingManual() || desktopBrowser()));
  const effectiveWorkspaceId = createMemo(() => active()?.workspaceId || props.workspaceId);
  const nativeSession = createMemo(() => panelItem()?.browserSessionId || '');
  createEffect(() => {
    nativeSession();
    setBrowserState({ url: '', title: '', loading: false, canGoBack: false, canGoForward: false, zoom: 1 });
  });
  let previousParent = props.parentSessionId;
  createEffect(() => {
    const parent = props.parentSessionId;
    if (parent === previousParent) return;
    previousParent = parent;
    const task = untrack(takenOverSession);
    for (const session of untrack(manualSessions)) {
      void desktop?.browserPanel?.(session, {action:'close'}).catch(() => {});
      notifyPanel(false, session);
    }
    if (task) void desktop?.browserControl?.(task, 'release').catch(() => {});
    batch(() => { setManualSessions([]); setSelectedBrowser(''); setTakenOverSession(''); setControlMode('agent'); setSidePanelOpen(false); });
  });
  onCleanup(() => {
    for (const session of manualSessions()) void desktop?.browserPanel?.(session, {action:'close'}).catch(() => {});
    if (takenOverSession()) void desktop?.browserControl?.(takenOverSession(), 'release').catch(() => {});
  });
  const connectionKey = createMemo(() => {
    const item = active();
    const workspaceId = effectiveWorkspaceId();
    if (!item || !workspaceId) return '';
    const status = String(item.status || '').trim().toLowerCase();
    const lifecycle = terminalBrowserStatuses.has(status) ? 'terminal' : 'live';
    return `${item.key}:${workspaceId}:${lifecycle}`;
  });

  const replaceFrame = (blob: Blob) => {
    const next = URL.createObjectURL(blob);
    setFrameUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return next;
    });
  };

  const clearFrame = () => {
    setFrameUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return '';
    });
  };

  const sendControl = (payload: Record<string, unknown>) => {
    if (liveSocket?.readyState !== WebSocket.OPEN) return;
    liveSocket.send(JSON.stringify(payload));
  };

  const takeControl = async () => {
    const item = active();
    if (item && takenOverSession() === item.browserSessionId && controlMode() === 'human') {
      setSidePanelOpen(true);
      setSelectedBrowser(item.browserSessionId); notifyPanel(true, item.browserSessionId);
      return;
    }
    try {
      setConnectionError('');
      if (item && desktop?.browserControl) {
        const result = await desktop.browserControl(item.browserSessionId, 'takeover');
        batch(() => { setDesktopBrowser(true); setTakenOverSession(item.browserSessionId); setControlMode(result.control_mode); setSidePanelOpen(true); });
        browserLiveStore.setHidden(item.key, true);
        setSelectedBrowser(item.browserSessionId); notifyPanel(true, item.browserSessionId);
        return;
      }
      sendControl({ action: 'set_control', mode: 'human' });
      setSidePanelOpen(true);
    } catch {
      setConnectionError('浏览器正在完成当前操作，请稍后重试接管');
    }
  };

  const releaseControl = async (session?: string) => {
    const item = session ? active() : panelItem();
    if (!session && displayingManual()) { await closeManual(manualSession()); return; }
    try {
      if (nativePanel() && item && desktop?.browserControl) {
        const result = await desktop.browserControl(item.browserSessionId, 'release');
        setControlMode(result.control_mode);
      } else {
        sendControl({ action: 'set_control', mode: 'agent' });
        setControlMode('agent');
      }
      batch(() => {
        setTakenOverSession(''); setSelectedBrowser(manualSession()); setControlMode('agent'); setSidePanelOpen(manualOpen());
      });
      if (item) notifyPanel(false, item.browserSessionId);
    } catch {
      setConnectionError('暂时无法交还控制，请稍后重试');
    }
  };
  const closeManual = async (session: string) => {
    if (!manualSessions().includes(session)) return;
    setManualSessions(previous => previous.filter(item => item !== session));
    if (selectedBrowser() === session) setSelectedBrowser(manualSession() || takenOverSession());
    setSidePanelOpen(manualOpen() || !!takenOverSession());
    await desktop?.browserPanel?.(session, {action:'close'}).catch(() => {});
    notifyPanel(false, session);
  };

  const panelCommand = async (command: BrowserCommand) => {
    const item = panelItem();
    if (!item || !desktop?.browserPanel) return;
    if (['navigate', 'back', 'forward', 'reload', 'clear-data'].includes(command.action)) {
      setBrowserState(previous => ({ ...previous, loading: true, error: '' }));
    }
    try {
      const state = await desktop.browserPanel(item.browserSessionId, command);
      if (nativeSession() !== item.browserSessionId) return;
      const menuAction = (state as BrowserState & { menuAction?: string }).menuAction;
      if (menuAction) window.dispatchEvent(new CustomEvent('tcode:browser-menu-action', { detail: menuAction }));
      setBrowserState(previous => JSON.stringify(previous) === JSON.stringify(state) ? previous : state);
      window.dispatchEvent(new CustomEvent('tcode:browser-title', { detail: {session:item.browserSessionId, title:state.title || state.url || '浏览器'} }));
      setConnectionError(state.error ? '页面没有打开，请检查链接或网络后重试' : '');
    } catch { if (nativeSession() === item.browserSessionId) setConnectionError('无法打开该页面，请检查网址或连接后重试'); }
  };

  createEffect(() => {
    const openBrowser = async (url = '') => {
      const browserPanel = desktop?.browserPanel;
      if (!browserPanel) return;
      try {
        const session = `manual-browser-${crypto.randomUUID()}`;
        await desktop?.browserControl?.(session, 'takeover');
        batch(() => { setManualSessions(previous => [...previous, session]); setSelectedBrowser(session); setDesktopBrowser(true); setSidePanelOpen(true); });
        notifyPanel(true, session);
        if (url) {
          setBrowserState(previous => ({ ...previous, loading: true, error: '' }));
          void browserPanel(session, { action: 'navigate', url }).catch(() => setConnectionError('无法打开新页面，请检查链接或连接后重试'));
        }
      } catch { setConnectionError('暂时无法打开手动浏览器，请重试'); }
    };
    const openBrowserEvent = () => { void openBrowser(); };
    window.addEventListener('tcode:open-browser', openBrowserEvent);
    const stopNewTabListener = desktop?.onBrowserNewTab?.(url => { void openBrowser(url); });
    const closeBrowser = (event: Event) => {
      const session = (event as CustomEvent<string>).detail;
      if (session && manualSessions().includes(session)) void closeManual(session);
      else if (!session || session === takenOverSession()) void releaseControl(session || undefined);
    };
    const selectBrowser = (event: Event) => {
      const session = (event as CustomEvent<string>).detail;
      if (manualSessions().includes(session) || session === takenOverSession()) setSelectedBrowser(session);
    };
    window.addEventListener('tcode:select-browser', selectBrowser);
    onCleanup(() => window.removeEventListener('tcode:select-browser', selectBrowser));
    window.addEventListener('tcode:close-browser', closeBrowser);
    onCleanup(() => window.removeEventListener('tcode:close-browser', closeBrowser));
    onCleanup(() => { window.removeEventListener('tcode:open-browser', openBrowserEvent); stopNewTabListener?.(); });
  });

  createEffect(() => {
    const session = takenOverSession();
    const item = active();
    if (!session || (item?.key !== 'manual' && item?.browserSessionId === session)) return;
    void desktop?.browserControl?.(session, 'release').catch(() => {});
    batch(() => { setTakenOverSession(''); setSelectedBrowser(manualSession()); setControlMode('agent'); setSidePanelOpen(manualOpen()); });
    notifyPanel(false, session);
  });

  const sendHumanInput = (input: { action: 'click' | 'scroll' | 'key' | 'text'; x?: number; y?: number; deltaX?: number; deltaY?: number; key?: string; text?: string }) => {
    const item = active();
    if (!item || controlMode() !== 'human') return;
    if (desktopBrowser() && desktop?.browserInput) {
      void desktop.browserInput(item.browserSessionId, input).then(async result => {
        if (result.screenshot) replaceFrame(await (await fetch(`data:image/jpeg;base64,${result.screenshot}`)).blob());
      }).catch(() => setConnectionError('浏览器操作未送达，请重试'));
      return;
    }
    const action = input.action === 'click' ? 'click_at' : input.action === 'key' ? 'key_down' : input.action;
    const { action: _inputAction, ...details } = input;
    sendControl({ ...details, action, delta_x: input.deltaX, delta_y: input.deltaY });
  };

  createEffect(() => {
    const session = nativeSession();
    const host = nativeHost();
    if (!session || !host || !sidePanelOpen() || !panelHuman() || !nativePanel() || !desktop?.browserPanel) return;
    const resize = () => {
      const rect = host.getBoundingClientRect();
      if (!host.isConnected || !host.getClientRects().length || rect.width < 1 || rect.height < 1) {
        void desktop?.browserPanel?.(session, { action: 'detach' }).catch(() => {});
        return;
      }
      void panelCommand({ action: 'attach', bounds: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } });
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    const visibilityObserver = new MutationObserver(resize);
    for (let ancestor: HTMLElement | null = host; ancestor; ancestor = ancestor.parentElement) {
      visibilityObserver.observe(ancestor, { attributes: true, attributeFilter: ['hidden', 'style', 'class', 'data-focus'] });
    }
    untrack(resize);
    window.addEventListener('resize', resize);
    let polling = false;
    const stateTimer = window.setInterval(() => {
      if (polling || document.hidden) return;
      polling = true;
      void panelCommand({ action: 'state' }).finally(() => { polling = false; });
    }, 250);
    const boundsTimer = window.setInterval(resize, 1000);
    onCleanup(() => {
      observer.disconnect(); visibilityObserver.disconnect(); window.clearInterval(stateTimer); window.clearInterval(boundsTimer);
      window.removeEventListener('resize', resize);
      void desktop?.browserPanel?.(session, { action: 'detach' }).catch(() => {});
    });
  });

  const framePoint = (event: MouseEvent | PointerEvent) => {
    const image = event.currentTarget as HTMLImageElement;
    const rect = image.getBoundingClientRect();
    const scale = Math.min(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    return {
      x: Math.max(0, Math.min(image.naturalWidth, (event.clientX - rect.left - (rect.width - width) / 2) / scale)),
      y: Math.max(0, Math.min(image.naturalHeight, (event.clientY - rect.top - (rect.height - height) / 2) / scale)),
    };
  };

  createEffect(() => {
    const key = connectionKey();
    // Only the stable connection key should restart this effect. Browser state
    // updates replace the active session object on every frame; tracking that
    // object here would close the socket after the JSON metadata arrives and
    // before the following binary JPEG frame can be delivered.
    const item = untrack(active);
    const workspaceId = untrack(effectiveWorkspaceId);
    if (!key || !item || !workspaceId || item.key === 'manual') return;

    clearFrame();
    setConnected(false);
    setControlMode('agent');
    setSidePanelOpen(untrack(manualOpen));
    setConnectionError('');
    if (terminalBrowserStatuses.has(String(item.status || '').trim().toLowerCase())) {
      return;
    }

    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let reconnectAttempt = 0;

    const connect = () => {
      if (disposed) return;
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const query = new URLSearchParams({
        browser_session_id: item.browserSessionId,
        workspace_id: workspaceId,
      });
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/browser?${query.toString()}`);
      liveSocket = socket;
      socket.binaryType = 'blob';
      socket.onopen = () => {
        reconnectAttempt = 0;
        setConnected(true);
        setConnectionError('');
      };
      socket.onmessage = (event) => {
        if (event.data instanceof Blob) {
          replaceFrame(event.data);
          return;
        }
        try {
          const data = JSON.parse(String(event.data || '{}'));
          if (data.type !== 'browser_state') return;
          const isDesktopBrowser = data.desktop_browser === true;
          setDesktopBrowser(isDesktopBrowser);
          // Native takeover is controlled by Electron. Desktop frame metadata is
          // produced by the Python bridge and cannot observe that native state,
          // so its hard-coded `agent` value must not overwrite a successful
          // takeover and hide the "交还 Agent" action.
          if (!isDesktopBrowser) {
            setControlMode(['agent', 'human', 'paused'].includes(data.control_mode) ? data.control_mode : 'agent');
          }
          setPendingConfirmation(data.pending_confirmation && typeof data.pending_confirmation === 'object'
            ? data.pending_confirmation
            : null);
          const nextStatus = String(data.status || '').trim().toLowerCase();
          if (terminalBrowserStatuses.has(nextStatus)) {
            clearFrame();
          }
          browserLiveStore.upsert({
            parentSessionId: item.parentSessionId,
            agentSessionId: item.agentSessionId,
            browserSessionId: item.browserSessionId,
            workspaceId,
            status: data.status,
            action: data.action,
            url: data.url,
            title: data.title,
            frameSeq: Number(data.frame_seq || 0),
            error: data.error,
          });
        } catch {
          // Heartbeats and malformed metadata must not interrupt the frame stream.
        }
      };
      socket.onerror = () => {
        setConnectionError('实时画面连接暂时不可用');
      };
      socket.onclose = () => {
        setConnected(false);
        socket = null;
        liveSocket = null;
        if (disposed) return;
        const latest = browserLiveStore.activeForParent(item.parentSessionId);
        if (latest?.browserSessionId !== item.browserSessionId) return;
        if (terminalBrowserStatuses.has(String(latest.status || '').toLowerCase())) return;
        reconnectAttempt += 1;
        const delay = Math.min(5000, 400 * (2 ** Math.min(reconnectAttempt, 4)));
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    onCleanup(() => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
      if (liveSocket === socket) liveSocket = null;
    });
  });

  onCleanup(() => {
    const current = frameUrl();
    if (current) URL.revokeObjectURL(current);
  });

  return (
    <Show when={active()}>
      {(itemAccessor) => {
        const item = () => itemAccessor();
        const terminal = () => terminalBrowserStatuses.has(String(item().status || '').trim().toLowerCase());
        return (
          <>
          <Show
            when={!item().hidden && item().key !== 'manual'}
            fallback={
              <Show when={item().key !== 'manual'}>
              <button
                type="button"
                class="fixed bottom-24 right-5 z-[110] inline-flex items-center gap-2 rounded-full border border-[#38bdf8]/45 bg-[#101a20]/95 px-4 py-2 text-xs font-semibold text-[#bdeaff] shadow-[0_16px_40px_rgba(0,0,0,0.4)] backdrop-blur"
                onClick={() => browserLiveStore.setHidden(item().key, false)}
              >
                <span class="h-2 w-2 rounded-full bg-[#38bdf8]" />
                显示浏览器画面
              </button>
              </Show>
            }
          >
            <section
              class={expanded()
                ? 'fixed inset-4 z-[140] flex flex-col overflow-hidden rounded-2xl border border-[#31536a] bg-[#090d10] shadow-[0_28px_100px_rgba(0,0,0,0.68)]'
                : 'fixed bottom-24 right-5 z-[110] flex w-[min(560px,calc(100vw-2.5rem))] flex-col overflow-hidden rounded-2xl border border-[#31536a] bg-[#0b1115]/98 shadow-[0_24px_70px_rgba(0,0,0,0.58)] backdrop-blur'}
              aria-label="浏览器实时观察面板"
            >
              <header class="flex items-start justify-between gap-3 border-b border-[#26313a] px-3 py-2.5">
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <span class={`h-2 w-2 rounded-full ${connected() ? 'bg-[#7ee787]' : 'bg-[#facc15]'}`} />
                    <span class="text-xs font-semibold text-[#e6edf3]">浏览器观察</span>
                    <span class="rounded-full border border-[#31536a] bg-[#10202a] px-2 py-0.5 text-[10px] text-[#8ed5ff]">
                      {desktopBrowser() && connected() && item().status === 'ready' ? '已更新' : statusLabel(item().status, connected())}
                    </span>
                    <Show when={item().action}>
                      <span class="font-mono text-[10px] text-[#7f8b93]">{item().action}</span>
                    </Show>
                  </div>
                  <div class="mt-1 truncate text-[11px] text-[#9faeb7]" title={item().url || item().title}>
                    {terminal() ? '浏览器会话已结束' : (item().title || item().url || '等待浏览器打开页面')}
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    class={`rounded-lg border px-2 py-1 text-[11px] ${controlMode() === 'human' ? 'border-[#f1a02b] bg-[#3a2910] text-[#ffd17a]' : 'border-[#3e484f] bg-[#171c20] text-[#b7c7d0]'}`}
                    disabled={!connected() || terminal()}
                    onClick={() => void takeControl()}
                  >
                    {controlMode() === 'human' ? '正在接管控制中' : '接管控制'}
                  </button>
                  <button
                    type="button"
                    class="rounded-lg border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[11px] text-[#b7c7d0] hover:bg-[#1d272d]"
                    onClick={() => setExpanded((value) => !value)}
                  >
                    {expanded() ? '还原' : '放大'}
                  </button>
                  <button
                    type="button"
                    class="rounded-lg border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[11px] text-[#b7c7d0] hover:bg-[#1d272d]"
                    onClick={() => browserLiveStore.setHidden(item().key, true)}
                  >
                    隐藏
                  </button>
                </div>
              </header>

              <div class={`relative flex items-center justify-center bg-black ${expanded() ? 'min-h-0 flex-1' : 'aspect-[16/10]'}`}>
                <Show when={pendingConfirmation()}>
                  {(confirmation) => (
                    <div class="absolute left-3 right-3 top-3 z-10 rounded-xl border border-[#f1a02b]/60 bg-[#241b0d]/95 p-3 text-xs text-[#ffe0a3] shadow-xl">
                      <div class="font-semibold">Agent 请求执行敏感操作</div>
                      <div class="mt-1 break-words text-[#d9c39a]">{confirmation().summary || confirmation().action || '请确认是否继续'}</div>
                      <div class="mt-2 flex gap-2">
                        <button class="rounded-lg bg-[#f1a02b] px-3 py-1 font-semibold text-black" onClick={() => sendControl({ action: 'confirm_pending' })}>确认执行</button>
                        <button class="rounded-lg border border-[#6b5a3b] px-3 py-1" onClick={() => sendControl({ action: 'reject_pending' })}>拒绝</button>
                      </div>
                    </div>
                  )}
                </Show>
                <Show
                  when={frameUrl()}
                  fallback={
                    <div class="flex flex-col items-center gap-3 px-6 text-center text-sm text-[#7f8b93]">
                      <Show when={!terminal()}>
                        <div class="h-7 w-7 animate-spin rounded-full border-2 border-[#31536a] border-t-[#8ed5ff]" />
                      </Show>
                      <span>{terminal() ? '实时画面已清理' : (connectionError() || '正在等待第一帧画面…')}</span>
                    </div>
                  }
                >
                  <img
                    src={frameUrl()}
                    alt="Browser Agent 实时页面"
                    class="h-full w-full object-contain outline-none"
                  />
                </Show>
              </div>

              <footer class="flex items-center justify-between gap-3 border-t border-[#26313a] px-3 py-2 text-[10px] text-[#6f7d87]">
                <span class="min-w-0 flex-1 truncate" title={terminal() ? '' : item().url}>
                  {terminal() ? '会话数据已清理' : (item().url || 'about:blank')}
                </span>
                <span class="shrink-0 font-mono">frame {terminal() ? 0 : item().frameSeq}</span>
              </footer>
            </section>
          </Show>
          <Show when={desktop?.browserPanel && sidePanelOpen() && panelHuman()}>
            <Portal mount={document.getElementById('studio-browser-host')!}>
            <section class="desktop-browser-panel" aria-label="浏览器接管侧边栏">
              <header class="flex items-center justify-between gap-3 border-b border-[#26313a] px-4 py-3">
                <div class="min-w-0">
                  <div class="text-xs font-semibold">{displayingManual() ? '手动浏览' : '你正在接管，Agent 等待交还控制'}</div>
                  <div class="mt-0.5 truncate text-[11px]" title={browserState().url || item().url}>{browserState().title || item().title || item().url}</div>
                </div>
                <button class="shrink-0 rounded-lg border border-[#f1a02b]/60 bg-[#3a2910] px-3 py-1.5 text-xs font-semibold text-[#ffd17a]" onClick={() => void releaseControl()}>
                  {displayingManual() ? '关闭浏览器' : '交还给 Agent'}
                </button>
              </header>
              <BrowserToolbar state={browserState()} command={panelCommand} />
              <Show when={connectionError()}><div class="desktop-browser-error" role="alert">{connectionError()}</div></Show>
              <div class="relative min-h-0 flex-1 bg-white">
                <Show when={nativePanel()} fallback={
                <Show when={frameUrl()} fallback={<div class="flex h-full items-center justify-center text-sm text-[#7f8b93]">正在等待浏览器画面…</div>}>
                  <img
                    src={frameUrl()}
                    alt="可操作的浏览器页面"
                    class="h-full w-full cursor-pointer object-contain outline-none"
                    tabindex="0"
                    onClick={(event) => sendHumanInput({ action: 'click', ...framePoint(event) })}
                    onWheel={(event) => { event.preventDefault(); sendHumanInput({ action: 'scroll', deltaX: event.deltaX, deltaY: event.deltaY }); }}
                    onKeyDown={(event) => sendHumanInput({ action: 'key', key: event.key })}
                  />
                </Show>
                }><div ref={setNativeHost} class="h-full w-full" aria-label="内置浏览器页面" /></Show>
              </div>
              <Show when={!nativePanel()}>
              <form class="border-t border-[#26313a] bg-[#10171c] p-3" onSubmit={(event) => {
                event.preventDefault();
                const value = question().trim();
                if (!value) return;
                sendHumanInput({ action: 'text', text: value });
                sendHumanInput({ action: 'key', key: 'Enter' });
                setQuestion('');
              }}>
                <div class="flex items-center gap-2 rounded-xl border border-[#35434c] bg-[#0b1115] p-2 focus-within:border-[#5ba8d8]">
                  <input class="min-w-0 flex-1 bg-transparent px-2 text-sm text-[#e6edf3] outline-none" value={question()} onInput={(event) => setQuestion(event.currentTarget.value)} placeholder="向当前网页输入内容…" />
                  <button class="rounded-lg bg-[#d9edf8] px-3 py-1.5 text-xs font-semibold text-[#10202a]" type="submit">发送</button>
                </div>
                <div class="mt-2 text-[10px] text-[#71808a]">可直接点击、滚动页面；完成后请交还给 Agent 继续执行。</div>
              </form>
              </Show>
            </section>
            </Portal>
          </Show>
          </>
        );
      }}
    </Show>
  );
}
