export function dismissDetailsOnOutsideClick(details: () => HTMLDetailsElement | undefined) {
  const dismiss = (event: PointerEvent) => {
    const element = details();
    if (element && !element.contains(event.target as Node)) element.open = false;
  };
  document.addEventListener('pointerdown', dismiss);
  return () => document.removeEventListener('pointerdown', dismiss);
}
