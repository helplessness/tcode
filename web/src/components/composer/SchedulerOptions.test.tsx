import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import SchedulerOptions from './SchedulerOptions';
import { autoSwarmAvailability } from '../../utils/schedulerCapabilities';

describe('scheduler availability', () => {
  it.each([undefined, {}, { schedulerModes: {} }])('disables automatic building until capability is confirmed (%j)', capabilities => {
    const html = renderToString(() => <select><SchedulerOptions autoSwarm={autoSwarmAvailability(capabilities)} /></select>);
    expect(html).toMatch(/value="auto_swarm_builder"[^>]*disabled/);
    expect(html).toContain('尚未确认');
  });

  it('shows the service reason while keeping the other schedulers available', () => {
    const html = renderToString(() => <select><SchedulerOptions autoSwarm={autoSwarmAvailability({
      schedulerModes: { auto_swarm_builder: { available: false, reason: '当前服务未安装 Swarms' } },
    })} /></select>);
    expect(html).toMatch(/value="auto_swarm_builder"[^>]*disabled/);
    expect(html).toContain('当前服务未安装 Swarms');
    for (const mode of ['supervisor', 'chain', 'swarm']) {
      expect(html.match(new RegExp(`<option[^>]*value="${mode}"[^>]*>`))?.[0]).not.toContain('disabled');
    }
  });

  it('enables automatic building when the service confirms availability', () => {
    const html = renderToString(() => <select><SchedulerOptions autoSwarm={autoSwarmAvailability({
      schedulerModes: { auto_swarm_builder: { available: true, reason: '' } },
    })} /></select>);
    expect(html).not.toContain('disabled');
    expect(html).not.toContain('不可用');
    expect(html).not.toContain('尚未确认');
  });
});
