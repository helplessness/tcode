import { Show, createEffect, createSignal, onCleanup, onMount } from 'solid-js';
import { Portal } from 'solid-js/web';
import { desktop } from '../desktop';

export type LightboxImageSource = {
  src: string;
  alt?: string;
  downloadName?: string;
};

type ImageLightboxProps = {
  image: LightboxImageSource | null;
  onClose: () => void;
};

type ZoomableImageProps = LightboxImageSource & {
  class?: string;
  loading?: 'eager' | 'lazy';
  title?: string;
  onError?: () => void;
};

const MIN_SCALE = 0.25;
const MAX_SCALE = 8;
const ZOOM_STEP = 0.25;

export const clampImageScale = (value: number) => (
  Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number.isFinite(value) ? value : 1))
);

export const lightboxDownloadName = (image: LightboxImageSource | null) => {
  const explicit = String(image?.downloadName || '').trim();
  if (explicit) return explicit.split('/').pop() || 'image';
  const source = String(image?.src || '').split(/[?#]/, 1)[0];
  try {
    return decodeURIComponent(source.split('/').pop() || '') || 'image';
  } catch {
    return source.split('/').pop() || 'image';
  }
};

export const usesSingleTapImagePreview = () => (
  typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(hover: none), (pointer: coarse)').matches
);

export default function ImageLightbox(props: ImageLightboxProps) {
  const [scale, setScale] = createSignal(1);
  const [offsetX, setOffsetX] = createSignal(0);
  const [offsetY, setOffsetY] = createSignal(0);
  const [viewMode, setViewMode] = createSignal<'fit' | 'original'>('fit');
  const [dragging, setDragging] = createSignal(false);
  let dragPointerId: number | null = null;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragOriginX = 0;
  let dragOriginY = 0;
  let dragMoved = false;
  let bodyOverflowBeforeOpen = '';
  let bodyScrollLocked = false;

  const resetPosition = () => {
    setOffsetX(0);
    setOffsetY(0);
  };

  const fitToWindow = () => {
    setViewMode('fit');
    setScale(1);
    resetPosition();
  };

  const showOriginalSize = () => {
    setViewMode('original');
    setScale(1);
    resetPosition();
  };

  const setSafeScale = (next: number) => {
    const normalized = clampImageScale(next);
    setScale(normalized);
    if (normalized <= 1 && viewMode() === 'fit') resetPosition();
  };

  const zoomBy = (delta: number) => setSafeScale(scale() + delta);

  const toggleZoom = () => {
    if (viewMode() !== 'fit' || Math.abs(scale() - 1) > 0.01) {
      fitToWindow();
      return;
    }
    setSafeScale(2);
  };

  const stopDragging = (event?: PointerEvent & { currentTarget: HTMLElement }) => {
    if (dragPointerId !== null && event?.currentTarget.hasPointerCapture(dragPointerId)) {
      event.currentTarget.releasePointerCapture(dragPointerId);
    }
    dragPointerId = null;
    setDragging(false);
  };

  const startDragging = (event: PointerEvent & { currentTarget: HTMLElement }) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragPointerId = event.pointerId;
    dragStartX = event.clientX;
    dragStartY = event.clientY;
    dragOriginX = offsetX();
    dragOriginY = offsetY();
    dragMoved = false;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  };

  const moveImage = (event: PointerEvent & { currentTarget: HTMLElement }) => {
    if (!dragging() || dragPointerId !== event.pointerId) return;
    if (Math.abs(event.clientX - dragStartX) > 4 || Math.abs(event.clientY - dragStartY) > 4) {
      dragMoved = true;
    }
    setOffsetX(dragOriginX + event.clientX - dragStartX);
    setOffsetY(dragOriginY + event.clientY - dragStartY);
  };

  const handleWheel = (event: WheelEvent) => {
    event.preventDefault();
    zoomBy(event.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
  };

  const handleKeyDown = (event: KeyboardEvent) => {
    if (!props.image) return;
    if (event.key === 'Escape') props.onClose();
    if (event.key === '+' || event.key === '=') zoomBy(ZOOM_STEP);
    if (event.key === '-') zoomBy(-ZOOM_STEP);
    if (event.key === '0') fitToWindow();
  };

  createEffect(() => {
    if (props.image) {
      fitToWindow();
      if (!bodyScrollLocked) {
        bodyOverflowBeforeOpen = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        bodyScrollLocked = true;
      }
    } else if (bodyScrollLocked) {
      document.body.style.overflow = bodyOverflowBeforeOpen;
      bodyScrollLocked = false;
    }
  });

  onMount(() => window.addEventListener('keydown', handleKeyDown));
  onCleanup(() => {
    window.removeEventListener('keydown', handleKeyDown);
    if (bodyScrollLocked) document.body.style.overflow = bodyOverflowBeforeOpen;
  });

  return (
    <Show when={props.image}>
      {(image) => (
        <Portal>
          <div
            class="fixed inset-0 z-[2200] flex flex-col bg-black/88 backdrop-blur-sm"
            role="dialog"
            aria-modal="true"
            aria-label="图片大图预览"
            onClick={(event) => {
              if (event.target === event.currentTarget) props.onClose();
            }}
          >
            <div class="relative z-10 flex flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-[#0b1014]/92 px-3 py-2.5 text-[#e6edf3] sm:px-5">
              <div class="min-w-0">
                <div class="truncate text-sm font-semibold">{image().alt || lightboxDownloadName(image())}</div>
                <div class="mt-0.5 text-[10px] text-[#8b949e]">滚轮缩放 · 拖拽移动 · 双击复位 · Esc 关闭</div>
              </div>
              <div class="flex flex-wrap items-center justify-end gap-1.5">
                <button type="button" class="rounded-lg border border-[#303b43] bg-[#171d22] px-2.5 py-1.5 text-xs hover:border-[#7bd0ff]/55" onClick={() => zoomBy(-ZOOM_STEP)} aria-label="缩小图片">−</button>
                <span class="min-w-[52px] text-center text-xs tabular-nums text-[#b9c5cc]">{Math.round(scale() * 100)}%</span>
                <button type="button" class="rounded-lg border border-[#303b43] bg-[#171d22] px-2.5 py-1.5 text-xs hover:border-[#7bd0ff]/55" onClick={() => zoomBy(ZOOM_STEP)} aria-label="放大图片">＋</button>
                <button type="button" class={`rounded-lg border px-2.5 py-1.5 text-xs ${viewMode() === 'fit' ? 'border-[#7bd0ff]/55 bg-[#143141] text-[#9bddff]' : 'border-[#303b43] bg-[#171d22] text-[#c9d4da]'}`} onClick={fitToWindow}>适应窗口</button>
                <button type="button" class={`rounded-lg border px-2.5 py-1.5 text-xs ${viewMode() === 'original' ? 'border-[#7bd0ff]/55 bg-[#143141] text-[#9bddff]' : 'border-[#303b43] bg-[#171d22] text-[#c9d4da]'}`} onClick={showOriginalSize}>原始尺寸</button>
                <Show when={!desktop || !image().src.includes('/api/files/')}><a class="rounded-lg border border-[#303b43] bg-[#171d22] px-2.5 py-1.5 text-xs text-[#c9d4da] hover:border-[#7bd0ff]/55" href={image().src} download={lightboxDownloadName(image())}>下载</a></Show>
                <button type="button" class="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-full border border-[#3a454d] bg-[#171d22] text-lg text-[#d6e0e5] hover:border-[#da6b66]/65 hover:text-[#ffb6b0]" onClick={props.onClose} aria-label="关闭图片预览">×</button>
              </div>
            </div>

            <div
              class={`relative min-h-0 flex-1 touch-none overflow-hidden ${dragging() ? 'cursor-grabbing' : 'cursor-grab'}`}
              onClick={(event) => {
                if (dragMoved) {
                  dragMoved = false;
                  return;
                }
                const target = event.target;
                if (target instanceof Element && target.closest('img')) return;
                props.onClose();
              }}
              onWheel={handleWheel}
              onDblClick={(event) => {
                event.preventDefault();
                toggleZoom();
              }}
              onPointerDown={startDragging}
              onPointerMove={moveImage}
              onPointerUp={stopDragging}
              onPointerCancel={stopDragging}
            >
              <div class="absolute inset-0 flex items-center justify-center p-3 sm:p-6">
                <img
                  src={image().src}
                  alt={image().alt || '大图预览'}
                  draggable={false}
                  class="block select-none object-contain shadow-[0_24px_90px_rgba(0,0,0,0.5)]"
                  style={{
                    'max-width': viewMode() === 'fit' ? 'calc(100vw - 1.5rem)' : 'none',
                    'max-height': viewMode() === 'fit' ? 'calc(100vh - 6.5rem)' : 'none',
                    transform: `translate3d(${offsetX()}px, ${offsetY()}px, 0) scale(${scale()})`,
                    'transform-origin': 'center center',
                    transition: dragging() ? 'none' : 'transform 120ms ease-out',
                  }}
                />
              </div>
            </div>
          </div>
        </Portal>
      )}
    </Show>
  );
}

export function ZoomableImage(props: ZoomableImageProps) {
  const [open, setOpen] = createSignal(false);
  const source = (): LightboxImageSource => ({
    src: props.src,
    alt: props.alt,
    downloadName: props.downloadName,
  });

  const openPreview = (event: MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setOpen(true);
  };

  return (
    <>
      <img
        src={props.src}
        alt={props.alt || '图片'}
        class={`${props.class || ''} cursor-zoom-in select-none`.trim()}
        loading={props.loading || 'lazy'}
        title={props.title || '双击查看大图'}
        draggable={false}
        onError={() => props.onError?.()}
        onClick={(event) => {
          if (usesSingleTapImagePreview()) openPreview(event);
        }}
        onDblClick={openPreview}
      />
      <Show when={open()}>
        <ImageLightbox image={source()} onClose={() => setOpen(false)} />
      </Show>
    </>
  );
}
