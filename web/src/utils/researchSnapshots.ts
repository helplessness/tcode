import type { ResearchSnapshotObject } from '../api/researchChanges';
import type { ResearchExperiment } from '../api/research';
import { experimentParents, type MapPoint } from './researchMap';
export type SnapshotMapNode = MapPoint & {title:string;text:string;status?:string;fields:Record<string,unknown>};
export const researchFieldLabels:Record<string,string>={evidence_relations:'假说证据关系',title:'名称',description:'说明',statement:'判断内容',summary:'摘要',status:'状态',reason:'实验目的',rationale:'提出理由',decision:'实验判断',result_summary:'结果摘要',test_plan:'验证计划',falsification_condition:'反证条件',hypothesis_id:'关联假说',hypothesis_revision:'假说版本',parent_experiment_id:'上级实验',workflow_run_id:'计算任务',run_snapshot:'计算与来源信息',metrics:'指标',input_artifacts:'输入产物',output_artifacts:'输出产物',evidence_refs:'支持证据引用',counter_evidence_refs:'反对证据引用',evidence_ids:'证据引用',source_event_ids:'来源活动引用',original_refs:'原判断引用',challenging_refs:'质疑引用',original_evidence_ids:'原证据引用',challenging_evidence_ids:'质疑证据引用',current_revision:'结论版本',current_resolution_id:'解决记录',provenance:'来源类型',scope:'适用范围',conflict_type:'冲突类型',origin_candidate_ids:'来源候选'};
const readableValues:Record<string,string>={active:'进行中',archived:'已归档',planned:'待执行',running:'进行中',completed:'已完成',failed:'失败',cancelled:'已取消',paused:'已暂停',testing:'验证中',approved:'已批准',proposed:'待验证',supported:'已有支持',refuted:'已否定',inconclusive:'尚无定论',frozen:'已冻结',open:'待处理',resolved:'已处理',confirmed:'已确认',challenged:'受到质疑',superseded:'已被修正',draft:'草稿',accepted:'已接受',rejected:'已拒绝',pending:'待确认',source_fact:'来源事实',model_inference:'模型推断',computation:'计算结果'};
export function researchFieldText(key:string,value:unknown,names:Record<string,string>={}):string {
  if(value===null||value===undefined||value==='')return '未记录';
  if(key==='hypothesis_id'||key==='parent_experiment_id')return names[key]||(key==='hypothesis_id'?'未保存假说名称':'未保存实验名称');
  if(key==='status'||key==='decision'||key==='provenance'||key==='conflict_type')return readableValues[String(value)]||'已记录，含义待补充';
  if(key==='hypothesis_revision'||key==='current_revision')return `第 ${value} 版`;
  if(key==='workflow_run_id')return '已关联计算任务';
  if(key==='current_resolution_id')return '已保存处理记录';
  if(key==='evidence_relations'&&Array.isArray(value))return value.map(r=>`${r.source||'未保存来源名称'}：${({supports:'支持',contradicts:'反对',context_only:'相关但不足',references:'待判断'} as Record<string,string>)[r.relation]||'待判断'}；${r.state==='removed'?'已移出':r.review_status==='confirmed'?'记录为用户已核对':'待核对'}；理由：${r.reason||'未记录'}；适用条件：${r.scope||'未记录'}`).join('\n')||'未记录';
  if(Array.isArray(value))return value.length?`已记录 ${value.length} 项${key.includes('artifact')?'产物':'来源或关联信息'}`:'未记录';
  if(key==='metrics'&&typeof value==='object'){
    const labels:Record<string,string>={score:'评分',confidence:'置信度',accuracy:'准确率',temperature:'温度',energy:'能量',rmsd:'结构偏差',iptm:'界面可信度',ptm:'整体可信度',plddt:'局部可信度'};
    return Object.entries(value).map(([k,v],i)=>`${labels[k]||`指标 ${i+1}`}：${typeof v==='number'?v:typeof v==='boolean'?(v?'是':'否'):'已记录'}`).join('；')||'未记录';
  }
  if(typeof value==='object')return Object.keys(value).length?`已保存 ${Object.keys(value).length} 项${key==='metrics'?'指标':'相关信息'}`:'未记录';
  return String(value);
}
export function snapshotMapNodes(objects:ResearchSnapshotObject[]):SnapshotMapNode[] {
  const ids=new Set(objects.map(o=>o.id));
  const titles=new Map(objects.map(o=>[o.id,o.title]));
  const fields=(data:Record<string,unknown>)=>Object.fromEntries(Object.entries(data).filter(([,v])=>v!==null&&v!==undefined&&v!==''&&(!Array.isArray(v)||v.length)).map(([k,v])=>[researchFieldLabels[k]||'补充信息',researchFieldText(k,v,{hypothesis_id:titles.get(`hypothesis:${data.hypothesis_id}`)||'',parent_experiment_id:titles.get(`experiment:${data.parent_experiment_id}`)||''})]));
  const experiments=objects.filter(o=>o.kind==='experiment');
  const parents=experimentParents(experiments.map(o=>({...o.data,experiment_id:o.object_id}) as ResearchExperiment));
  const nodes:SnapshotMapNode[]=objects.filter(o=>o.kind!=='experiment').map(o=>({id:o.id,parent:o.kind==='project'?'':'project',kind:o.kind,title:o.title,text:String(o.data.description||o.data.statement||o.data.summary||''),status:String(o.data.status||''),fields:fields(o.data)}));
  const sessionIds=new Set(experiments.map(o=>(o.data.run_snapshot as Record<string,unknown>|undefined)?.session_id).filter((s):s is string=>typeof s==='string' && !!s));
  sessionIds.forEach(id=>nodes.push({id:`session:${id}`,parent:'project',kind:'session',title:'来源会话',text:'此版本只保存会话引用，不还原历史会话标题或消息。',fields:{'来源信息':'已保存来源会话关联；此版本未保存会话名称。'}}));
  const rows=experiments.map(o=>{
    const parentId=parents.get(o.object_id), session=(o.data.run_snapshot as Record<string,unknown>|undefined)?.session_id;
    const parent=parentId?`experiment:${parentId}`:typeof session==='string'&&session?`session:${session}`:ids.has(`hypothesis:${o.data.hypothesis_id}`)?`hypothesis:${o.data.hypothesis_id}`:'project';
    return {id:o.id,parent,kind:'experiment' as const,title:o.title,text:String(o.data.result_summary||o.data.reason||''),status:String(o.data.status||''),fields:fields(o.data)};
  });
  const append=(parent:string)=>rows.filter(n=>n.parent===parent).forEach(n=>{nodes.push(n);append(n.id);});
  [...nodes].forEach(n=>append(n.id));
  return nodes;
}
