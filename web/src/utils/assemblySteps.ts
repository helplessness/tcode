import type {CanvasAnimation,SolidScene} from '../api/canvas';

export function solidPlans(scene:SolidScene,target=scene.id){
  return target===scene.id?scene.assemblies.flatMap(g=>[g,...g.pieces.filter(p=>p.explode)]):scene.assemblies.find(g=>g.id===target)?.pieces.filter(p=>p.explode)||[];
}
export function assemblySteps(scene:SolidScene|undefined,mode:CanvasAnimation['mode']){
  if(!scene?.bore_fits)return [];
  const plans=solidPlans(scene).filter(p=>p.explode?.some(Boolean));
  const orders=[...new Set(plans.map(p=>p.order))].sort((a,b)=>a-b);
  const steps=orders.map(order=>({plans:plans.filter(p=>p.order===order),sign:1}));
  const reverse=[...steps].reverse().map(step=>({...step,sign:-1}));
  return mode==='assemble'?reverse:mode==='explode'?steps:[...steps,...reverse];
}

export function assemblyStepIndex(progress:number,count:number){
  return Math.min(count-1,Math.max(0,Math.ceil(progress*count-1e-8)-1));
}

export function assemblyDirection(vector:number[],sign:number){
  return vector.map((v,i)=>v?`${'XYZ'[i]}${v*sign>0?' 正向':' 负向'} ${Math.abs(v)}`:'').filter(Boolean).join('、');
}
