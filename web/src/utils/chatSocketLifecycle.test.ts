import { describe, expect, it } from 'vitest';

import {
  chatPollDelayMs,
  chatSocketCloseStatusText,
  classifyChatSocketClose,
  isForegroundChatRunning,
  shouldAdoptSocketSession,
  shouldKeepRunningAfterSocketClose,
} from './chatSocketLifecycle';

describe('chat socket lifecycle', () => {
  it('releases chat input while detached work is still running', () => {
    expect(isForegroundChatRunning({
      running: true, chat_running: false, child_session_active: true,
    })).toBe(false);
    expect(isForegroundChatRunning({ running: true, chat_running: true })).toBe(true);
    // Older servers retain their conservative behavior.
    expect(isForegroundChatRunning({ running: true })).toBe(true);
    expect(isForegroundChatRunning({ workflow_background_active: true })).toBe(true);
  });
  it('does not let stale background state overwrite a terminal error', () => {
    expect(shouldKeepRunningAfterSocketClose('terminal', true, true)).toBe(false);
  });

  it('keeps explicitly detached background work running', () => {
    expect(shouldKeepRunningAfterSocketClose('background', false, false)).toBe(true);
  });

  it('uses runtime hints only before a terminal disposition is known', () => {
    expect(shouldKeepRunningAfterSocketClose('unsettled', true, false)).toBe(true);
    expect(shouldKeepRunningAfterSocketClose('unsettled', false, false)).toBe(false);
  });

  it('classifies authentication and offline closes before generic network failures', () => {
    expect(classifyChatSocketClose(4401, true, 'unsettled')).toBe('auth');
    expect(classifyChatSocketClose(1006, false, 'unsettled')).toBe('offline');
    expect(classifyChatSocketClose(1011, true, 'unsettled')).toBe('server');
    expect(classifyChatSocketClose(1000, true, 'terminal')).toBe('intentional');
    expect(chatSocketCloseStatusText('offline')).toContain('恢复联网');
  });

  it('backs polling off with bounded jitter', () => {
    expect(chatPollDelayMs(true, 0, 0.5)).toBe(3000);
    expect(chatPollDelayMs(false, 0, 0.5)).toBe(15000);
    expect(chatPollDelayMs(true, 2, 0.5)).toBe(12000);
    expect(chatPollDelayMs(false, 5, 1)).toBe(60000);
  });

  it('does not let a late socket event steal the selected session', () => {
    expect(shouldAdoptSocketSession('session-new', 'session-old', 'session-old')).toBe(false);
    expect(shouldAdoptSocketSession('session-old', 'session-old', 'session-remapped')).toBe(true);
    expect(shouldAdoptSocketSession(null, 'session-old', 'session-old')).toBe(true);
  });
});
