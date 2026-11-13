import { For, Index, Show, Suspense, createMemo, createSignal, lazy } from 'solid-js';
import { sessionStore } from '../stores/session';
import {
  getPresentedImageUrl,
  groupCanvasResults,
  type CanvasContentBlock,
  type MessageContentBlock,
} from '../utils/contentBlocks';
import DataTableBlock from './DataTableBlock';
import { ZoomableImage } from './ImageLightbox';
import JobSummaryBlock from './JobSummaryBlock';

const McpAppHost = lazy(() => import('./McpAppHost'));
const CanvasBlock = lazy(() => import('./CanvasBlock'));
const IntelligentPageBlock = lazy(() => import('./IntelligentPageBlock'));
const ResearchCompareBlock = lazy(() => import('./ResearchCompareBlock'));

type StructuredContentBlocksProps = {
  blocks?: MessageContentBlock[];
  class?: string;
  allowSessionActions?: boolean;
};

export default function StructuredContentBlocks(props: StructuredContentBlocksProps) {
  const canvases = createMemo(() => groupCanvasResults(props.blocks || []));
  const pages = createMemo(() => (props.blocks || []).filter(b => b.type === 'intelligent_page'));
  const comparisons = createMemo(() => (props.blocks || []).filter(b => b.type === 'research_compare'));
  const selections = createMemo(() => (props.blocks || []).filter(b => b.type === 'result_selection'));
  const images = createMemo(() =>
    (Array.isArray(props.blocks) ? props.blocks : []).filter((block) => block.type === 'image')
  );
  const mcpApps = createMemo(() =>
    props.allowSessionActions === false ? [] : (Array.isArray(props.blocks) ? props.blocks : []).filter((block) => block.type === 'mcp_app')
  );
  const artifacts = createMemo(() =>
    (Array.isArray(props.blocks) ? props.blocks : []).filter((block) => block.type === 'artifact')
  );
  const jobSummaries = createMemo(() =>
    (Array.isArray(props.blocks) ? props.blocks : []).filter((block) => block.type === 'job_summary')
  );
  const dataTables = createMemo(() =>
    (Array.isArray(props.blocks) ? props.blocks : []).filter((block) => block.type === 'data_table')
  );
  const [failedAssets, setFailedAssets] = createSignal<Set<string>>(new Set());
  const markFailed = (assetId: string) => {
    setFailedAssets((current) => new Set([...current, assetId]));
  };

  return (
    <Show when={
      pages().length > 0 || selections().length > 0 || images().length > 0 || canvases().length > 0 || comparisons().length > 0
      || mcpApps().length > 0
      || artifacts().length > 0
      || jobSummaries().length > 0
      || dataTables().length > 0
    }>
      <div class={`space-y-3 ${props.class || ''}`.trim()}>
        <For each={pages()}>{block => <Suspense fallback={<div role="status">正在加载交互页面…</div>}>
          <IntelligentPageBlock block={block} allowSessionActions={props.allowSessionActions} />
        </Suspense>}</For>
        <For each={comparisons()}>{block => <Suspense fallback={<div role="status">正在加载候选比较…</div>}>
          <ResearchCompareBlock block={block} allowSessionActions={props.allowSessionActions} />
        </Suspense>}</For>
        <Index each={canvases()}>{group => <CanvasResults blocks={group()} allowSessionActions={props.allowSessionActions} />}</Index>
        <For each={selections()}>{block => <div role="status" class="rounded-xl border border-green-700 p-3 text-sm">
          <div>{block.fallback}</div>
          <For each={block.items}>{item => <div class="mt-1 text-xs text-[#9faeb7]">
            <span>{item.label} · {item.item_id}</span>
            <For each={Object.entries(item.metrics || {}).slice(0, 8)}>{([key, value]) => <span class="ml-3">{key}: {String(value ?? '—')}</span>}</For>
          </div>}</For>
        </div>}</For>
        <For each={jobSummaries()}>
          {(block) => <JobSummaryBlock block={block} />}
        </For>
        <For each={dataTables()}>
          {(block) => <DataTableBlock block={block} />}
        </For>
        <For each={images()}>
          {(block) => (
            <figure class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0f1418]">
              <Show
                when={!failedAssets().has(block.asset_id)}
                fallback={
                  <div class="px-4 py-3 text-sm text-[#ffb6b0]">
                    图片资源当前不可用：{block.caption || block.alt || block.path}
                  </div>
                }
              >
                <button
                  type="button"
                  class="block w-full cursor-zoom-in bg-[#0b1013] text-left"
                  title={`打开图片文件：${block.path}`}
                  onClick={() => sessionStore.requestFilePreview(block.path)}
                  disabled={props.allowSessionActions === false}
                >
                  <ZoomableImage
                    class="mx-auto block max-h-[72vh] max-w-full object-contain"
                    src={getPresentedImageUrl(block)}
                    alt={block.alt || block.caption || '图片'}
                    downloadName={block.path}
                    loading="lazy"
                    onError={() => markFailed(block.asset_id)}
                  />
                </button>
              </Show>
              <Show when={block.caption}>
                <figcaption class="border-t border-[#2a3136] px-4 py-2.5 text-xs leading-relaxed text-[#9faeb7]">
                  {block.caption}
                </figcaption>
              </Show>
            </figure>
          )}
        </For>
        <For each={mcpApps()}>
          {(block) => (
            <Suspense
              fallback={
                <div
                  data-testid="mcp-app-loading"
                  class="rounded-2xl border border-[#2a3136] bg-[#0f1418] px-4 py-4 text-sm text-[#9faeb7]"
                >
                  正在加载交互界面…
                </div>
              }
            >
              <McpAppHost block={block} />
            </Suspense>
          )}
        </For>
        <For each={artifacts()}>
          {(block) => (
            <a
              href={block.uri}
              download={block.title}
              data-testid="artifact-download"
              class="flex items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0f1418] px-4 py-3 text-left transition-colors hover:border-[#7bd0ff]/40 hover:bg-[#121a1f]"
            >
              <span class="min-w-0">
                <span class="block truncate text-sm font-semibold text-[#dce7ed]">{block.title}</span>
                <span class="block truncate text-xs text-[#83929b]">{block.media_type}</span>
              </span>
              <span class="shrink-0 text-xs font-semibold text-[#8ed5ff]">下载</span>
            </a>
          )}
        </For>
      </div>
    </Show>
  );
}

function CanvasResults(props: { blocks: CanvasContentBlock[]; allowSessionActions?: boolean }) {
  const [selected, setSelected] = createSignal('');
  const current = () => props.blocks.find(block=>block.canvas_id===selected()) || props.blocks[props.blocks.length-1];
  return <Show when={current()?.canvas_id} keyed>{(_id)=><Suspense fallback={<div role="status">正在加载画布…</div>}>
    <CanvasBlock block={current()!} alternatives={props.blocks} onAlternative={setSelected} allowSessionActions={props.allowSessionActions} />
  </Suspense>}</Show>;
}
