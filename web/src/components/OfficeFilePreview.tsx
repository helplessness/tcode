import { Show, createEffect, createSignal, on, onCleanup, onMount } from 'solid-js';
import type { FileViewer } from '@open-file-viewer/core';
import { desktop } from '../desktop';
import '@open-file-viewer/core/style.css';

interface OfficeFilePreviewProps {
  src: string;
  fileName: string;
  onError?: (message: string) => void;
}

export default function OfficeFilePreview(props: OfficeFilePreviewProps) {
  const [loading, setLoading] = createSignal(true);
  let container!: HTMLDivElement;
  let viewer: FileViewer | undefined;
  let version = 0;

  onMount(() => {
    createEffect(on(() => [props.src, props.fileName] as const, async ([src, fileName]) => {
      const currentVersion = ++version;
      viewer?.destroy();
      viewer = undefined;
      container.replaceChildren();
      setLoading(true);

      try {
        const { createViewer, officePlugin } = await import('./officeViewerRuntime');
        if (currentVersion !== version) return;
        viewer = createViewer({
          container,
          file: src,
          fileName,
          width: '100%',
          height: '100%',
          fit: 'width',
          locale: 'zh-CN',
          theme: 'dark',
          toolbar: { download: !desktop },
          plugins: [officePlugin()],
          onLoad: () => setLoading(false),
          onError: error => {
            setLoading(false);
            props.onError?.(error.message || 'Office 文件预览失败');
          },
        });
      } catch (reason) {
        if (currentVersion !== version) return;
        setLoading(false);
        props.onError?.(reason instanceof Error ? reason.message : 'Office 文件预览失败');
      }
    }));
  });

  onCleanup(() => {
    version += 1;
    viewer?.destroy();
  });

  return <div class="relative h-[72vh] min-h-[520px] overflow-hidden rounded-2xl border border-[#2a3136] bg-white">
    <Show when={loading()}>
      <div class="absolute inset-0 z-10 grid place-items-center bg-[#171c20] text-sm text-[#8b949e]">正在加载 Office 预览…</div>
    </Show>
    <div ref={container} class="h-full w-full" />
  </div>;
}
