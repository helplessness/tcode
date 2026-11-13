import { confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createSignal, on } from 'solid-js';
import {
  createWikiShareCollection,
  deleteWikiShareCollection,
  getWikiShareContext,
  grantWikiShare,
  listWikiPages,
  revokeWikiShare,
  type WikiPage,
  type WikiShareContext,
  type WikiShareGrant,
} from '../api/paperWiki';

type ShareTarget = {
  type: WikiShareGrant['target_type'];
  id: string;
  label?: string;
};

type Props = {
  open: boolean;
  workspaceId?: string;
  entityTypes: Array<{ type_id: string; display_name: string }>;
  initialTarget?: ShareTarget | null;
  onClose: () => void;
  onChanged?: () => void;
};

const emptyContext: WikiShareContext = {
  tenant_id: '',
  owner_user_id: '',
  workspace_id: '',
  members: [],
  collections: [],
  shares: [],
};

const COLLECTION_PAGE_BATCH_SIZE = 200;
const COLLECTION_PAGE_LOAD_LIMIT = 2000;

export default function WikiSharePanel(props: Props) {
  const [context, setContext] = createSignal<WikiShareContext>(emptyContext);
  const [targetType, setTargetType] = createSignal<ShareTarget['type']>('page');
  const [targetId, setTargetId] = createSignal('');
  const [selectedMembers, setSelectedMembers] = createSignal<Set<string>>(new Set());
  const [collectionName, setCollectionName] = createSignal('');
  const [collectionDescription, setCollectionDescription] = createSignal('');
  const [collectionPages, setCollectionPages] = createSignal<Set<string>>(new Set());
  const [loading, setLoading] = createSignal(false);
  const [saving, setSaving] = createSignal(false);
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [availablePages, setAvailablePages] = createSignal<WikiPage[]>([]);
  const [availablePageTotal, setAvailablePageTotal] = createSignal(0);

  const workspaceId = () => String(props.workspaceId || '').trim();
  const activeShares = createMemo(() => context().shares.filter(
    (share) => share.target_type === targetType() && share.target_id === targetId() && share.status === 'active',
  ));
  const memberById = createMemo(() => new Map(context().members.map((member) => [member.user_id, member])));
  const allSelected = createMemo(() => (
    context().members.length > 0 && context().members.every((member) => selectedMembers().has(member.user_id))
  ));

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [result, firstPageResult] = await Promise.all([
        getWikiShareContext(workspaceId()),
        listWikiPages({
          workspaceId: workspaceId(),
          includeArchived: true,
          limit: COLLECTION_PAGE_BATCH_SIZE,
        }),
      ]);
      const pages = [...(firstPageResult.pages || [])];
      const pageTotal = Math.max(0, Number(firstPageResult.total || 0));
      while (pages.length < pageTotal && pages.length < COLLECTION_PAGE_LOAD_LIMIT) {
        const pageResult = await listWikiPages({
          workspaceId: workspaceId(),
          includeArchived: true,
          limit: COLLECTION_PAGE_BATCH_SIZE,
          offset: pages.length,
        });
        const nextPages = pageResult.pages || [];
        if (nextPages.length === 0) break;
        pages.push(...nextPages);
      }
      setContext(result);
      setAvailablePages(pages.slice(0, COLLECTION_PAGE_LOAD_LIMIT));
      setAvailablePageTotal(pageTotal);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载共享设置失败');
    } finally {
      setLoading(false);
    }
  };

  createEffect(on(
    () => [props.open, props.initialTarget?.type, props.initialTarget?.id, workspaceId()] as const,
    ([open]) => {
      if (!open) return;
      const initial = props.initialTarget;
      setTargetType(initial?.type || 'page');
      setTargetId(initial?.id || '');
      setSelectedMembers(new Set<string>());
      setCollectionPages(new Set<string>());
      setError('');
      setNotice('');
      void load();
    },
  ));

  const targets = createMemo(() => {
    if (targetType() === 'page') {
      return availablePages().map((page) => ({ id: page.page_id, label: `${page.title} · ${page.page_type}` }));
    }
    if (targetType() === 'entity_type') {
      const labels = new Map(props.entityTypes.map((item) => [item.type_id, item.display_name]));
      const values = new Set([
        ...props.entityTypes.map((item) => item.type_id),
        ...availablePages().map((page) => page.page_type),
      ]);
      return [...values].sort().map((id) => ({ id, label: labels.get(id) || id }));
    }
    return context().collections.map((collection) => ({
      id: collection.collection_id,
      label: `${collection.name} · ${collection.page_ids.length} 页`,
    }));
  });

  createEffect(() => {
    const options = targets();
    if (!options.some((item) => item.id === targetId())) {
      setTargetId(options[0]?.id || '');
    }
  });

  const toggleMember = (userId: string, checked: boolean) => {
    setSelectedMembers((previous) => {
      const next = new Set(previous);
      if (checked) next.add(userId);
      else next.delete(userId);
      return next;
    });
  };

  const toggleAllMembers = (checked: boolean) => {
    setSelectedMembers(new Set(checked ? context().members.map((member) => member.user_id) : []));
  };

  const toggleCollectionPage = (pageId: string, checked: boolean) => {
    setCollectionPages((previous) => {
      const next = new Set(previous);
      if (checked) next.add(pageId);
      else next.delete(pageId);
      return next;
    });
  };

  const grant = async () => {
    if (!targetId()) {
      setError('请选择要共享的 Wiki 页面、实体类型或页面集合');
      return;
    }
    if (selectedMembers().size === 0) {
      setError('请至少选择一位同租户成员');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const result = await grantWikiShare({
        workspaceId: workspaceId(),
        targetType: targetType(),
        targetId: targetId(),
        granteeUserIds: [...selectedMembers()],
      });
      setNotice(`已创建或恢复 ${result.count} 条只读授权。全选只包含当前租户成员，不会自动授权未来新成员。`);
      setSelectedMembers(new Set<string>());
      await load();
      props.onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '共享授权失败');
    } finally {
      setSaving(false);
    }
  };

  const revoke = async (shareId: string) => {
    if (!await confirmAction('确认撤销这条 Wiki 共享？对方之后将无法检索或打开来源；历史对话不会被改写。')) return;
    setSaving(true);
    try {
      await revokeWikiShare(shareId, workspaceId());
      setNotice('共享已撤销。');
      await load();
      props.onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '撤销共享失败');
    } finally {
      setSaving(false);
    }
  };

  const createCollection = async () => {
    if (!collectionName().trim() || collectionPages().size === 0) {
      setError('页面集合需要名称，并至少包含一个页面');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const result = await createWikiShareCollection({
        workspaceId: workspaceId(),
        name: collectionName().trim(),
        description: collectionDescription().trim(),
        pageIds: [...collectionPages()],
      });
      setContext((previous) => ({
        ...previous,
        collections: [result.collection, ...previous.collections],
      }));
      setTargetType('collection');
      setTargetId(result.collection.collection_id);
      setCollectionName('');
      setCollectionDescription('');
      setCollectionPages(new Set<string>());
      setNotice('页面集合已创建，现在可以选择成员并授权。');
      props.onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '创建页面集合失败');
    } finally {
      setSaving(false);
    }
  };

  const deleteCollection = async (collectionId: string) => {
    if (!await confirmAction('删除页面集合会同时撤销基于该集合的全部共享，确认继续？')) return;
    setSaving(true);
    try {
      await deleteWikiShareCollection(collectionId, workspaceId());
      setNotice('页面集合及其授权已删除。');
      await load();
      props.onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除页面集合失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Show when={props.open}>
      <div class="fixed inset-0 z-[130] flex justify-end bg-black/60" onClick={(event) => { if (event.target === event.currentTarget) props.onClose(); }}>
        <aside class="tcode-scrollbar h-full w-full max-w-2xl overflow-y-auto border-l border-[#2a3136] bg-[#11171b] p-5 shadow-2xl">
          <div class="flex items-start justify-between gap-3 border-b border-[#2a3136] pb-4">
            <div>
              <div class="text-[10px] uppercase tracking-[0.16em] text-[#7bd0ff]">Same-tenant read-only access</div>
              <h2 class="mt-1 text-lg font-semibold text-[#f7fafc]">共享 Wiki</h2>
              <p class="mt-1 text-xs leading-5 text-[#8b949e]">内容保留在你的 Workspace；这里只记录逻辑授权，不复制文件，也不向对方暴露本地路径。</p>
            </div>
            <button class="text-sm text-[#8b949e] hover:text-[#dee3e8]" onClick={props.onClose}>关闭</button>
          </div>

          <Show when={error()}><div class="mt-4 rounded-lg border border-[#da3633]/30 bg-[#da3633]/10 px-3 py-2 text-xs text-[#ffb6b0]">{error()}</div></Show>
          <Show when={notice()}><div class="mt-4 rounded-lg border border-[#38b46a]/25 bg-[#38b46a]/8 px-3 py-2 text-xs leading-5 text-[#9ee6bb]">{notice()}</div></Show>
          <Show when={!loading()} fallback={<div class="p-12 text-center text-sm text-[#8b949e]">正在加载同租户成员和授权…</div>}>
            <section class="mt-5 rounded-2xl border border-[#2a3136] bg-[#0f1418] p-4">
              <h3 class="text-sm font-semibold text-[#f7fafc]">1. 选择共享范围</h3>
              <div class="mt-3 grid gap-3 md:grid-cols-[180px_1fr]">
                <select class="rounded-lg border border-[#303539] bg-[#11171b] px-3 py-2 text-xs" value={targetType()} onChange={(event) => setTargetType(event.currentTarget.value as ShareTarget['type'])}>
                  <option value="page">指定 Wiki 页面</option>
                  <option value="entity_type">实体类型（含未来页面）</option>
                  <option value="collection">命名页面集合</option>
                </select>
                <select class="min-w-0 rounded-lg border border-[#303539] bg-[#11171b] px-3 py-2 text-xs" value={targetId()} onChange={(event) => setTargetId(event.currentTarget.value)}>
                  <Show when={targets().length > 0} fallback={<option value="">暂无可选内容</option>}>
                    <For each={targets()}>{(item) => <option value={item.id}>{item.label}</option>}</For>
                  </Show>
                </select>
              </div>
              <Show when={targetType() === 'entity_type'}><p class="mt-2 text-[11px] leading-5 text-[#ffc176]">实体类型授权是动态范围：该类型以后新增的页面也会自动对这些用户只读可见。</p></Show>
            </section>

            <section class="mt-4 rounded-2xl border border-[#2a3136] bg-[#0f1418] p-4">
              <div class="flex items-center justify-between gap-3">
                <div><h3 class="text-sm font-semibold text-[#f7fafc]">2. 选择当前租户成员</h3><p class="mt-1 text-[10px] text-[#6f7d87]">仅列出有效成员；不会跨租户。</p></div>
                <label class="flex items-center gap-2 text-xs text-[#8ed5ff]"><input type="checkbox" checked={allSelected()} onChange={(event) => toggleAllMembers(event.currentTarget.checked)} />全选当前成员</label>
              </div>
              <div class="mt-3 grid gap-2 sm:grid-cols-2">
                <For each={context().members}>{(member) => (
                  <label class={`flex cursor-pointer items-center gap-3 rounded-xl border p-3 ${selectedMembers().has(member.user_id) ? 'border-[#38bdf8]/30 bg-[#38bdf8]/8' : 'border-[#303539] bg-[#11171b]'}`}>
                    <input type="checkbox" checked={selectedMembers().has(member.user_id)} onChange={(event) => toggleMember(member.user_id, event.currentTarget.checked)} />
                    <div class="min-w-0"><div class="truncate text-xs font-semibold text-[#dee3e8]">{member.name || member.email}</div><div class="truncate text-[10px] text-[#6f7d87]">{member.email} · {member.role}</div></div>
                  </label>
                )}</For>
                <Show when={context().members.length === 0}><div class="col-span-full rounded-lg border border-dashed border-[#303539] p-5 text-center text-xs text-[#6f7d87]">当前租户没有其他可共享成员。</div></Show>
              </div>
              <button class="mt-4 w-full rounded-lg border border-[#38b46a]/30 bg-[#38b46a]/12 px-4 py-2.5 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50" disabled={saving() || !targetId() || selectedMembers().size === 0} onClick={() => void grant()}>{saving() ? '处理中…' : '授予只读访问'}</button>
            </section>

            <section class="mt-4 rounded-2xl border border-[#2a3136] bg-[#0f1418] p-4">
              <h3 class="text-sm font-semibold text-[#f7fafc]">当前范围的有效授权</h3>
              <div class="mt-3 space-y-2">
                <For each={activeShares()}>{(share) => {
                  const member = () => memberById().get(share.grantee_user_id);
                  return <div class="flex items-center justify-between gap-3 rounded-lg border border-[#303539] bg-[#11171b] px-3 py-2"><div class="min-w-0"><div class="truncate text-xs text-[#dee3e8]">{member()?.name || member()?.email || share.grantee_user_id}</div><div class="font-mono text-[9px] text-[#6f7d87]">{share.share_id}</div></div><button class="shrink-0 text-[11px] text-[#ff9c96] hover:underline" disabled={saving()} onClick={() => void revoke(share.share_id)}>撤销</button></div>;
                }}</For>
                <Show when={activeShares().length === 0}><div class="rounded-lg border border-dashed border-[#303539] p-4 text-center text-xs text-[#6f7d87]">这个范围尚未共享给任何人。</div></Show>
              </div>
            </section>

            <section class="mt-4 rounded-2xl border border-[#2a3136] bg-[#0f1418] p-4">
              <h3 class="text-sm font-semibold text-[#f7fafc]">页面集合</h3>
              <p class="mt-1 text-[10px] leading-5 text-[#6f7d87]">集合是命名的页面快照；修改集合内容会立即改变它覆盖的页面范围。</p>
              <div class="mt-3 grid gap-2 md:grid-cols-2"><input class="rounded-lg border border-[#303539] bg-[#11171b] px-3 py-2 text-xs" value={collectionName()} onInput={(event) => setCollectionName(event.currentTarget.value)} placeholder="集合名称，例如：酶工程核心资料" /><input class="rounded-lg border border-[#303539] bg-[#11171b] px-3 py-2 text-xs" value={collectionDescription()} onInput={(event) => setCollectionDescription(event.currentTarget.value)} placeholder="可选说明" /></div>
              <div class="tcode-scrollbar mt-3 max-h-44 overflow-y-auto rounded-xl border border-[#303539] bg-[#11171b] p-2">
                <For each={availablePages()}>{(page) => <label class="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-xs hover:bg-[#171c20]"><input type="checkbox" checked={collectionPages().has(page.page_id)} onChange={(event) => toggleCollectionPage(page.page_id, event.currentTarget.checked)} /><span class="min-w-0 flex-1 truncate text-[#bdc8d1]">{page.title}</span><span class="shrink-0 text-[9px] text-[#6f7d87]">{page.page_type}</span></label>}</For>
              </div>
              <Show when={availablePageTotal() > COLLECTION_PAGE_LOAD_LIMIT}><p class="mt-2 text-[10px] leading-5 text-[#ffc176]">当前共有 {availablePageTotal()} 个页面。为避免界面卡顿，本次显示最近更新的 {COLLECTION_PAGE_LOAD_LIMIT} 个页面；更大规模集合建议按实体类型授权。</p></Show>
              <button class="mt-3 rounded-lg border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-3 py-2 text-xs text-[#8ed5ff] disabled:opacity-50" disabled={saving() || !collectionName().trim() || collectionPages().size === 0} onClick={() => void createCollection()}>创建集合</button>
              <div class="mt-3 space-y-2"><For each={context().collections}>{(collection) => <div class="flex items-center justify-between gap-3 rounded-lg border border-[#303539] bg-[#11171b] px-3 py-2"><button class="min-w-0 text-left" onClick={() => { setTargetType('collection'); setTargetId(collection.collection_id); }}><div class="truncate text-xs text-[#dee3e8]">{collection.name}</div><div class="text-[9px] text-[#6f7d87]">{collection.page_ids.length} 个页面 · 点击选择并授权</div></button><button class="shrink-0 text-[10px] text-[#ff9c96] hover:underline" onClick={() => void deleteCollection(collection.collection_id)}>删除</button></div>}</For></div>
            </section>
          </Show>
        </aside>
      </div>
    </Show>
  );
}
