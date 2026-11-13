import { Show, createEffect, createMemo, createSignal, on, onCleanup, onMount } from 'solid-js';
import { desktop, type ComputerState } from '../desktop';

export function computerPanelDisclosureTransition(
  open: boolean,
  event: 'toggle' | 'escape' | 'outside' | 'blur',
  targetInside = false,
): { open: boolean; focusTrigger: boolean } {
  if (event === 'toggle') return { open: !open, focusTrigger: false };
  if (event === 'blur') return { open: false, focusTrigger: false };
  if (event === 'escape' && open) return { open: false, focusTrigger: true };
  if (event === 'outside' && open && !targetInside) return { open: false, focusTrigger: false };
  return { open, focusTrigger: false };
}

export function validComputerPanelRequest(value: unknown): number | null {
  if (!value || typeof value !== 'object') return null;
  const requestId = (value as { request_id?: unknown }).request_id;
  return Number.isSafeInteger(requestId) && Number(requestId) > 0 ? Number(requestId) : null;
}

export function computerPermissionsReady(state: ComputerState | undefined): boolean {
  const permissions = state?.permissions;
  return Boolean(state?.enabled && permissions?.status === 'known' && permissions.identity_verified
    && permissions.accessibility === true && permissions.screen_recording === true);
}

export function computerPanelApprovalKey(state: ComputerState | undefined): string {
  const pending = state?.pending_approval;
  return pending ? `${pending.approval_id}:${pending.revision || 0}` : '';
}

export function computerPanelOpenForState(open: boolean, state: ComputerState | undefined, permissionGuidance = false, dismissedApproval = ''): boolean {
  if (state?.pending_approval && computerPanelApprovalKey(state) !== dismissedApproval) return true;
  return permissionGuidance && computerPermissionsReady(state) ? false : open;
}

export function computerNavigationDefault(summary: Record<string, unknown>): boolean {
  return summary.approval_kind === 'task' && summary.navigation_proposal === true;
}

export function computerPreviewContext(state: ComputerState | undefined): string {
  return JSON.stringify([state?.enabled, state?.status, state?.driver_generation, state?.control_epoch,
    state?.active_scope?.display_name, state?.pending_approval?.task_id, computerPanelApprovalKey(state),
    computerPermissionsReady(state)]);
}

export function computerPreviewNotice(observedAt: number | undefined, now = Date.now()): string {
  if (!Number.isFinite(observedAt) || !observedAt || observedAt > now + 1000) return '采集时间未知；这是历史观察，不是实时画面，也不是任务完成证明。';
  return `${now - observedAt > 15_000 ? '较早的观察' : '最近观察'} · ${new Date(observedAt).toLocaleTimeString()}；不是实时画面，也不是任务完成证明。`;
}

export function ExactComputerAction(props: { text: string }) {
  return <pre class="computer-exact-action" aria-label="本次精确操作">{props.text}</pre>;
}

export default function ComputerUsePanel() {
  let container!: HTMLDivElement;
  let trigger!: HTMLButtonElement;
  const [panelOpen, setPanelOpen] = createSignal(false);
  let dismissedApproval = '';
  const [previewVisible, setPreviewVisible] = createSignal(false);
  const dismissPanel = () => { dismissedApproval = computerPanelApprovalKey(state()); setPanelOpen(false); setPermissionGuidance(false); setPreviewVisible(false); };
  const togglePanel = () => { if (panelOpen()) dismissPanel(); else { setPermissionGuidance(false); setPanelOpen(true); } };
  const [permissionGuidance, setPermissionGuidance] = createSignal(false);
  const [safetyOpen, setSafetyOpen] = createSignal(false);
  const [state, setState] = createSignal<ComputerState>();
  const [preview, setPreview] = createSignal<{ image: string; app: string; observed_at?: number } | null>(null);
  const [error, setError] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [rememberControl, setRememberControl] = createSignal(false);
  const [navigation, setNavigation] = createSignal(false);
  const [singleMessage, setSingleMessage] = createSignal(false);
  const [recipient, setRecipient] = createSignal('');
  const [messageText, setMessageText] = createSignal('');
  const approvalId = createMemo(() => computerPanelApprovalKey(state()));
  const previewContext = createMemo(() => computerPreviewContext(state()));
  let stateRevision = 0;
  const applyState = (next: ComputerState) => {
    stateRevision += 1;
    setState(next);
    setPanelOpen(open => computerPanelOpenForState(open, next, permissionGuidance(), dismissedApproval));
  };
  createEffect(() => {
    if (permissionGuidance() && computerPermissionsReady(state())) {
      setPanelOpen(Boolean(state()?.pending_approval));
      setPermissionGuidance(false);
      setError('');
    }
  });
  createEffect(on(approvalId, () => { setRememberControl(false); setNavigation(computerNavigationDefault(state()?.pending_approval?.action_summary || {})); setSingleMessage(false); setRecipient(''); setMessageText(''); }));
  const label = () => !state() ? '状态读取中' : !state()?.enabled ? '已关闭' : state()?.pending_approval ? '等待授权'
    : ({ running: '正在操作电脑', paused: '本地已暂停', taken_over: '由你接管', unavailable: '不可用' } as Record<string, string>)[state()?.status || 'unavailable'] || '等待操作';
  const summary = () => state()?.pending_approval?.action_summary || {};
  const readable = (key: string, fallback = '') => String(summary()[key] || fallback);
  const operationLabel = () => {
    const pending = state()?.pending_approval;
    if (!pending) return '';
    const clicks = Number(summary().click_count || (pending.operation === 'double_click' ? 2 : 0));
    if (clicks === 1) return '单击';
    if (clicks === 2) return '双击';
    if (clicks === 3) return '三击';
    return pending.operation;
  };
  onMount(() => {
    let reading = false;
    let disposed = false;
    const readStatus = async () => {
      if (reading || busy() || !desktop?.computerStatus) return;
      reading = true;
      const revision = stateRevision;
      try {
        const next = await desktop.computerStatus();
        if (!disposed && revision === stateRevision && !busy()) applyState(next);
      } catch { if (!disposed && revision === stateRevision) setError('无法读取电脑控制状态'); }
      finally { reading = false; }
    };
    let lastRequestId = 0;
    const openFromDesktop = (request: unknown) => {
      const requestId = validComputerPanelRequest(request);
      if (requestId === null || requestId <= lastRequestId) return;
      lastRequestId = requestId;
      const isPermission = (request as { reason?: string }).reason === 'permissions';
      if (isPermission && computerPermissionsReady(state())) {
        void desktop?.computerPanelAck?.(requestId);
        return;
      }
      setPermissionGuidance(isPermission);
      setPanelOpen(true);
      queueMicrotask(() => trigger.focus());
      void desktop?.computerPanelAck?.(requestId);
    };
    const closeOutside = (event: PointerEvent) => {
      const next = computerPanelDisclosureTransition(panelOpen(), 'outside', container.contains(event.target as Node));
      if (!next.open) dismissPanel();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        const next = computerPanelDisclosureTransition(panelOpen(), 'escape');
        if (!next.open) dismissPanel();
        if (next.focusTrigger) trigger.focus();
      }
    };
    const toggleFromKeyboard = (event: KeyboardEvent) => {
      if (event.ctrlKey && event.altKey && !event.metaKey && event.key.toLowerCase() === 'c') {
        event.preventDefault();
        togglePanel();
        queueMicrotask(() => trigger.focus());
      }
    };
    window.addEventListener('blur', dismissPanel);
    document.addEventListener('pointerdown', closeOutside, true);
    document.addEventListener('keydown', closeOnEscape);
    document.addEventListener('keydown', toggleFromKeyboard);
    void readStatus();
    const dispose = desktop?.onComputerStatus?.(applyState);
    const disposePanelOpen = desktop?.onComputerPanelOpen?.(openFromDesktop);
    void desktop?.computerPanelRequest?.().then(openFromDesktop).catch(() => {});
    const timer = window.setInterval(() => {
      void readStatus();
      refreshPreview();
    }, 1000);
    onCleanup(() => {
      disposed = true; stateRevision += 1; previewRequest += 1;
      dispose?.(); disposePanelOpen?.(); window.clearInterval(timer);
      window.removeEventListener('blur', dismissPanel);
      document.removeEventListener('pointerdown', closeOutside, true);
      document.removeEventListener('keydown', closeOnEscape);
      document.removeEventListener('keydown', toggleFromKeyboard);
    });
  });
  let previewRequest = 0;
  function refreshPreview() {
    const request = ++previewRequest;
    const context = computerPreviewContext(state());
    if (!panelOpen() || !computerPermissionsReady(state()) || ['taken_over', 'unavailable'].includes(state()?.status || '')) { setPreview(null); return; }
    void desktop?.computerPreview?.().then(frame => {
      if (request === previewRequest && panelOpen() && context === computerPreviewContext(state())) setPreview(frame);
    }).catch(() => { if (request === previewRequest) setPreview(null); });
  }
  createEffect(on(() => [panelOpen(), previewContext()], () => { setPreview(null); if (panelOpen()) refreshPreview(); else ++previewRequest; }));
  async function control(action: 'pause' | 'takeover' | 'emergency_stop') {
    if (!desktop?.computerControl || busy()) return;
    setBusy(true); setError('');
    const revision = ++stateRevision;
    try { const next = await desktop.computerControl(action); if (revision === stateRevision) applyState(next); }
    catch { setError('控制操作未完成，请重试'); }
    finally { setBusy(false); }
  }
  async function setEnabled(enabled: boolean) {
    if (!desktop?.computerSetEnabled || busy()) return;
    setBusy(true); setError('');
    const revision = ++stateRevision;
    try { const next = await desktop.computerSetEnabled(enabled); if (revision === stateRevision) applyState(next); }
    catch { setError('开关未更新，请重试'); }
    finally { setBusy(false); }
  }
  async function openPermission(permission: 'accessibility' | 'screen_recording') {
    if (!desktop?.computerOpenPermission || busy()) return;
    setBusy(true); setError('');
    try { await desktop.computerOpenPermission(permission); }
    catch { setError('未能打开系统权限页面，请在系统设置的“隐私与安全性”中检查权限。'); }
    finally { setBusy(false); }
  }
  async function refreshPermissions() {
    if (!desktop?.computerRefreshPermissions || busy()) return;
    setBusy(true); setError('');
    const revision = ++stateRevision;
    try { const next = await desktop.computerRefreshPermissions(); if (revision === stateRevision) applyState(next); }
    catch { setError('权限检查未完成，请重试。'); }
    finally { setBusy(false); }
  }
  async function approve() {
    const pending = state()?.pending_approval;
    if (!pending || !desktop?.computerGrant || busy()) return;
    setBusy(true); setError('');
    const revision = ++stateRevision;
    try {
      const response = await desktop.computerGrant({ approval_id: pending.approval_id, task_id: pending.task_id, operation: pending.operation, parameter_digest: pending.parameter_digest, remember_control: rememberControl(), navigation: navigation(),
        ...(singleMessage() ? { single_message: { recipient: recipient(), text: messageText() } } : {}) });
      if (revision === stateRevision) applyState(response.state);
    } catch { setError('授权目标已经变化，请让 Agent 重新观察'); }
    finally { setBusy(false); }
  }
  async function reject() {
    const pending = state()?.pending_approval;
    if (!pending || !desktop?.computerReject || busy()) return;
    setBusy(true); setError('');
    const revision = ++stateRevision;
    try { const next = await desktop.computerReject({ approval_id: pending.approval_id, parameter_digest: pending.parameter_digest }); if (revision === stateRevision) applyState(next); }
    catch { setError('该授权请求已经失效'); }
    finally { setBusy(false); }
  }
  return <div ref={container} class="studio-layout-menu computer-use-panel">
    <button ref={trigger} type="button" class="computer-use-trigger"
      aria-expanded={panelOpen()} aria-controls="computer-use-controls" aria-keyshortcuts="Control+Alt+C"
      onClick={togglePanel}>
      <span class={`desktop-service-light is-${state()?.status === 'running' ? 'connected' : state()?.available ? 'unconfigured' : 'failed'}`} aria-hidden="true"/>
      电脑控制 · {label()}<span aria-hidden="true">⌄</span>
    </button>
    <Show when={panelOpen()}><div id="computer-use-controls" role="region" aria-label="电脑控制">
      <div class="computer-panel-scroll" role="region" tabIndex={0} aria-label="电脑控制与授权内容">
      <b>本机电脑控制</b>
      <p>默认后台执行；需要前台时单独请求授权。你可继续使用其他应用。</p>
      <div class="computer-app-preview" onMouseEnter={() => setPreviewVisible(true)} onMouseLeave={() => setPreviewVisible(false)}>
        <button type="button" class="computer-app-row" aria-expanded={previewVisible()} aria-controls="computer-app-frame"
          onFocus={() => setPreviewVisible(true)} onBlur={() => setPreviewVisible(false)} onClick={() => setPreviewVisible(value => !value)}>
          <span class={`desktop-service-light is-${state()?.status === 'running' ? 'connected' : 'unconfigured'}`} aria-hidden="true" />
          <span><strong>{state()?.active_scope?.display_name || preview()?.app || '暂无正在操作的软件'}</strong><small>{label()}</small></span>
          <span aria-hidden="true">›</span>
        </button>
        <Show when={previewVisible() && preview()}><figure id="computer-app-frame" class="computer-app-frame">
          <Show when={preview()}>{frame => <>
            <img src={frame().image} alt={`最近采集的软件画面：${frame().app}`} />
            <figcaption>{computerPreviewNotice(frame().observed_at)}</figcaption>
          </>}</Show>
        </figure></Show>
      </div>
      <label class="computer-use-toggle-row">
        <span>允许 Computer Use</span>
        <span class="computer-use-switch">
          <input type="checkbox" role="switch" aria-label="允许 Computer Use" checked={state()?.enabled === true} disabled={busy() || !state()?.available} onChange={event => void setEnabled(event.currentTarget.checked)} />
          <span class="computer-use-switch-track" aria-hidden="true"><span class="computer-use-switch-face" /></span>
        </span>
      </label>
      <p>{state()?.enabled ? '应用开关已开启；macOS 系统权限需单独通过检查，具体任务仍需授权。' : '已关闭；Agent 不能读取应用画面或操作电脑。'}</p>
      <p role="status">{error() || (state()?.available ? '所有动作都受当前任务、应用、窗口和控制代际约束。' : '本机 Driver 尚未配置。')}</p>
      <Show when={state()?.permissions}>{permissions => <p class="computer-permissions">
        权限归属 {permissions().identity_verified ? 'TCode' : '未知（已阻断）'} · 辅助功能 {permissions().accessibility ? '已确认' : permissions().status === 'known' ? '进程未确认' : '检查未完成'} · 屏幕录制 {permissions().screen_recording ? '已确认' : permissions().status === 'known' ? '进程未确认' : '检查未完成'}
      </p>}</Show>
      <Show when={state()?.enabled && state()?.permissions && (state()?.permissions?.accessibility !== true || state()?.permissions?.screen_recording !== true || !state()?.permissions?.identity_verified)}>
        <div class="computer-approval" role="status" aria-label="电脑控制权限引导">
          <strong>{state()?.permissions?.runtime_mismatch ? '系统授权与控制进程检测不一致' : '控制进程尚未确认系统权限'}</strong>
          <p>{state()?.permissions?.runtime_mismatch
            ? '宿主程序已检测到授权，但控制进程还未识别。请先重新检查；如仍不一致，保存工作后重新启动源码桌面端，不需要反复切换系统权限。'
            : '如果你已在系统设置中授权，请先重新检查；不要反复开关权限。检测未通过可能与当前进程、应用身份或需要重启有关，不能据此断定你没有授权。'}</p>
          <Show when={state()?.permissions?.identity_verified === false}>
            <p>控制进程身份尚未通过校验。重新授权未必能解决，请检查桌面 Driver；不会绕过这项检查。</p>
          </Show>
          <Show when={state()?.permissions?.status === 'known' && state()?.permissions?.accessibility === false && state()?.permissions?.host_accessibility !== true}>
            <button disabled={busy()} onClick={() => void openPermission('accessibility')}>查看辅助功能设置</button>
          </Show>
          <Show when={state()?.permissions?.status === 'known' && state()?.permissions?.screen_recording === false && state()?.permissions?.host_screen_recording !== true}>
            <button disabled={busy()} onClick={() => void openPermission('screen_recording')}>查看屏幕录制设置</button>
          </Show>
          <button disabled={busy()} onClick={() => void refreshPermissions()}>我已开启，重新检查</button>
          <p>检查通过后，在聊天中回复“继续”。如 macOS 提示退出并重新打开应用，请先保存当前工作；不会自动重放之前的操作。</p>
        </div>
      </Show>
      <Show when={state()?.pending_approval}>{pending => <div class="computer-approval">
        <strong>{readable('title', '允许 TCode 执行这项电脑操作？')}</strong>
        <span><b>允许范围</b>：{readable('allowed', '仅执行当前显示的操作')}</span>
        <span><b>再次确认</b>：{readable('ask_again', '目标或权限范围变化时会再次询问')}</span>
        <span><b>有效时间</b>：{readable('duration', '本次授权可随时暂停或撤销')}</span>
        <Show when={summary().screenshot}><span>{readable('screenshot')}</span></Show>
        <Show when={summary().exact_action}><ExactComputerAction text={readable('exact_action')} /></Show>
        <Show when={summary().navigation_proposal}><label>
          <input type="checkbox" checked={navigation()} onChange={event => setNavigation(event.currentTarget.checked)} />
          允许本任务的一般导航（默认勾选，可取消）：在上方应用与任务范围内搜索、导航、输入任务中的原文，以及按新鲜截图点击，无需逐项确认。不含发送、付款、删除；未知位置和按键不能保证语义，可能产生实际影响。
        </label></Show>
        <Show when={summary().navigation_proposal}><label>
          <input type="checkbox" checked={singleMessage()} onChange={event => setSingleMessage(event.currentTarget.checked)} />
          本任务还允许发送一条明确消息（收件人与正文必须来自上方原始任务；未知结果不重发）
        </label></Show>
        <Show when={singleMessage()}>
          <label>收件人<input aria-label="本任务消息收件人" value={recipient()} onInput={event => setRecipient(event.currentTarget.value)} /></label>
          <label>精确正文<textarea aria-label="本任务消息精确正文" value={messageText()} onInput={event => setMessageText(event.currentTarget.value)} /></label>
          <span>发送前仍须新观察中核实唯一收件人与精确正文；无法核实时停止，不猜测。最终提交仅一次，回车与点击发送共享消费。</span>
        </Show>
        <Show when={summary().control_proposal}><label>
          <input type="checkbox" checked={rememberControl()} onChange={event => setRememberControl(event.currentTarget.checked)} />
          将这项精确控件操作加入本轮任务范围（其他控件、文本及提交动作仍需确认）
        </label></Show>
        <button type="button" aria-expanded={safetyOpen()} aria-controls="computer-approval-safety"
          onClick={() => setSafetyOpen(open => !open)}>安全详情</button>
        <Show when={safetyOpen()}><small id="computer-approval-safety">
          应用 {pending().app?.bundle_id || '系统发现'} · 操作 {operationLabel()} · 模式 {pending().delivery_mode} · 到期 {pending().expires_at ? new Date(pending().expires_at!).toLocaleTimeString() : '本轮'} · 校验 {pending().parameter_digest.slice(0, 10)}
        </small></Show>
      </div>}</Show>
      <Show when={!state()?.pending_approval && state()?.active_scope}>{scope => <p role="status">正在{scope().display_name}{scope().target_label} · 剩余 {scope().remaining_actions} 个动作 · 可随时停止</p>}</Show>
      </div>
      <div class="computer-controls">
        <Show when={state()?.pending_approval}>
          <button disabled={busy()} onClick={() => void approve()}>{summary().approval_kind === 'task' ? navigation() || singleMessage() ? '按以上范围允许本任务' : '仅允许观察与基础操作' : '仅允许这次操作'}</button>
          <button disabled={busy()} onClick={() => void reject()}>拒绝</button>
        </Show>
        <button class="danger" disabled={busy() || !state()?.available || !state()?.enabled} onClick={() => void control('emergency_stop')}>停止控制</button>
      </div>
      <small>本轮已执行 {state()?.action_budget?.used ?? '未知'}/{state()?.action_budget?.limit ?? '未记录'} 个动作；动作回执不等于任务完成。</small>
    </div></Show>
  </div>;
}
