import { renderToString } from 'solid-js/web';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ScheduledTasksWorkspace from './ScheduledTasksWorkspace';
import SettingsWorkspace from './SettingsWorkspace';
import PluginAsyncTargetsSettings from './PluginAsyncTargetsSettings';
import JobWorkspacePanel from './JobWorkspacePanel';

afterEach(() => vi.unstubAllGlobals());

describe('list-first services and tasks', () => {
  it('keeps the scheduled task list visible without the creation form', () => {
    const html = renderToString(() => <ScheduledTasksWorkspace currentUser={{ role: 'admin' } as any} />);
    expect(html).toContain('新建任务');
    expect(html).toContain('aria-label="定时任务列表"');
    expect(html).not.toContain('<form');
    expect(html).not.toContain('生成确认预览');
    expect(html).not.toContain('390px_minmax');
  });
  it('opens regular MCP configuration on demand and keeps diagnostics collapsed', () => {
    vi.stubGlobal('localStorage', { getItem: () => null });
    const html = renderToString(() => <SettingsWorkspace initialSection="mcp" />);
    expect(html).toContain('MCP 服务');
    expect(html).not.toContain('placeholder="例如 literature-search"');
    expect(html).toContain('<summary>高级：连接诊断与代理路由</summary>');
    expect(html).toContain('<summary>高级：配置文件与重新加载</summary>');
  });
  it('keeps asynchronous deployment instructions in a closed disclosure', () => {
    const html = renderToString(() => <PluginAsyncTargetsSettings workspaceId="test" />);
    expect(html).toContain('<details><summary>高级：接入协议与部署说明</summary>');
    expect(html).not.toContain('type="url"');
  });
  it('places task result controls ahead of timestamps and technical policies', () => {
    const html = renderToString(() => <JobWorkspacePanel workspace={{ workdir: '/workspace/test', tool: 'test', status: 'completed' } as any} />);
    expect(html).toContain('查看结果文件');
    expect(html.indexOf('查看结果文件')).toBeLessThan(html.indexOf('提交时间'));
  });
});
