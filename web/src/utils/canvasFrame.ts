import DOMPurify from 'dompurify';
import type { CanvasExplosion } from '../api/canvas';
import { explosionPortion } from './canvasAnimation';

// The only executable code in this frame is this fixed host-owned selection
// and motion bridge. Source SVG is sanitized again and has no active content privileges.
export function canvasFrame(source: string, partIds: string[], nonce: string, style: 'clay' | 'manual' = 'clay', partLabels: Record<string, string> = {}, explosions: Record<string, CanvasExplosion> = {}, isometric = false): string {
  const svg = DOMPurify.sanitize(source, {
    USE_PROFILES: { svg: true }, FORBID_TAGS: ['script', 'foreignObject', 'style', 'a', 'image', 'use', 'animate', 'set'],
    FORBID_ATTR: ['href', 'xlink:href', 'style'],
  });
  const config = JSON.stringify({ partIds, nonce, partLabels, explosions, isometric }).replace(/</g, '\\u003c');
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'">
<style>html,body{height:100%;box-sizing:border-box}body{margin:0;padding:20px;background:white;overflow:auto;font-family:system-ui,sans-serif}.canvas-viewport{display:grid;place-items:center;min-width:100%;min-height:100%;width:max-content}svg{display:block;margin:auto}svg [id]{cursor:pointer}svg [role=button]:focus-visible{outline:3px solid #2563eb;outline-offset:5px}.canvas-selected{outline:2px solid #2563eb;outline-offset:5px}.canvas-muted{opacity:.18}svg .canvas-part{transition:opacity 160ms ease}@media(prefers-reduced-motion:reduce){svg .canvas-part{transition:none}}
${style === 'clay' ? 'body{background:linear-gradient(145deg,#fff9fc,#f8f4ff)}.canvas-selected{outline-color:#80629d}svg .canvas-part{filter:drop-shadow(3px 5px 4px #cabbd466)}' : ''}${isometric ? 'body{background:#f6f7f4;padding:12px}svg .canvas-part{filter:none}' : ''}</style></head>
<body><div class="canvas-viewport">${svg}</div><script>
const config=${config};
const explosionPortion=${explosionPortion.toString()};
const parts=new Set(config.partIds);
parts.forEach(id=>{
 const node=document.getElementById(id);if(!node)return;
 node.classList.add('canvas-part');node.setAttribute('tabindex','0');node.setAttribute('role','button');
 node.setAttribute('aria-label',config.partLabels[id]||node.querySelector('title')?.textContent||'查看部件说明');
 node.setAttribute('aria-pressed','false');
});
let zoom=1;
const svgRoot=document.querySelector('.canvas-viewport>svg');
const wholeViewBox=svgRoot?.getAttribute('viewBox');
let focused='';
let selectedNode=null;
let exploded='';
let displaced=[];
let assemblyId='',share=0,motionBounds=null,tween=0,finishTween=null;
let suspended=false,reduced=false;
const media=typeof matchMedia==='function'?matchMedia('(prefers-reduced-motion: reduce)'):null;
const motionAllowed=()=>!suspended&&!reduced&&!media?.matches&&!document.hidden&&typeof requestAnimationFrame==='function';
function writeShare(value){
 share=value;
 const last=Math.max(0,...displaced.map(p=>p.delay||0));
 displaced.forEach(({node,transform,x,y,delay=0})=>{
  const portion=explosionPortion(value,delay,last);
  if(portion===0){if(transform===null)node.removeAttribute('transform');else node.setAttribute('transform',transform);}
  else node.setAttribute('transform','translate('+(x*portion)+' '+(y*portion)+')'+(transform?' '+transform:''));
 });
}
function rootBounds(node){
 const box=node.getBBox(),matrix=svgRoot.getCTM().inverse().multiply(node.getCTM());
 const corners=[[box.x,box.y],[box.x+box.width,box.y],[box.x,box.y+box.height],[box.x+box.width,box.y+box.height]].map(([x,y])=>new DOMPoint(x,y).matrixTransform(matrix));
 const x=Math.min(...corners.map(p=>p.x)),y=Math.min(...corners.map(p=>p.y));
 return {x,y,width:Math.max(...corners.map(p=>p.x))-x,height:Math.max(...corners.map(p=>p.y))-y};
}
function collapseComponent(){
 if(tween&&typeof cancelAnimationFrame==='function')cancelAnimationFrame(tween);
 tween=0;finishTween=null;writeShare(0);
 displaced=[];exploded='';assemblyId='';motionBounds=null;
}
function transitionComponent(id){
 if(tween&&typeof cancelAnimationFrame==='function')cancelAnimationFrame(tween);
 tween=0;
 const start=share,target=id?1:0;
 const receipt=(phase)=>parent.postMessage({type:'tcode-canvas-explode-result',nonce:config.nonce,requested_id:id,expanded_id:phase==='settled'?id:'',phase,error:''},'*');
 const finish=()=>{
  if(finishTween!==finish)return;
  if(tween&&typeof cancelAnimationFrame==='function')cancelAnimationFrame(tween);
  tween=0;finishTween=null;writeShare(target);
  if(!id){displaced=[];assemblyId='';motionBounds=null;}
  receipt('settled');
 };
 finishTween=finish;
 if(!motionAllowed()||Math.abs(target-start)<.00001){finish();return;}
 receipt(id?'opening':'closing');
 let began;
 const duration=360*(1+Math.max(0,...displaced.map(p=>p.delay||0)))*Math.max(.2,Math.abs(target-start));
 const tick=(now)=>{
  if(finishTween!==finish)return;
  if(began===undefined)began=now;
  const t=Math.min(1,Math.max(0,(now-began)/duration)),ease=t*t*(3-2*t);
  writeShare(start+(target-start)*ease);
  if(t>=1){finish();return;}
  tween=requestAnimationFrame(tick);
 };
 tween=requestAnimationFrame(tick);
}
function partParent(node){
 let parent=node.parentElement;
 while(parent&&parent!==svgRoot){if(parts.has(parent.id))return parent.id;parent=parent.parentElement;}
 return '';
}
function expandComponent(requested,controlled=false){
 const id=typeof requested==='string'?requested:'';
 if(id===exploded)return false;
 if(displaced.length&&(!id||id===assemblyId)){
  exploded=id;if(!controlled)transitionComponent(id);return true;
 }
 collapseComponent();
 let error='';
 if(id){
  try{
   const assembly=parts.has(id)?document.getElementById(id):null;
   if(!assembly)throw Error('unsupported_component');
   // Controlled kit timelines include nested piece plans as well as assembly plans.
   const nested=config.isometric&&(controlled||!partParent(assembly));
   const children=[...parts].map(key=>document.getElementById(key)).filter(node=>node&&node!==assembly&&(nested?assembly.contains(node)&&config.explosions[node.id]:partParent(node)===id));
   if(children.length<2||children.length>(nested?60:12))throw Error('unsupported_component');
   const planFor=node=>Object.prototype.hasOwnProperty.call(config.explosions||{},node.id)?config.explosions[node.id]:null;
   const planned=children.map(planFor);
   const explicit=planned.some(Boolean);
   if(explicit&&planned.some(p=>!p||![p.x,p.y].every(Number.isFinite)||Math.max(Math.abs(p.x),Math.abs(p.y))>1000||!Number.isInteger(p.order)||p.order<0||p.order>11))throw Error('incomplete_component_plan');
   const orders=explicit?[...new Set(planned.map(p=>p.order))].sort((a,b)=>a-b):[];
   const base=assembly.getBBox();
   const measures=children.map(node=>{
    const box=node.getBBox(),matrix=assembly.getCTM().inverse().multiply(node.getCTM());
    const corners=[[box.x,box.y],[box.x+box.width,box.y],[box.x,box.y+box.height],[box.x+box.width,box.y+box.height]].map(([x,y])=>new DOMPoint(x,y).matrixTransform(matrix));
    const x=Math.min(...corners.map(p=>p.x)),y=Math.min(...corners.map(p=>p.y));
    const width=Math.max(...corners.map(p=>p.x))-x,height=Math.max(...corners.map(p=>p.y))-y;
    if(![x,y,width,height].every(Number.isFinite)||width<=0||height<=0)throw Error('unmeasurable_component');
    return {node,x,y,width,height};
   });
   const gap=Math.max(24,Math.max(base.width,base.height)*.12);
   const width=measures.reduce((sum,item)=>sum+item.width,0)+gap*(measures.length-1);
   let cursor=base.x+(base.width-width)/2;
   const plans=measures.map(item=>{
    const plan=explicit?planFor(item.node):null;
    const dx=plan?plan.x:cursor-item.x,dy=plan?plan.y:base.y+base.height+gap-item.y;cursor+=item.width+gap;
    const matrix=item.node.parentElement.getCTM().inverse().multiply(assembly.getCTM());
    const zero=new DOMPoint(0,0).matrixTransform(matrix),delta=new DOMPoint(dx,dy).matrixTransform(matrix);
    const x=delta.x-zero.x,y=delta.y-zero.y;
    if(![x,y].every(Number.isFinite)||Math.max(Math.abs(x),Math.abs(y))>100000)throw Error('unmeasurable_component');
    return {node:item.node,transform:item.node.getAttribute('transform'),x,y,delay:plan?orders.indexOf(plan.order)*.25:0};
   });
   displaced=plans;
   assemblyId=id;
   const before=rootBounds(assembly);
   writeShare(1);const after=rootBounds(assembly);writeShare(0);
   const x=Math.min(before.x,after.x),y=Math.min(before.y,after.y);
   motionBounds={x,y,width:Math.max(before.x+before.width,after.x+after.width)-x,height:Math.max(before.y+before.height,after.y+after.height)-y};
   exploded=id;if(!controlled)transitionComponent(id);return true;
  }catch(reason){collapseComponent();error=reason.message||'unmeasurable_component';}
 }
 parent.postMessage({type:'tcode-canvas-explode-result',nonce:config.nonce,requested_id:id,expanded_id:exploded,phase:'settled',error},'*');
 return true;
}
function focusPart(id,force=false){
 if(!svgRoot||!wholeViewBox)return;
 id=typeof id==='string'&&parts.has(id)?id:'';
 if(id===focused&&!force)return;
 focused=id;svgRoot.setAttribute('viewBox',wholeViewBox);
 const node=id?document.getElementById(id):null;
 if(node){
  try{
   const box=node.id===assemblyId&&motionBounds?motionBounds:rootBounds(node);
   if(box.width>0&&box.height>0){
    const {x,y,width:w,height:h}=box;
    const pad=config.isometric?Math.max(12,Math.max(w,h)*.08):Math.max(24,Math.max(w,h)*.2);
    svgRoot.setAttribute('viewBox',[x-pad,y-pad,w+2*pad,h+2*pad].join(' '));
   }
  }catch{ /* Empty or non-rendered groups keep the whole view. */ }
 }
 fit();
}
function fit(){
 if(!svgRoot)return;
 const view=svgRoot.viewBox.baseVal;
 if(!(view.width>0&&view.height>0))return;
 const scale=Math.min(Math.max(1,innerWidth-40)/view.width,Math.max(1,innerHeight-40)/view.height)*zoom;
 svgRoot.style.width=view.width*scale+'px';svgRoot.style.height=view.height*scale+'px';
}
fit();window.addEventListener('resize',fit);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&finishTween)finishTween();});
media?.addEventListener?.('change',()=>{if(media.matches&&finishTween)finishTween();});
if(typeof IntersectionObserver==='function')new IntersectionObserver(entries=>{if(!entries[entries.length-1]?.isIntersecting&&finishTween)finishTween();}).observe(svgRoot);
window.addEventListener('pagehide',collapseComponent);
document.addEventListener('keydown',event=>{
 if(!['Enter',' '].includes(event.key)||!parts.has(event.target?.id))return;
 event.preventDefault();parent.postMessage({type:'tcode-canvas-select',nonce:config.nonce,element_id:event.target.id},'*');
});
document.addEventListener('click',event=>{
 let node=event.target; let first=''; let chosen='';
 while(node && node.tagName && node.tagName.toLowerCase()!=='svg'){
  if(node.id && !first) first=node.id;
  if(parts.has(node.id)){chosen=node.id;break;}
  node=node.parentElement;
 }
 const id=chosen||first;
 if(id) parent.postMessage({type:'tcode-canvas-select',nonce:config.nonce,element_id:id},'*');
});
window.addEventListener('message',event=>{
 if(event.source!==parent || !event.data || event.data.nonce!==config.nonce)return;
 if(event.data.type==='tcode-canvas-animation-snapshot'){
  try{
   if(!assemblyId||event.data.element_id!==assemblyId||displaced.length<2)throw Error('请先打开拆解动画');
   const clone=svgRoot.cloneNode(true);
   clone.querySelectorAll('*').forEach(node=>{['class','tabindex','role','aria-label','aria-pressed'].forEach(key=>node.removeAttribute(key));});
   clone.removeAttribute('style');
   const copied=new Map([...clone.querySelectorAll('[id]')].map(node=>[node.id,node]));
   const plans=displaced.map(p=>{
    const node=copied.get(p.node.id);if(!node)throw Error('拆解部件缺失');
    if(p.transform===null)node.removeAttribute('transform');else node.setAttribute('transform',p.transform);
    return {id:p.node.id,label:config.partLabels[p.node.id]||'部件',transform:p.transform,x:p.x,y:p.y,delay:p.delay};
   });
   const [vx,vy,vw,vh]=wholeViewBox.split(/[ ,]+/).map(Number),b=motionBounds;
   const x=Math.min(vx,b.x-18),y=Math.min(vy,b.y-18);
   clone.setAttribute('viewBox',[x,y,Math.max(vx+vw,b.x+b.width+18)-x,Math.max(vy+vh,b.y+b.height+18)-y].join(' '));
   parent.postMessage({type:'tcode-canvas-animation-snapshot-result',nonce:config.nonce,request_id:event.data.request_id,snapshot:{source:new XMLSerializer().serializeToString(clone),plans}},'*');
  }catch(reason){parent.postMessage({type:'tcode-canvas-animation-snapshot-result',nonce:config.nonce,request_id:event.data.request_id,error:reason.message||'无法读取拆解图形'},'*');}
  return;
 }
 if(event.data.type==='tcode-canvas-viewport'){
  if(typeof event.data.zoom==='number'&&Number.isFinite(event.data.zoom)&&event.data.zoom>=.25&&event.data.zoom<=4){zoom=event.data.zoom;focusPart(focused,true);}
  return;
 }
 if(event.data.type!=='tcode-canvas-highlight')return;
 suspended=event.data.active===false||!event.data.focus_id;reduced=event.data.reduced_motion===true;
 if(!motionAllowed()&&finishTween)finishTween();
 const timeline=event.data.timeline;
 const controlled=timeline&&parts.has(timeline.id)&&Number.isFinite(timeline.share)&&timeline.share>=0&&timeline.share<=1;
 const geometryChanged=expandComponent(controlled?timeline.id:event.data.expanded_id,!!controlled);
 if(controlled){
  if(tween&&typeof cancelAnimationFrame==='function')cancelAnimationFrame(tween);
  tween=0;finishTween=null;
  if(assemblyId===timeline.id)writeShare(timeline.share);
 }
 focusPart(event.data.focus_id,geometryChanged);
 document.querySelectorAll('.canvas-selected').forEach(n=>n.classList.remove('canvas-selected'));
 document.querySelectorAll('.canvas-muted').forEach(n=>n.classList.remove('canvas-muted'));
 parts.forEach(id=>document.getElementById(id)?.setAttribute('aria-pressed','false'));
 const node=typeof event.data.element_id==='string'?document.getElementById(event.data.element_id):null;
 if(node){
  node.classList.add('canvas-selected');
  if(parts.has(node.id))node.setAttribute('aria-pressed','true');
  if(node!==selectedNode)node.scrollIntoView({block:'nearest',inline:'nearest'});
 }
 selectedNode=node;
 const focusNode=focused?document.getElementById(focused):null;
 if(focusNode)parts.forEach(id=>{const part=document.getElementById(id);if(part&&part!==focusNode&&!part.contains(focusNode)&&!focusNode.contains(part))part.classList.add('canvas-muted');});
});
</script></body></html>`;
}
