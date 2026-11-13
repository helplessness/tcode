import { requestJson, workspaceQuery } from './research';
export type ResearchSnapshotObject = { id:string; kind:'project'|'hypothesis'|'experiment'|'finding'|'conflict'; object_id:string; title:string; data:Record<string,unknown> };
export type ResearchSnapshot = { snapshot_id:string; research_id:string; title:string; captured_at:string; hidden:number; content_hash:string; document:{schema:string;objects:ResearchSnapshotObject[]} };
export type SnapshotReceipt = Omit<ResearchSnapshot,'document'>;
export type ResearchChange = { id:string; kind:ResearchSnapshotObject['kind']; title:string; status:'added'|'removed'|'changed'; fields:string[]; source_changed:boolean; before_names?:Record<string,string>; after_names?:Record<string,string>; before:Record<string,unknown>|null; after:Record<string,unknown>|null };
export type ResearchChanges = { schema:string; research_id:string; base:SnapshotReceipt; head:SnapshotReceipt; counts:{added:number;removed:number;changed:number};source_changed_count:number;total:number;changes:ResearchChange[];next_offset:number|null;history_notice:string };
export type ResearchChangesLink = {schema:'tcode.research-changes-link.v1';workspace_id:string;research_id:string;base_snapshot_id:string;head_snapshot_id:string};
export type ResearchChangeFocus = { baseId:string; headId:string; nodeId?:string; side?:'before'|'after'; key:number };
export function researchChangesLink(value: unknown): ResearchChangesLink | null {
  if (!value || typeof value !== 'object') return null;
  const link = value as Record<string,unknown>;
  return link.schema === 'tcode.research-changes-link.v1' && ['workspace_id','research_id','base_snapshot_id','head_snapshot_id'].every(k=>typeof link[k]==='string' && !!(link[k] as string).trim() && (link[k] as string).length<=160) ? link as ResearchChangesLink : null;
}
export function researchChangesLinks(tools:readonly {tool_name:string;state?:string;error?:string;metadata?:Record<string,unknown>}[]) {
  const links=new Map<string,ResearchChangesLink>();
  for(const tool of tools){
    if(tool.tool_name!=='research_workspace'||tool.state!=='completed'||tool.error)continue;
    const link=researchChangesLink(tool.metadata?.research_changes);
    if(link)links.set(JSON.stringify([link.workspace_id,link.research_id,link.base_snapshot_id,link.head_snapshot_id]),link);
  }
  return [...links.values()];
}
export function snapshotRequest<T>(path:string, workspaceId?:string, body?:unknown, method='GET') {
  return requestJson<T>(`/api/research/${path}?${workspaceQuery(workspaceId)}`, {method,credentials:'same-origin',...(body ? {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});
}
export function compareSnapshots(baseId:string,headId:string,workspaceId?:string,offset=0) {
  const params=workspaceQuery(workspaceId); params.set('offset',String(offset));
  return requestJson<ResearchChanges>(`/api/research/snapshots/${encodeURIComponent(baseId)}/compare/${encodeURIComponent(headId)}?${params}`);
}
export function listSnapshots(researchId:string,workspaceId?:string,offset=0,hidden=false) {
  const params=workspaceQuery(workspaceId);params.set('offset',String(offset));params.set('hidden',String(hidden));
  return requestJson<{items:SnapshotReceipt[];next_offset:number|null}>(`/api/research/projects/${encodeURIComponent(researchId)}/snapshots?${params}`);
}
export function openResearchChanges(link:ResearchChangesLink,nodeId='project',side:'before'|'after'='after') {
  return new Promise<void>(resolve=>window.dispatchEvent(new CustomEvent('tcode-open-research-changes',{detail:{...link,nodeId,side,onSettled:resolve}})));
}
