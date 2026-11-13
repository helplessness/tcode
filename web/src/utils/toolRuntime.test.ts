import { describe, expect, it } from 'vitest';

import { getToolRuntimeSummary } from './toolRuntime';

describe('tool runtime summary', () => {
  it('shows a blocked direct browser call as not executed', () => {
    const summary = getToolRuntimeSummary({
      tool_name: 'browser',
      metadata: {
        execution_skipped: true,
        execution_environment: {
          location: 'not_executed',
          sandboxed: false,
        },
        required_execution_environment: {
          location: 'sandbox',
          sandboxed: true,
          agent: 'browser',
          image: 'tcode-browser:playwright',
          network: 'egress',
        },
      },
    });

    expect(summary?.badge).toBe('未执行');
    expect(summary?.rows).toContainEqual({ label: '要求代理', value: 'browser' });
    expect(summary?.rows).toContainEqual({ label: '镜像', value: 'tcode-browser:playwright' });
    expect(summary?.rows).toContainEqual({ label: '网络', value: 'egress' });
    expect(summary?.rows).not.toContainEqual({ label: '执行环境', value: '宿主机' });
  });

  it('shows delegated browser execution as host to sandbox', () => {
    const summary = getToolRuntimeSummary({
      tool_name: 'task',
      metadata: {
        delegated_execution: true,
        delegated_execution_environment: {
          location: 'sandbox',
          sandboxed: true,
          source_agent_name: 'browser',
          source_tool_name: 'browser',
          image: 'tcode-browser:playwright',
          network: 'egress',
        },
      },
    });

    expect(summary?.badge).toBe('Host -> Sandbox');
    expect(summary?.rows).toContainEqual({ label: '子代理', value: 'browser' });
    expect(summary?.rows).toContainEqual({ label: '镜像', value: 'tcode-browser:playwright' });
    expect(summary?.rows).toContainEqual({ label: '网络', value: 'egress' });
  });
});
