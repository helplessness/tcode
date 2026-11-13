import { For, Show, createResource, createSignal, createUniqueId } from 'solid-js';
import type { CanvasHistory } from '../api/canvas';

export default function RevisionDeletion(props: {
  current: string; viewed: string; canWrite: boolean;
  request: <T>(suffix: string, body?: unknown) => Promise<T>;
  onChange: (deleted: boolean, adjacent?: string) => Promise<void>;
}) {
  const [open,setOpen]=createSignal(false), [before,setBefore]=createSignal('');
  const [busy,setBusy]=createSignal(false), [notice,setNotice]=createSignal(''), [error,setError]=createSignal('');
  const [purgeTarget,setPurgeTarget]=createSignal<{id:string;label:string}>();
  const purgeWarningId=createUniqueId();
  const [trash,{refetch}]=createResource(()=>open() && JSON.stringify([props.current,before()]),
    ()=>props.request<CanvasHistory>(`/revisions?deleted=true${before()?`&before=${encodeURIComponent(before())}`:''}`));
  const change=async(revision:string,deleted:boolean)=>{
    if(busy()||!props.canWrite)return;
    setBusy(true);setError('');setNotice('');
    try {
      const receipt = await props.request<{ adjacent_revision_id?: string }>('/revision-deletion',{base_revision_id:props.current,revision_id:revision,deleted});
      setBefore(''); await props.onChange(deleted, receipt.adjacent_revision_id); if(open())await refetch();
      setNotice(deleted?'已删除，可从“已删除版本”找回；已保存的研究资料快照不受影响。':'已移回版本列表。');
    } catch(reason){setError(reason instanceof Error?reason.message:'版本操作失败，请重试。');}
    finally{setBusy(false);}
  };
  const purge=async()=>{
    const target=purgeTarget();
    if(!target||busy()||!props.canWrite)return;
    setBusy(true);setError('');setNotice('');
    try {
      await props.request('/revision-purge',{base_revision_id:props.current,revision_id:target.id});
      setPurgeTarget(undefined);setBefore('');await props.onChange(false);await refetch();
      setNotice('已彻底删除该版本的数据，无法找回。');
    } catch(reason){setError(reason instanceof Error?reason.message:'彻底删除失败，请重试。');}
    finally{setBusy(false);}
  };
  return <div class="intelligent-revision-deletion">
    <button type="button" disabled={busy()||!props.canWrite||props.current===props.viewed} onClick={()=>void change(props.viewed,true)}>删除正在查看的版本</button>
    <p class="intelligent-canvas-hint">当前版本不能删除。删除的历史可找回，已保存快照会保留。</p>
    <details open={open()} onToggle={event=>setOpen(event.currentTarget.open)}><summary>已删除版本</summary>
      <Show when={trash.loading}><p role="status">正在加载…</p></Show>
      <Show when={trash.error}><p role="alert">已删除版本加载失败。</p><button type="button" onClick={()=>void refetch()}>重试</button></Show>
      <Show when={!trash.loading&&!trash.error&&trash.latest}>{list=><>
        <div class="intelligent-revision-trash-list"><For each={list().items} fallback={<p>没有已删除的版本。</p>}>{item=><div>
          <span>{new Date(item.created).toLocaleString()} · 历史版本</span>
          <button type="button" disabled={busy()||!props.canWrite} onClick={()=>void change(item.revision_id,false)}>找回</button>
          <button type="button" disabled={busy()||!props.canWrite} onClick={()=>setPurgeTarget({id:item.revision_id,label:new Date(item.created).toLocaleString()})}>彻底删除</button>
        </div>}</For></div>
        <Show when={before()}><button type="button" onClick={()=>setBefore('')}>最近删除版本</button></Show>
        <Show when={list().next_before}><button type="button" onClick={()=>setBefore(list().next_before)}>更早删除版本</button></Show>
      </>}</Show>
    </details>
    <Show when={purgeTarget()}>{target=><div role="alertdialog" aria-label="确认彻底删除版本" aria-describedby={purgeWarningId} onKeyDown={event=>{if(event.key==='Escape'&&!busy()){event.stopPropagation();setPurgeTarget(undefined);}}}>
      <p id={purgeWarningId}>彻底删除 {target().label} 的历史版本？该版本的图形或页面内容、部件说明及版本数据将被删除，无法找回。单独保存的研究快照和已导出文件保留。</p>
      <button ref={element=>queueMicrotask(()=>element.focus())} type="button" disabled={busy()} onClick={()=>setPurgeTarget(undefined)}>取消</button>
      <button type="button" disabled={busy()||!props.canWrite} onClick={()=>void purge()}>确认彻底删除</button>
    </div>}</Show>
    <Show when={notice()}><p role="status">{notice()}</p></Show>
    <Show when={error()}><p role="alert">{error()}</p></Show>
  </div>;
}
