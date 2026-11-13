import {afterEach,expect,it,vi} from 'vitest';
import {canvasRequest} from './canvas';
afterEach(()=>vi.unstubAllGlobals());
it('explains rate limiting without declaring the saved drawing lost or retrying in a loop',async()=>{
  const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:'canvas_rate_limited'}),{status:429,headers:{'Retry-After':'17'}}));vi.stubGlobal('fetch',fetch);
  await expect(canvasRequest('c','s')).rejects.toThrow('17秒后重新加载画布；已保存数据不会丢失');expect(fetch).toHaveBeenCalledTimes(1);
});
it('keeps native spatial-check query options alongside the owned session and exact revision',async()=>{
  const fetch=vi.fn().mockResolvedValue(new Response('{}'));vi.stubGlobal('fetch',fetch);
  await canvasRequest('canvas','session','/spatial-check?suggest=true',undefined,'revision');
  const url=new URL(fetch.mock.calls[0][0],'https://local.test');expect(url.searchParams.get('suggest')).toBe('true');expect(url.searchParams.get('session_id')).toBe('session');expect(url.searchParams.get('revision_id')).toBe('revision');
});
it('provides readable recovery for disconnected service and missing canvas',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));await expect(canvasRequest('c','s')).rejects.toThrow('无法连接本地服务');
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:'canvas_not_found'}),{status:404})));await expect(canvasRequest('c','s')).rejects.toThrow('当前工作区中找不到这份画布');
});
