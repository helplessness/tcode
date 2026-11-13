import {afterEach,expect,it,vi} from 'vitest';
import {encodeSvgGif} from './modelGif';
afterEach(()=>vi.unstubAllGlobals());
it('terminates a pending worker frame on cancellation and never finishes a partial GIF',async()=>{
  const controller=new AbortController(),messages:string[]=[];let terminated=0;
  class EncoderWorker extends EventTarget {
    postMessage(value:{type:string}){messages.push(value.type);if(value.type==='init')queueMicrotask(()=>this.dispatchEvent(new MessageEvent('message',{data:{ok:true}})));else if(value.type==='frame')queueMicrotask(()=>controller.abort());}
    terminate(){terminated++;}
  }
  vi.stubGlobal('Worker',EncoderWorker);
  await expect(encodeSvgGif(6,String,controller.signal,()=>{},async(_source,_signal,width=400,height=600)=>new Uint8ClampedArray(width*height*4),960,720)).rejects.toThrow();
  expect(messages).toEqual(['init','frame']);expect(terminated).toBeGreaterThan(0);
});
it('reports worker encoding failures and releases its worker',async()=>{
  let terminated=0;
  class EncoderWorker extends EventTarget {
    postMessage(value:{type:string}){queueMicrotask(()=>this.dispatchEvent(new MessageEvent('message',{data:value.type==='init'?{ok:true}:{error:'单帧编码失败'}})));}
    terminate(){terminated++;}
  }
  vi.stubGlobal('Worker',EncoderWorker);
  await expect(encodeSvgGif(1,String,new AbortController().signal,()=>{},async()=>new Uint8ClampedArray(16),2,2)).rejects.toThrow('单帧编码失败');
  expect(terminated).toBe(1);
});
