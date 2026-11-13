import { describe, expect, it } from 'vitest';

import { resolveActiveModel, shouldAcceptResponseModel } from './modelSelection';

describe('resolveActiveModel', () => {
  it('keeps a server-confirmed selection when a background projection is stale', () => {
    expect(resolveActiveModel(
      'kimi/kimi-k2.6',
      'kimi/kimi-k2.5',
      'kimi/kimi-k2.5',
    )).toBe('kimi/kimi-k2.6');
  });

  it('falls back to server projections before a selection is loaded', () => {
    expect(resolveActiveModel('', 'kimi/kimi-k2.6', 'kimi/kimi-k2.5'))
      .toBe('kimi/kimi-k2.6');
    expect(resolveActiveModel('', '', 'kimi/kimi-k2.5'))
      .toBe('kimi/kimi-k2.5');
  });

  it('rejects a late response from the model used before a rapid switch', () => {
    expect(shouldAcceptResponseModel('kimi/kimi-k2.6', 'kimi/kimi-k2.5')).toBe(false);
    expect(shouldAcceptResponseModel('kimi/kimi-k2.6', 'kimi/kimi-k2.6')).toBe(true);
  });
});
