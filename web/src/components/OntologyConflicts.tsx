import { For, Show, createEffect, createSignal, on, onCleanup } from 'solid-js';
import { ontologyRequest } from '../api/ontology';
import { conflictLabels } from '../utils/ontologyConflicts';
import { SUPPORT_LABELS } from '../utils/ontologySupport';
import { confirmAction } from './FeedbackDialog';
import { openCitation } from '../utils/citations';
import './OntologyConflicts.css';

const actions: Record<string,string> = {
  different_conditions:'记录为条件不同', insufficient_information:'标记待补充信息',
  retain_dispute:'保留争议', withdraw_incorrect:'撤回错误私有主张', reopen:'重新打开核对',
};
const time = (value:number) => new Date(value*1000).toLocaleString();

export default function OntologyConflicts(props:{workspaceId:string;leftId?:string;rightId?:string;onChanged?:()=>void;onClose?:()=>void}) {
  const [current,setCurrent]=createSignal<any>();
  const [history,setHistory]=createSignal<any[]>([]);
  const [historyCursor,setHistoryCursor]=createSignal('');
  const [action,setAction]=createSignal('retain_dispute');
  const [reason,setReason]=createSignal('');
  const [withdrawId,setWithdrawId]=createSignal('');
  const [busy,setBusy]=createSignal(false);
  const [error,setError]=createSignal('');
  const [notice,setNotice]=createSignal('');
  const [rechecked,setRechecked]=createSignal<Record<string,any>>({});
  let generation=0, requestSerial=0;
  onCleanup(()=>{generation++;requestSerial++;});
  const request=async(operation:()=>Promise<void>)=>{
    const serial=++requestSerial;
    setError('');setBusy(true);
    try{await operation();}catch(cause){if(serial===requestSerial){const message=cause instanceof Error?cause.message:String(cause);setError(/changed; refresh/.test(message)?'证据或处理记录已变化，请刷新后重新决定。':/assertion is withdrawn/.test(message)?'主张已撤回，请查看最新证据。':message);}}
    finally{if(serial===requestSerial)setBusy(false);}
  };
  const loadCase=async(left:string,right:string)=>{
    const version=++generation;
    setCurrent(undefined);setReason('');setWithdrawId('');setRechecked({});
    const params=new URLSearchParams({left_id:left,right_id:right});
    const result=await ontologyRequest<any>(props.workspaceId,`/conflicts/pair?${params}`);
    if(version!==generation)return;
    setCurrent(result);
    setAction(result.condition_relation==='different_conditions'?'different_conditions':result.condition_relation==='unknown_conditions'?'insufficient_information':'retain_dispute');
  };
  const loadHistory=async(after='')=>{
    const version=++generation;
    const result=await ontologyRequest<any>(props.workspaceId,`/conflicts?after=${encodeURIComponent(after)}`);
    if(version===generation){setHistory(old=>after?[...old,...result.cases]:result.cases);setHistoryCursor(result.next_cursor||'');}
  };
  createEffect(on(()=>[props.workspaceId,props.leftId,props.rightId],()=>{
    ++generation;setCurrent(undefined);setHistory([]);setNotice('');setError('');
    void request(()=>props.leftId&&props.rightId?loadCase(props.leftId,props.rightId):loadHistory());
  }));
  const save=async()=>{
    const value=current();if(!value)return;
    const workspace=props.workspaceId, started=generation;
    if(action()==='withdraw_incorrect' && !await confirmAction('将撤回选中的私有主张。历史处理记录会保留，引用该主张的报告将需要重新核对。确认继续？'))return;
    if(started!==generation||workspace!==props.workspaceId)return;
    await request(async()=>{
      const version=generation;
      const result=await ontologyRequest<any>(workspace,'/conflicts/decisions',{
        left_id:value.facts[0].id,right_id:value.facts[1].id,action:action(),reason:reason(),
        expected_snapshot:value.snapshot,expected_version:value.version,
        withdraw_id:action()==='withdraw_incorrect'?withdrawId():'',
      });
      if(version!==generation)return;
      setCurrent(result);setReason('');setNotice('处理决定已记录；自动证据检查不会因人工备注而变成通过。');
      props.onChanged?.();
    });
  };
  const refresh=()=>{const value=current();if(value)void request(()=>loadCase(value.facts[0].id,value.facts[1].id));};
  return <section class="ontology-conflicts" aria-label="冲突处理与影响范围">
    <header><div><h3>冲突处理与影响范围</h3><p>保留原始主张和人工处理记录；处理备注不会认证结论。</p></div><Show when={props.onClose}><button type="button" onClick={props.onClose}>关闭处理详情</button></Show></header>
    <Show when={error()}><p role="alert">{error()}<button type="button" disabled={busy()} onClick={refresh}>刷新当前证据</button></p></Show>
    <Show when={notice()}><p role="status">{notice()}</p></Show>
    <Show when={busy()}><p role="status">正在读取或保存核对记录…</p></Show>
    <Show when={!props.leftId&&!current()}><p>此处保留已记录的处理。新争议可从知识查询或关系探索进入。</p><For each={history()}>{item=><Show when={!item.unavailable} fallback={<article><p>原主张已不可访问，处理记录需要重新核对。</p><strong>{actions[item.latest_decision?.action]}</strong><p>{item.latest_decision?.reason}</p><small>{item.latest_decision?.actor} · {time(item.latest_decision?.created_at)}</small></article>}><button type="button" disabled={busy()} class="ontology-conflict-history" onClick={()=>void request(()=>loadCase(item.facts[0].id,item.facts[1].id))}>
      {item.facts[0].subject_label} → {item.facts[0].predicate} → {item.facts[0].object_label}<br/>{actions[item.latest_decision?.action] || '待处理'} · {item.decision_stale?'证据已变化，需重新核对':item.active?'主张保留':'含已撤回主张'}
    </button></Show>}</For><Show when={historyCursor()}><button type="button" disabled={busy()} onClick={()=>void request(()=>loadHistory(historyCursor()))}>更多处理记录</button></Show></Show>
    <Show when={current()}>{value=><>
      <Show when={!props.leftId}><button type="button" disabled={busy()} onClick={()=>{setCurrent(undefined);void request(()=>loadHistory());}}>返回处理记录</button></Show>
      <p class="ontology-conflict-status">{value().active?'系统比较：':'撤回前的比较：'}{value().active?conflictLabels[value().condition_relation]:conflictLabels[value().condition_relation]?.replace('，待核对','')}<br/>人工处理：{actions[value().latest_decision?.action] || '尚未记录'}<Show when={value().decision_stale}> · 证据已变化，旧决定不再适用</Show></p>
      <div class="ontology-conflict-facts"><For each={value().facts}>{fact=><article><h4>{fact.polarity==='supports'?'支持主张':'反对主张'} · {fact.scope==='private'?'Workspace 私有':'公共知识'}</h4><p>{fact.subject_label} → {fact.predicate} → {fact.object_label}</p><p>{fact.active?'当前有效':'已撤回'} · {fact.source_id||'来源未标识'}</p><p>记录条件：{JSON.stringify(fact.qualifiers||{})}</p><blockquote>{fact.quote||'原文缺失'}</blockquote><For each={fact.evidence_ids||[]}>{id=><button type="button" onClick={event=>openCitation({kind:'ontology_evidence',id},event.currentTarget)}>查看该主张原文</button>}</For></article>}</For></div>
      <Show when={value().active} fallback={<p>主张已撤回，历史决定和影响记录仍保留。撤回不会自动修改已有报告。</p>}>
        <form onSubmit={event=>{event.preventDefault();void save();}}>
          <label>处理决定<select aria-label="冲突处理决定" value={action()} disabled={busy()} onChange={event=>setAction(event.currentTarget.value)}><For each={Object.entries(actions)}>{([key,label])=><option value={key} disabled={key==='different_conditions'&&value().condition_relation!=='different_conditions'}>{label}</option>}</For></select></label>
          <Show when={action()==='withdraw_incorrect'}><label>撤回哪条私有主张<select aria-label="撤回的私有主张" value={withdrawId()} onChange={event=>setWithdrawId(event.currentTarget.value)}><option value="">请选择要撤回的主张</option><For each={value().facts.filter((fact:any)=>fact.scope==='private')}>{fact=><option value={fact.id}>{fact.polarity==='supports'?'支持':'反对'} · {fact.source_id}</option>}</For></select></label></Show>
          <label>处理理由<textarea aria-label="冲突处理理由" maxlength={4000} required value={reason()} onInput={event=>setReason(event.currentTarget.value)} placeholder="说明判断依据、缺少的信息或撤回原因"/></label>
          <button type="submit" disabled={busy()||!reason().trim()||(action()==='withdraw_incorrect'&&!withdrawId())}>保存处理决定</button>
        </form>
      </Show>
      <details open><summary>受影响的已保存报告</summary><p>包含直接引用或相同结构化关系的报告；重新核对不会自动改写结论。</p><Show when={!value().impacts.reports.length}><p>当前未找到受影响的已保存报告。</p></Show>
        <For each={value().impacts.reports}>{item=><article class="ontology-conflict-impact"><strong>{item.claims[0]?.text || '已保存的证据草稿'}</strong><p>{item.needs_recheck?'知识版本已变化，需要重新核对':item.last_check?.reference_status==='invalid_references'?'已重新核对：引用失效，需重选证据':`已在当前知识版本核对，仍需人工审阅`}</p><Show when={item.last_check}><small>上次核对：{time(item.last_check.checked_at)} · {item.last_check.statuses.map((status:string)=>SUPPORT_LABELS[status]||status).join('；')}</small></Show>
          <button type="button" disabled={busy()} onClick={()=>void request(async()=>{const version=generation;const result=await ontologyRequest<any>(props.workspaceId,`/reports/${item.report_id}/recheck`,{});if(version!==generation)return;setRechecked(old=>({...old,[item.report_id]:result}));const params=new URLSearchParams({left_id:value().facts[0].id,right_id:value().facts[1].id});const latest=await ontologyRequest<any>(props.workspaceId,`/conflicts/pair?${params}`);if(version===generation)setCurrent(latest);})}>重新核对该报告</button>
          <Show when={rechecked()[item.report_id]}>{result=><p role="status">{result().reference_status==='invalid_references'?'引用已撤回或失效，请重选证据后修改结论。':`重新核对结果：${result().assessments.map((row:any)=>SUPPORT_LABELS[row.status]||row.status).join('；')}，仍需人工审阅。`}</p>}</Show>
        </article>}</For>
        <Show when={value().impacts.next_cursor}><button type="button" disabled={busy()} onClick={()=>void request(async()=>{const version=generation;const params=new URLSearchParams({left_id:value().facts[0].id,right_id:value().facts[1].id,after:value().impacts.next_cursor});const next=await ontologyRequest<any>(props.workspaceId,`/conflicts/pair?${params}`);if(version===generation)setCurrent({...next,impacts:{...next.impacts,reports:[...value().impacts.reports,...next.impacts.reports]}});})}>更多受影响报告</button></Show>
      </details>
      <details><summary>处理历史（{value().history.length}）</summary><For each={value().history}>{event=><article><strong>{actions[event.action]}</strong><p>{event.reason}</p><small>{event.actor} · {time(event.created_at)} · 第 {event.version} 次决定</small></article>}</For><Show when={value().history_truncated}><p>仅显示最近 50 次决定。</p></Show></details>
    </>}</Show>
  </section>;
}
