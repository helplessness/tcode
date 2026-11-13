/** A bounded, decorative ripple; never changes button layout or intercepts input. */
export function rippleGeometry(width: number, height: number, x: number, y: number) {
  return { x, y, diameter: 2 * Math.hypot(Math.max(x, width - x), Math.max(y, height - y)) };
}

export function installButtonFeedback(): () => void {
  const active = new Set<HTMLElement>();
  const media = window.matchMedia('(prefers-reduced-motion: reduce)');
  const clear = () => { active.forEach(node => node.remove()); active.clear(); };
  const reduced = () => media.matches || document.documentElement.dataset.reduceMotion === 'true';
  const preferenceChanged = () => { if (reduced()) clear(); };
  const click = (event: MouseEvent) => {
    if (reduced() || !(event.target instanceof Element)) return;
    const button = event.target.closest<HTMLButtonElement>('.studio-rail-new, .settings-save, .session-shortcuts button, [data-composer] button[type="submit"], button[data-ripple]');
    if (!button || button.disabled || !button.isConnected) return;
    const rect = button.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const { x, y, diameter } = rippleGeometry(rect.width, rect.height,
      event.detail === 0 ? rect.width / 2 : Math.max(0, Math.min(rect.width, event.clientX - rect.left)),
      event.detail === 0 ? rect.height / 2 : Math.max(0, Math.min(rect.height, event.clientY - rect.top)));
    const overlay = document.createElement('span');
    overlay.className = 'tcode-button-feedback';
    overlay.setAttribute('aria-hidden', 'true');
    Object.assign(overlay.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`, borderRadius: getComputedStyle(button).borderRadius });
    const wave = document.createElement('span');
    Object.assign(wave.style, { left: `${x - diameter / 2}px`, top: `${y - diameter / 2}px`, width: `${diameter}px`, height: `${diameter}px` });
    overlay.append(wave);
    // Bound rapid-click work, and remove completed effects even if the button unmounts.
    if (active.size >= 4) { const oldest = active.values().next().value!; oldest.remove(); active.delete(oldest); }
    active.add(overlay);
    document.body.append(overlay);
    const animation = wave.animate([{ transform: 'scale(0)', opacity: .24 }, { transform: 'scale(1)', opacity: 0 }], { duration: 420, easing: 'cubic-bezier(.2,.8,.2,1)' });
    void animation.finished.catch(() => {}).finally(() => { overlay.remove(); active.delete(overlay); });
  };
  document.addEventListener('click', click, true);
  window.addEventListener('tcode:settings-changed', preferenceChanged);
  media.addEventListener('change', preferenceChanged);
  return () => { document.removeEventListener('click', click, true); window.removeEventListener('tcode:settings-changed', preferenceChanged); media.removeEventListener('change', preferenceChanged); clear(); };
}
