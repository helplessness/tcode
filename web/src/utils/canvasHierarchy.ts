import type { CanvasData, CanvasPart } from '../api/canvas';

// Only actual SVG ancestry defines a group; names never imply containment.
export function canvasHierarchy(parts:CanvasPart[], elements:CanvasData['elements']) {
  const byId=new Map(parts.map(part=>[part.element_id,part]));
  const elementsById=new Map(elements.map(element=>[element.element_id,element]));
  const parent=new Map<string,string>();
  for(const part of parts) {
    const seen=new Set([part.element_id]);
    let id=elementsById.get(part.element_id)?.parent_id||'';
    while(id&&!seen.has(id)) {
      seen.add(id);
      if(byId.has(id)){parent.set(part.element_id,id);break;}
      id=elementsById.get(id)?.parent_id||'';
    }
  }
  const trail=(id:string)=>{
    const result:CanvasPart[]=[],seen=new Set<string>();
    while(byId.has(id)&&!seen.has(id)){seen.add(id);result.unshift(byId.get(id)!);id=parent.get(id)||'';}
    return result;
  };
  const children=(id:string)=>parts.filter(part=>(parent.get(part.element_id)||'')===id);
  const navigation=(focus:string)=>children(focus).length?children(focus):children(parent.get(focus)||'');
  const target=(focus:string,hit:string)=>{
    const path=trail(hit).map(part=>part.element_id);
    const index=path.indexOf(focus);
    return path.length?path[index>=0?Math.min(index+1,path.length-1):0]:hit;
  };
  const assembly=(id:string)=>[...trail(id)].reverse().find(part=>children(part.element_id).length)?.element_id||'';
  return {parent,trail,children,navigation,target,assembly};
}
