/** Move only visible slots; filtered-out sessions keep their positions. */
export function reorderSessions(order: string[], visible: string[], moving: string, target: string) {
  const from = visible.indexOf(moving), to = visible.indexOf(target);
  if (from < 0 || to < 0 || from === to) return order;
  const next = [...visible]; next.splice(to, 0, next.splice(from, 1)[0]);
  const slots = new Set(visible); let index = 0;
  return order.map(id => slots.has(id) ? next[index++] : id);
}

/** Delegated pointer capture survives keyed rows and never hijacks a short click. */
export function sessionDragSort(host: HTMLElement, callbacks: {
  start: () => void; drop: (id: string, target: string, kind?:string) => void; end: () => void;
}) {
  let pending: { id: number; x: number; y: number; row: HTMLElement } | undefined;
  let active: { rows: HTMLElement[]; tops: number[]; heights: number[]; from: number; to: number; scroll: number } | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined, frame = 0, pointerY = 0, suppressClick = false;
  const animations = new Set<Animation>();
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.reduceMotion === 'true';
  const selector='[data-session-id],[data-workspace-sort-id]';
  const rows = () => [...host.querySelectorAll<HTMLElement>(selector)].filter(row=>row.getBoundingClientRect().height>0);
  const rowId=(row:HTMLElement)=>row.dataset.workspaceSortId??row.dataset.sessionId!;
  const stopTimer = () => { if (timer) clearTimeout(timer); timer = undefined; };
  const paint = () => {
    if (!active || !pending) return;
    const d = active;
    let delta = pointerY - pending.y + host.scrollTop - d.scroll;
    if(pending.row.dataset.sortGroup!==undefined&&pending.row.dataset.workspaceSortId===undefined){
      const last=d.rows.length-1;
      delta=Math.max(d.tops[0]-d.tops[d.from],Math.min(d.tops[last]+d.heights[last]-d.tops[d.from]-d.heights[d.from],delta));
    }
    const center = d.tops[d.from] + d.heights[d.from] / 2 + delta;
    d.to = d.tops.reduce((closest, top, i) =>
      Math.abs(center-top-d.heights[i]/2) < Math.abs(center-d.tops[closest]-d.heights[closest]/2) ? i : closest, d.from);
    const gap=d.rows.length>1?Math.max(0,Math.min(...d.tops.slice(1).map((top,i)=>top-d.tops[i]-d.heights[i]))):0;
    const step = d.heights[d.from]+gap;
    d.rows.forEach((row, i) => {
      const shift = i === d.from ? delta : d.to > d.from && i > d.from && i <= d.to ? -step : d.to < d.from && i >= d.to && i < d.from ? step : 0;
      row.style.setProperty('--session-drag-y', `${shift}px`);
    });
  };
  const tick = () => {
    if (!active || !pending) return;
    const rect = host.getBoundingClientRect();
    if (pointerY > rect.top && pointerY < rect.bottom) {
      const speed = pointerY < rect.top + 32 ? -10 : pointerY > rect.bottom - 32 ? 10 : 0;
      if (speed) { host.scrollTop += speed; paint(); }
    }
    frame = requestAnimationFrame(tick);
  };
  const finish = (commit: boolean, animate = true) => {
    stopTimer(); cancelAnimationFrame(frame);
    const p = pending, d = active; pending = undefined; active = undefined;
    if (!d || !p) return;
    const positions = new Map(d.rows.map(row => [rowId(row), row.getBoundingClientRect().top]));
    for (const row of d.rows) {
      row.style.transition = 'none'; row.style.removeProperty('--session-drag-y'); row.classList.remove('is-session-dragging');
    }
    host.classList.remove('is-session-sorting');
    if (host.hasPointerCapture(p.id)) host.releasePointerCapture(p.id);
    if (commit && d.from !== d.to) callbacks.drop(rowId(p.row), rowId(d.rows[d.to]),p.row.dataset.workspaceSortId===undefined?'session':'workspace');
    callbacks.end();
    for (const row of rows()) {
      const delta = (positions.get(rowId(row)) ?? row.getBoundingClientRect().top) - row.getBoundingClientRect().top;
      if (animate && !reduced() && Math.abs(delta) > .5) {
        const animation = row.animate([{ translate: `0 ${delta}px` }, { translate: '0 0' }], { duration: 460, easing: 'linear(0, .25, .6, .87, 1.02, 1.07, 1.045, 1.015, 1)' });
        animations.add(animation); animation.onfinish = () => animations.delete(animation);
      }
      row.style.removeProperty('transition');
    }
  };
  const down = (event: PointerEvent) => {
    if (event.button !== 0 || !event.isPrimary || pending || !(event.target instanceof Element)) return;
    const row = event.target.closest<HTMLElement>(selector);
    if (!row || event.target.closest('button,input,textarea,select,a,summary') || event.ctrlKey || event.metaKey || event.altKey) return;
    suppressClick = false;
    pending = {id:event.pointerId, x:event.clientX, y:event.clientY, row}; pointerY = event.clientY;
    timer = setTimeout(() => {
      if (!pending || !row.isConnected) return;
      callbacks.start(); animations.forEach(a => a.cancel()); animations.clear();
      const group = rows().filter(r => (r.dataset.workspaceSortId===undefined)===(row.dataset.workspaceSortId===undefined)
        && r.dataset.sortGroup===row.dataset.sortGroup && r.dataset.sessionPinned === row.dataset.sessionPinned);
      const from = group.indexOf(row); if (from < 0) { pending = undefined; callbacks.end(); return; }
      const bounds = group.map(r => r.getBoundingClientRect());
      active = {rows:group,tops:bounds.map(r=>r.top),heights:bounds.map(r=>r.height),from,to:from,scroll:host.scrollTop};
      suppressClick = true; row.classList.add('is-session-dragging'); host.classList.add('is-session-sorting');
      host.setPointerCapture(pending.id); frame = requestAnimationFrame(tick);
    }, 320);
  };
  const move = (event: PointerEvent) => {
    if (!pending || pending.id !== event.pointerId) return;
    pointerY = event.clientY;
    if (!active) { if (Math.hypot(event.clientX-pending.x,event.clientY-pending.y)>7) {stopTimer();pending=undefined;} return; }
    event.preventDefault(); paint();
  };
  const up = (event: PointerEvent) => { if (pending?.id === event.pointerId) { finish(true); pending=undefined; } };
  const cancel = () => { finish(false); pending=undefined; };
  const key = (event: KeyboardEvent) => {if (event.key==='Escape' && pending) {event.preventDefault();cancel();}};
  const click = (event: MouseEvent) => {if(suppressClick){event.preventDefault();event.stopImmediatePropagation();suppressClick=false;}};
  const context = (event: Event) => { if(active){event.preventDefault();event.stopImmediatePropagation();cancel();} };
  host.addEventListener('pointerdown',down); host.addEventListener('click',click,true); host.addEventListener('contextmenu',context,true);
  window.addEventListener('pointermove',move,{passive:false}); window.addEventListener('pointerup',up); window.addEventListener('pointercancel',cancel); window.addEventListener('keydown',key); window.addEventListener('blur',cancel);
  return () => {
    finish(false,false); stopTimer(); animations.forEach(a=>a.cancel());
    host.removeEventListener('pointerdown',down);host.removeEventListener('click',click,true);host.removeEventListener('contextmenu',context,true);
    window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',cancel);window.removeEventListener('keydown',key);window.removeEventListener('blur',cancel);
  };
}
