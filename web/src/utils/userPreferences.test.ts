import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readBooleanPreference, readPreference, savePreference } from './userPreferences';

describe('userPreferences', () => {
  const values = new Map<string, string>();
  beforeEach(() => {
    values.clear();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    });
  });

  it('uses defaults and persists boolean and select values', () => {
    expect(readBooleanPreference('motion', true)).toBe(true);
    expect(readPreference('mode', 'async')).toBe('async');
    savePreference('motion', false);
    savePreference('mode', 'direct');
    expect(readBooleanPreference('motion', true)).toBe(false);
    expect(readPreference('mode', 'async')).toBe('direct');
  });
});
