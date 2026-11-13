import { render } from 'solid-js/web';
import { Show, createSignal } from 'solid-js';
import SketchPanel from '../../src/components/SketchPanel';
import { ZoomableImage } from '../../src/components/ImageLightbox';
import '../../src/index.css';

function Demo() {
  const [open, setOpen] = createSignal(true);
  const [image, setImage] = createSignal('');
  const [fail, setFail] = createSignal(false);
  return <><button onClick={() => setOpen(true)}>绘制草图</button>
    <label><input type="checkbox" checked={fail()} onChange={e => setFail(e.currentTarget.checked)} />模拟一次上传失败</label>
    <Show when={image()}><div class="relative h-20 w-20"><ZoomableImage src={image()} alt="导出草图" class="h-full w-full object-contain" /><button aria-label="删除草图附件" class="absolute -right-2 -top-2" onClick={() => { URL.revokeObjectURL(image()); setImage(''); }}>×</button></div></Show>
    <Show when={open()}><SketchPanel onClose={() => setOpen(false)} onComplete={async file => {
      if (fail()) { setFail(false); throw new Error('模拟上传失败，请重试'); }
      if (image()) URL.revokeObjectURL(image());
      setImage(URL.createObjectURL(file));
    }} /></Show></>;
}
render(() => <Demo />, document.getElementById('root')!);
