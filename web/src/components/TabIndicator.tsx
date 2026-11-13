import { createEffect, onMount, onCleanup } from 'solid-js';

export function tabIndicatorFrames(from: { left: number; width: number }, to: { left: number; width: number }, extent: number) {
  const peak = Math.min(extent, 2 * Math.max(from.width, to.width));
  const clamp = (left: number, width: number) => Math.max(0, Math.min(left, extent - width));
  const direction = Math.sign(to.left - from.left);
  return [
    { left: `${from.left}px`, width: `${from.width}px`, offset: 0 },
    { left: `${clamp((from.left + to.left) / 2 - (peak - to.width) / 2, peak)}px`, width: `${peak}px`, offset: .45 },
    { left: `${clamp(to.left + direction * 4, to.width)}px`, width: `${to.width}px`, offset: .82 },
    { left: `${to.left}px`, width: `${to.width}px`, offset: 1 },
  ];
}

/** Only the decorative bar moves. Labels, hit targets and rounded ends stay intact. */
export default function TabIndicator(props: { active: string; selector?: string; top?: boolean }) {
  let bar!: HTMLSpanElement;
  onMount(() => {
    const host = bar.parentElement!;
    let animation: Animation | undefined, selected: HTMLElement | null = null;
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = (animate = false) => {
      const target = host.querySelector<HTMLElement>(props.selector || 'button[data-active=true]');
      if (target !== selected) { if (selected) observer.unobserve(selected); if (target) observer.observe(target); selected = target; }
      if (!target || !target.offsetWidth) { animation?.cancel(); bar.style.visibility = 'hidden'; return; }
      const rect = target.getBoundingClientRect(), parent = host.getBoundingClientRect();
      const to = { left: rect.left - parent.left + host.scrollLeft - host.clientLeft, width: rect.width };
      if (!animate && bar.style.visibility === 'visible' && Math.abs(parseFloat(bar.style.left) - to.left) < .5 && Math.abs(parseFloat(bar.style.width) - to.width) < .5) return;
      const style = getComputedStyle(bar), from = { left: parseFloat(style.left) || 0, width: parseFloat(style.width) || 0 };
      animation?.cancel();
      Object.assign(bar.style, { left: `${to.left}px`, width: `${to.width}px`, visibility: 'visible' });
      if (animate && from.width && Math.abs(from.left - to.left) > .5 && !media.matches && document.documentElement.dataset.reduceMotion !== 'true') {
        animation = bar.animate(tabIndicatorFrames(from, to, host.scrollWidth), { duration: 440, easing: 'ease-in-out' });
      }
    };
    const observer = new ResizeObserver(() => update());
    observer.observe(host);
    createEffect(() => { props.active; update(true); });
    const preferenceChanged = () => update();
    media.addEventListener('change', preferenceChanged);
    window.addEventListener('tcode:settings-changed', preferenceChanged);
    onCleanup(() => { animation?.cancel(); observer.disconnect(); media.removeEventListener('change', preferenceChanged); window.removeEventListener('tcode:settings-changed', preferenceChanged); });
  });
  return <span ref={bar} class="elastic-tab-indicator" data-top={Boolean(props.top)} aria-hidden="true" />;
}
