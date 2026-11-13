import type { autoSwarmAvailability } from '../../utils/schedulerCapabilities';

export default function SchedulerOptions(props: { autoSwarm: ReturnType<typeof autoSwarmAvailability> }) {
  return <>
    <option value="supervisor">监督式</option>
    <option value="chain">链式</option>
    <option value="swarm">群体</option>
    <option value="auto_swarm_builder" disabled={!props.autoSwarm.available} title={props.autoSwarm.reason || undefined}>
      自动群体构建{props.autoSwarm.available ? '' : '（不可用）'}
    </option>
  </>;
}
