import { Show, createSignal, createEffect, createMemo, createUniqueId, onCleanup, untrack } from 'solid-js';
import { Portal } from 'solid-js/web';
import { goalRequest, goalQuestionNotice, type GoalState } from '../api/goalMode';
import QuestionPager from './QuestionPager';
import { getSession } from '../api/client';
import { sessionStore } from '../stores/session';
import { mergeAndStabilizeVisibleMessages } from '../utils/messageSnapshots';
import ResearchDialog from './ResearchDialog';
import { goalAnswerNotice, goalBudgetExhausted, goalControlNotice, externalRuntimeNotice } from '../utils/runBudget';
import './GoalModePanel.css';

const labels: Record<string, string> = {ready:'准备推进', running:'正在推进', waiting_external:'等待已有计算', recovering:'检查恢复状态',
  needs_input:'需要你处理', paused:'本地已暂停', stopped:'目标已停止', completed:'已完成验收', budget_exhausted:'预算耗尽／禁止新提交'};

export default function GoalModePanel(props: {sessionId?: string; workspaceId: string; native: boolean; desktopPermissionMode?: 'ask' | 'auto'; identityKey?: string; controlMount?: HTMLElement; onRunning?: (value: boolean) => void; onMode?: (value: boolean) => void; onSubmitReady?: (handler: ((text: string) => Promise<boolean>) | undefined) => void}) {
  const [goal, setGoal] = createSignal<GoalState | null>(null);
  const [editing, setEditing] = createSignal(false);
  const [details, setDetails] = createSignal(false);
  const [armed, setArmed] = createSignal(false);
  const [criteria, setCriteria] = createSignal('');
  const [rounds, setRounds] = createSignal(8);
  const [minutes, setMinutes] = createSignal(30);
  const [draftCriteria, setDraftCriteria] = createSignal('');
  const [draftRounds, setDraftRounds] = createSignal(8);
  const [draftMinutes, setDraftMinutes] = createSignal(30);
  const [autoReviews, setAutoReviews] = createSignal(false);
  const [autoLimit, setAutoLimit] = createSignal(3);
  const [draftAutoReviews, setDraftAutoReviews] = createSignal(false);
  const [draftAutoLimit, setDraftAutoLimit] = createSignal(3);
  const [note, setNote] = createSignal('');
  const [extraRounds, setExtraRounds] = createSignal(4);
  const [extraMinutes, setExtraMinutes] = createSignal(15);
  const [error, setError] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [loaded, setLoaded] = createSignal(false);
  const [answerSaved, setAnswerSaved] = createSignal(false);
  const questionIds = createMemo(() => (goal()?.questions || []).filter(question => question.status !== 'answered').map(question => question.id));
  const tipId = createUniqueId();
  const [tipPosition, setTipPosition] = createSignal<{left: number; bottom: number} | null>(null);
  let tipTrigger: HTMLButtonElement | undefined;
  const showTip = () => {
    if (!tipTrigger) return;
    const rect = tipTrigger.getBoundingClientRect();
    setTipPosition({left: Math.max(12, Math.min(rect.right - 240, window.innerWidth - 252)), bottom: window.innerHeight - rect.top + 8});
  };
  const leaveTip = (event: MouseEvent) => {
    if (!(event.relatedTarget instanceof Element) || !event.relatedTarget.closest('.goal-mode-help, .goal-mode-help-tip')) setTipPosition(null);
  };
  createEffect(() => {
    if (!tipPosition()) return;
    const closeTip = () => setTipPosition(null);
    const keydown = (event: KeyboardEvent) => { if (event.key === 'Escape') closeTip(); };
    const outside = (event: PointerEvent) => { if (!tipTrigger?.contains(event.target as Node)) closeTip(); };
    window.addEventListener('keydown', keydown);
    window.addEventListener('pointerdown', outside);
    window.addEventListener('resize', closeTip);
    window.addEventListener('scroll', closeTip, true);
    onCleanup(() => {
      window.removeEventListener('keydown', keydown);
      window.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', closeTip);
      window.removeEventListener('scroll', closeTip, true);
    });
  });
  let generation = 0;
  let updateRevision = 0;
  let readFailed = false;
  const contextKey = createMemo(() => JSON.stringify([props.sessionId, props.workspaceId, props.identityKey]));
  createEffect(() => {
    const [sid, workspace] = JSON.parse(contextKey()) as [string | undefined, string];
    const current = ++generation;
    const controller = new AbortController();
    setGoal(null); setNote(''); setArmed(false); setEditing(false); setDetails(false); setError(''); setBusy(false); setLoaded(false); setTipPosition(null);
    setExtraRounds(4); setExtraMinutes(15);
    setAutoReviews(false); setAutoLimit(3);
    setAnswerSaved(false);
    readFailed = false;
    const poll = async () => {
      if (!sid || !workspace || busy()) return;
      const revision = updateRevision;
      try {
        const state = await goalRequest(sid, workspace, undefined, false, controller.signal);
        if (current !== generation || revision !== updateRevision) return;
        const changed = JSON.stringify(state) !== JSON.stringify(goal());
        if (state?.id !== goal()?.id) setNote('');
        setGoal(state);
        if (readFailed) { setError(''); readFailed = false; }
        if (changed && state && ['completed', 'stopped'].includes(state.status)) setArmed(false);
        setLoaded(true);
        if (state && (changed || ['ready','running','waiting_external','recovering'].includes(state.status)) && sessionStore.currentSessionId() === sid && !sessionStore.isRunning()) {
          const previousMessages = sessionStore.messages();
          const detail = await getSession(sid);
          if (current === generation && revision === updateRevision && detail && !sessionStore.isRunning() && sessionStore.messages() === previousMessages) {
            const messages = mergeAndStabilizeVisibleMessages(previousMessages, detail.messages || []);
            if (messages !== previousMessages) sessionStore.setMessages(messages);
          }
        }
      } catch (err) { if (current === generation && revision === updateRevision && !controller.signal.aborted) { readFailed = true; setError(err instanceof Error ? err.message : '目标状态读取失败'); } }
    };
    void untrack(poll);
    let inflight = false;
    const timer = setInterval(async () => { if ((!goal() && loaded()) || inflight) return; inflight = true; try { await poll(); } finally { inflight = false; } }, 2000);
    onCleanup(() => { clearInterval(timer); controller.abort(); });
  });
  onCleanup(() => { generation += 1; props.onSubmitReady?.(undefined); });
  createEffect(() => props.onRunning?.(Boolean(goal() && ['ready','running','waiting_external','recovering'].includes(goal()!.status))));
  const send = async (payload: Record<string, unknown>, action = false) => {
    if (!props.sessionId || busy()) return false;
    const current = generation;
    updateRevision += 1;
    readFailed = false;
    setBusy(true); setError('');
    try {
      const settings = props.desktopPermissionMode && (!action || ['resume', 'guide', 'answer'].includes(String(payload.action)))
        ? {desktop_permission_mode: props.desktopPermissionMode} : {};
      const state = await goalRequest(props.sessionId, props.workspaceId, {...payload, ...settings}, action);
      if (current !== generation) return false;
      if (state?.id !== goal()?.id) setNote('');
      setGoal(state); setEditing(false);
      setAnswerSaved(payload.action === 'answer');
      if (payload.action === 'answer' && state && !['ready', 'running', 'waiting_external', 'recovering'].includes(state.status)) {
        setDetails(true);
      }
      // Approval consent belongs to this goal, never the next goal in the same chat.
      if (!action) { setAutoReviews(false); setAutoLimit(3); }
      if (payload.action === 'stop') setArmed(false);
      if (!action || ['stop','resume','guide'].includes(String(payload.action))) setDetails(false);
      return true;
    } catch (err) { if (current === generation) { setError(err instanceof Error ? err.message : '目标操作失败'); setDetails(true); } return false; }
    finally { if (current === generation) setBusy(false); }
  };
  const action = (value: string, extra = {}) => void send({action: value, goal_id: goal()!.id, ...extra}, true);
  const active = () => goal() && !['completed', 'stopped'].includes(goal()!.status);
  const enabled = () => Boolean(active() || armed());
  createEffect(() => {
    props.onMode?.(enabled());
    props.onSubmitReady?.(enabled() && loaded() && props.native ? async text => {
      const limit = active() ? 2000 : 4000;
      if (text.length > limit) {
        setError(`目标${active() ? '指导' : '描述'}及附件上下文最多 ${limit} 字符；请缩短文字，或将内容附件切换为路径模式。附件仍保留在输入框。`);
        setDetails(true);
        return false;
      }
      if (active()) return send({action: 'guide', goal_id: goal()!.id, note: text}, true);
      return send({objective: text, from_chat: true, max_rounds: rounds(), max_minutes: minutes(), auto_approve_reviews: autoReviews(), max_auto_computations: autoLimit(), ...(criteria().trim() ? {criteria: criteria().trim()} : {})});
    } : undefined);
  });
  const close = () => { setEditing(false); setDetails(false); };
  const tip = () => !props.sessionId ? '先创建或选择一个会话' : !props.native ? '当前仅支持 TCode 原生 Agent' : '开启后直接在聊天框描述目标；不明确时 Agent 会询问。后续消息作为指导在下一轮接收。设置可调整下次目标预算；关闭会停止目标并回到普通模式。';
  const control = () => <div class="flex items-center gap-1.5 text-xs" style={{ color: 'var(--tcode-text)' }}>
    <Show when={goal() || error()}><button type="button" onClick={() => setDetails(true)} aria-label="查看目标状态" aria-haspopup="dialog" class="goal-mode-status" data-status={goal()?.status} data-attention={Boolean(error() || ['needs_input','budget_exhausted'].includes(goal()?.status || ''))}>
      <span class="goal-mode-status-dot" aria-hidden="true" />
      <span role="status">{error() ? '需要处理' : labels[goal()!.status] || '状态更新中'}</span>
      <span class="hidden xl:inline tabular-nums">{goal() ? `${goal()!.rounds_used}/${goal()!.max_rounds}` : ''}</span>
    </button></Show>
    <label class="inline-flex min-h-8 cursor-pointer items-center gap-2">
      <span class="hidden sm:inline">目标模式</span>
      <input type="checkbox" role="switch" aria-label="目标模式" aria-describedby={tipId} checked={enabled()}
        disabled={!props.sessionId || !props.native || !loaded() || busy()} class="peer sr-only"
        onChange={event => { if (active()) action('stop'); else setArmed(event.currentTarget.checked); event.currentTarget.checked = enabled(); }} />
      {/* Visual reference: Javierrocadev / bright-catfish-97 (Uiverse); compact track and sliding thumb. */}
      <span aria-hidden="true" class="relative h-6 w-12 rounded-full bg-rose-400 shadow-md transition-colors duration-300 peer-checked:bg-emerald-500 peer-focus-visible:ring-2 peer-focus-visible:ring-sky-400 peer-focus-visible:ring-offset-2 peer-disabled:opacity-40 motion-reduce:transition-none">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute left-1 top-1 h-4 w-4 text-gray-900"><path d="M7 10V7a5 5 0 0 1 10 0"/><rect x="5" y="10" width="14" height="11" rx="2"/></svg>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute right-1 top-1 h-4 w-4 text-gray-900"><path d="M7 10V7a5 5 0 0 1 10 0v3"/><rect x="5" y="10" width="14" height="11" rx="2"/></svg>
        <span class="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-gray-50 shadow-sm transition-transform duration-300 motion-reduce:transition-none" style={{ transform: enabled() ? 'translateX(24px)' : 'translateX(0)' }} />
      </span>
    </label>
    <button type="button" aria-label="目标模式设置" aria-haspopup="dialog" disabled={busy()} onClick={() => {setDraftCriteria(criteria()); setDraftRounds(rounds()); setDraftMinutes(minutes()); setDraftAutoReviews(active() ? Boolean(goal()?.auto_approve_reviews) : autoReviews()); setDraftAutoLimit(active() ? goal()?.max_auto_computations || 3 : autoLimit()); setEditing(true); setDetails(false);}} class="min-h-8 rounded-full border px-2.5">设置</button>
    <Show when={active() && goal()?.auto_approve_reviews}><span class="text-xs" title="工具 review 自动通过；安全与预算限制仍有效">自动审核</span></Show>
    <button ref={tipTrigger} type="button" aria-label="目标模式说明" aria-describedby={tipId} class="goal-mode-help"
      onMouseEnter={showTip} onMouseLeave={leaveTip} on:focus={showTip} on:blur={() => setTipPosition(null)} onClick={showTip}>
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5 5-2Z"/></svg>
      <span>指南</span>
    </button>
    <span id={tipId} class="sr-only">{tip()}</span>
  </div>;

  return <>
    <Show when={tipPosition()}>{position => <Portal><div class="goal-mode-help-tip" role="tooltip" style={{left: `${position().left}px`, bottom: `${position().bottom}px`}}
      onMouseEnter={showTip} onMouseLeave={leaveTip}>
      <strong>让目标持续推进</strong>
      <p>{tip()}</p>
      <div class="goal-mode-help-steps"><span>设定目标</span><span aria-hidden="true">→</span><span>限定预算</span><span aria-hidden="true">→</span><span>检查结果</span></div>
      <small>暂停与等待也计时 · 权限与安全检查保持不变</small>
    </div></Portal>}</Show>
    <Show when={props.controlMount} fallback={control()}>{mount => <Portal mount={mount()}>{control()}</Portal>}</Show>
    <Show when={answerSaved() && goal()}>{state => <p class="goal-mode-notice mt-2" role="status">{goalAnswerNotice(state())}</p>}</Show>
    <Show when={goal()?.status !== 'stopped' && goal()?.questions?.some(question => question.status !== 'answered')}>
      <div class="mt-2" aria-label="目标待回答问题">
          <QuestionPager compact requests={questionIds().map(id => ({request_id:id, session_id:props.sessionId || '', question:{...goal()!.questions!.find(question => question.id === id)!.payload, required:goal()!.questions!.find(question => question.id === id)!.required}}))}
            notice={request => { const question = goal()?.questions?.find(question => question.id === request.request_id); return question ? goalQuestionNotice(question) : ''; }} onAnswers={async answers => {
              const ok = await send({action:'answer', goal_id:goal()!.id, answers}, true);
              if (!ok) throw new Error(error() || '提交失败，请重试');
            }} />
      </div>
    </Show>
    <Show when={editing() || details()}><Portal><div class="goal-mode-overlay"><ResearchDialog portal={false} title={editing() ? '目标模式设置' : '目标模式'} onClose={close} busy={busy()}>
    <p class="goal-mode-intro">{editing() ? '目标从聊天输入框发起。轮次与时间用于下次新目标；审核设置可应用到当前目标，不会重置预算。' : '收起只隐藏详情，不会改变目标状态。需要中断时，请选择暂停或停止。'}</p>
    <Show when={!props.sessionId || !props.native}><p>{tip()}</p></Show>
    <Show when={editing()}><form class="mt-3 space-y-3" onSubmit={async event => {event.preventDefault(); if (active() && !await send({action:'review_policy', goal_id:goal()!.id, auto_approve_reviews:draftAutoReviews(), max_auto_computations:draftAutoLimit()}, true)) return; setCriteria(draftCriteria()); setRounds(draftRounds()); setMinutes(draftMinutes()); setAutoReviews(active() ? false : draftAutoReviews()); setAutoLimit(active() ? 3 : draftAutoLimit()); close();}}>
      <details><summary>可选完成条件</summary><label class="block">完成条件<textarea maxLength={4000} value={draftCriteria()} onInput={e => setDraftCriteria(e.currentTarget.value)} class="mt-1 block w-full rounded border p-2" /></label><p class="text-xs">留空时以聊天中的目标要求和真实证据验收；缺少条件时先澄清。</p></details>
      <div class="flex flex-wrap gap-3"><label>最多轮次 <input type="number" min="1" max="100" required value={draftRounds()} onInput={e => setDraftRounds(e.currentTarget.valueAsNumber)} class="w-20 rounded bg-[#11171b] p-1" /></label>
        <label>时间预算（分钟） <input type="number" min="1" max="1440" required value={draftMinutes()} onInput={e => setDraftMinutes(e.currentTarget.valueAsNumber)} class="w-20 rounded bg-[#11171b] p-1" /></label></div>
      <p class="text-xs">开启模式后发送目标，即授权在预算内持续推进。权限、安全检查不变；等待和暂停时间计入预算。</p>
      <fieldset class="space-y-2 rounded border p-3" style={{'border-color':'var(--tcode-border)'}}>
        <label class="flex min-h-10 cursor-pointer items-center justify-between gap-4"><span>自动通过工具人工审核</span><input type="checkbox" role="switch" aria-label="自动通过工具人工审核" checked={draftAutoReviews()} onChange={e => setDraftAutoReviews(e.currentTarget.checked)} /></label>
        <p class="text-xs">关闭：遇到 review 暂停并通知你。开启：由 Agent 决定调用，人工审核自动通过，不逐项打断；可能消耗远程资源。仅当前目标生效，明确拒绝、安全检查和暂停停止仍有效。</p>
        <Show when={draftAutoReviews()}><label class="flex items-center gap-2">自动计算提交上限 <input aria-label="自动计算提交上限" type="number" required min="1" max="100" value={draftAutoLimit()} onInput={e => setDraftAutoLimit(e.currentTarget.valueAsNumber)} class="w-20 rounded border p-1" /></label><p class="text-xs">每项申请仅执行一次。达到上限后新计算转为人工审核，不会无限重跑。</p></Show>
      </fieldset>
      <button disabled={busy()} class="btn-primary rounded px-3 py-2">保存设置</button>
      <button type="button" disabled={busy()} onClick={close} class="ml-3 underline">取消</button>
    </form></Show>
    <Show when={!editing() ? goal() : null}>{g => <div class="mt-4 space-y-4">
      <div class="goal-mode-metrics"><div><span>当前状态</span><strong>{labels[g().status] || '状态更新中'}</strong></div><div><span>已用轮次</span><strong>{g().rounds_used} <small>/ {g().max_rounds}</small></strong></div></div>
      <p class="goal-mode-notice" role="status">{goalControlNotice(g())}</p>
      <Show when={['paused','needs_input'].includes(g().status) && goalBudgetExhausted(g())}><p class="goal-mode-notice">轮次或时间预算已耗尽，继续前需明确增加预算；等待和暂停也计时。</p></Show>
      <p class="goal-mode-intro">{externalRuntimeNotice(g().external_runtime)}</p>
      <section class="goal-mode-section"><h3>研究目标</h3><p>{g().objective}</p></section>
      <Show when={g().progress}><section class="goal-mode-section"><h3>最新进展</h3><p>{g().progress}</p></section></Show>
      <Show when={g().next_step}><section class="goal-mode-section"><h3>下一步</h3><p>{g().next_step}</p></section></Show>
      <Show when={g().reason}><p class="goal-mode-notice">{g().reason}</p></Show>
      <details class="goal-mode-section"><summary>完成条件与预算</summary><p class="mt-2">{g().criteria}</p><p class="mt-2">时间上限：{new Date(g().deadline * 1000).toLocaleString()}（等待和暂停也计时）</p></details>
      <Show when={g().auto_approve_reviews}><p class="goal-mode-intro">自动审核已用 {g().auto_approved_review_ids ? g().auto_approved_review_ids!.length : '未记录'} / {g().max_auto_computations ?? '未记录'}。达到上限后新计算转人工审核，不代表取消已有作业或禁止所有人工授权提交。</p></Show>
      <Show when={active()}><div class="flex flex-wrap gap-2">
        <Show when={['ready','running','waiting_external','recovering'].includes(g().status)}><button type="button" disabled={busy()} onClick={() => action('pause')} class="rounded border px-3 py-1">暂停目标</button></Show>
        <Show when={['paused','needs_input','budget_exhausted'].includes(g().status)}><label>补充说明<input value={note()} onInput={e => setNote(e.currentTarget.value)} maxLength={2000} class="ml-2 rounded bg-[#11171b] p-1" /></label>
          <button type="button" disabled={busy() || goalBudgetExhausted(g())} title={goalBudgetExhausted(g()) ? '请先明确增加预算' : undefined} onClick={() => action('resume', {note: note()})} class="rounded border px-3 py-1">继续目标</button>
          <form class="flex flex-wrap items-center gap-2" onSubmit={event => {event.preventDefault(); action('resume', {note: note(), extra_rounds: extraRounds(), extra_minutes: extraMinutes()});}}>
            <label>增加轮次 <input type="number" min="0" max="100" step="1" required value={extraRounds()} onInput={e => setExtraRounds(e.currentTarget.valueAsNumber)} class="w-20 rounded border p-1" /></label>
            <label>增加时间（分钟） <input type="number" min="0" max="1440" step="1" required value={extraMinutes()} onInput={e => setExtraMinutes(e.currentTarget.valueAsNumber)} class="w-20 rounded border p-1" /></label>
            <button type="submit" disabled={busy() || !(extraRounds() > 0 || extraMinutes() > 0)} class="rounded border px-3 py-1">增加预算并继续</button>
          </form></Show>
        <button type="button" disabled={busy()} onClick={() => action('stop')} class="rounded border px-3 py-1">停止目标／回到普通模式</button>
      </div></Show>
    </div>}</Show>
    <Show when={!editing()}><p class="goal-mode-intro mt-4">{tip()}</p></Show>
    <Show when={error()}><p role="alert" class="mt-2 text-[#ffb3ad]">{error()}</p></Show>
  </ResearchDialog></div></Portal></Show></>;
}
