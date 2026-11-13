export function generateId(): string {
  if (typeof globalThis !== 'undefined') {
    const c = globalThis.crypto;
    if (c && typeof c.randomUUID === 'function') {
      return c.randomUUID();
    }
  }

  const timePart = Date.now().toString(36);
  const randomPart = Math.random().toString(36).slice(2, 10);
  return `${timePart}-${randomPart}`;
}
