import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import SwarmRunSummary from './SwarmRunSummary';
import { sanitizeToolCall } from '../stores/session';

describe('actual swarm execution summary', () => {
  it('renders the successful auto-builder count without a fallback warning', () => {
    const html = renderToString(() => <SwarmRunSummary metadata={{
      scheduler_mode: 'auto_swarm_builder', engine: 'swarms_auto_swarm_builder', participant_count: 2,
    }} />).replace(/<!--[\s\S]*?-->/g, '');
    expect(html).toContain('蜂群实际参与：2 个成员');
    expect(html).not.toContain('已回退');
  });
  it.each([0, 1, 2])('renders the persisted actual count (%s)', count => {
    const tool = sanitizeToolCall({ tool_name: 'task', state: 'completed', metadata: {
      scheduler_mode: 'swarm', participant_count: count,
    } });
    const html = renderToString(() => <SwarmRunSummary metadata={tool?.metadata} />).replace(/<!--[\s\S]*?-->/g, '');
    expect(html).toContain(`蜂群实际参与：${count} 个成员`);
    expect(html.includes('单成员执行')).toBe(count === 1);
  });

  it('keeps fallback visible with the actual count', () => {
    const html = renderToString(() => <SwarmRunSummary metadata={{
      scheduler_mode: 'auto_swarm_builder', effective_scheduler_mode: 'swarm',
      participant_count: 1, fallback_reason: '工具权限约束',
    }} />);
    expect(html).toContain('已回退为普通蜂群');
    expect(html).toContain('工具权限约束');
  });

  it('does not invent a member count when the runtime has not reported one', () => {
    const html = renderToString(() => <SwarmRunSummary metadata={{ scheduler_mode: 'swarm' }} />);
    expect(html).not.toContain('蜂群实际参与');
  });
});
