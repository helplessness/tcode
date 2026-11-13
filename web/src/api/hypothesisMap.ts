import {requestJson,workspaceQuery} from './research';
export type EvidenceRelation={id:string;object_type:string;object_id:string;relation:string;title:string;source_status:string;review_status:string;reason:string;scope:string;revision:number;legacy:boolean};
export type MapHypothesis={hypothesis_id:string;title:string;status:string;revision:number;rationale:string;test_plan:string;falsification_condition:string;relations:EvidenceRelation[];relation_total:number;relation_offset:number;relation_next_offset:number|null};
export type MapSource={object_type:string;object_id:string;title:string;status:string;source_type?:string;source_label?:string;content_label?:string;content?:string;locator?:string;content_hash?:string};
export type HypothesisGraph={research_id:string;hypotheses:MapHypothesis[];sources:MapSource[];total:number;next_offset:number|null;source_total:number;source_offset:number;source_next_offset:number|null;notice:string;readonly?:boolean};
export type HypothesisMapLink={schema:'tcode.hypothesis-map-link.v1';workspace_id:string;research_id:string;hypothesis_id:string};
export type HypothesisFocus={hypothesisId:string;objectId?:string;objectType?:string;key:number};
export function hypothesisMapLink(value:unknown):HypothesisMapLink|null {
  if(!value||typeof value!=='object')return null;
  const v=value as Record<string,unknown>;
  return v.schema==='tcode.hypothesis-map-link.v1'&&['workspace_id','research_id'].every(k=>typeof v[k]==='string'&&!!String(v[k]).trim()&&String(v[k]).length<=160)&&typeof v.hypothesis_id==='string'&&v.hypothesis_id.length<=160?v as HypothesisMapLink:null;
}
export function hypothesisMapLinks(tools:readonly {tool_name:string;state?:string;error?:string;metadata?:Record<string,unknown>}[]) {
  const links=new Map<string,HypothesisMapLink>();
  for(const tool of tools){if(tool.tool_name!=='research_workspace'||tool.state!=='completed'||tool.error)continue;const link=hypothesisMapLink(tool.metadata?.hypothesis_map);if(link)links.set(JSON.stringify(link),link);}
  return [...links.values()];
}
export function getHypothesisMap(researchId:string,workspaceId?:string,offset=0,hypothesisId='',assetId='',relationOffset=0,sourceOffset=0) {
  const params=workspaceQuery(workspaceId);params.set('offset',String(offset));params.set('relation_offset',String(relationOffset));params.set('source_offset',String(sourceOffset));if(hypothesisId)params.set('hypothesis_id',hypothesisId);
  return requestJson<HypothesisGraph>(`/api/research/${assetId?'pack-assets':'projects'}/${encodeURIComponent(assetId||researchId)}/hypothesis-map?${params}`);
}
export function getHypothesisSource(researchId:string,source:MapSource|EvidenceRelation,workspaceId?:string,assetId=''){const params=workspaceQuery(workspaceId);params.set('object_type',source.object_type);params.set('object_id',source.object_id);return requestJson<MapSource>(`/api/research/${assetId?'pack-assets':'projects'}/${encodeURIComponent(assetId||researchId)}/hypothesis-map/source?${params}`);}
export function reviewHypothesisSource(researchId:string,hypothesisId:string,body:Record<string,unknown>,workspaceId?:string){return requestJson(`/api/research/projects/${encodeURIComponent(researchId)}/hypothesis-map/${encodeURIComponent(hypothesisId)}/review?${workspaceQuery(workspaceId)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
export function openHypothesisMap(link:HypothesisMapLink,hypothesisId=link.hypothesis_id,source?:EvidenceRelation){return new Promise<void>(resolve=>window.dispatchEvent(new CustomEvent('tcode-open-hypothesis-map',{detail:{...link,hypothesis_id:hypothesisId,objectId:source?.object_id,objectType:source?.object_type,onSettled:resolve}})));}
