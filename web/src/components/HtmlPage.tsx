import { createMemo, createSignal, createEffect, onMount, onCleanup } from 'solid-js';
import type { CanvasStyle } from '../api/canvas';
import { htmlPageDocument } from '../utils/htmlPage';
export default function HtmlPage(props: { html: string; css: string; title: string; style: CanvasStyle; onEscape: () => void }) {
 let frame!: HTMLIFrameElement;
 const [height,setHeight]=createSignal(320);
 createEffect(()=>{props.html;props.css;setHeight(320);});
 onMount(() => {
  const close = (event: MessageEvent) => {
   if(event.source!==frame.contentWindow)return;
   if(event.data?.type==='tcode:html-close')props.onEscape();
   if(event.data?.type==='tcode:html-size'&&typeof event.data.height==='number'&&Number.isFinite(event.data.height))setHeight(Math.max(160,Math.min(720,event.data.height)));
  };
  window.addEventListener('message', close);
  onCleanup(() => window.removeEventListener('message', close));
 });
 const document = createMemo(() => htmlPageDocument(props.html, props.css, props.style, crypto.randomUUID()));
 return <iframe ref={frame} class="intelligent-page-html-frame" style={{'--html-page-height':`${height()}px`}} title={props.title} sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc={document()} />;
}
