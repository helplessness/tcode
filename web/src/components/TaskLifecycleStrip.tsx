import { createMemo, For } from 'solid-js';
import { sessionStore } from '../stores/session';
import { deriveTaskLifecyclePhase, taskLifecycleLabels, type TaskLifecyclePhase } from '../utils/taskLifecycle';

const stages: TaskLifecyclePhase[] = ['clarifying', 'planning', 'executing', 'awaiting_review', 'completed'];

export default function TaskLifecycleStrip(props:{compact?:boolean}={}) {
  const phase = createMemo(() => {
    const session = sessionStore.currentSession();
    const runtime = sessionStore.getSessionRuntimeState(session?.session_id || '');
    return deriveTaskLifecyclePhase({
      session,
      isRunning: runtime.deliveryTurnRunning ?? runtime.isRunning,
      hasQuestion: Boolean(runtime.questionRequest),
      hasPermissionRequest: Boolean(runtime.permissionRequest),
    });
  });
  const activeIndex = createMemo(() => {
    if (phase() === 'awaiting_authorization') return 1;
    if (phase() === 'revision_requested') return 3;
    if (['blocked', 'failed', 'cancelled'].includes(phase())) return 2;
    return Math.max(0, stages.indexOf(phase()));
  });
  const strip=()=><div class="task-lifecycle" aria-label={`任务阶段：${taskLifecycleLabels[phase()]}`} data-phase={phase()}>
    <For each={stages}>{(stage, index) => <span data-state={index() < activeIndex() ? 'done' : index() === activeIndex() ? 'active' : 'future'}>
      <i aria-hidden="true">{index() < activeIndex() ? '✓' : index() + 1}</i>{index() === activeIndex() ? taskLifecycleLabels[phase()] : taskLifecycleLabels[stage]}
    </span>}</For>
  </div>;
  return props.compact?<details class="task-phase-menu" data-phase={phase()}>
    <summary aria-label={`任务阶段：${taskLifecycleLabels[phase()]}，查看完整进度`}><span class="task-phase-dot" aria-hidden="true"/>{taskLifecycleLabels[phase()]}<svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></summary>
    {strip()}
  </details>:strip();
}
