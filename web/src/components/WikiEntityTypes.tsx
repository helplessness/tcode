import { For, createEffect, createSignal } from 'solid-js';
import { manageWikiEntityTypes, requestWikiEntityType, reviewWikiEntityType, type WikiEntityType } from '../api/paperWiki';

export default function WikiEntityTypes(props: { workspaceId: string; onUpdated: () => void }) {
  const [items, setItems] = createSignal<WikiEntityType[]>([]);
  const [id, setId] = createSignal('');
  const [name, setName] = createSignal('');
  const [reason, setReason] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const labels: Record<string, string> = { pending: '待审核', approved: '已批准', rejected: '已驳回', disabled: '已停用' };
  const run = async (action: () => Promise<{ types: WikiEntityType[] }>) => {
    setBusy(true); setError('');
    try { setItems((await action()).types); props.onUpdated(); }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  };
  createEffect(() => { const ws = props.workspaceId; void run(() => manageWikiEntityTypes(ws)); });
  return <section class="tcode-scrollbar flex-1 overflow-auto p-5 text-sm text-[#e6edf3]">
    <h2 class="text-lg font-semibold">实体类型管理</h2>
    <p class="mt-2 text-[#9faeb7]">类型按当前 Workspace 管理。申请后需人工批准；停用不会删除已有页面。内置类型不可停用。</p>
    <form class="mt-4 flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); void run(() => requestWikiEntityType(props.workspaceId, id(), name())); }}>
      <label>类型 ID<input class="mt-1 block rounded border border-[#303539] bg-[#11171b] p-3" required pattern="[a-z][a-z0-9_]{0,63}" value={id()} onInput={e => setId(e.currentTarget.value)} placeholder="例如 enzyme_family" /></label>
      <label>显示名称<input class="mt-1 block rounded border border-[#303539] bg-[#11171b] p-3" required maxlength={100} value={name()} onInput={e => setName(e.currentTarget.value)} /></label>
      <button class="min-h-11 rounded border border-[#a78bfa] px-4 disabled:opacity-50" disabled={busy()}>提交申请</button>
    </form>
    <label class="mt-4 block">审核备注（驳回必填）<input class="mt-1 block w-full rounded border border-[#303539] bg-[#11171b] p-3" maxlength={1000} value={reason()} onInput={e => setReason(e.currentTarget.value)} /></label>
    <p role="alert" class="mt-3 text-red-300">{error()}</p>
    <div class="mt-4 space-y-3"><For each={items()}>{item => <article class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#303539] p-3">
      <div><strong>{item.display_name}</strong> <span>{item.type_id} · {labels[item.status] || item.status}</span><div class="text-xs text-[#9faeb7]">{item.schema.requested_by ? `申请人：${item.schema.requested_by}` : '内置类型'} {item.schema.review_reason || ''}</div></div>
      <div class="flex gap-2"><For each={item.schema.requested_by ? ({ pending: ['approved', 'rejected'], approved: ['disabled'], rejected: ['pending'], disabled: ['approved'] } as Record<string, string[]>)[item.status] || [] : []}>{status => <button disabled={busy()} class="min-h-11 rounded border border-[#303539] px-3 disabled:opacity-50" onClick={() => void run(() => reviewWikiEntityType(props.workspaceId, item, status, reason()))}>{({ approved: '批准 / 恢复', rejected: '驳回', disabled: '停用', pending: '重新申请' } as Record<string, string>)[status]}</button>}</For></div>
    </article>}</For></div>
  </section>;
}
