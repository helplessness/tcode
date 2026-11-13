import { afterEach, expect, it, vi } from 'vitest';
import { saveGif } from './saveGif';

afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();vi.restoreAllMocks();});

function fixture(native=true) {
  vi.useFakeTimers();
  let receive:((value:{url:string;state:'completed'|'cancelled'|'interrupted'})=>void)|undefined;
  const unsubscribe=vi.fn(),link={href:'',download:'',click:vi.fn(),remove:vi.fn()};
  vi.stubGlobal('window',{tcodeDesktop:native?{onDownloadResult:(callback:typeof receive)=>{receive=callback;return unsubscribe;}}:undefined});
  vi.stubGlobal('document',{createElement:()=>link,body:{append:vi.fn()}});
  vi.spyOn(URL,'createObjectURL').mockReturnValue('blob:gif');
  const revoke=vi.spyOn(URL,'revokeObjectURL').mockImplementation(()=>{});
  return {receive:(url:string,state:'completed'|'cancelled'|'interrupted')=>receive!({url,state}),unsubscribe,link,revoke};
}

it('retains the native download beyond a slow save picker and waits for its own completion',async()=>{
  const f=fixture();let settled=false;
  const result=saveGif(new Blob(['GIF89a']),'轴承/教学').then(value=>{settled=true;return value;});
  await vi.advanceTimersByTimeAsync(20*60*1000);
  expect(settled).toBe(false);expect(f.revoke).not.toHaveBeenCalled();
  expect(f.link.download).toBe('轴承_教学.gif');expect(f.link.remove).toHaveBeenCalledOnce();
  f.receive('blob:another','completed');expect(f.revoke).not.toHaveBeenCalled();
  f.receive('blob:gif','completed');expect(await result).toBe('saved');
  expect(f.revoke).toHaveBeenCalledWith('blob:gif');expect(f.unsubscribe).toHaveBeenCalledOnce();
});

it('distinguishes cancelled and interrupted downloads, releases each native subscription',async()=>{
  const f=fixture();const result=saveGif(new Blob(['GIF89a']),'轴承');
  f.receive('blob:gif','cancelled');expect(await result).toBe('cancelled');
  expect(f.unsubscribe).toHaveBeenCalledOnce();
  const g=fixture();const failure=saveGif(new Blob(['GIF89a']),'轴承');
  const rejected=expect(failure).rejects.toThrow('未能保存');
  g.receive('blob:gif','interrupted');await rejected;expect(g.unsubscribe).toHaveBeenCalledOnce();
});

it('uses a bounded browser fallback and never claims that hand-off proves a save',async()=>{
  const f=fixture(false);expect(await saveGif(new Blob(['GIF89a']),'轴承')).toBe('handed-off');
  await vi.advanceTimersByTimeAsync(1000);expect(f.revoke).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(299000);expect(f.revoke).toHaveBeenCalledOnce();
});
