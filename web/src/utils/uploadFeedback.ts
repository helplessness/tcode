export function nearUploadTarget(rect: { left: number; right: number; top: number; bottom: number }, x: number, y: number) {
  return x >= rect.left - 32 && x <= rect.right + 32 && y >= rect.top - 32 && y <= rect.bottom + 32;
}

/** Decorative feedback only: never delays or changes the actual upload. */
export function installUploadFeedback(target: HTMLElement, accepts: (event?: DragEvent) => boolean): () => void {
  let ghost: HTMLElement | undefined;
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.reduceMotion === 'true';
  const reset = () => { target.removeAttribute('data-upload-near'); };
  const over = (event: DragEvent) => {
    const files = Array.from(event.dataTransfer?.types || []).includes('Files');
    if (files && accepts() && nearUploadTarget(target.getBoundingClientRect(), event.clientX, event.clientY)) target.dataset.uploadNear = 'true';
    else reset();
  };
  const leave = (event: DragEvent) => { if (event.clientX <= 0 || event.clientY <= 0 || event.clientX >= window.innerWidth || event.clientY >= window.innerHeight) reset(); };
  const drop = (event: DragEvent) => {
    reset();
    const files = event.dataTransfer?.files;
    if (!files?.length || !accepts(event) || reduced()) return;
    ghost?.remove();
    const rect = target.getBoundingClientRect();
    const card = document.createElement('span');
    card.className = 'upload-file-ingest'; card.setAttribute('aria-hidden', 'true');
    card.textContent = files.length > 1 ? `${files[0].name} +${files.length - 1}` : files[0].name;
    Object.assign(card.style, { left: `${event.clientX - 70}px`, top: `${event.clientY - 22}px` });
    document.body.append(card); ghost = card;
    const animation = card.animate([
      { transform: 'translate(0, -12px) scale(1)', opacity: 1 },
      { transform: `translate(${rect.left + rect.width / 2 - event.clientX}px, ${rect.top + rect.height / 2 - event.clientY}px) scale(.08)`, opacity: 0 },
    ], { duration: 460, easing: 'cubic-bezier(.55,0,.8,.45)' });
    void animation.finished.catch(() => {}).finally(() => { card.remove(); if (ghost === card) ghost = undefined; });
  };
  document.addEventListener('dragover', over, true);
  document.addEventListener('dragleave', leave);
  document.addEventListener('drop', reset);
  document.addEventListener('dragend', reset);
  window.addEventListener('blur', reset);
  target.addEventListener('drop', drop, true);
  return () => {
    document.removeEventListener('dragover', over, true); document.removeEventListener('dragleave', leave);
    document.removeEventListener('drop', reset); document.removeEventListener('dragend', reset);
    window.removeEventListener('blur', reset); target.removeEventListener('drop', drop, true);
    reset(); ghost?.remove();
  };
}
