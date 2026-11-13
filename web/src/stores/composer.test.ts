import { beforeEach, describe, expect, it, vi } from 'vitest';
import { composerDrafts, composerKey } from './composer';

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  });
});

describe('composer draft persistence', () => {
  it('rehydrates selected skills only in their own session draft', () => {
    const key = composerKey('skills-restored');
    localStorage.setItem('tcode-composer-v1:' + key, JSON.stringify({ text: '预测结构', selectedSkills: ['boltz-runner', null, 123] }));
    expect(composerDrafts.get(key).selectedSkills).toEqual(['boltz-runner']);
    expect(composerDrafts.get(composerKey('skills-other')).selectedSkills).toEqual([]);
  });

  it('reuses the guidance id after an uncertain response, including restored drafts', () => {
    const key = composerKey('guidance-retry');
    const sent = { text: 'keep going carefully', files: [] };
    composerDrafts.update(key, sent);
    expect(composerDrafts.guidanceRequest(key, sent, 'steer', 'first')).toBe('first');
    composerDrafts.update(key, { failed: { ...sent, id: 'first', error: 'network' } });
    composerDrafts.restoreFailed(key);
    expect(composerDrafts.guidanceRequest(key, sent, 'steer', 'retry')).toBe('first');
    const persistedKey = composerKey('guidance-rehydrated');
    localStorage.setItem('tcode-composer-v1:' + persistedKey, localStorage.getItem('tcode-composer-v1:' + key)!);
    expect(composerDrafts.guidanceRequest(persistedKey, sent, 'steer', 'reload')).toBe('first');
    composerDrafts.guidanceAccepted(key, 'first');
    expect(composerDrafts.guidanceRequest(key, sent, 'steer', 'next')).toBe('next');
    expect(composerDrafts.guidanceRequest(key, { text: 'changed', files: [] }, 'steer', 'changed')).toBe('changed');
  });

  it('separates both session and identity', () => {
    localStorage.setItem('tcode_agent_identity_scope', 'user-a:tenant-a');
    const a = composerKey('session-a');
    composerDrafts.update(a, { text: 'A only' });
    expect(composerDrafts.get(composerKey('session-b')).text).toBe('');
    localStorage.setItem('tcode_agent_identity_scope', 'user-b:tenant-b');
    expect(composerDrafts.get(composerKey('session-a')).text).toBe('');
    expect(composerDrafts.get(a).text).toBe('A only');
  });

  it('preserves edits made while an older submission was in flight', () => {
    const key = composerKey('concurrent-edit');
    const sent = { text: 'original request', files: [] };
    composerDrafts.update(key, sent);
    composerDrafts.update(key, { text: 'new draft' });
    composerDrafts.accepted(key, sent);
    expect(composerDrafts.get(key).text).toBe('new draft');
    composerDrafts.update(key, { failed: { ...sent, id: 'request-1', error: '503' } });
    composerDrafts.restoreFailed(key);
    expect(composerDrafts.get(key).text).toBe('original request\n\nnew draft');
  });

  it('rehydrates text and same-session references without binary File data', () => {
    const key = composerKey('restored');
    localStorage.setItem('tcode-composer-v1:' + key, JSON.stringify({ text: 'restored text', files: [
      { id: 'ok', name: 'a.csv', path: 'upload/a.csv', sessionId: 'restored', mode: 'path' },
      { id: 'wrong-owner', name: 'b.csv', path: 'upload/b.csv', sessionId: 'other', mode: 'path' },
    ] }));
    expect(composerDrafts.get(key).text).toBe('restored text');
    expect(composerDrafts.get(key).files.map(file => file.id)).toEqual(['ok']);
  });

  it('stages running guidance until the user confirms it', () => {
    const key = composerKey('staged-guidance');
    composerDrafts.update(key, { text: '先检查失败原因', files: [] });

    composerDrafts.stageGuidance(
      key,
      { text: '先检查失败原因', files: [] },
      'steer',
      'stage-1',
    );

    expect(composerDrafts.get(key).text).toBe('');
    expect(composerDrafts.get(key).stagedGuidance).toMatchObject([
      { id: 'stage-1', mode: 'steer', text: '先检查失败原因' },
    ]);

    composerDrafts.restoreStagedGuidance(key, 'stage-1');
    expect(composerDrafts.get(key).text).toBe('先检查失败原因');
    expect(composerDrafts.get(key).stagedGuidance).toEqual([]);

    composerDrafts.stageGuidance(
      key,
      { text: '先检查失败原因', files: [] },
      'steer',
      'stage-2',
    );
    composerDrafts.removeStagedGuidance(key, 'stage-2');
    expect(composerDrafts.get(key).stagedGuidance).toEqual([]);
  });
});
