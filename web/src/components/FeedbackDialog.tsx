import { createUniqueId, onCleanup, onMount, type JSX } from 'solid-js';
import { render } from 'solid-js/web';
import './FeedbackDialog.css';

type Tone='info'|'success'|'warning'|'danger';
export default function FeedbackDialog(props:{title:string;tone?:Tone;onClose:()=>void;children:JSX.Element}){
  let dialog!:HTMLDialogElement;
  const titleId=createUniqueId();
  const tone=()=>props.tone||'info';
  onMount(()=>dialog.showModal());
  onCleanup(()=>dialog.close());
  return <dialog ref={dialog} class="feedback-dialog" data-tone={tone()} aria-labelledby={titleId} onKeyDown={event=>event.stopPropagation()} onCancel={event=>{event.preventDefault();props.onClose();}}>
    <header><span class="feedback-dialog-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>{tone()==='success'?<path d="m8 12 3 3 5-6"/>:<><path d="M12 7v6"/><path d="M12 17h.01"/></>}</svg></span><div><small>TCode · {tone()==='success'?'操作完成':tone()==='danger'?'谨慎操作':tone()==='warning'?'操作确认':'操作提示'}</small><h2 id={titleId}>{props.title}</h2></div></header>
    <div class="feedback-dialog-body">{props.children}</div>
  </dialog>;
}

function requestFeedback(message:string,options:{title:string;tone:Tone;confirmLabel:string;cancel:boolean}):Promise<boolean>{
  return new Promise(resolve=>{
    const mount=document.createElement('div');document.body.appendChild(mount);
    let dispose:()=>void,finished=false;
    const finish=(confirmed:boolean)=>{if(finished)return;finished=true;queueMicrotask(()=>{dispose();mount.remove();resolve(confirmed);});};
    dispose=render(()=><FeedbackDialog title={options.title} tone={options.tone} onClose={()=>finish(false)}><p>{message}</p><footer class="feedback-dialog-actions">{options.cancel&&<button type="button" autofocus onClick={()=>finish(false)}>取消</button>}<button type="button" class="feedback-primary" autofocus={!options.cancel} onClick={()=>finish(true)}>{options.confirmLabel}</button></footer></FeedbackDialog>,mount);
  });
}
export function confirmAction(message:string,options?:{title?:string;tone?:Tone;confirmLabel?:string}){return requestFeedback(message,{title:options?.title||'确认这项操作？',tone:options?.tone||'warning',confirmLabel:options?.confirmLabel||'确认继续',cancel:true});}
export async function showFeedback(message:string,options?:{title?:string;tone?:Tone}){await requestFeedback(message,{title:options?.title||'操作未完成',tone:options?.tone||'danger',confirmLabel:'知道了',cancel:false});}
export function promptAction(message:string,defaultValue=''):Promise<string|null>{
  return new Promise(resolve=>{
    const mount=document.createElement('div');document.body.appendChild(mount);
    let dispose:()=>void,finished=false,input!:HTMLInputElement;
    const finish=(value:string|null)=>{if(finished)return;finished=true;queueMicrotask(()=>{dispose();mount.remove();resolve(value);});};
    dispose=render(()=><FeedbackDialog title="请输入确认内容" tone="warning" onClose={()=>finish(null)}><p>{message}</p><form onSubmit={event=>{event.preventDefault();finish(input.value);}}><input ref={element=>{input=element;queueMicrotask(()=>{input.focus();input.select();});}} aria-label={message} value={defaultValue}/><footer class="feedback-dialog-actions"><button type="button" onClick={()=>finish(null)}>取消</button><button type="submit" class="feedback-primary">确认</button></footer></form></FeedbackDialog>,mount);
  });
}
