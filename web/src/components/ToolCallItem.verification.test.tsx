import { renderToString } from 'solid-js/web';
import { expect, it, vi } from 'vitest';

vi.mock('../stores/session', () => ({ sessionStore: {
  isMessageToolDetailsCollapsed: () => false,
  setMessageToolDetailsCollapsed: vi.fn(),
} }));
vi.mock('../api/client', () => ({}));
vi.mock('./MarkdownContent', () => ({ default: () => null }));
import { ToolCallItem } from './MessageItem';

it.each(['live', 'history'])('shows a natural pending receipt for %s with one tool card', (mode) => {
  const tool = { tool_name: 'computer_press_key', call_id: 'pending-1', arguments: {},
    state: mode === 'live' ? 'completed' : 'error',
    error: mode === 'history' ? 'effect_unverifiable' : null,
    metadata: mode === 'live' ? { computer_verification_pending: true } : {
      known_action_receipt: true, computer_audit: { status: 'failed_known', error_code: 'effect_unverifiable' },
    },
  };
  const html = renderToString(() => <ToolCallItem messageId="test" tool={tool as any} index={0} />);
  expect(html).toContain('已发送');
  expect(html).toContain('操作已发送；此条记录不代表任务完成，实际效果请查看后续观察。');
  expect(html.match(/键盘操作/g)).toHaveLength(1);
  expect(html).not.toContain('computer_press_key');
  expect(html).not.toContain('effect_unverifiable');
  expect(html).not.toContain('bg-[#2d1418]');
});
