import { For, Show, createEffect, createSignal, createUniqueId, onMount, onCleanup, type Accessor } from 'solid-js';
import { sessionStore } from '../../stores/session';
import { isWorkflowModeAgent } from '../../utils/teamRegistry';
import { agentDisplayLabel, reasoningEffortLabel, reasoningEffortOptions } from './executionLabels';
import { autoSwarmAvailability } from '../../utils/schedulerCapabilities';
import SchedulerOptions from './SchedulerOptions';

type ModelOption = { id: string; provider_id: string; model_id: string; name: string };
type RuntimeOption = { runtime_id: string; display_name: string; available: boolean; enabled: boolean; capabilities?: Record<string, unknown> };

type ExecutionSettingsProps = {
  goalControlRef?: (element: HTMLDivElement) => void;
  agents: Accessor<Array<{ name: string; description: string; mode: string }>>;
  handleAgentChange: (name: string) => Promise<void>;
  handleReasoningEffortChange: (effort: 'minimal' | 'low' | 'medium' | 'high') => void;
  runtime: {
    selected: Accessor<string>;
    available: Accessor<RuntimeOption[]>;
    error: Accessor<string>;
    pending: Accessor<boolean>;
    onChange: (id: string) => Promise<void>;
  };
  model: {
    selected: Accessor<string>;
    available: Accessor<ModelOption[]>;
    visible: Accessor<ModelOption[]>;
    error: Accessor<string>;
    pending: Accessor<boolean>;
    onChange: (id: string) => Promise<void>;
    onRefresh?: () => void;
  };
};

export default function ExecutionSettings(props: ExecutionSettingsProps) {
  const [showExecution, setShowExecution] = createSignal(false);
  const panelId=createUniqueId();
  let root!:HTMLDivElement;
  let trigger!:HTMLButtonElement;
  let modelSelectRef: HTMLSelectElement | undefined;
  onMount(()=>{
    const pane = root.closest('.composer-input-card');
    const details = root.querySelector<HTMLDetailsElement>('.composer-model-slot')!;
    let compact: boolean | undefined;
    const observer = new ResizeObserver(() => {
      const next = (pane?.clientWidth ?? window.innerWidth) - 24 <= 420;
      if (next !== compact) details.open = !next;
      compact = next;
    });
    if (pane) observer.observe(pane);
    else details.open = true;
    onCleanup(() => observer.disconnect());
    const dismiss=(event:PointerEvent)=>{if(!root.contains(event.target as Node))setShowExecution(false);};
    document.addEventListener('pointerdown',dismiss);
    onCleanup(()=>document.removeEventListener('pointerdown',dismiss));
  });

  createEffect(() => {
    const model = props.model.selected();
    props.model.visible();
    queueMicrotask(() => {
      if (modelSelectRef && model && modelSelectRef.value !== model) modelSelectRef.value = model;
    });
  });

  const isNativeHarness = () => props.runtime.selected() === 'tcode-native';
  const showScheduler = () => {
    const name = sessionStore.selectedAgentName();
    const agent = props.agents().find(item => item.name === name);
    return isNativeHarness() && name !== 'manager' && agent?.mode !== 'manager' && !isWorkflowModeAgent(name);
  };
  const nativeCapabilities = () => props.runtime.available().find(item => item.runtime_id === 'tcode-native')?.capabilities;
  const autoSwarm = () => autoSwarmAvailability(nativeCapabilities());
  const [schedulerReset, setSchedulerReset] = createSignal(false);
  createEffect(() => {
    if (nativeCapabilities()?.schedulerModes && showScheduler() && !sessionStore.isRunning()
      && !autoSwarm().available && sessionStore.selectedSchedulerMode() === 'auto_swarm_builder') {
      sessionStore.setSelectedSchedulerMode('supervisor');
      setSchedulerReset(true);
    }
  });

  return (
    <div ref={root} class="composer-execution-settings" onKeyDown={event=>{if(event.key==='Escape'&&showExecution()){event.preventDefault();event.stopPropagation();setShowExecution(false);trigger.focus();}}}>
      <div class="composer-execution-controls">
        <details class="composer-model-slot">
          <summary aria-label="展开模型选择" title="模型与推理强度">模型</summary>
          <div class="composer-model-fields">
        <select
          ref={modelSelectRef}
          aria-label="模型"
          title={props.model.selected() || '选择模型'}
          class="select select-sm h-8 w-[190px] min-w-0 shrink px-2 text-[11px] backdrop-blur-md sm:w-[220px]"
          style={{ background: 'color-mix(in srgb, var(--tcode-surface) 72%, transparent)', color: 'var(--tcode-text)', 'border-color': 'var(--tcode-border)' }}
          value={props.model.selected()}
          onFocus={() => props.model.onRefresh?.()}
          onChange={(event) => void props.model.onChange(event.currentTarget.value)}
          disabled={sessionStore.isRunning() || props.model.pending() || !sessionStore.currentSessionId() || props.model.available().length === 0}
        >
          <For each={props.model.visible().length > 0 ? props.model.visible() : [{ id: '', provider_id: '', model_id: '', name: props.model.error() ? '模型加载失败' : '无可用模型' }]}>
            {(model) => <option value={model.id}>{model.id || model.name}</option>}
          </For>
        </select>

        <select
          aria-label="推理强度"
          class="select select-sm h-8 w-[96px] px-2 text-[11px] backdrop-blur-md"
          style={{ background: 'color-mix(in srgb, var(--tcode-surface) 72%, transparent)', color: 'var(--tcode-text)', 'border-color': 'var(--tcode-border)' }}
          value={sessionStore.selectedReasoningEffort()}
          onChange={(event) => props.handleReasoningEffortChange(event.currentTarget.value as 'minimal' | 'low' | 'medium' | 'high')}
          disabled={sessionStore.isRunning()}
        >
          <For each={reasoningEffortOptions}>
            {(effort) => <option value={effort}>{reasoningEffortLabel(effort)}</option>}
          </For>
        </select>

          </div>
        </details>

        <button
          ref={trigger}
          type="button"
          aria-label="高级配置"
          aria-controls={panelId}
          aria-haspopup="true"
          aria-expanded={showExecution()}
          class={`h-8 shrink-0 rounded-md border px-2.5 text-[11px] font-medium transition-colors ${showExecution() ? 'border-[#66727b] bg-[#20272c]/70 text-white' : 'border-[#485159]/55 bg-[#0f1418]/30 text-[#aebbc4] hover:border-[#66727b] hover:text-white'}`}
          style={{ background: 'color-mix(in srgb, var(--tcode-surface) 68%, transparent)', color: 'var(--tcode-text)', 'border-color': 'var(--tcode-border)' }}
          onClick={() => setShowExecution((visible) => !visible)}
        >
          <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 17h16M9 4v6M15 14v6"/></svg><span>配置</span>
        </button>
        <Show when={props.goalControlRef}><div ref={props.goalControlRef} class="ml-auto shrink-0" /></Show>
      </div>

      <Show when={props.model.error()}>
        <div class="composer-model-error text-[11px] text-[#fca5a5]" role="status">{props.model.error()}</div>
      </Show>

      <Show when={showExecution()}>
        <div id={panelId} role="region" aria-label="高级执行配置" class="composer-execution-popover grid gap-2 rounded-lg border p-2.5 backdrop-blur-xl md:grid-cols-3" style={{ background: 'var(--tcode-surface)', 'border-color': 'var(--tcode-border)' }}>
          <label class="min-w-0">
            <span class="mb-1 block text-[10px] uppercase tracking-[0.14em] text-[#7f8b93]">Harness</span>
            <select
              data-testid="harness-runtime-select"
              class="select select-sm w-full border-[#2a3136] bg-[#0b1013] text-[#e6edf3]"
              value={props.runtime.selected()}
              onChange={(event) => void props.runtime.onChange(event.currentTarget.value)}
              disabled={sessionStore.isRunning() || props.runtime.pending()}
            >
              <For each={props.runtime.available()}>
                {(runtime) => (
                  <option value={runtime.runtime_id} disabled={!runtime.available || !runtime.enabled}>
                    {runtime.display_name}{!runtime.available ? '（未安装）' : !runtime.enabled ? '（未启用）' : ''}
                  </option>
                )}
              </For>
            </select>
          </label>

          <label class="min-w-0">
            <span class="mb-1 block text-[10px] uppercase tracking-[0.14em] text-[#7f8b93]">模式</span>
            <select
              class="select select-sm w-full border-[#2a3136] bg-[#0b1013] text-[#e6edf3]"
              value={sessionStore.selectedAgentName()}
              onChange={(event) => void props.handleAgentChange(event.currentTarget.value)}
              disabled={sessionStore.isRunning()}
            >
              <For each={props.agents().length > 0 ? props.agents() : [{ name: 'build', description: '', mode: 'primary' }]}>
                {(agent) => <option value={agent.name}>{agentDisplayLabel(agent.name)}</option>}
              </For>
            </select>
          </label>

          <Show when={showScheduler()}>
            <label class="min-w-0">
              <span class="mb-1 block text-[10px] uppercase tracking-[0.14em] text-[#7f8b93]">调度器</span>
              <select
                class="select select-sm w-full border-[#2a3136] bg-[#0b1013] text-[#e6edf3]"
                value={sessionStore.selectedSchedulerMode()}
                onChange={(event) => sessionStore.setSelectedSchedulerMode(event.currentTarget.value as 'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder')}
                disabled={sessionStore.isRunning()}
              >
                <SchedulerOptions autoSwarm={autoSwarm()} />
              </select>
              <Show when={!autoSwarm().available}>
                <span class="mt-1 block text-[11px] leading-5 text-[#9faeb7]">
                  自动群体构建不可用：{autoSwarm().reason}{schedulerReset() && sessionStore.selectedSchedulerMode() === 'supervisor' ? ' 已切换为监督式。' : ''}
                </span>
              </Show>
            </label>
          </Show>

          <Show when={props.runtime.error()}>
            <div class="text-[11px] text-[#fca5a5] md:col-span-3">{props.runtime.error()}</div>
          </Show>

        </div>
      </Show>
    </div>
  );
}
