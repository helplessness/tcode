/** Native snapshots keep card geometry continuous without delaying data loading. */
export function openCardDetail(source: HTMLElement, destination: string, open: () => void): void {
  if (document.documentElement.dataset.desktop !== 'true'
    || document.documentElement.dataset.reduceMotion === 'true'
    || window.matchMedia('(prefers-reduced-motion: reduce)').matches
    || !document.startViewTransition) {
    open();
    return;
  }
  // A second click must not create duplicate named elements in a snapshot.
  if (document.querySelector('[data-card-transition]')) {
    open();
    return;
  }
  const previousName = source.style.viewTransitionName;
  source.dataset.cardTransition = 'true';
  document.documentElement.classList.add('tcode-card-transition');
  source.style.viewTransitionName = 'tcode-card-detail';
  let target: HTMLElement | null = null;
  let targetName = '';
  const transition = document.startViewTransition(() => {
    source.style.viewTransitionName = previousName;
    open();
    target = document.querySelector<HTMLElement>(destination);
    if (target) {
      targetName = target.style.viewTransitionName;
      target.style.viewTransitionName = 'tcode-card-detail';
      target.dataset.cardTransition = 'true';
    }
  });
  void transition.finished.catch(() => {}).finally(() => {
    source.style.viewTransitionName = previousName;
    delete source.dataset.cardTransition;
    document.documentElement.classList.remove('tcode-card-transition');
    if (target) {
      target.style.viewTransitionName = targetName;
      delete target.dataset.cardTransition;
    }
  });
}
