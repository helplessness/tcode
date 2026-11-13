import {createEffect,createSignal,onCleanup,onMount,Show} from 'solid-js';
import type {SolidScene} from '../api/canvas';
import {createSolidView,type SolidView} from '../utils/solidScene';
export default function SolidSceneViewer(props:{fixed?:boolean;scene:SolidScene;share:number;target:string;selected:string|string[];onSelect:(id:string)=>void;ready:(view:SolidView|undefined)=>void}){
  let canvas!:HTMLCanvasElement,host!:HTMLDivElement;let view:SolidView|undefined;
  const [error,setError]=createSignal('');
  onMount(()=>{try{view=createSolidView(canvas,props.scene,props.onSelect);props.ready(view);view.motion(props.share,props.target);view.select(props.selected);const observer=new ResizeObserver(()=>view?.resize(host.clientWidth,host.clientHeight));observer.observe(host);onCleanup(()=>observer.disconnect());}catch(reason){setError('空间视图暂不可用，请确认图形加速可用后重试；不会用平面叠图代替。');console.warn('solid scene unavailable',reason);}});
  createEffect(()=>{const share=props.share,target=props.target,selected=props.selected;view?.fixed(!!props.fixed);view?.motion(share,target);view?.select(selected);});
  onCleanup(()=>{props.ready(undefined);view?.dispose();});
  return <div class="canvas-solid-shell"><div ref={host} class="canvas-solid-host"><canvas ref={canvas} aria-label={props.fixed?"固定等轴测装配图":"可旋转的三维部件模型"} tabIndex={0} onKeyDown={event=>{const keys:Record<string,[number,number]>={ArrowLeft:[-.15,0],ArrowRight:[.15,0],ArrowUp:[0,-.15],ArrowDown:[0,.15]};if(!props.fixed&&keys[event.key]){event.preventDefault();view?.rotate(...keys[event.key]);}else if(event.key==='Home'){event.preventDefault();view?.reset();}}}/><Show when={error()}><p role="alert">{error()}</p></Show></div><div class="canvas-solid-controls"><span>{props.fixed?"固定等轴测 · 滚轮缩放 · 点击查看部件":"拖动旋转 · 滚轮缩放 · 点击查看部件"}</span><Show when={!props.fixed}><button type="button" onClick={()=>view?.rotate(-.3)}>向左旋转</button><button type="button" onClick={()=>view?.rotate(.3)}>向右旋转</button></Show><button type="button" onClick={()=>view?.reset()}>重置视角</button></div></div>;
}
