import { GIFEncoder, quantize, applyPalette } from 'gifenc';

// Platform-owned encoder only. Generated content never runs in this worker.
let encoder: ReturnType<typeof GIFEncoder>, width = 0, height = 0;
self.onmessage = event => {
  try {
    const value = event.data;
    if (value.type === 'init') { width=value.width; height=value.height; encoder=GIFEncoder(); }
    else if (value.type === 'frame') {
      const rgba = new Uint8ClampedArray(value.rgba);
      if (!encoder || rgba.length !== width*height*4) throw Error('导出帧尺寸不一致');
      const colors=quantize(rgba,256);
      encoder.writeFrame(applyPalette(rgba,colors),width,height,{palette:colors,delay:value.delay,repeat:-1,dispose:2});
      if (encoder.bytesView().length>24*1024*1024) throw Error('GIF超过24MB，请缩短动画或简化图形');
    } else if (value.type === 'finish') {
      encoder.finish(); const bytes=new Uint8Array(encoder.bytes());
      self.postMessage({bytes:bytes.buffer}, {transfer:[bytes.buffer]}); return;
    }
    self.postMessage({ok:true});
  } catch (error) { self.postMessage({error:error instanceof Error?error.message:'GIF编码失败'}); }
};
