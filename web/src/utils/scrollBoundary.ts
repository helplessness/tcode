// Fixed/popover scroll regions do not reliably chain to their DOM ancestors.
// Keep native scrolling inside the region; only forward at its vertical edge.
export function passVerticalScrollToParent(event: WheelEvent) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
  const viewport = event.currentTarget as HTMLElement;
  const canMove = (element: HTMLElement) => /auto|scroll/.test(window.getComputedStyle(element).overflowY)
    && (event.deltaY < 0 ? element.scrollTop > 1 : element.scrollTop + element.clientHeight < element.scrollHeight - 1);
  let inner = event.target as HTMLElement | null;
  while (inner && inner !== viewport) {
    if (canMove(inner)) return;
    inner = inner.parentElement;
  }
  if (canMove(viewport)) return;
  let outer = viewport.parentElement;
  while (outer) {
    if (canMove(outer)) {
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? outer.clientHeight : 1;
      outer.scrollBy({ top: event.deltaY * unit, behavior: 'instant' });
      return;
    }
    outer = outer.parentElement;
  }
}
