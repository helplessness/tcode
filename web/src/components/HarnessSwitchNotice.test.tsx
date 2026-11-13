import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';

import HarnessSwitchNotice from './HarnessSwitchNotice';

describe('Harness switch notice', () => {
  it('renders persistent memory risk, fallback, and truncation details', () => {
    const html = renderToString(() => (
      <HarnessSwitchNotice
        state={{
          runtime_id: 'opencode',
          model_id: 'deepseek/deepseek-v4-flash',
          switch_version: 2,
          pending_handoff_id: 'handoff-1',
          handoff_notice_pending: true,
          summary_status: 'fallback',
          truncated: true,
        }}
        onDismiss={() => undefined}
      />
    ));

    expect(html).toContain('data-testid="harness-switch-notice"');
    expect(html).toContain('已切换到');
    expect(html).toContain('opencode');
    expect(html).toContain('私有记忆');
    expect(html).toContain('基础上下文');
    expect(html).toContain('较早原文已裁剪');
    expect(html).not.toContain('&lt;dialog');
  });
});
