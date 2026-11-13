import type { ResearchChange, ResearchSnapshotObject } from '../api/researchChanges';
import { researchMapAncestors, researchMapLayout, type MapPoint } from './researchMap';
import { researchFieldLabels, researchFieldText, snapshotMapNodes } from './researchSnapshots';

export const comparisonLabels = { added:'新增', removed:'删除', changed:'内容修改', relation:'关系修改' };
export type ComparisonMark = { status:'added'|'removed'|'changed'; relationChanges:string[]; parentChanged?:boolean };
function signature(value:unknown):string {
  if(Array.isArray(value))return `[${value.map(signature).join(',')}]`;
  if(value&&typeof value==='object')return `{${Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>`${JSON.stringify(k)}:${signature(v)}`).join(',')}}`;
  return JSON.stringify(value)??'null';
}
const relationFields=['hypothesis_id','hypothesis_revision','parent_experiment_id','evidence_refs','counter_evidence_refs','evidence_ids','evidence_relations','original_refs','challenging_refs','original_evidence_ids','challenging_evidence_ids','source_event_ids'];
export function comparisonChangeLabel(change:ResearchChange) {
  if(change.status!=='changed')return comparisonLabels[change.status];
  const a=change.before||{},b=change.after||{};
  const session=(data:Record<string,unknown>)=>(data.run_snapshot as Record<string,unknown>|undefined)?.session_id||'';
  return relationFields.some(field=>signature(a[field]??null)!==signature(b[field]??null))||session(a)!==session(b)?comparisonLabels.relation:comparisonLabels.changed;
}

function relationChanges(before:ResearchSnapshotObject, after:ResearchSnapshotObject,
  beforeObjects:ResearchSnapshotObject[], afterObjects:ResearchSnapshotObject[]) {
  const names=(objects:ResearchSnapshotObject[], data:Record<string,unknown>)=>({
    hypothesis_id:objects.find(o=>o.id===`hypothesis:${data.hypothesis_id}`)?.title || '',
    parent_experiment_id:objects.find(o=>o.id===`experiment:${data.parent_experiment_id}`)?.title || '',
  });
  const changes:string[]=[];
  for(const field of relationFields) {
    const a=before.data[field]??null,b=after.data[field]??null;
    if(signature(a)===signature(b))continue;
    changes.push(`${researchFieldLabels[field]}：${a===null?'未关联':researchFieldText(field,a,names(beforeObjects,before.data))} → ${b===null?'未关联':researchFieldText(field,b,names(afterObjects,after.data))}`);
  }
  const session=(o:ResearchSnapshotObject)=>(o.data.run_snapshot as Record<string,unknown>|undefined)?.session_id || '';
  if(session(before)!==session(after))changes.push(`来源会话：${session(before)?'原来源会话':'未关联'} → ${session(after)?'新的来源会话':'未关联'}`);
  return changes;
}

export function snapshotComparison(before:ResearchSnapshotObject[], after:ResearchSnapshotObject[]) {
  const beforeNodes=snapshotMapNodes(before),afterNodes=snapshotMapNodes(after);
  // Retained identities keep baseline ancestry for placement only. Actual edges
  // are always rendered from the chosen snapshot, never from this union tree.
  const union=new Map<string,MapPoint>(beforeNodes.map(n=>[n.id,n]));
  afterNodes.forEach(n=>{if(!union.has(n.id))union.set(n.id,n);});
  const anchors=[...union.values()];
  const tree=researchMapLayout(anchors,new Set(anchors.map(n=>n.id)),true);
  // Room for the right-hand detour when a changed parent shares the same column.
  const layout={...tree,width:tree.width+120};
  const a=new Map(before.map(o=>[o.id,o])), b=new Map(after.map(o=>[o.id,o]));
  const marks=new Map<string,ComparisonMark>();
  for(const id of new Set([...a.keys(),...b.keys()])) {
    const old=a.get(id),next=b.get(id);
    if(!old || !next)marks.set(id,{status:old?'removed':'added',relationChanges:[]});
    else if(signature([old.title,old.data])!==signature([next.title,next.data]))
      marks.set(id,{status:'changed',relationChanges:relationChanges(old,next,before,after),
        parentChanged:beforeNodes.find(n=>n.id===id)?.parent!==afterNodes.find(n=>n.id===id)?.parent});
  }
  const nodeIds={before:new Set(beforeNodes.map(n=>n.id)),after:new Set(afterNodes.map(n=>n.id))};
  for(const id of union.keys())if(!marks.has(id)&&nodeIds.before.has(id)!==nodeIds.after.has(id))
    marks.set(id,{status:nodeIds.before.has(id)?'removed':'added',relationChanges:[]});
  return {layout,marks,nodeIds};
}
export type SnapshotComparison = ReturnType<typeof snapshotComparison>;

export function comparisonLayout(comparison:SnapshotComparison,nodes:MapPoint[],expanded:Set<string>) {
  const points=new Map(comparison.layout.points);
  for(const node of nodes) {
    const anchor=points.get(node.id);
    if(anchor)points.set(node.id,{...anchor,visible:!node.parent || [...researchMapAncestors(nodes,node.id)].every(id=>expanded.has(id))});
  }
  return {...comparison.layout,points};
}
