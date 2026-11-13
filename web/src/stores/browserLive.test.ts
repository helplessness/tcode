import { describe, expect, it } from 'vitest';
import { browserLiveStore } from './browserLive';

describe('browser live store', () => {
  it('routes a browser event to its parent session and preserves viewer state', () => {
    const suffix = Date.now().toString(36);
    const parentSessionId = `parent-${suffix}`;
    const browserSessionId = `browser-${suffix}`;

    browserLiveStore.upsertFromAgentEvent({
      parent_session_id: parentSessionId,
      session_id: `agent-${suffix}`,
      browser_session_id: browserSessionId,
      workspace_id: 'workspace-1',
      status: 'ready',
      action: 'open',
      url: 'https://example.com',
      title: 'Example Domain',
      frame_seq: 2,
    }, 'fallback-parent');

    const active = browserLiveStore.activeForParent(parentSessionId);
    expect(active?.browserSessionId).toBe(browserSessionId);
    expect(active?.title).toBe('Example Domain');
    expect(active?.frameSeq).toBe(2);

    browserLiveStore.setHidden(active!.key, true);
    expect(browserLiveStore.activeForParent(parentSessionId)?.hidden).toBe(true);

    browserLiveStore.upsert({
      parentSessionId,
      browserSessionId,
      status: 'running',
      frameSeq: 3,
    });
    expect(browserLiveStore.activeForParent(parentSessionId)?.hidden).toBe(true);
    expect(browserLiveStore.activeForParent(parentSessionId)?.frameSeq).toBe(3);

    const childBrowserId = `${browserSessionId}-child`;
    browserLiveStore.upsert({ parentSessionId, browserSessionId: childBrowserId, status: 'ready' });
    const child = browserLiveStore.sessions().find((item) => item.browserSessionId === childBrowserId)!;
    expect(child.hidden).toBe(true);
    browserLiveStore.setHidden(child.key, false);
    expect(browserLiveStore.sessions().filter((item) => item.parentSessionId === parentSessionId)
      .every((item) => !item.hidden)).toBe(true);
  });

  it('clears sensitive preview metadata when a browser session ends', () => {
    const suffix = Date.now().toString(36);
    const parentSessionId = `terminal-parent-${suffix}`;
    const browserSessionId = `terminal-browser-${suffix}`;

    browserLiveStore.upsert({
      parentSessionId,
      browserSessionId,
      status: 'ready',
      url: 'https://private.example/account',
      title: 'Private account',
      frameSeq: 9,
      error: 'page details',
    });
    browserLiveStore.upsert({
      parentSessionId,
      browserSessionId,
      status: 'closed',
      url: 'https://private.example/account',
      title: 'Private account',
      frameSeq: 9,
      error: 'page details',
    });

    const active = browserLiveStore.activeForParent(parentSessionId);
    expect(active?.status).toBe('closed');
    expect(active?.url).toBe('');
    expect(active?.title).toBe('');
    expect(active?.frameSeq).toBe(0);
    expect(active?.error).toBe('');
  });
});
