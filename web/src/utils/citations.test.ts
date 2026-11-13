import { describe, expect, it, vi } from 'vitest';
import { CITATION_OPEN_EVENT, openCitation } from './citations';

describe('openCitation', () => {
  it('dispatches a typed request without exposing storage paths', () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal('window', { dispatchEvent });
    vi.stubGlobal('CustomEvent', class { type: string; detail: unknown; constructor(type: string, init: { detail: unknown }) { this.type = type; this.detail = init.detail; } });
    openCitation({ kind: 'ontology_evidence', id: 'private-evidence:1' });
    const event = dispatchEvent.mock.calls[0][0] as CustomEvent;
    expect(event.type).toBe(CITATION_OPEN_EVENT);
    expect(event.detail.ref).toEqual({ kind: 'ontology_evidence', id: 'private-evidence:1' });
  });
});
