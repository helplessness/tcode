import { renderToString } from 'solid-js/web';
import { expect, it, vi } from 'vitest';
import type { Message } from '../stores/session';

const preferences = vi.hoisted(() => ({ collapsed: undefined as boolean | undefined }));
vi.mock('../stores/session', () => ({ sessionStore: {
  messageToolsCollapsePreference: () => preferences.collapsed,
  setMessageToolsCollapsed: vi.fn(),
} }));
vi.mock('../api/client', () => ({}));
vi.mock('./MessageItem', () => ({ default: () => null, ToolCallItem: () => { throw new Error('Main tool renderer must not run in side chat'); } }));
vi.mock('./MarkdownContent', () => ({ default: (props: any) => <p data-session-actions={String(props.allowSessionActions)}>{props.content}</p> }));
vi.mock('./StructuredContentBlocks', () => ({ default: (props: any) => <span data-session-actions={String(props.allowSessionActions)}/> }));
vi.mock('./BrowserLivePanel', () => ({ default: () => null }));
vi.mock('./composer/InteractionPanel', () => ({ default: () => null }));

import SideChatTranscript from './SideChatTranscript';

const messages: Message[] = [
  {id:'question',role:'user',content:'请检查输入'},
  {id:'narration',role:'assistant',content:'先读文件，再确认结果',presentation:'commentary',
    tool_calls:[{tool_name:'read',call_id:'read-1',arguments:{},state:'running'}]},
  {id:'tool-result',role:'tool',content:'文件内容',
    tool_calls:[{tool_name:'read',call_id:'read-1',arguments:{},state:'completed',output:'文件内容'}]},
  {id:'old-tool',role:'tool',content:'旧格式工具结果'},
  {id:'answer',role:'assistant',content:'输入已确认'},
];

it('uses one shared narration/tool group per turn, with user on the right and Agent on the left', () => {
  preferences.collapsed = false;
  const html = renderToString(() => <SideChatTranscript messages={messages} running={false} sessionId="side-one"
    renderTool={entry => <span>{`${entry.tool.tool_name}:${entry.tool.output}`}</span>}/>);
  expect(html.replace(/aria-label="[^"]*"/g, '').match(/旁白/g)).toHaveLength(1);
  expect(html.match(/运行工具/g)).toHaveLength(1);
  expect(html.match(/read:文件内容/g)).toHaveLength(1);
  expect(html).toContain('工具输出:旧格式工具结果');
  expect(html).toContain('side-chat-bubble--user');
  expect(html).not.toContain('>你<');
  expect(html).toContain('请检查输入');
  expect(html).toContain('side-chat-bubble--agent');
  expect(html).toContain('先读文件，再确认结果');
  expect(html).toContain('输入已确认');
  expect(html).toContain('max-h-[min(36rem,60vh)]');
  expect(html).toContain('max-h-[min(25rem,60vh)]');
  expect(html.match(/overflow-y-auto overscroll-x-contain overscroll-y-auto/g)).toHaveLength(2);
  expect(html).not.toContain('data-session-actions="true"');
});

it.each([true, false])('shows folded tools above open narration (running=%s)', (running) => {
  preferences.collapsed = undefined;
  const html = renderToString(() => <SideChatTranscript messages={messages.slice(0,-1)} running={running} sessionId="side-two"
    renderTool={entry => <span>{`${entry.tool.tool_name}:${entry.tool.output}`}</span>}/>);
  expect(html).toContain('aria-expanded="false"');
  expect(html.indexOf('运行工具')).toBeLessThan(html.indexOf('旁白'));
  expect(html.match(/aria-expanded="(true|false)"/g)).toEqual(['aria-expanded="false"', 'aria-expanded="true"']);
  expect(html).toContain('请检查输入');
  expect(html).toContain('先读文件，再确认结果');
  expect(html).not.toContain('read:文件内容');
});

it('folds narration after the side chat result arrives', () => {
  preferences.collapsed = undefined;
  const html = renderToString(() => <SideChatTranscript messages={messages} running={false} sessionId="completed"
    renderTool={entry => <span>{entry.tool.tool_name}</span>}/>);
  expect(html.match(/aria-expanded="(true|false)"/g)).toEqual(['aria-expanded="false"', 'aria-expanded="false"']);
  expect(html).not.toContain('先读文件，再确认结果');
  expect(html).toContain('输入已确认');
});
