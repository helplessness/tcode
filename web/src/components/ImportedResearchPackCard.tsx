import { researchStatusLabel, userFacingTitle } from '../utils/userFacingLabels';
import ResearchRecordDetails from './ResearchRecordDetails';
import HypothesisEvidenceMap from './HypothesisEvidenceMap';
import { readableSourceLabel } from '../utils/evidenceDisplay';
import { researchSourceTypeLabel } from '../utils/researchKnowledgeDisplay';
import { For, Show, createMemo, createSignal } from 'solid-js';

import {
  getResearchPackReadObject,
  listResearchPackReadObjects,
  getResearchPackAsset,
  rebuildResearchPackIndex,
  searchResearchPackRead,
  type ResearchPackAsset,
  type ResearchPackAssetDetail,
  type ResearchPackSearchHit,
  type ResearchPackSearchResponse,
} from '../api/research';
import {
  researchPackAssetGeneration,
  researchPackAssetParents,
  researchPackConflictState,
} from '../utils/researchPackUi';
import MarkdownContent from './MarkdownContent';
import {readableResearchExcerpt,researchIndexTitle} from '../utils/researchMarkdown';
import CanvasBlock from './CanvasBlock';
import { packCanvasRequest, type CanvasData } from '../api/canvas';
import { sessionStore } from '../stores/session';
import type { CanvasContentBlock } from '../utils/contentBlocks';
import type { PageContentBlock } from '../utils/contentBlocks';
import type { PageData } from '../api/intelligentPage';
import IntelligentPageBlock from './IntelligentPageBlock';

type Props = {
  asset: ResearchPackAsset;
  workspaceId?: string;
  onUseInChat: (asset: ResearchPackAsset, detail: ResearchPackAssetDetail | null) => void;
  onReexport: (asset: ResearchPackAsset) => void;
  onReproduce?: (asset: ResearchPackAsset, experimentId: string) => Promise<void>;
  onOpen?: (asset: ResearchPackAsset) => void;
};

const formatTime = (value?: string) => {
  const timestamp = Date.parse(String(value || ''));
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : String(value || '');
};

const sumCounts = (counts?: Record<string, number>) => Object.values(counts || {})
  .reduce((total, value) => total + Math.max(0, Number(value || 0)), 0);

export default function ImportedResearchPackCard(props: Props) {
  const [detail, setDetail] = createSignal<ResearchPackAssetDetail | null>(null);
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [searchQuery, setSearchQuery] = createSignal('');
  const [searching, setSearching] = createSignal(false);
  const [searchResult, setSearchResult] = createSignal<ResearchPackSearchResponse | null>(null);
  const [selectedHit, setSelectedHit] = createSignal<ResearchPackSearchHit | null>(null);
  const [exactObject, setExactObject] = createSignal<Record<string, any> | null>(null);
  const [objectLoading, setObjectLoading] = createSignal(false);
  const [indexRebuilding, setIndexRebuilding] = createSignal(false);
  const [sourceExperiments, setSourceExperiments] = createSignal<Array<Record<string, any>>>([]);
  const [reproducing, setReproducing] = createSignal('');
  const [canvases, setCanvases] = createSignal<Array<{ object_id: string; title: string; available: boolean }>>([]);
  const [openedCanvas, setOpenedCanvas] = createSignal<CanvasContentBlock>();
  const [canvasBusy, setCanvasBusy] = createSignal(false);
  const [openedObjectId, setOpenedObjectId] = createSignal('');
  const [pages, setPages] = createSignal<Array<{ object_id: string; title: string; available: boolean }>>([]);
  const [openedPage, setOpenedPage] = createSignal<PageContentBlock>();
  const [pageObjectId, setPageObjectId] = createSignal('');
  const [pageBusy, setPageBusy] = createSignal(false);
  const loadPages = async () => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId || pageBusy()) return;
    setPageBusy(true); setError('');
    try { setPages((await packCanvasRequest<{ items: Array<{ object_id: string; title: string; available: boolean }> }>(props.asset.asset_id, sessionId, undefined, 'pages')).items); }
    catch (e) { setError(e instanceof Error ? e.message : '读取页面失败'); }
    finally { setPageBusy(false); }
  };
  const openPage = async (objectId: string) => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId || pageBusy()) return;
    setPageBusy(true); setError('');
    try {
      const v = await packCanvasRequest<PageData>(props.asset.asset_id, sessionId, objectId, 'pages');
      if (sessionStore.currentSessionId() !== sessionId) return;
      setPageObjectId(objectId);
      setOpenedPage({ type: 'intelligent_page', schema: 'tcode.intelligent-page.v1', page_id: v.page_id,
        revision_id: v.revision_id, session_id: v.session_id, title: v.title, fallback: '研究包页面的本地副本' });
    } catch (e) { setError(e instanceof Error ? e.message : '接收页面失败'); }
    finally { setPageBusy(false); }
  };
  const loadCanvases = async () => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId || canvasBusy()) return;
    setCanvasBusy(true); setError('');
    try { setCanvases((await packCanvasRequest<{ items: Array<{ object_id: string; title: string; available: boolean }> }>(props.asset.asset_id, sessionId)).items); }
    catch (e) { setError(e instanceof Error ? e.message : '读取画布失败'); }
    finally { setCanvasBusy(false); }
  };
  const openCanvas = async (objectId: string) => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId || canvasBusy()) return;
    setCanvasBusy(true); setError('');
    try {
      const v = await packCanvasRequest<CanvasData>(props.asset.asset_id, sessionId, objectId);
      setOpenedObjectId(objectId);
      setOpenedCanvas({ type: 'canvas', schema: 'tcode.canvas.v1', kind: 'svg', canvas_id: v.canvas_id,
        revision_id: v.revision_id, session_id: v.session_id, title: v.title, fallback: '研究包画布的本地副本' });
    } catch (e) { setError(e instanceof Error ? e.message : '接收画布失败'); }
    finally { setCanvasBusy(false); }
  };
  const generation = createMemo(() => researchPackAssetGeneration(props.asset));
  const parents = createMemo(() => researchPackAssetParents(props.asset));
  const conflict = createMemo(() => researchPackConflictState(props.asset));
  const overlayCount = createMemo(() => (
    detail()
      ? sumCounts(detail()?.overlay_object_counts)
      : Math.max(0, Number(props.asset.summary?.overlay?.local_object_count || 0))
  ));
  const overlayStatus = createMemo(() => (
    detail()?.overlays?.[0]?.status
    || String(props.asset.summary?.overlay?.state || 'active')
  ));
  const selectedContext = createMemo(() => {
    const hit = selectedHit();
    if (!hit) return [];
    return [
      ...(hit.neighbors || []).filter((item) => item.ordinal < hit.ordinal),
      {
        chunk_id: hit.chunk_id,
        ordinal: hit.ordinal,
        heading_path: hit.heading_path,
        content: hit.content,
        line_start: hit.line_start,
        line_end: hit.line_end,
        byte_start: hit.byte_start,
        byte_end: hit.byte_end,
      },
      ...(hit.neighbors || []).filter((item) => item.ordinal > hit.ordinal),
    ].sort((left, right) => left.ordinal - right.ordinal);
  });

  const loadDetail = async () => {
    if (detail() || loading()) return;
    setLoading(true);
    setError('');
    try {
      setDetail(await getResearchPackAsset(props.asset.asset_id, props.workspaceId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取资料包详情');
    } finally {
      setLoading(false);
    }
  };

  const runSearch = async () => {
    const query = searchQuery().trim();
    if (!query || searching()) return;
    setSearching(true);
    setError('');
    setExactObject(null);
    try {
      const result = await searchResearchPackRead(
        props.asset.asset_id,
        query,
        '',
        20,
        props.workspaceId,
      );
      setSearchResult(result);
      setSelectedHit(result.hits?.[0] || null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '检索研究资料失败');
    } finally {
      setSearching(false);
    }
  };

  const selectHit = (hit: ResearchPackSearchHit) => {
    setSelectedHit(hit);
    setExactObject(null);
  };

  const loadExactObject = async () => {
    const hit = selectedHit();
    if (!hit || objectLoading()) return;
    const identifier = hit.layer === 'overlay'
      ? hit.local_object_id || hit.object_id
      : hit.source_object_id || hit.object_id;
    if (!hit.object_type || !identifier) return;
    setObjectLoading(true);
    setError('');
    try {
      setExactObject(await getResearchPackReadObject(
        props.asset.asset_id,
        hit.object_type,
        identifier,
        props.workspaceId,
      ));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '查看完整资料失败');
    } finally {
      setObjectLoading(false);
    }
  };

  const rebuildIndex = async () => {
    if (indexRebuilding()) return;
    setIndexRebuilding(true);
    setError('');
    try {
      await rebuildResearchPackIndex(props.asset.asset_id, false, props.workspaceId);
      if (searchQuery().trim()) await runSearch();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重建资料索引失败');
    } finally {
      setIndexRebuilding(false);
    }
  };
  const loadExperiments = async () => {
    try {
      const result = await listResearchPackReadObjects(props.asset.asset_id, { object_type: 'experiment', limit: 100 }, props.workspaceId);
      setSourceExperiments((result.items || []).map((item) => item.payload || item));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '读取实验失败'); }
  };
  const reproduce = async (id: string) => {
    if (!props.onReproduce || reproducing()) return;
    setReproducing(id); setError('');
    try { await props.onReproduce(props.asset, id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '创建复现实验失败'); }
    finally { setReproducing(''); }
  };

  if (props.onOpen) return <article class="rounded-xl border border-[#2f3940] bg-[#11171b] p-4">
    <h3 class="text-sm font-semibold">{userFacingTitle(props.asset.title, '研究资料包')}</h3>
    <p class="mt-2 text-xs">第 {generation()} 次交接 · {conflict().label}</p>
    <p class="mt-2 line-clamp-2 text-xs">{String(props.asset.summary?.summary || props.asset.summary?.description || '研究资料包')}</p>
    <button type="button" class="mt-3 rounded-lg border border-[#30383e] px-3 py-2 text-xs" onClick={() => props.onOpen?.(props.asset)}>打开资料与交接</button>
  </article>;
  return (
    <article class="rounded-xl border border-[#2f3940] bg-[#11171b] p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <h3 class="truncate text-sm font-semibold text-[#e6edf3]">{userFacingTitle(props.asset.title, '研究资料包')}</h3>
            <span class="rounded-full border border-[#c4a7ff]/30 bg-[#c4a7ff]/8 px-2 py-0.5 text-[10px] text-[#d8c8ff]">
              导入资料 · 第 {generation()} 次交接
            </span>
          </div>
        </div>
        <span class={`rounded-full border px-2 py-0.5 text-[10px] ${conflict().blocked ? 'border-[#ef6b73]/35 text-[#fca5a5]' : 'border-[#38b46a]/30 text-[#9ee6bb]'}`}>
          {conflict().label}
        </span>
      </div>

      <p class="mt-3 line-clamp-3 text-xs leading-5 text-[#9faeb7]">
        {String(props.asset.summary?.summary || props.asset.summary?.description || '这是一个可由 Agent 渐进读取并继续追加的研究资料包。')}
      </p>

      <div class="mt-3 grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4">
        <div class="rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
          <div class="text-[#596873]">时间范围</div>
          <div class="mt-1 text-[#9faeb7]">
            {props.asset.started_at || props.asset.ended_at
              ? `${formatTime(props.asset.started_at)} ～ ${formatTime(props.asset.ended_at)}`
              : '完整课题范围'}
          </div>
        </div>
        <div class="rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
          <div class="text-[#596873]">来源项目</div>
          <div class="mt-1 font-semibold text-[#bdc8d1]">{detail()?.scopes?.length ?? props.asset.source_research_ids.length}</div>
        </div>
        <div class="rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
          <div class="text-[#596873]">来源研究包</div>
          <div class="mt-1 text-[#9faeb7]">{parents().length ? `继承 ${parents().length} 份来源研究包` : '独立研究包'}</div>
        </div>
        <div class="rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
          <div class="text-[#596873]">本地接力记录</div>
          <div class="mt-1 text-[#9faeb7]">{loading() ? '读取中…' : `${overlayStatus()==='active'?'可继续研究':researchStatusLabel(overlayStatus(),overlayCount()===0?'暂无接续记录':'接续记录已保存')} · ${overlayCount()} 条`}</div>
        </div>
      </div>

      <Show when={error()}>
        <div class="mt-3 rounded-lg border border-[#f1a02b]/25 bg-[#f1a02b]/8 px-3 py-2 text-xs text-[#ffc176]">{error()}</div>
      </Show>

      <details
        class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2"
        onToggle={(event) => {
          if (event.currentTarget.open) void loadDetail();
        }}
      >
        <summary class="cursor-pointer text-xs font-semibold text-[#9faeb7]">查看来源项目与待归属研究经历</summary>
        <div class="research-scroll-content mt-3 space-y-2">
          <Show when={loading()}>
            <div class="text-xs text-[#71808a]">正在读取来源项目…</div>
          </Show>
          <Show when={!loading() && (detail()?.scopes?.length || 0) === 0}>
            <div class="text-xs text-[#71808a]">这个资料包没有独立来源项目视图。</div>
          </Show>
          <For each={detail()?.scopes || []}>
            {(scope) => (
              <div class="rounded-lg border border-[#293238] bg-[#11171b] px-3 py-2">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <span class="text-xs font-semibold text-[#bdc8d1]">{scope.title || '待归属研究经历'}</span>
                  <span class="rounded-full border border-[#30383e] px-2 py-0.5 text-[10px] text-[#71808a]">
                    {scope.scope_type === 'unassigned' ? '待归属研究经历' : '来源项目'}
                  </span>
                </div>
                <Show when={scope.summary}>
                  <div class="research-progress-markdown"><MarkdownContent content={scope.summary||''} allowSessionActions={false}/></div>
                </Show>
              </div>
            )}
          </For>
        </div>
      </details>

      <details class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
        <summary class="cursor-pointer text-xs">假说与证据交接地图</summary>
        <HypothesisEvidenceMap project={{research_id:props.asset.asset_id,title:props.asset.title||'研究包假说与证据'}} workspaceId={props.workspaceId} assetId={props.asset.asset_id}/>
      </details>
      <details class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2" onToggle={(event) => { if (event.currentTarget.open && !sourceExperiments().length) void loadExperiments(); }}>
        <summary class="cursor-pointer text-xs font-semibold text-[#9faeb7]">复现来源实验</summary>
        <div class="research-scroll-content mt-3 space-y-2"><Show when={sourceExperiments().length} fallback={<div class="text-xs text-[#71808a]">资料包中没有可复现的实验。</div>}><For each={sourceExperiments()}>{(item) => <div class="flex items-center justify-between gap-3 rounded-lg border border-[#293238] bg-[#11171b] p-3"><div><div class="text-xs font-semibold text-[#bdc8d1]">{readableSourceLabel(item.title, '来源实验')}</div><div class="mt-1 text-[10px] text-[#71808a]">{researchStatusLabel(item.status)} · {researchStatusLabel(item.decision,'判断未说明')}</div></div><button class="rounded-md border border-[#c4a7ff]/30 px-2.5 py-1 text-xs text-[#d8c8ff] disabled:opacity-50" disabled={!props.onReproduce || reproducing() === item.experiment_id} onClick={() => void reproduce(String(item.experiment_id))}>{reproducing() === item.experiment_id ? '创建中…' : '创建复现实验'}</button></div>}</For></Show></div>
      </details>

      <details class="mt-3 rounded-lg border border-[#293238] p-3" onToggle={e => { if (e.currentTarget.open) void loadCanvases(); }}>
        <summary class="cursor-pointer text-xs">交互画布交接</summary>
        <p class="mt-2 text-xs">打开为当前会话的本地副本，原研究包保持只读。请先打开此工作区的一条会话。</p>
        <Show when={canvasBusy()}><p role="status">正在读取画布…</p></Show>
        <div class="max-h-64 overflow-y-auto"><For each={canvases()}>{c => <div class="flex items-center gap-3 p-2 text-xs">
          <span>{c.title}</span><button disabled={!c.available || canvasBusy()} onClick={() => void openCanvas(c.object_id)}>{c.available ? '打开本地画布' : '仅引用，未携带图形'}</button>
        </div>}</For></div>
        <Show when={openedCanvas()}>{b => <CanvasBlock block={b()} onSaveHandoff={async value => {
          const query = new URLSearchParams({ session_id: value.session_id });
          const response = await fetch(`/api/intelligent-ui/v1/pack-assets/${encodeURIComponent(props.asset.asset_id)}/canvases/${encodeURIComponent(openedObjectId())}/snapshots?${query}`, {
            method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ canvas_id: value.canvas_id, revision_id: value.revision_id }),
          });
          if (!response.ok) { const error = await response.json(); throw new Error(String(error.detail || '接力记录保存失败')); }
        }} />}</Show>
      </details>

      <details class="mt-3 rounded-lg border border-[#293238] p-3" onToggle={e => { if (e.currentTarget.open) void loadPages(); }}>
        <summary class="cursor-pointer text-xs">交互页面交接</summary>
        <p class="mt-2 text-xs">打开为当前会话的本地页面副本，原研究包保持只读。请先打开此工作区的一条会话。</p>
        <Show when={pageBusy()}><p role="status">正在读取页面…</p></Show>
        <div class="max-h-64 overflow-y-auto"><For each={pages()}>{p => <div class="flex items-center gap-3 p-2 text-xs">
          <span>{p.title}</span><button disabled={!p.available || pageBusy()} onClick={() => void openPage(p.object_id)}>{p.available ? '打开本地页面' : '仅引用，未携带页面'}</button>
        </div>}</For></div>
        <Show when={openedPage()} keyed>{b => <IntelligentPageBlock block={b} onSaveHandoff={async value => {
          const query = new URLSearchParams({ session_id: value.session_id });
          const response = await fetch(`/api/intelligent-ui/v1/pack-assets/${encodeURIComponent(props.asset.asset_id)}/pages/${encodeURIComponent(pageObjectId())}/snapshots?${query}`, {
            method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ page_id: value.page_id, revision_id: value.revision_id }),
          });
          if (!response.ok) { const error = await response.json(); throw new Error(String(error.detail || '页面接力记录保存失败')); }
        }} />}</Show>
      </details>

      <details class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
        <summary class="cursor-pointer text-xs font-semibold text-[#9faeb7]">按内容检索与分块浏览</summary>
        <div class="mt-3">
          <div class="flex flex-col gap-2 sm:flex-row">
            <input
              class="min-w-0 flex-1 rounded-lg border border-[#30383e] bg-[#11171b] px-3 py-2 text-xs text-[#d8dee4] outline-none focus:border-[#38bdf8]/50"
              value={searchQuery()}
              onInput={(event) => setSearchQuery(event.currentTarget.value)}
              onKeyDown={(event) => { if (event.key === 'Enter') void runSearch(); }}
              placeholder="搜索结论、实验条件、Wiki 内容或论文对象"
            />
            <button
              class="rounded-lg border border-[#38bdf8]/35 bg-[#38bdf8]/10 px-3 py-2 text-xs font-semibold text-[#8ed5ff] disabled:opacity-40"
              disabled={!searchQuery().trim() || searching()}
              onClick={() => void runSearch()}
            >
              {searching() ? '检索中…' : '检索'}
            </button>
            <button
              class="rounded-lg border border-[#30383e] px-3 py-2 text-xs text-[#8b99a4] disabled:opacity-40"
              disabled={indexRebuilding()}
              onClick={() => void rebuildIndex()}
            >
              {indexRebuilding() ? '重建中…' : '重建索引'}
            </button>
          </div>

          <Show when={searchResult()}>
            {(result) => (
              <div class="mt-3 flex flex-wrap items-center gap-2 text-[10px] text-[#71808a]">
                <span class="rounded-full border border-[#30383e] px-2 py-0.5">{result().search_mode==='fts'?'全文检索':'资料检索'}</span>
                <span>{result().hits?.length || 0} 条匹配内容</span>
                <span>索引 {result().index?.document_count || 0} 文档 / {result().index?.chunk_count || 0} 块</span>
                <Show when={result().index?.layout}>
                  <span>{result().index?.layout === 'parent-overlay-v1' ? '来源资料与本地接力记录' : '资料索引'}</span>
                </Show>
                <Show when={Number(result().query_ms || 0) > 0}>
                  <span>{Math.round(Number(result().query_ms || 0))} 毫秒</span>
                </Show>
                <Show when={result().index?.status !== 'ready' || result().index_stale}>
                  <span class="text-[#8ed5ff]">索引正在后台建立或刷新，本次未阻塞等待</span>
                </Show>
                <Show when={result().index?.completeness?.complete === false}>
                  <span class="text-[#ffc176]">索引不完整，请检查对象上限</span>
                </Show>
              </div>
            )}
          </Show>

          <Show when={searchResult() && (searchResult()?.hits?.length || 0) === 0}>
            <div class="mt-3 rounded-lg border border-dashed border-[#30383e] px-3 py-5 text-center text-xs text-[#71808a]">
              {searchResult()?.index?.status !== 'ready' || searchResult()?.index_stale
                ? '索引正在后台准备。你可以继续聊天，稍后重试检索；系统不会在当前请求中同步重建大索引。'
                : '没有找到相关内容。'}
            </div>
          </Show>

          <Show when={(searchResult()?.hits?.length || 0) > 0}>
            <div class="mt-3 grid min-h-0 gap-3 lg:grid-cols-[minmax(220px,0.8fr)_minmax(0,1.6fr)]">
              <div class="max-h-[520px] space-y-2 overflow-y-auto pr-1">
                <For each={searchResult()?.hits || []}>
                  {(hit) => (
                    <button
                      class={`w-full rounded-lg border p-3 text-left ${selectedHit()?.chunk_id === hit.chunk_id ? 'border-[#38bdf8]/45 bg-[#38bdf8]/8' : 'border-[#293238] bg-[#11171b] hover:border-[#3d4b54]'}`}
                      onClick={() => selectHit(hit)}
                    >
                      <div class="flex items-center justify-between gap-2">
                        <span class="truncate text-xs font-semibold text-[#bdc8d1]">{researchIndexTitle(hit.title, '研究来源')}</span>
                        <span class="shrink-0 text-[9px] text-[#71808a]">
                          {Math.round(Math.max(Number(hit.lexical_score || 0), Number(hit.vector_score || 0)) * 100)}%
                        </span>
                      </div>
                      <div class="mt-1 truncate text-[9px] text-[#596873]">{hit.layer === 'parent' ? '来源研究包' : '本地研究资料'} · 第 {hit.line_start}–{hit.line_end} 行</div>
                      <Show when={hit.heading_path}><div class="mt-1 truncate text-[10px] text-[#8ed5ff]">{researchIndexTitle(hit.heading_path, '来源章节')}</div></Show>
                      <p class="mt-2 line-clamp-3 text-[11px] leading-5 text-[#8b99a4]">{readableResearchExcerpt(hit.content, /结构化字段/.test(hit.heading_path||''))}</p>
                    </button>
                  )}
                </For>
              </div>

              <Show when={selectedHit()}>
                {(hit) => (
                  <div class="min-w-0 rounded-lg border border-[#293238] bg-[#11171b] p-3">
                    <div class="flex flex-wrap items-start justify-between gap-2 border-b border-[#293238] pb-3">
                      <div class="min-w-0">
                        <div class="text-xs font-semibold text-[#d8dee4]">{researchIndexTitle(hit().title, '研究来源')}</div>
                        <div class="mt-1 break-all font-mono text-[9px] text-[#596873]">
                          {hit().layer === 'parent' ? '来源研究包 · 只读' : '本地研究资料'} · {researchSourceTypeLabel(hit().object_type)}
                        </div>
                      </div>
                      <button
                        class="rounded border border-[#30383e] px-2 py-1 text-[10px] text-[#8ed5ff] disabled:opacity-40"
                        disabled={objectLoading()}
                        onClick={() => void loadExactObject()}
                      >
                        {objectLoading() ? '读取中…' : '查看完整资料'}
                      </button>
                    </div>
                    <div class="mt-3 max-h-[440px] overflow-y-auto pr-1">
                      <For each={selectedContext()}>
                        {(chunk) => (
                          <section class={chunk.chunk_id === hit().chunk_id ? 'rounded-lg border border-[#38bdf8]/25 bg-[#38bdf8]/5 p-3' : 'px-3 py-2 opacity-70'}>
                            <div class="mb-2 flex flex-wrap items-center gap-2 text-[9px] text-[#71808a]">
                              <span>第 {chunk.line_start}–{chunk.line_end} 行</span>
                              <Show when={chunk.heading_path}><span class="text-[#8ed5ff]">{researchIndexTitle(chunk.heading_path, '来源章节')}</span></Show>
                              <Show when={chunk.chunk_id === hit().chunk_id}><span class="rounded-full bg-[#38bdf8]/15 px-2 py-0.5 text-[#8ed5ff]">命中</span></Show>
                            </div>
                            <MarkdownContent content={readableResearchExcerpt(chunk.content, /结构化字段/.test(chunk.heading_path||''))} role="assistant" renderKey={chunk.chunk_id} />
                          </section>
                        )}
                      </For>
                    </div>
                    <Show when={exactObject()}>
                      <details open class="mt-3 rounded-lg border border-[#30383e] bg-[#0b1014] p-3">
                        <summary class="cursor-pointer text-[10px] font-semibold text-[#9faeb7]">完整资料说明</summary>
                        <ResearchRecordDetails value={exactObject()}/>
                      </details>
                    </Show>
                  </div>
                )}
              </Show>
            </div>
          </Show>
        </div>
      </details>

      <div class="mt-4 flex flex-wrap gap-2">
        <button
          class="rounded-lg border border-[#38bdf8]/35 bg-[#38bdf8]/10 px-3 py-2 text-xs font-semibold text-[#8ed5ff]"
          onClick={() => props.onUseInChat(props.asset, detail())}
        >
          在聊天中使用
        </button>
        <button
          class="rounded-lg border border-[#38b46a]/35 bg-[#38b46a]/10 px-3 py-2 text-xs font-semibold text-[#9ee6bb] disabled:cursor-not-allowed disabled:opacity-40"
          disabled={conflict().blocked}
          title={conflict().blocked ? '存在未解决冲突，暂时不能再次导出' : ''}
          onClick={() => props.onReexport(props.asset)}
        >
          再次导出
        </button>
      </div>
    </article>
  );
}
