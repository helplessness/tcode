import { afterEach, describe, expect, it, vi } from 'vitest';
import { uploadWorkspaceFile } from './files';

afterEach(() => vi.unstubAllGlobals());

describe('asset workspace uploads', () => {
  it('uses explicit workspace scope, multipart file and server-renamed path', async () => {
    const file = new File(['hello'], 'sample.txt', { type: 'text/plain' });
    const controller = new AbortController();
    const request = vi.fn().mockResolvedValue(new Response(JSON.stringify({ relative_path: 'upload/sample-unique.txt' })));
    vi.stubGlobal('fetch', request);
    expect(await uploadWorkspaceFile(file, ' test 1 ', controller.signal)).toBe('upload/sample-unique.txt');
    const [url, options] = request.mock.calls[0];
    expect(url).toBe('/api/files/upload?workspace_id=test+1');
    expect(options.method).toBe('POST');
    expect(options.body.get('file')).toBe(file);
    expect(options.signal).toBe(controller.signal);
    expect(options.headers).toBeUndefined(); // Browser supplies multipart boundary.
  });

  it('rejects missing scope and oversized files before making a request', async () => {
    const request = vi.fn(); vi.stubGlobal('fetch', request);
    const file = new File(['hello'], 'sample.txt');
    await expect(uploadWorkspaceFile(file, ' ')).rejects.toThrow('请先选择工作区');
    Object.defineProperty(file, 'size', { value: 100 * 1024 * 1024 + 1 });
    await expect(uploadWorkspaceFile(file, 'test1')).rejects.toThrow('100 MB');
    expect(request).not.toHaveBeenCalled();
  });

  it('reports server errors and malformed responses without inventing a filename', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'permission denied' }), { status: 403 }))
      .mockResolvedValueOnce(new Response('gateway failed', { status: 502 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ path: '/private/file.txt' })));
    vi.stubGlobal('fetch', request);
    const file = new File(['hello'], 'sample.txt');
    await expect(uploadWorkspaceFile(file, 'test1')).rejects.toThrow('permission denied');
    await expect(uploadWorkspaceFile(file, 'test1')).rejects.toThrow('502');
    await expect(uploadWorkspaceFile(file, 'test1')).rejects.toThrow('刷新资产库确认');
  });

  it('preserves cancellation instead of retrying into another workspace', async () => {
    const request = vi.fn().mockRejectedValue(new DOMException('Aborted', 'AbortError'));
    vi.stubGlobal('fetch', request);
    const controller = new AbortController(); controller.abort();
    await expect(uploadWorkspaceFile(new File(['x'], 'x.txt'), 'original', controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(request).toHaveBeenCalledTimes(1);
  });
});
