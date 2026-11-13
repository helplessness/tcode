import { Show, createEffect, createSignal, on } from 'solid-js';
import { getInlineFileUrl } from '../api/client';
import { isHtmlFile, isImageFile, isOfficeFile, isPdfFile } from '../utils/filePreview';
import { ZoomableImage } from './ImageLightbox';
import OfficeFilePreview from './OfficeFilePreview';

interface FilePreviewPanelProps {
  path?: string;
  workspaceId?: string;
  workdir?: string;
}

export default function FilePreviewPanel(props: FilePreviewPanelProps) {
  const [content, setContent] = createSignal('');
  const [error, setError] = createSignal('');
  const [loading, setLoading] = createSignal(false);
  const path = () => String(props.path || '').trim();
  const displayPath = () => path().startsWith('/') ? path() : `/workspace/${path()}`;
  const isRichPreview = () => isImageFile(path()) || isHtmlFile(path()) || isPdfFile(path()) || isOfficeFile(path());
  const previewUrl = () => {
    if (isHtmlFile(path())) {
      const params = new URLSearchParams({ path: path() });
      if (props.workspaceId) params.set('workspace_id', props.workspaceId);
      if (props.workdir) params.set('workdir', props.workdir);
      return `/api/files/html-preview?${params}`;
    }
    return getInlineFileUrl(path(), props.workdir, props.workspaceId);
  };

  createEffect(on(path, async () => {
    const selected = path();
    setContent(''); setError('');
    if (!selected || isRichPreview()) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({ path: selected });
      if (props.workspaceId) params.set('workspace_id', props.workspaceId);
      if (props.workdir) params.set('workdir', props.workdir);
      const response = await fetch(`/api/files/content?${params}`);
      const data = await response.json();
      if (!response.ok) throw new Error(String(data.detail || `读取失败 (${response.status})`));
      setContent(String(data.content || ''));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '文件预览失败');
    } finally {
      setLoading(false);
    }
  }));

  return <section class="file-preview-panel h-full min-h-0 bg-[#171c20]" aria-label="文件预览">
    <header class="flex h-12 items-center border-b border-[#2a3136] px-4">
      <span class="truncate text-xs text-[#b6c2c9]" title={displayPath()}>{displayPath() || '尚未选择文件'}</span>
    </header>
    <div class="tcode-scrollbar h-[calc(100%-3rem)] overflow-auto p-3">
      <Show when={loading()}><div class="tcode-loading-state" role="status" aria-live="polite">正在加载文件预览…</div></Show>
      <Show when={error()}><div role="alert" class="rounded border border-[#f85149]/35 bg-[#f85149]/10 p-4 text-sm text-[#ffb4ae]">{error()}</div></Show>
      <Show when={!loading() && !error() && path()} fallback={<div class="p-3 text-sm text-[#8b949e]">从聊天或文件列表中选择一个文件。</div>}>
        <Show when={isImageFile(path())}>
          <ZoomableImage src={previewUrl()} alt={path()} downloadName={path()} class="mx-auto max-h-[calc(100vh-10rem)] max-w-full rounded" />
        </Show>
        <Show when={isHtmlFile(path()) || isPdfFile(path())}>
          <iframe src={previewUrl()} title={`预览 ${path()}`} sandbox={isHtmlFile(path()) ? 'allow-scripts' : undefined} class="h-[calc(100vh-10rem)] min-h-[560px] w-full rounded border border-[#2a3136] bg-white" />
        </Show>
        <Show when={isOfficeFile(path())}>
          <OfficeFilePreview src={previewUrl()} fileName={path()} onError={setError} />
        </Show>
        <Show when={!isRichPreview()}>
          <pre class="min-h-full overflow-x-auto rounded border border-[#2a3136] bg-[#0d1216] p-4 font-mono text-xs text-[#e6edf3] whitespace-pre-wrap">{content()}</pre>
        </Show>
      </Show>
    </div>
  </section>;
}
