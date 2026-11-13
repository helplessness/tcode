import { afterEach, describe, expect, it, vi } from 'vitest';
import { evidenceImageUrl, resolveCitation, evidenceMaintenance } from './evidence';

afterEach(() => vi.unstubAllGlobals());

describe('evidence API', () => {
  it('维护请求绑定工作区并支持取消，不提交内部任务编号', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ status: 'queued', processed: 0, total: 30 })));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    await evidenceMaintenance('ws 2', signal, { rebuild: false, verify_assets: true });
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ workspace_id: 'ws 2', rebuild: false, verify_assets: true });
    expect(fetch.mock.calls[0][1].signal).toBe(signal);
    await evidenceMaintenance('ws 2', signal);
    expect(new URL(fetch.mock.calls[1][0], 'http://localhost').searchParams.get('workdir_name')).toBe('ws 2');
  });
  it('保留内部引用用于请求，并绑定工作区、会话和取消信号', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ state: 'available', title: '论文' })));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    expect(await resolveCitation({ kind: 'evidence', id: 'ev-demo' }, 'ws2', 'session-1', signal)).toEqual({ state: 'available', title: '论文' });
    const options = fetch.mock.calls[0][1];
    expect(options.signal).toBe(signal);
    expect(JSON.parse(options.body)).toEqual({ kind: 'evidence', id: 'ev-demo', workspace_id: 'ws2', session_id: 'session-1' });
  });

  it('图片地址安全编码引用和工作区', () => {
    const url = evidenceImageUrl('paper:doc/a#b', 2, 'ws 2');
    expect(url).toContain('paper%3Adoc%2Fa%23b/images/2');
    expect(new URL(url, 'http://localhost').searchParams.get('workdir_name')).toBe('ws 2');
  });

  it('知识引用复用来源面板接口，保持类型和工作区隔离', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ state: 'historical', title: '历史研究记录' })));
    vi.stubGlobal('fetch', fetch);
    const result = await resolveCitation({ kind: 'knowledge', id: 'opaque_reference' }, 'ws2', '', new AbortController().signal);
    expect(result.state).toBe('historical');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ kind: 'knowledge', id: 'opaque_reference', workspace_id: 'ws2', session_id: '' });
  });

  it('访问失败不假装来源可用', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '来源不可访问' }), { status: 403 })));
    await expect(resolveCitation({ kind: 'evidence', id: 'ev-demo' }, 'ws2', '', new AbortController().signal)).rejects.toThrow('来源不可访问');
  });
});
