import { renderToString } from 'solid-js/web';
import { beforeEach, expect, it, vi } from 'vitest';

const preferences = vi.hoisted(() => ({ collapsed: undefined as boolean | undefined }));
beforeEach(() => { preferences.collapsed = false; });

vi.mock('../stores/session', () => ({ sessionStore: {
  messages: () => [],
  messageToolsCollapsePreference: () => preferences.collapsed,
  setMessageToolsCollapsed: vi.fn(),
} }));
vi.mock('../api/client', () => ({}));
vi.mock('./MessageItem', () => ({
  default: (props: any) => <p>{props.message.content}</p>,
  ToolCallItem: (props: any) => <span>{props.tool.tool_name}</span>,
}));
vi.mock('./MarkdownContent', () => ({ default: (props: any) => <p>{props.content}</p> }));
vi.mock('./BrowserLivePanel', () => ({ default: () => null }));
vi.mock('./composer/InteractionPanel', () => ({ default: () => null }));

import { MainChatTurn, MainProcessGroup } from './ChatView';

it('humanizes streamed and persisted commentary without rewriting source messages', () => {
  const message = { id: 'catalog', role: 'assistant' as const,
    content: '我调用 capability_catalog 确认工具；计算尚未提交。', presentation: 'commentary' };
  for (const running of [true, false]) {
    const html = renderToString(() => <MainProcessGroup
      preferenceId="catalog" running={running} messages={[message]} />);
    expect(html).not.toContain('capability_catalog');
    expect(html).toContain('查找可用能力');
    expect(html).toContain('计算尚未提交');
  }
  expect(message.content).toContain('capability_catalog');
});

it('renders narration and tools in separate expandable regions and hides template notes', () => {
  const html = renderToString(() => <MainProcessGroup
    preferenceId="turn-1"
    running={true}
    messages={[
      { id: 'first', role: 'assistant', content: '**第一步**：先确认输入', presentation: 'commentary' },
      { id: 'first', role: 'assistant', content: '**第一步**：先确认输入', presentation: 'commentary' },
      {
        id: 'stage', role: 'assistant', content: '第二步：发现配置缺失，调整检查路径', presentation: 'workflow_narration',
        tool_calls: [{ tool_name: 'read_config', call_id: 'read-1', arguments: {}, state: 'running' }],
        process_notes: [{ id: 'note', message: '模板过程摘要', timestamp: 1 }],
      },
      {
        id: 'result', role: 'assistant', content: '',
        tool_calls: [{ tool_name: 'read_config', call_id: 'read-1', arguments: {}, state: 'completed' }],
      },
    ]}
  />);
  expect(html.match(/行动说明/g)).toHaveLength(1);
  expect(html).toContain('先确认输入');
  expect(html).toContain('发现配置缺失，调整检查路径');
  expect(html).not.toMatch(/第一步|第二步|编排器/);
  expect(html).toContain('read_config');
  expect(html.match(/read_config/g)).toHaveLength(1);
  expect(html).toContain('运行工具');
  expect(html).toContain('运行工具 · 1');
  expect(html).toContain('process-disclosure-hint');
  expect(html.indexOf('运行工具')).toBeLessThan(html.indexOf('行动说明'));
  expect(html.match(/aria-expanded="true"/g)).toHaveLength(2);
  expect(html).not.toContain('模板过程摘要');
});

it.each([true, false])('defaults to tools folded and narration open (running=%s)', (running) => {
  preferences.collapsed = undefined;
  const html = renderToString(() => <MainProcessGroup
    preferenceId="defaults" running={running}
    messages={[{ id: 'step', role: 'assistant', content: '先确认输入', presentation: 'commentary',
      tool_calls: [{ tool_name: 'read_config', arguments: {}, state: 'running' }] }]}
  />);
  expect(html.indexOf('运行工具')).toBeLessThan(html.indexOf('行动说明'));
  expect(html.match(/aria-expanded="(true|false)"/g)).toEqual(['aria-expanded="false"', 'aria-expanded="true"']);
  expect(html).toContain('先确认输入');
  expect(html).not.toContain('read_config');
});

it('folds narration when the main turn has a result, preserving the final answer', () => {
  preferences.collapsed = undefined;
  const html = renderToString(() => <MainChatTurn latestAssistantMessageId="answer" turn={{
    key: 'completed', running: false,
    processMessages: [{id: 'step', role: 'assistant', content: '检查输入', presentation: 'commentary'}],
    result: {id: 'answer', role: 'assistant', content: '最终结果'},
  }}/>);
  expect(html).toContain('aria-expanded="false"');
  expect(html).not.toContain('检查输入');
  expect(html).toContain('最终结果');
});

it('bounds expanded tool and narration regions without truncating their contents', () => {
  const narration = Array.from({ length: 45 }, (_, i) => `旁白第 ${i + 1} 行`).join('\n');
  const html = renderToString(() => <MainProcessGroup preferenceId="scrolling" running={false}
    messages={[{id: 'long', role: 'assistant', content: narration, presentation: 'commentary',
      tool_calls: Array.from({length: 25}, (_, i) => ({tool_name: `tool_${i}`, call_id: `call_${i}`, arguments: {}, state: 'completed'}))}]}/>);
  expect(html).toContain('max-h-[min(36rem,60vh)]');
  expect(html).toContain('max-h-[min(25rem,60vh)]');
  expect(html.match(/overflow-y-auto overscroll-x-contain overscroll-y-auto/g)).toHaveLength(2);
  expect(html).toContain('aria-label="工具调用记录" tabindex="0"');
  expect(html).toContain('aria-label="旁白内容" tabindex="0"');
  expect(html).toContain('tool_24');
  expect(html).toContain('旁白第 45 行');
});

it('counts consecutive identical narration without counting duplicate snapshots or changing history', () => {
  const repeated = '我调用 enzyme_card_search 获取下一步需要的信息。';
  const messages = [
    { id: 'one', role: 'assistant' as const, content: repeated, presentation: 'commentary' },
    { id: 'one', role: 'assistant' as const, content: repeated, presentation: 'commentary' },
    { id: 'two', role: 'assistant' as const, content: repeated, presentation: 'commentary' },
    { id: 'three', role: 'assistant' as const, content: repeated, presentation: 'commentary' },
    { id: 'different', role: 'assistant' as const, content: '已找到证据，开始比较。', presentation: 'commentary' },
    { id: 'later', role: 'assistant' as const, content: repeated, presentation: 'commentary' },
  ];
  const before = JSON.stringify(messages);
  const html = renderToString(() => <MainProcessGroup preferenceId="repeated" running={true} messages={messages}/>);
  expect(html.match(/我调用 检索资料/g)).toHaveLength(2);
  expect(html).not.toContain('enzyme_card_search');
  expect(html).toContain('重复 3 次');
  expect(html).toContain('×3');
  expect(html).toContain('已找到证据，开始比较。');
  expect(JSON.stringify(messages)).toBe(before);
});

it('collapses repeated paragraphs inside batched tool narration as well as across messages', () => {
  const repeated = '我调用 enzyme_card_search 获取下一步需要的信息。';
  const html = renderToString(() => <MainProcessGroup preferenceId="batched" running={false}
    messages={Array.from({length: 4}, (_, i) => ({id: String(i), role: 'assistant' as const,
      presentation: 'commentary', content: Array(4).fill(repeated).join('\n\n')}))}/>);
  expect(html.match(/我调用 检索资料/g)).toHaveLength(1);
  expect(html).toContain('×16');
});

it('aligns user messages right and Agent replies/process left without replacing message content', () => {
  const html = renderToString(() => <MainChatTurn latestAssistantMessageId="answer" turn={{
    key: 'user-1', running: false,
    user: { id: 'user-1', role: 'user', content: '用户问题' },
    processMessages: [{ id: 'narration', role: 'assistant', content: '检查输入', presentation: 'commentary' }],
    result: { id: 'answer', role: 'assistant', content: 'Agent 回答' },
  }}/>);
  expect(html).toMatch(/main-chat-row--user[^>]*><p[^>]*>用户问题/);
  expect(html.match(/main-chat-row--agent/g)).toHaveLength(2);
  expect(html).toContain('检查输入');
  expect(html).toContain('Agent 回答');
});

it('keeps internal async prompts hidden in the new conversation layout', () => {
  const html = renderToString(() => <MainChatTurn latestAssistantMessageId="answer" turn={{
    key: 'internal', running: false, processMessages: [],
    user: { id: 'internal', role: 'user', content: '内部继续提示', presentation: 'async_hidden_prompt' },
    result: { id: 'answer', role: 'assistant', content: '可见答案' },
  }}/>);
  expect(html).not.toContain('内部继续提示');
  expect(html).not.toContain('main-chat-row--user');
  expect(html).toContain('可见答案');
});
