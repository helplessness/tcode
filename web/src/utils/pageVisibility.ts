import { createSignal, onCleanup, onMount, type Accessor } from 'solid-js';

export function createPageVisibleSignal(): Accessor<boolean> {
  const [pageVisible, setPageVisible] = createSignal(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  );

  onMount(() => {
    const sync = () => {
      setPageVisible(document.visibilityState === 'visible');
    };
    document.addEventListener('visibilitychange', sync);
    onCleanup(() => document.removeEventListener('visibilitychange', sync));
  });

  return pageVisible;
}
