import { afterEach, describe, expect, it, vi } from 'vitest';
import { dismissDetailsOnOutsideClick } from './dismissDetails';

describe('dismissDetailsOnOutsideClick', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('closes only when the pointer is outside', () => {
    let listener: ((event: PointerEvent) => void) | undefined;
    vi.stubGlobal('document', {
      addEventListener: (_name: string, callback: (event: PointerEvent) => void) => { listener = callback; },
      removeEventListener: vi.fn(),
    });
    const inside = {};
    const details = { open: true, contains: (target: unknown) => target === inside } as HTMLDetailsElement;
    dismissDetailsOnOutsideClick(() => details);

    listener?.({ target: inside } as PointerEvent);
    expect(details.open).toBe(true);
    listener?.({ target: {} } as PointerEvent);
    expect(details.open).toBe(false);
  });
});
