/** Measure the real, possibly async-updated menu instead of guessing its height. */
export function watchSessionMenu(panel: HTMLElement, host: HTMLElement, anchor: { x: number; y: number }) {
  let disposed = false;
  const update = () => {
    if (disposed || !panel.isConnected) return;
    const bounds = host.getBoundingClientRect();
    const left = Math.max(8, bounds.left + 8), right = Math.min(innerWidth - 8, bounds.right - 8);
    const top = Math.max(8, bounds.top + 8), bottom = Math.min(innerHeight - 8, bounds.bottom - 8);
    const width = Math.max(0, Math.min(196, right - left));
    panel.style.boxSizing = 'border-box';
    panel.style.width = `${width}px`;
    panel.style.maxHeight = `${Math.max(0, bottom - top)}px`;
    const height = Math.min(panel.scrollHeight + 2, Math.max(0, bottom - top));
    const up = bottom - anchor.y - 4 < height && anchor.y - top > bottom - anchor.y;
    panel.style.left = `${Math.max(left, Math.min(anchor.x, right - width))}px`;
    panel.style.top = `${Math.max(top, Math.min(up ? anchor.y - height - 4 : anchor.y + 4, bottom - height))}px`;
  };
  const observer = new ResizeObserver(update);
  observer.observe(panel);
  observer.observe(host);
  // Child labels/options can change without changing the constrained panel size.
  const mutation = new MutationObserver(update);
  mutation.observe(panel, { childList: true, subtree: true, characterData: true });
  window.addEventListener('resize', update);
  queueMicrotask(update);
  return () => { disposed = true; observer.disconnect(); mutation.disconnect(); window.removeEventListener('resize', update); };
}

/** Keep workspace menus within their scrolling sidebar, not just the window. */
export function watchWorkspaceMenus(host: HTMLElement) {
  const position = (menu: HTMLDetailsElement) => {
    const panel = menu.querySelector<HTMLElement>(':scope > div');
    if (!menu.open || !panel) return;
    const anchor = menu.querySelector('summary')!.getBoundingClientRect();
    const bounds = host.getBoundingClientRect();
    const left = Math.max(8, bounds.left + host.clientLeft + 8);
    const right = Math.min(innerWidth - 8, bounds.left + host.clientLeft + host.clientWidth - 8);
    const top = Math.max(8, bounds.top + host.clientTop + 8);
    const bottom = Math.min(innerHeight - 8, bounds.top + host.clientTop + host.clientHeight - 8);
    if (anchor.bottom < top || anchor.top > bottom) { menu.open = false; return; }
    const width = Math.max(0, Math.min(160, right - left));
    panel.style.boxSizing = 'border-box';
    panel.style.width = `${width}px`;
    panel.style.maxHeight = 'none';
    const height = panel.scrollHeight + 2;
    const below = Math.max(0, bottom - anchor.bottom - 4);
    const above = Math.max(0, anchor.top - top - 4);
    const up = below < height && above > below;
    menu.dataset.menuSide = up ? 'up' : 'down';
    panel.style.maxHeight = `${up ? above : below}px`;
    // Details and summary share the same origin, even inside translated groups.
    const owner = menu.getBoundingClientRect();
    panel.style.left = `${Math.max(left, Math.min(anchor.right - width, right - width)) - owner.left}px`;
  };
  const update = () => host.querySelectorAll<HTMLDetailsElement>('.session-workspace-menu[open]').forEach(position);
  const toggle = (event: Event) => {
    if (event.target instanceof HTMLDetailsElement && event.target.matches('.session-workspace-menu')) {
      if (event.target.open) {
        host.querySelectorAll<HTMLDetailsElement>('.session-workspace-menu[open]').forEach(other=>{if(other!==event.target)other.open=false;});
        position(event.target);
      } else delete event.target.dataset.menuSide;
    }
  };
  const observer = new ResizeObserver(update);
  observer.observe(host);
  host.addEventListener('toggle', toggle, true);
  host.addEventListener('scroll', update);
  window.addEventListener('resize', update);
  return () => {
    observer.disconnect(); host.removeEventListener('toggle', toggle, true);
    host.removeEventListener('scroll', update); window.removeEventListener('resize', update);
  };
}
