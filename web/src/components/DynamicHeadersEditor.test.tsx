import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';
import DynamicHeadersEditor from './DynamicHeadersEditor';

describe('dynamic business headers', () => {
  it('keeps the editor collapsed with labelled fields and safe defaults', () => {
    const html = renderToString(() => <DynamicHeadersEditor value={{'X-Project-ID': {
      variable: 'project_id', description: '操作所属项目', extraction_hint: '', required: true, max_length: 128,
    }}} onChange={() => {}} />);
    expect(html.replace(/<!--.*?-->/g, '')).toContain('<summary>本次调用变量 Header（1）</summary>');
    expect(html).not.toContain('<details open');
    expect(html).toContain('业务含义（必填，提供给 Agent）');
    expect(html).toContain('只用于本次调用或异步任务');
    expect(html).toContain('X-Project-ID');
    expect(html).toContain('添加动态请求头');
    expect(html).toContain('身份、Token 和 uid 不可由自然语言设置');
  });
});
