import FeedbackDialog, { confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createSignal, onCleanup, onMount } from 'solid-js';
import {
  confirmScheduledTask,
  deleteScheduledTask,
  getScheduledTaskPreferences,
  listScheduledNotifications,
  listScheduledTaskRuns,
  listScheduledTasks,
  markAllScheduledNotificationsRead,
  markScheduledNotificationRead,
  prepareScheduledTask,
  scheduledTaskAction,
  updateScheduledTask,
  updateScheduledTaskPreferences,
  type AuthUser,
  type ScheduledNotification,
  type ScheduledTask,
  type ScheduledTaskInput,
  type ScheduledTaskPreview,
  type ScheduledTaskRun,
  type ScheduleType,
} from '../api/client';

type Props = {
  currentUser: AuthUser;
  sourceSessionId?: string;
  workspaceId?: string;
  workdir?: string;
  focusRequest?: {id: string; key: number};
};

const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

function defaultDateTimeLocal(): string {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  date.setSeconds(0, 0);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function formatTimestamp(value?: number | null): string {
  if (!value) return '—';
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
}

function scheduleSummary(task: ScheduledTask): string {
  const schedule = task.schedule || {};
  const time = String(schedule.time || '09:00').slice(0, 5);
  switch (task.schedule_type) {
    case 'at':
    case 'delay':
      return `一次性 · ${task.next_run_at ? formatTimestamp(task.next_run_at) : '已执行'}`;
    case 'daily':
      return `每天 ${time}`;
    case 'weekdays':
      return `工作日 ${time}`;
    case 'weekly':
      return `每周 ${(schedule.weekdays || []).map((day: number) => WEEKDAYS[day]).join('、')} ${time}`;
    case 'monthly':
      return `每月 ${schedule.day} 日 ${time}`;
    case 'cron':
      return `Cron · ${schedule.expression}`;
    default:
      return task.schedule_type;
  }
}

function statusLabel(status: string): string {
  return ({
    active: '运行中',
    paused: '已暂停',
    completed: '已完成',
    failed: '失败',
    deleted: '已删除',
  } as Record<string, string>)[status] || status;
}

function statusClass(status: string): string {
  if (status === 'active') return 'border-[#38bdf8]/35 bg-[#38bdf8]/10 text-[#8ed5ff]';
  if (status === 'failed') return 'border-[#ef4444]/35 bg-[#ef4444]/10 text-[#fca5a5]';
  if (status === 'completed') return 'border-[#22c55e]/35 bg-[#22c55e]/10 text-[#9ee6bb]';
  return 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]';
}

export default function ScheduledTasksWorkspace(props: Props) {
  const [tab, setTab] = createSignal<'tasks' | 'notifications'>('tasks');
  const [tasks, setTasks] = createSignal<ScheduledTask[]>([]);
  const [notifications, setNotifications] = createSignal<ScheduledNotification[]>([]);
  const [unreadCount, setUnreadCount] = createSignal(0);
  const [adminScope, setAdminScope] = createSignal(false);
  const [loading, setLoading] = createSignal(true);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [preview, setPreview] = createSignal<ScheduledTaskPreview | null>(null);
  const [editingId, setEditingId] = createSignal('');
  const [editorOpen, setEditorOpen] = createSignal(false);
  const [editingTask, setEditingTask] = createSignal<ScheduledTask | null>(null);
  const [historyTask, setHistoryTask] = createSignal<ScheduledTask | null>(null);
  const [runs, setRuns] = createSignal<ScheduledTaskRun[]>([]);
  let appliedSearchKey: number | undefined;
  createEffect(() => {
    const request = props.focusRequest;
    if (!request) {setHistoryTask(null);return;}
    if (request.key === appliedSearchKey) return;
    const task = tasks().find(item => item.id === request.id);
    if (task) {appliedSearchKey = request.key;setTab('tasks');void openHistory(task);}
  });

  const [name, setName] = createSignal('');
  const [instruction, setInstruction] = createSignal('');
  const [scheduleType, setScheduleType] = createSignal<ScheduleType>('delay');
  const [timezone, setTimezone] = createSignal('Asia/Shanghai');
  const [personalTimezone, setPersonalTimezone] = createSignal('Asia/Shanghai');
  const [runAt, setRunAt] = createSignal(defaultDateTimeLocal());
  const [delayAmount, setDelayAmount] = createSignal(1);
  const [delayUnit, setDelayUnit] = createSignal<'seconds' | 'minutes' | 'hours' | 'days'>('minutes');
  const [clock, setClock] = createSignal('09:00');
  const [weekdays, setWeekdays] = createSignal<number[]>([0]);
  const [monthDay, setMonthDay] = createSignal(1);
  const [cronExpression, setCronExpression] = createSignal('0 9 * * 1-5');

  const canAdmin = createMemo(() => ['owner', 'admin'].includes(String(props.currentUser.role || '')));
  const inputClass = 'h-10 w-full rounded-xl border border-[#303539] bg-[#0f1418] px-3 text-sm text-[#eef3f6] outline-none focus:border-[#7bd0ff]/55';
  const buttonClass = 'inline-flex h-8 items-center justify-center rounded-full border border-[#303539] bg-[#171c20] px-3 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/40 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50';

  const load = async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const [taskItems, messageData] = await Promise.all([
        listScheduledTasks(adminScope()),
        listScheduledNotifications(),
      ]);
      setTasks(taskItems);
      setNotifications(messageData.notifications);
      setUnreadCount(messageData.unread_count);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载任务中心失败');
    } finally {
      if (showLoading) setLoading(false);
    }
  };

  onMount(() => {
    void load(true);
    void getScheduledTaskPreferences()
      .then((preferences) => {
        const value = preferences.timezone || 'Asia/Shanghai';
        setPersonalTimezone(value);
        if (!editingId() && !preview()) setTimezone(value);
      })
      .catch(() => undefined);
    const timer = window.setInterval(() => void load(false), 5000);
    onCleanup(() => window.clearInterval(timer));
  });

  const resetForm = () => {
    setEditorOpen(false);
    setEditingId('');
    setEditingTask(null);
    setName('');
    setInstruction('');
    setScheduleType('delay');
    setTimezone(personalTimezone());
    setRunAt(defaultDateTimeLocal());
    setDelayAmount(1);
    setDelayUnit('minutes');
    setClock('09:00');
    setWeekdays([0]);
    setMonthDay(1);
    setCronExpression('0 9 * * 1-5');
    setPreview(null);
  };

  const buildInput = (): ScheduledTaskInput => {
    let schedule: Record<string, any>;
    switch (scheduleType()) {
      case 'at':
        schedule = { run_at: runAt() };
        break;
      case 'delay': {
        const multiplier = { seconds: 1, minutes: 60, hours: 3600, days: 86400 }[delayUnit()];
        schedule = { delay_seconds: Number(delayAmount()) * multiplier };
        break;
      }
      case 'daily':
      case 'weekdays':
        schedule = { time: clock() };
        break;
      case 'weekly':
        schedule = { time: clock(), weekdays: weekdays() };
        break;
      case 'monthly':
        schedule = { time: clock(), day: Number(monthDay()) };
        break;
      case 'cron':
        schedule = { expression: cronExpression() };
        break;
    }
    return {
      name: name().trim() || undefined,
      instruction: instruction().trim(),
      schedule_type: scheduleType(),
      schedule,
      timezone: timezone().trim() || 'Asia/Shanghai',
      source_session_id: editingTask()?.source_session_id || props.sourceSessionId || undefined,
      workspace_id: editingTask()?.workspace_id || props.workspaceId || undefined,
      workdir: editingTask()?.workdir || props.workdir || undefined,
    };
  };

  const submitForm = async (event: SubmitEvent) => {
    event.preventDefault();
    setError('');
    setNotice('');
    if (!instruction().trim()) {
      setError('请填写到期后要执行的完整指令。');
      return;
    }
    if (scheduleType() === 'weekly' && weekdays().length === 0) {
      setError('每周任务至少选择一个星期。');
      return;
    }
    setBusy(true);
    try {
      if (editingId()) {
        await updateScheduledTask(editingId(), buildInput(), adminScope());
        setNotice('任务已更新，下一次执行时间已重新计算。');
        resetForm();
        await load(false);
      } else {
        setPreview(await prepareScheduledTask(buildInput()));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存任务失败');
    } finally {
      setBusy(false);
    }
  };

  const confirmCreate = async () => {
    const current = preview();
    if (!current) return;
    setBusy(true);
    setError('');
    try {
      await confirmScheduledTask(current.confirmation_token);
      setNotice('定时任务已创建。');
      resetForm();
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '确认任务失败');
    } finally {
      setBusy(false);
    }
  };

  const beginEdit = (task: ScheduledTask) => {
    setEditorOpen(true);
    setEditingId(task.id);
    setEditingTask(task);
    setName(task.name);
    setInstruction(task.instruction);
    setScheduleType(task.schedule_type);
    setTimezone(task.timezone || 'Asia/Shanghai');
    const schedule = task.schedule || {};
    if (task.schedule_type === 'at' && task.next_run_at_local) {
      setRunAt(task.next_run_at_local.slice(0, 16));
    }
    if (task.schedule_type === 'delay') {
      setDelayAmount(Math.max(1, Number(schedule.delay_seconds || 60) / 60));
      setDelayUnit('minutes');
    }
    if (schedule.time) setClock(String(schedule.time).slice(0, 5));
    if (Array.isArray(schedule.weekdays)) setWeekdays(schedule.weekdays.map(Number));
    if (schedule.day) setMonthDay(Number(schedule.day));
    if (schedule.expression) setCronExpression(String(schedule.expression));
    setPreview(null);
  };

  const runAction = async (task: ScheduledTask, action: 'pause' | 'resume' | 'run' | 'retry') => {
    setError('');
    setNotice('');
    try {
      await scheduledTaskAction(task.id, action, adminScope());
      setNotice(action === 'pause' ? '任务已暂停。' : action === 'resume' ? '任务已恢复。' : '任务已进入执行队列。');
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '任务操作失败');
    }
  };

  const removeTask = async (task: ScheduledTask) => {
    if (!await confirmAction(`确认删除任务“${task.name}”吗？执行历史将保留。`)) return;
    try {
      await deleteScheduledTask(task.id, adminScope());
      if (editingId() === task.id) resetForm();
      setNotice('任务已删除。');
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除任务失败');
    }
  };

  const openHistory = async (task: ScheduledTask) => {
    setHistoryTask(task);
    setRuns([]);
    try {
      setRuns(await listScheduledTaskRuns(task.id, adminScope()));
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载执行记录失败');
    }
  };

  const markRead = async (item: ScheduledNotification) => {
    if (item.is_read) return;
    await markScheduledNotificationRead(item.id);
    setNotifications((current) => current.map((entry) => entry.id === item.id ? { ...entry, is_read: true } : entry));
    setUnreadCount((value) => Math.max(0, value - 1));
  };

  const toggleWeekday = (day: number) => {
    setWeekdays((current) => current.includes(day) ? current.filter((item) => item !== day) : [...current, day].sort());
  };

  const savePersonalTimezone = async () => {
    setError('');
    try {
      const updated = await updateScheduledTaskPreferences(timezone().trim());
      setPersonalTimezone(updated.timezone);
      setNotice(`个人默认时区已设为 ${updated.timezone}。`);
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存默认时区失败');
    }
  };

  return (
    <section class="flex min-w-0 flex-1 flex-col overflow-hidden bg-[#0f1418]/78">
      <div class="border-b border-[#2a3136] bg-[#0d1216]/90 px-5 py-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.24em] text-[#7bd0ff]">自动化与消息</div>
            <h1 class="mt-1 text-xl font-semibold text-[#f7fafc]">定时任务中心</h1>
            <p class="mt-1 text-sm text-[#9faeb7]">持久化执行智能体指令；服务恢复后补发一次遗漏任务。</p>
          </div>
          <div class="flex items-center gap-2">
            <Show when={canAdmin()}>
              <label class="flex h-9 items-center gap-2 rounded-full border border-[#303539] bg-[#171c20] px-3 text-xs text-[#bdc8d1]">
                <input
                  type="checkbox"
                  checked={adminScope()}
                  onChange={(event) => {
                    setAdminScope(event.currentTarget.checked);
                    window.setTimeout(() => void load(true), 0);
                  }}
                />
                租户后台
              </label>
            </Show>
            <button class={buttonClass} onClick={() => { resetForm(); setEditorOpen(true); }}>新建任务</button>
            <button class={buttonClass} onClick={() => void load(true)}>刷新</button>
          </div>
        </div>
        <div class="mt-4 flex gap-2">
          <button class={`${buttonClass} ${tab() === 'tasks' ? '!border-[#38bdf8]/40 !bg-[#38bdf8]/12 !text-[#8ed5ff]' : ''}`} onClick={() => setTab('tasks')}>任务</button>
          <button class={`${buttonClass} ${tab() === 'notifications' ? '!border-[#38bdf8]/40 !bg-[#38bdf8]/12 !text-[#8ed5ff]' : ''}`} onClick={() => setTab('notifications')}>
            消息
            <Show when={unreadCount() > 0}><span class="rounded-full bg-[#ef4444]/85 px-1.5 py-0.5 text-[10px] text-white">{unreadCount()}</span></Show>
          </button>
        </div>
      </div>

      <Show when={error()}>
        <div class="mx-5 mt-4 rounded-xl border border-[#ef4444]/35 bg-[#ef4444]/10 px-4 py-3 text-sm text-[#fca5a5]">{error()}</div>
      </Show>
      <Show when={notice()}>
        <div class="mx-5 mt-4 rounded-xl border border-[#22c55e]/35 bg-[#22c55e]/10 px-4 py-3 text-sm text-[#9ee6bb]">{notice()}</div>
      </Show>

      <Show when={tab() === 'tasks'}>
        <div class="flex min-h-0 flex-1 flex-col overflow-hidden">
          <Show when={editorOpen()}><FeedbackDialog title={editingId() ? '编辑定时任务' : '新建定时任务'} onClose={() => { if (!busy()) setEditorOpen(false); }}>
          <form class="custom-scrollbar max-h-[56dvh] overflow-y-auto p-1" onSubmit={submitForm}>
            <Show when={error()}><p role="alert">{error()}</p></Show>
            <div class="mb-4 flex items-center justify-between">
              <Show when={editingId()}><button type="button" class={buttonClass} disabled={busy()} onClick={resetForm}>取消编辑</button></Show>
            </div>
            <label class="mb-3 block">
              <span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">任务名称</span>
              <input class={inputClass} value={name()} onInput={(e) => setName(e.currentTarget.value)} placeholder="例如：周报生成" />
            </label>
            <label class="mb-3 block">
              <span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">到期后执行的完整指令</span>
              <textarea class="min-h-28 w-full rounded-xl border border-[#303539] bg-[#0f1418] px-3 py-2 text-sm text-[#eef3f6] outline-none focus:border-[#7bd0ff]/55" value={instruction()} onInput={(e) => setInstruction(e.currentTarget.value)} placeholder="写清楚交付内容、范围、格式和可用资料；信息不完整时请先回到聊天中补充。" required />
            </label>
            <div class="mb-3 grid grid-cols-2 gap-3">
              <label>
                <span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">任务类型</span>
                <select class={inputClass} value={scheduleType()} onChange={(e) => setScheduleType(e.currentTarget.value as ScheduleType)}>
                  <option value="delay">延迟任务</option>
                  <option value="at">指定日期</option>
                  <option value="daily">每天</option>
                  <option value="weekly">每周</option>
                  <option value="monthly">每月</option>
                  <option value="weekdays">工作日</option>
                  <option value="cron">Cron</option>
                </select>
              </label>
              <label>
                <span class="mb-1 flex items-center justify-between gap-2 text-xs font-semibold text-[#bdc8d1]">时区<button type="button" class="text-[10px] font-medium text-[#8ed5ff] hover:underline" onClick={() => void savePersonalTimezone()}>设为个人默认</button></span>
                <input class={inputClass} value={timezone()} onInput={(e) => setTimezone(e.currentTarget.value)} />
              </label>
            </div>

            <Show when={scheduleType() === 'delay'}>
              <div class="mb-3 grid grid-cols-2 gap-3">
                <label><span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">延迟</span><input class={inputClass} type="number" min="0.01" step="0.01" value={delayAmount()} onInput={(e) => setDelayAmount(Number(e.currentTarget.value))} /></label>
                <label><span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">单位</span><select class={inputClass} value={delayUnit()} onChange={(e) => setDelayUnit(e.currentTarget.value as any)}><option value="seconds">秒</option><option value="minutes">分钟</option><option value="hours">小时</option><option value="days">天</option></select></label>
              </div>
            </Show>
            <Show when={scheduleType() === 'at'}>
              <label class="mb-3 block"><span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">执行日期与时间</span><input class={inputClass} type="datetime-local" value={runAt()} onInput={(e) => setRunAt(e.currentTarget.value)} /></label>
            </Show>
            <Show when={['daily', 'weekly', 'monthly', 'weekdays'].includes(scheduleType())}>
              <label class="mb-3 block"><span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">执行时间</span><input class={inputClass} type="time" value={clock()} onInput={(e) => setClock(e.currentTarget.value)} /></label>
            </Show>
            <Show when={scheduleType() === 'weekly'}>
              <div class="mb-3"><span class="mb-2 block text-xs font-semibold text-[#bdc8d1]">执行星期</span><div class="flex flex-wrap gap-2"><For each={WEEKDAYS}>{(label, day) => <button type="button" class={`${buttonClass} ${weekdays().includes(day()) ? '!border-[#38bdf8]/40 !bg-[#38bdf8]/12 !text-[#8ed5ff]' : ''}`} onClick={() => toggleWeekday(day())}>{label}</button>}</For></div></div>
            </Show>
            <Show when={scheduleType() === 'monthly'}>
              <label class="mb-3 block"><span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">每月几号</span><input class={inputClass} type="number" min="1" max="31" value={monthDay()} onInput={(e) => setMonthDay(Number(e.currentTarget.value))} /></label>
            </Show>
            <Show when={scheduleType() === 'cron'}>
              <label class="mb-3 block"><span class="mb-1 block text-xs font-semibold text-[#bdc8d1]">Cron 表达式（5 或 6 段）</span><input class={`${inputClass} font-mono`} value={cronExpression()} onInput={(e) => setCronExpression(e.currentTarget.value)} /></label>
            </Show>

            <button class="mt-2 inline-flex h-10 w-full items-center justify-center rounded-xl border border-[#38bdf8]/35 bg-[#38bdf8]/14 text-sm font-semibold text-[#8ed5ff] hover:bg-[#38bdf8]/20 disabled:opacity-50" type="submit" disabled={busy()}>
              {busy() ? '处理中...' : editingId() ? '确认保存修改' : '生成确认预览'}
            </button>

            <Show when={preview()}>
              {(value) => (
                <div class="mt-4 rounded-xl border border-[#f1a02b]/35 bg-[#f1a02b]/8 p-4 text-sm">
                  <div class="font-semibold text-[#ffc176]">创建前确认</div>
                  <div class="mt-3 space-y-1 text-[#bdc8d1]"><div>名称：{value().preview.name || instruction().slice(0, 30)}</div><div>类型：{value().preview.schedule_type}</div><div>时区：{value().preview.timezone}</div><div>首次执行：{value().preview.next_run_at ? formatTimestamp(value().preview.next_run_at) : '—'}</div></div>
                  <div class="mt-3 whitespace-pre-wrap rounded-lg bg-[#0f1418] p-3 text-xs text-[#dee3e8]">{value().preview.instruction}</div>
                  <div class="mt-3 flex gap-2"><button type="button" class="inline-flex h-9 flex-1 items-center justify-center rounded-lg border border-[#22c55e]/35 bg-[#22c55e]/12 font-semibold text-[#9ee6bb]" disabled={busy()} onClick={() => void confirmCreate()}>确认创建</button><button type="button" class={buttonClass} onClick={() => setPreview(null)}>返回修改</button></div>
                </div>
              )}
            </Show>
          </form>
          <div class="feedback-dialog-actions"><button type="button" disabled={busy()} onClick={() => setEditorOpen(false)}>返回任务列表</button></div>
          </FeedbackDialog></Show>

          <div class="custom-scrollbar min-h-0 flex-1 overflow-y-auto p-5" aria-label="定时任务列表">
            <Show when={!loading()} fallback={<div class="text-sm text-[#9faeb7]">正在加载定时任务...</div>}>
              <Show when={tasks().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] p-10 text-center text-sm text-[#7f8b93]">还没有定时任务。你也可以直接在聊天中说“一分钟后和我打个招呼”。</div>}>
                <div class="space-y-3">
                  <For each={tasks()}>{(task) => (
                    <article class="rounded-2xl border border-[#2a3136] bg-[#151b1f]/92 p-4">
                      <div class="flex flex-wrap items-start justify-between gap-3">
                        <div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><h3 class="font-semibold text-[#eef3f6]">{task.name}</h3><span class={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${statusClass(task.status)}`}>{task.claimed ? '执行中' : statusLabel(task.status)}</span><Show when={adminScope()}><span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#9faeb7]">{task.user_id}</span></Show></div><div class="mt-1 text-xs text-[#8ed5ff]">{scheduleSummary(task)}</div></div>
                        <div class="text-right text-[11px] text-[#7f8b93]"><div>下次：{formatTimestamp(task.next_run_at)}</div><div>上次：{task.last_status || '尚未执行'}</div></div>
                      </div>
                      <p class="mt-3 whitespace-pre-wrap text-sm text-[#bdc8d1]">{task.instruction}</p>
                      <Show when={task.last_error}><div class="mt-3 rounded-lg border border-[#ef4444]/25 bg-[#ef4444]/8 px-3 py-2 text-xs text-[#fca5a5]">{task.last_error}</div></Show>
                      <div class="mt-4 flex flex-wrap gap-2">
                        <Show when={task.status === 'active' || task.status === 'paused'}><button class={buttonClass} disabled={task.claimed} onClick={() => void runAction(task, task.status === 'paused' ? 'resume' : 'pause')}>{task.status === 'paused' ? '恢复' : '暂停'}</button></Show>
                        <button class={buttonClass} disabled={task.claimed} onClick={() => void runAction(task, 'run')}>立即运行</button>
                        <Show when={task.last_status === 'failed' || task.status === 'failed'}><button class={`${buttonClass} !border-[#f1a02b]/35 !text-[#ffc176]`} disabled={task.claimed} onClick={() => void runAction(task, 'retry')}>手动重试</button></Show>
                        <button class={buttonClass} disabled={task.claimed} onClick={() => beginEdit(task)}>编辑</button>
                        <button class={buttonClass} onClick={() => void openHistory(task)}>执行记录</button>
                        <button class={`${buttonClass} !border-[#ef4444]/30 !text-[#fca5a5]`} disabled={task.claimed} onClick={() => void removeTask(task)}>删除</button>
                      </div>
                    </article>
                  )}</For>
                </div>
              </Show>
            </Show>
          </div>
        </div>
      </Show>

      <Show when={tab() === 'notifications'}>
        <div class="custom-scrollbar min-h-0 flex-1 overflow-y-auto p-5">
          <div class="mb-4 flex items-center justify-between"><h2 class="font-semibold text-[#eef3f6]">站内消息</h2><button class={buttonClass} disabled={unreadCount() === 0} onClick={() => void markAllScheduledNotificationsRead().then(() => load(false))}>全部已读</button></div>
          <Show when={notifications().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] p-10 text-center text-sm text-[#7f8b93]">暂无任务消息。</div>}>
            <div class="space-y-3"><For each={notifications()}>{(item) => <button type="button" class={`block w-full rounded-2xl border p-4 text-left ${item.is_read ? 'border-[#2a3136] bg-[#151b1f]/70' : 'border-[#38bdf8]/30 bg-[#38bdf8]/8'}`} onClick={() => void markRead(item)}><div class="flex items-start justify-between gap-3"><div class="font-semibold text-[#eef3f6]">{item.title}</div><div class="shrink-0 text-[11px] text-[#7f8b93]">{formatTimestamp(item.created_at)}</div></div><div class="mt-2 whitespace-pre-wrap text-sm text-[#bdc8d1]">{item.content}</div></button>}</For></div>
          </Show>
        </div>
      </Show>

      <Show when={historyTask()}>
        {(task) => <div class="fixed inset-0 z-[80] flex items-center justify-center bg-black/65 p-4" onClick={() => setHistoryTask(null)}><div class="custom-scrollbar max-h-[82vh] w-full max-w-3xl overflow-y-auto rounded-2xl border border-[#303539] bg-[#11171b] p-5 shadow-2xl" onClick={(event) => event.stopPropagation()}><div class="mb-4 flex items-center justify-between gap-3"><div><div class="text-xs text-[#7bd0ff]">执行记录</div><h2 class="mt-1 text-lg font-semibold text-[#f7fafc]">{task().name}</h2></div><button class={buttonClass} onClick={() => setHistoryTask(null)}>关闭</button></div><Show when={runs().length > 0} fallback={<div class="text-sm text-[#7f8b93]">尚无执行记录。</div>}><div class="space-y-3"><For each={runs()}>{(run) => <div class="rounded-xl border border-[#2a3136] bg-[#0f1418] p-4"><div class="flex flex-wrap items-center justify-between gap-2"><span class={`rounded-full border px-2 py-0.5 text-[10px] ${run.status === 'succeeded' ? 'border-[#22c55e]/35 text-[#9ee6bb]' : run.status === 'failed' ? 'border-[#ef4444]/35 text-[#fca5a5]' : 'border-[#38bdf8]/35 text-[#8ed5ff]'}`}>{run.status}</span><span class="text-[11px] text-[#7f8b93]">{formatTimestamp(run.started_at)} · {run.trigger}</span></div><Show when={run.output}><div class="mt-3 whitespace-pre-wrap text-sm text-[#bdc8d1]">{run.output}</div></Show><Show when={run.error}><div class="mt-3 whitespace-pre-wrap text-sm text-[#fca5a5]">{run.error}</div></Show></div>}</For></div></Show></div></div>}
      </Show>
    </section>
  );
}
