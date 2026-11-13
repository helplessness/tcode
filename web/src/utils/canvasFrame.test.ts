import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { expect,it } from 'vitest';
import { explosionPortion } from './canvasAnimation';

// Execute the real fixed bridge against a small SVG DOM mock, without opening a
// browser or allowing generated SVG to execute code.
it('focuses in root coordinates, preserves descendants, authenticates messages and returns to the whole',()=>{
  const source=readFileSync(new URL('./canvasFrame.ts',import.meta.url),'utf8');
  const script=source.slice(source.indexOf('const parts=new Set'),source.indexOf('</script>'));
  const handlers:Record<string,(event:any)=>void>={};
  const classes=()=>{const values=new Set<string>();return {add:(v:string)=>values.add(v),remove:(v:string)=>values.delete(v),has:(v:string)=>values.has(v)};};
  const matrix={inverse:()=>matrix,multiply:()=>({tx:100,ty:20})};
  const node=(id:string,parentId='')=>({id,parentId,classList:classes(),setAttribute:()=>{},querySelector:()=>null,
    contains:(other:{id:string})=>id==='motor'&&other.id==='shaft',scrollIntoView:()=>{},getBBox:()=>({x:0,y:0,width:50,height:30}),getCTM:()=>matrix});
  const nodes=[node('motor'),node('shaft','motor'),node('body')];
  let viewBox='0 0 400 200';
  const root={getAttribute:()=>viewBox,setAttribute:(_k:string,v:string)=>{viewBox=v;},getCTM:()=>matrix,style:{},
    viewBox:{get baseVal(){const [x,y,width,height]=viewBox.split(' ').map(Number);return {x,y,width,height};}}};
  const parent={};
  const doc={getElementById:(id:string)=>nodes.find(n=>n.id===id),querySelector:()=>root,
    querySelectorAll:(selector:string)=>nodes.filter(n=>n.classList.has(selector.slice(1))),addEventListener:()=>{}};
  class Point {constructor(public x:number,public y:number){} matrixTransform(m:{tx:number;ty:number}){return {x:this.x+m.tx,y:this.y+m.ty};}}
  runInNewContext(script,{explosionPortion,config:{partIds:nodes.map(n=>n.id),nonce:'token',partLabels:{}},document:doc,parent,
    window:{addEventListener:(name:string,fn:(e:any)=>void)=>{handlers[name]=fn;}},innerWidth:800,innerHeight:600,DOMPoint:Point});
  const send=(data:Record<string,unknown>,source=parent)=>handlers.message({source,data:{nonce:'token',type:'tcode-canvas-highlight',...data}});
  send({element_id:'shaft',focus_id:'motor'},{});
  expect(viewBox).toBe('0 0 400 200');
  send({nonce:'wrong',element_id:'shaft',focus_id:'motor'});
  expect(viewBox).toBe('0 0 400 200');
  send({element_id:'motor',focus_id:'motor'});
  expect(viewBox).toBe('76 -4 98 78');
  expect(nodes[2].classList.has('canvas-muted')).toBe(true);
  expect(nodes[1].classList.has('canvas-muted')).toBe(false);
  send({type:'tcode-canvas-viewport',zoom:2});
  expect(viewBox).toBe('76 -4 98 78');
  send({element_id:'shaft',focus_id:'shaft'});
  expect(nodes[0].classList.has('canvas-muted')).toBe(false);
  send({element_id:'',focus_id:''});
  expect(viewBox).toBe('0 0 400 200');
  expect(nodes.every(n=>!n.classList.has('canvas-muted'))).toBe(true);
});

function disassemblyFixture(animated=false, explosions:Record<string,{x:number;y:number;order:number}>={},isometric=false){
  const source=readFileSync(new URL('./canvasFrame.ts',import.meta.url),'utf8');
  const script=source.slice(source.indexOf('const parts=new Set'),source.indexOf('</script>'));
  const handlers:Record<string,(event:any)=>void>={},receipts:any[]=[];
  const pending=new Map<number,FrameRequestCallback>();let nextFrame=0;
  const media={matches:false,addEventListener:(_name:string,callback:()=>void)=>{handlers.motion=callback;}};
  let offscreen:((entries:any[])=>void)|undefined;
  class Matrix {
    constructor(public tx=0,public ty=0){}
    inverse(){return new Matrix(-this.tx,-this.ty);}
    multiply(other:Matrix){return new Matrix(this.tx+other.tx,this.ty+other.ty);}
  }
  class Point {constructor(public x:number,public y:number){} matrixTransform(m:Matrix){return {x:this.x+m.tx,y:this.y+m.ty};}}
  const root:any={id:'root',parentElement:null,style:{},getCTM:()=>new Matrix()};
  let viewBox='0 0 400 200';
  root.getAttribute=()=>viewBox;root.setAttribute=(_k:string,v:string)=>{viewBox=v;};
  root.viewBox={get baseVal(){const [x,y,width,height]=viewBox.split(' ').map(Number);return {x,y,width,height};}};
  const nodes:any[]=[];
  const make=(id:string,parent:any,box:{x:number;y:number;width:number;height:number}|null,transform?:string)=>{
    const attrs=new Map<string,string>(transform?[['transform',transform]]:[]),classes=new Set<string>();
    const n:any={id,parentElement:parent,attrs,classList:{add:(v:string)=>classes.add(v),remove:(v:string)=>classes.delete(v),has:(v:string)=>classes.has(v)},
      getAttribute:(k:string)=>attrs.get(k)??null,setAttribute:(k:string,v:string)=>attrs.set(k,v),removeAttribute:(k:string)=>attrs.delete(k),
      querySelector:()=>null,scrollIntoView:()=>{},contains:(other:any)=>{while(other){if(other===n)return true;other=other.parentElement;}return false;}};
    n.getCTM=()=>{
      const offset=[...(attrs.get('transform')||'').matchAll(/translate\(([-.\d]+)[ ,]+([-.\d]+)\)/g)].reduce((m,hit)=>m.multiply(new Matrix(Number(hit[1]),Number(hit[2]))),new Matrix());
      return parent.getCTM().multiply(offset);
    };
    n.getBBox=()=>{
      if(box)return box;
      const children=nodes.filter(child=>child.parentElement===n).map(child=>{
        const b=child.getBBox(),m=n.getCTM().inverse().multiply(child.getCTM());return {...b,x:b.x+m.tx,y:b.y+m.ty};
      });
      const x=Math.min(...children.map(b=>b.x)),y=Math.min(...children.map(b=>b.y));
      return {x,y,width:Math.max(...children.map(b=>b.x+b.width))-x,height:Math.max(...children.map(b=>b.y+b.height))-y};
    };
    nodes.push(n);return n;
  };
  const assembly=make('assembly',root,null,'translate(50 20)');
  make('base',assembly,{x:0,y:0,width:120,height:80});
  const wrapper=make('wrapper',assembly,null,'translate(10 20)');
  const a=make('a',wrapper,null,'translate(3 4)');
  const leaf=make('leaf',a,{x:0,y:0,width:20,height:10});
  const b=make('b',assembly,{x:70,y:30,width:20,height:10});
  const other=make('other',root,null),otherA=make('other_a',other,{x:0,y:0,width:10,height:10});
  make('other_b',other,{x:20,y:0,width:10,height:10});
  const large=make('large',root,null);
  for(let i=0;i<13;i++)make(`many${i}`,large,{x:i*12,y:0,width:10,height:10});
  const parent={postMessage:(receipt:any)=>receipts.push(receipt)};
  const document={getElementById:(id:string)=>nodes.find(n=>n.id===id),querySelector:()=>root,
    querySelectorAll:(selector:string)=>nodes.filter(n=>n.classList.has(selector.slice(1))),hidden:false,
    addEventListener:(name:string,fn:(e:any)=>void)=>{handlers[name]=fn;}};
  runInNewContext(script,{explosionPortion,config:{partIds:['assembly','a','leaf','b','other','other_a','other_b','large',...Array.from({length:13},(_,i)=>`many${i}`)],nonce:'token',partLabels:{},explosions,isometric},document,parent,
    window:{addEventListener:(name:string,fn:(e:any)=>void)=>{handlers[name]=fn;}},innerWidth:800,innerHeight:600,DOMPoint:Point,
    matchMedia:animated?()=>media:undefined,
    requestAnimationFrame:animated?(callback:FrameRequestCallback)=>{pending.set(++nextFrame,callback);return nextFrame;}:undefined,
    cancelAnimationFrame:(id:number)=>pending.delete(id),
    IntersectionObserver:class {constructor(callback:(entries:any[])=>void){offscreen=callback;}observe(){}},
  });
  const send=(expanded_id:string,options:Record<string,unknown>={})=>handlers.message({source:parent,data:{type:'tcode-canvas-highlight',nonce:'token',element_id:'assembly',focus_id:'assembly',expanded_id,...options}});
  const tick=(time:number)=>{const tasks=[...pending.values()];pending.clear();tasks.forEach(callback=>callback(time));};
  return {send,a,b,wrapper,leaf,otherA,large,nodes,receipts,make,assembly,pending,tick,handlers,document,media,offscreen:()=>offscreen?.([{isIntersecting:false}])};
}

it('applies and restores nested kit translations on the whole timeline',()=>{
  const {send,a,b,leaf}=disassemblyFixture(false,{a:{x:0,y:0,order:0},b:{x:0,y:0,order:0},leaf:{x:40,y:-75,order:1}},true);
  send('',{timeline:{id:'assembly',share:1}});
  expect(leaf.getAttribute('transform')).toBe('translate(40 -75)');
  expect(a.getCTM().tx).toBe(63);
  expect(a.getCTM().ty).toBe(44);
  expect(b.getCTM().tx).toBe(50);
  send('',{timeline:{id:'assembly',share:0}});
  expect(leaf.getAttribute('transform')).toBeNull();
});

it('temporarily separates only direct parts, restores exact transforms and rejects invalid expansion atomically',()=>{
  const {send,a,b,wrapper,leaf,otherA,large,nodes,receipts,make,assembly}=disassemblyFixture();
  send('assembly',{nonce:'wrong'});
  expect(a.getAttribute('transform')).toBe('translate(3 4)');
  send('assembly');
  expect(a.getAttribute('transform')).not.toBe('translate(3 4)');
  expect(b.getAttribute('transform')).toMatch(/^translate/);
  expect(wrapper.getAttribute('transform')).toBe('translate(10 20)');
  expect(leaf.getAttribute('transform')).toBeNull();
  expect(receipts[receipts.length-1]).toMatchObject({expanded_id:'assembly',error:''});
  const moved=a.getAttribute('transform');send('assembly');expect(a.getAttribute('transform')).toBe(moved);
  send('other');
  expect(a.getAttribute('transform')).toBe('translate(3 4)');expect(b.getAttribute('transform')).toBeNull();
  expect(otherA.getAttribute('transform')).toMatch(/^translate/);
  send('');
  expect(otherA.getAttribute('transform')).toBeNull();
  expect(a.getAttribute('transform')).toBe('translate(3 4)');expect(b.getAttribute('transform')).toBeNull();
  for(let i=0;i<3;i++){send('assembly');send('');}
  expect(a.getAttribute('transform')).toBe('translate(3 4)');expect(b.getAttribute('transform')).toBeNull();
  send('assembly');send('leaf');
  expect(a.getAttribute('transform')).toBe('translate(3 4)');expect(b.getAttribute('transform')).toBeNull();
  expect(receipts[receipts.length-1].error).toBeTruthy();
  const old=b.getBBox;b.getBBox=()=>({x:0,y:0,width:0,height:0});send('assembly');
  expect(a.getAttribute('transform')).toBe('translate(3 4)');expect(b.getAttribute('transform')).toBeNull();
  b.getBBox=old;
  for(let i=0;i<11;i++)make(`extra${i}`,assembly,{x:0,y:0,width:10,height:10});
  // Only registered parts are candidates; unrelated decorative elements are never moved.
  send('assembly');expect(receipts[receipts.length-1].expanded_id).toBe('assembly');send('');
  send('large');expect(receipts[receipts.length-1].error).toBeTruthy();
  expect(nodes.filter(n=>n.parentElement===large).every(n=>n.getAttribute('transform')===null)).toBe(true);
});

it('uses explicit directions and staggered order, closes in reverse order and restores exact source transforms',()=>{
  const f=disassemblyFixture(true,{a:{x:0,y:-60,order:0},b:{x:40,y:0,order:1}});
  f.send('assembly');f.tick(0);f.tick(80);
  expect(f.a.getAttribute('transform')).not.toBe('translate(3 4)');
  expect(f.b.getAttribute('transform')).toBeNull();
  f.tick(450);
  expect(f.a.getAttribute('transform')).toBe('translate(0 -60) translate(3 4)');
  expect(f.b.getAttribute('transform')).toBe('translate(40 0)');
  f.send('');f.tick(500);f.tick(580);
  expect(f.a.getAttribute('transform')).toBe('translate(0 -60) translate(3 4)');
  expect(f.b.getAttribute('transform')).not.toBe('translate(40 0)');
  const reversing=f.b.getAttribute('transform');f.send('assembly');
  expect(f.b.getAttribute('transform')).toBe(reversing);
  f.tick(600);f.tick(1100);
  f.send('',{reduced_motion:true});
  expect(f.a.getAttribute('transform')).toBe('translate(3 4)');expect(f.b.getAttribute('transform')).toBeNull();
});

it('scrubs the measured plan without an independent tween and restores when leaving timeline mode',()=>{
  const f=disassemblyFixture(true,{a:{x:0,y:-60,order:0},b:{x:40,y:0,order:1}});
  f.send('',{timeline:{id:'assembly',share:.5}});
  expect(f.pending.size).toBe(0);
  expect(f.a.getAttribute('transform')).toContain('translate(0 -41.015625)');
  expect(f.b.getAttribute('transform')).toBe('translate(12.65625 0)');
  f.send('',{timeline:{id:'assembly',share:1}});
  expect(f.b.getAttribute('transform')).toBe('translate(40 0)');
  f.send('',{timeline:{id:'assembly',share:0}});
  expect(f.a.getAttribute('transform')).toBe('translate(3 4)');
  expect(f.b.getAttribute('transform')).toBeNull();
  f.send('',{reduced_motion:true});
  expect(f.a.getAttribute('transform')).toBe('translate(3 4)');
});

it('rejects a partial explicit plan atomically and permits a fixed base',()=>{
  const partial=disassemblyFixture(false,{a:{x:0,y:-40,order:0}});
  partial.send('assembly');
  expect(partial.receipts[partial.receipts.length-1].error).toBe('incomplete_component_plan');
  expect(partial.a.getAttribute('transform')).toBe('translate(3 4)');expect(partial.b.getAttribute('transform')).toBeNull();
  const fixed=disassemblyFixture(false,{a:{x:0,y:-40,order:0},b:{x:0,y:0,order:2}});
  fixed.send('assembly');expect(fixed.b.getAttribute('transform')).toBe('translate(0 0)');
  fixed.send('');expect(fixed.b.getAttribute('transform')).toBeNull();
});

it('smoothly opens and closes, reverses from the current position and ignores canceled frames',()=>{
  const {send,a,b,receipts,pending,tick}=disassemblyFixture(true);
  send('assembly');
  expect(a.getAttribute('transform')).toBe('translate(3 4)');
  expect(receipts[receipts.length-1]).toMatchObject({phase:'opening',expanded_id:''});
  tick(0);tick(180);
  const middle=a.getAttribute('transform');
  expect(middle).not.toBe('translate(3 4)');
  const stale=[...pending.values()][0];
  send(''); expect(receipts[receipts.length-1].phase).toBe('closing');
  stale(360);expect(a.getAttribute('transform')).toBe(middle);
  tick(200);tick(290); const closing=a.getAttribute('transform');
  expect(closing).not.toBe(middle);
  send('assembly'); expect(a.getAttribute('transform')).toBe(closing);
  tick(310);tick(580);
  expect(receipts[receipts.length-1]).toMatchObject({phase:'settled',expanded_id:'assembly'});
  const opened=a.getAttribute('transform');expect(opened).not.toBe(middle);
  send('assembly');expect(pending.size).toBe(0);
  send('');tick(600);tick(960);
  expect(a.getAttribute('transform')).toBe('translate(3 4)');expect(b.getAttribute('transform')).toBeNull();
  expect(receipts[receipts.length-1]).toMatchObject({phase:'settled',expanded_id:''});
  expect(pending.size).toBe(0);
});

it('settles without motion when hidden, offscreen or reduced, and cancels all work on unload',()=>{
  const f=disassemblyFixture(true);
  f.send('assembly');f.tick(0);f.tick(100);f.offscreen();
  expect(f.pending.size).toBe(0);expect(f.receipts[f.receipts.length-1]).toMatchObject({phase:'settled',expanded_id:'assembly'});
  f.send('',{reduced_motion:true});
  expect(f.a.getAttribute('transform')).toBe('translate(3 4)');expect(f.pending.size).toBe(0);
  f.send('assembly',{active:false});expect(f.pending.size).toBe(0);
  f.send('');f.tick(0);f.document.hidden=true;f.handlers.visibilitychange({});
  expect(f.pending.size).toBe(0);expect(f.b.getAttribute('transform')).toBeNull();
  f.document.hidden=false;f.send('assembly');f.media.matches=true;f.handlers.motion({});
  expect(f.pending.size).toBe(0);
  f.media.matches=false;f.send('');f.tick(10);f.handlers.pagehide({});
  expect(f.pending.size).toBe(0);expect(f.a.getAttribute('transform')).toBe('translate(3 4)');
});
