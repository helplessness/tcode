import { For, Show, createEffect, createSignal, untrack } from 'solid-js';
import { ontologyRequest } from '../api/ontology';
import { openCitation } from '../utils/citations';
import { conflictLabels } from '../utils/ontologyConflicts';
import { SUPPORT_LABELS, statementConditions, supportReason } from '../utils/ontologySupport';

const BASIS_LABELS:Record<string,string>={experimental:'实验观察',computational:'计算结果',literature_report:'文献转述',hypothesis:'未验证假设',unspecified:'未标明'};
const KIND_LABELS:Record<string,string>={reported:'已报道',card_reported:'来源已报道',hypothesis:'假设',analogy:'类比',annotation:'标注',curator_inference:'整理者推断',unspecified:'未标明'};

export default function OntologyEvidenceReport(props:{workspaceId:string;facts:any[]}) {
  const [text,setText]=createSignal('');
  const [selected,setSelected]=createSignal('');
  const [conditions,setConditions]=createSignal<Array<{key:string;value:string}>>([]);
  const [polarity,setPolarity]=createSignal('supports');
  const [kind,setKind]=createSignal('reported');
  const [basis,setBasis]=createSignal('any');
  const [report,setReport]=createSignal<any>();
  const [busy,setBusy]=createSignal(false);
  const [error,setError]=createSignal('');
  let generation=0;
  const invalidate=()=>{++generation;setReport(undefined);setError('');setBusy(false);};
  const target=()=>props.facts.find(row=>row.id===selected());
  const title=(row:any)=>`${row.subject_label||row.subject_id} → ${row.predicate} → ${row.object_label||row.object_id}`;
  createEffect(()=>{props.workspaceId;setText('');setSelected('');setConditions([]);setBasis('any');});
  createEffect(()=>{
    const facts=props.facts;invalidate();
    if (!facts.some(row=>row.id===untrack(selected))) {setSelected('');setConditions([]);}
  });
  const choose=(id:string)=>{
    invalidate();setSelected(id);
    const row=props.facts.find(item=>item.id===id);
    setConditions(Object.entries(row?.qualifiers||{}).filter(([key])=>!['evidence_basis','primary_reference'].includes(key)).map(([key,value])=>({key,value:JSON.stringify(value)??'null'})));
    if (row) {setPolarity(row.polarity||'supports');setKind(row.kind==='hypothesis'?'hypothesis':'reported');if (!text())setText(title(row));}
  };
  const check=async()=>{
    const version=++generation;setBusy(true);setReport(undefined);setError('');
    try {
      const fact=target();
      const claim={text:text().trim(),assertion_ids:props.facts.map(row=>row.id),required_evidence_basis:basis(),
        ...(fact?{statement:{subject_id:fact.subject_id,predicate:fact.predicate,object_id:fact.object_id,polarity:polarity(),kind:kind(),qualifiers:statementConditions(conditions())}}:{})};
      const saved=await ontologyRequest<any>(props.workspaceId,'/reports/validate',{claims:[claim]});
      const latest=await ontologyRequest<any>(props.workspaceId,`/reports/${saved.report_id}`);
      if (version===generation)setReport(latest);
    } catch(cause) {
      if(version===generation){const message=cause instanceof Error?cause.message:String(cause);setError(/withdrawn|inaccessible|unknown.*evidence/i.test(message)?'引用已撤回、失效或无法访问，请重新选择证据后核对。':message);}
    }
    finally {if(version===generation)setBusy(false);}
  };
  return <section class="ontology-support-editor" aria-label="结论与证据核对">
    <h3>结论与证据核对</h3><p>已选择 {props.facts.length} 条引用。选择明确的事实对象并核对结论条件；原文说明仍需人工审阅。</p>
    <label>结论说明<textarea value={text()} onInput={e=>{invalidate();setText(e.currentTarget.value);}} placeholder="说明选中证据支持什么，以及有哪些限制"/></label>
    <label>结论核对对象<select aria-label="结论核对对象" value={selected()} onChange={e=>choose(e.currentTarget.value)}><option value="">仅自由文本（不能自动判断支持性）</option><For each={props.facts}>{row=><option value={row.id}>{title(row)}</option>}</For></select></label>
    <Show when={target()}>{row=><><p class="ontology-support-target">核对事实：{title(row())}</p><div class="ontology-filter-grid"><label>结论方向<select aria-label="结论方向" value={polarity()} onChange={e=>{invalidate();setPolarity(e.currentTarget.value);}}><option value="supports">支持该关系</option><option value="opposes">反对该关系</option></select></label><label>结论性质<select aria-label="结论性质" value={kind()} onChange={e=>{invalidate();setKind(e.currentTarget.value);}}><option value="reported">已报道事实</option><option value="hypothesis">待验证假设</option></select></label></div>
      <p>以下为结论适用的条件。移除已有条件可能构成过度推广；补入未记录的条件不能获得完整支持。</p>
      <For each={conditions()}>{(condition,index)=><div class="ontology-condition-row"><label>条件名称<input aria-label={`结论条件 ${index()+1} 名称`} value={condition.key} onInput={e=>{invalidate();setConditions(old=>old.map((item,i)=>i===index()?{...item,key:e.currentTarget.value}:item));}}/></label><label>条件值<input aria-label={`结论条件 ${index()+1} 值`} value={condition.value} onInput={e=>{invalidate();setConditions(old=>old.map((item,i)=>i===index()?{...item,value:e.currentTarget.value}:item));}}/></label><button onClick={()=>{invalidate();setConditions(old=>old.filter((_,i)=>i!==index()));}}>移除结论条件 {index()+1}</button></div>}</For>
      <button disabled={conditions().length>=20} onClick={()=>{invalidate();setConditions(old=>[...old,{key:'',value:''}]);}}>添加结论条件</button>
    </>}</Show>
    <label>所需证据类型<select aria-label="所需证据类型" value={basis()} onChange={e=>{invalidate();setBasis(e.currentTarget.value);}}><option value="any">不限定类型（仍保留证据限制）</option><option value="experimental">必须有实验观察</option><option value="computational">必须有计算结果</option><option value="literature_report">必须有文献转述</option><option value="hypothesis">只核对假设类证据</option></select></label>
    <button disabled={busy()||!props.facts.length||!text().trim()} onClick={()=>void check()}>{busy()?'正在核对…':'检查并保存证据草稿'}</button>
    <Show when={error()}><p role="alert">{error()}</p></Show>
    <Show when={report()}>{value=><div class="ontology-support-results"><p role="status">引用在本次检查时有效。{value().delivery_status==='ready_for_human_review'?'结构化检查通过，仍需人工核对结论说明。':'存在需要复核的问题，当前为证据草稿。'}</p><p>此检查不认证科学正确性，也不验证自由文本与所选事实是否语义一致。</p>
      <For each={value().assessments}>{assessment=><article data-support-status={assessment.status}><h4>{SUPPORT_LABELS[assessment.status]||'需要复核'}</h4><ul><For each={assessment.reasons}>{reason=><li>{supportReason(reason)}</li>}</For></ul>
        <For each={[...assessment.evidence_checks,...assessment.counterevidence]}>{check=><details><summary>{SUPPORT_LABELS[check.status]||'需要复核'} · {check.source_id||check.assertion_id}</summary><ul><For each={check.reasons}>{reason=><li>{supportReason(reason)}</li>}</For></ul><Show when={check.conflict_categories?.length}><p>比较分类：{check.conflict_categories.map((key:string)=>conflictLabels[key]||key).join('；')}</p></Show><Show when={check.quote}><blockquote>{check.quote}</blockquote></Show><p>证据条件：{Object.entries(check.recorded_conditions||{}).map(([key,value])=>`${key}=${typeof value==='string'?value:JSON.stringify(value)}`).join('；')||'未记录具体条件'}</p><Show when={check.missing_conditions?.length}><p>证据未记录：{check.missing_conditions.join('、')}</p></Show><p>证据类型：{BASIS_LABELS[check.evidence_basis]||'未标明'}；性质：{KIND_LABELS[check.kind]||'未标明'}</p><For each={check.evidence_ids}>{id=><button onClick={event=>openCitation({kind:'ontology_evidence',id},event.currentTarget)}>查看证据原文</button>}</For></details>}</For>
      </article>}</For><small>只检查当前可见知识，不代表已穷尽所有研究资料。再次读取报告时会重新检查引用和相反证据。</small>
    </div>}</Show>
  </section>;
}
