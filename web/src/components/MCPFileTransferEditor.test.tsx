import { renderToString } from 'solid-js/web';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MCPFileTransferEditor from './MCPFileTransferEditor';
import { discoverMCPFiles, saveMCPConfig } from '../api/capabilities';

const connection = () => ({name: 'custom', url: 'https://example.test/mcp', transport: 'streamable-http', headers: {}});
afterEach(() => vi.unstubAllGlobals());

describe('custom MCP optional file transport', () => {
  it('keeps the feature optional and does not require a plugin or raw JSON pointer', () => {
    const html = renderToString(() => <MCPFileTransferEditor value={null} onChange={() => {}} connection={connection} disabled={false}/>);
    expect(html).toContain('输入文件传输（可选）');
    expect(html).toContain('不配置时保持原有行为');
    expect(html).not.toContain('发现工具与参数');
  });
  it('clearly marks server-path receivers as not connected, not as successful transport', () => {
    const html = renderToString(() => <MCPFileTransferEditor value={{receiver: 'server_paths', tools: [{name: 'analyze', files: []}]}} onChange={() => {}} connection={connection} disabled={false}/>);
    expect(html).toContain('文件传输尚未接通');
    expect(html).toContain('不会上传或提交');
    expect(html.replace(/<!--.*?-->/g, '')).toContain('已保存 1 个工具');
    expect(html).toContain('普通 JSON');
    expect(html).not.toContain('placeholder="JSON Pointer"');
  });
  it('discovery and save send explicit configuration without submitting a job', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({tools: []}), {status: 200}));
    vi.stubGlobal('fetch', fetcher);
    await discoverMCPFiles(connection());
    expect(fetcher.mock.calls[0][0]).toBe('/api/mcp/config/discover-files');
    await saveMCPConfig('custom', {...connection(), transport: 'streamable-http', invocation_mode: 'async', enabled: true, connect_timeout: 10000, timeout: 30000, submit_via: 'job-protocol-mcp', file_transfer: null}, 'selected-workspace');
    expect(fetcher.mock.calls[1][0]).toBe('/api/mcp/config/custom?workdir_name=selected-workspace');
    expect(JSON.parse(fetcher.mock.calls[1][1].body).file_transfer).toBeNull();
    expect(fetcher.mock.calls.every(call => !String(call[0]).includes('submit_job'))).toBe(true);
  });
});
