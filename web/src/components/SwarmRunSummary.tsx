import { Show } from 'solid-js';

export default function SwarmRunSummary(props: { metadata?: Record<string, any> }) {
  const isSwarm = () => ['swarm', 'auto_swarm_builder'].includes(props.metadata?.scheduler_mode);
  const count = () => {
    const value = props.metadata?.participant_count;
    return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
  };
  const fallback = () => typeof props.metadata?.fallback_reason === 'string' ? props.metadata.fallback_reason : '';
  return <Show when={isSwarm() && (count() !== null || fallback())}>
    <div class="border-t border-[#2a3136] px-4 py-2 text-xs leading-5 text-[#9faeb7]" aria-label="蜂群执行摘要">
      <Show when={count() !== null}>
        <div>蜂群实际参与：{count()} 个成员{count() === 1 ? '（单成员执行，未发生多成员协作）' : ''}</div>
      </Show>
      <Show when={fallback()}><div role="status">已回退为普通蜂群：{fallback()}</div></Show>
    </div>
  </Show>;
}
