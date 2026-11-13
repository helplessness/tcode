import { describe, expect, it } from 'vitest';

import type { ProcessNote } from '../stores/session';
import { compactProcessNotes } from './processNotes';

const note = (
  id: string,
  phase: string,
  message: string,
  extra: Partial<ProcessNote> = {},
): ProcessNote => ({
  id,
  phase,
  message,
  timestamp: Number(id.replace(/\D/g, '') || 0),
  ...extra,
});

describe('process note compaction', () => {
  it('keeps the latest heartbeat for the same running tool', () => {
    const notes = compactProcessNotes([
      note('n1', 'tool_progress', 'bash 还在跑，已等待 12 秒', {
        tool_name: 'bash',
        call_id: 'call-1',
      }),
      note('n2', 'tool_progress', 'bash 还在跑，已等待 24 秒', {
        tool_name: 'bash',
        call_id: 'call-1',
      }),
      note('n3', 'tool_result', '命令执行完成', {
        tool_name: 'bash',
        call_id: 'call-1',
      }),
    ]);

    expect(notes.map((item) => item.id)).toEqual(['n2', 'n3']);
    expect(notes[0].repeat_count).toBe(2);
  });

  it('preserves distinct process narration while trimming old groups', () => {
    const notes = compactProcessNotes([
      note('n1', 'turn_start', '我先对齐上下文'),
      note('n2', 'preparing', '我在扫可用技能'),
      note('n3', 'context_check', '先看看上下文'),
      note('n4', 'model_wait', '我来判断下一步'),
    ], 3);

    expect(notes.map((item) => item.id)).toEqual(['n2', 'n3', 'n4']);
    expect(notes.every((item) => item.repeat_count === undefined)).toBe(true);
  });

  it('does not trim by default', () => {
    const notes = compactProcessNotes([
      note('n1', 'turn_start', '我先对齐上下文'),
      note('n2', 'preparing', '我在扫可用技能'),
      note('n3', 'context_check', '先看看上下文'),
      note('n4', 'model_wait', '我来判断下一步'),
    ]);

    expect(notes.map((item) => item.id)).toEqual(['n1', 'n2', 'n3', 'n4']);
  });

  it('keeps the newest structured progress values for one running tool', () => {
    const notes = compactProcessNotes([
      note('n1', 'tool_progress', '已读取 1/2 篇', {
        tool_name: 'skill_run',
        call_id: 'call-progress',
        current: 1,
        total: 2,
        unit: 'paper',
      }),
      note('n2', 'tool_progress', '已读取 2/2 篇', {
        tool_name: 'skill_run',
        call_id: 'call-progress',
        current: 2,
        total: 2,
        unit: 'paper',
      }),
    ]);

    expect(notes).toHaveLength(1);
    expect(notes[0].current).toBe(2);
    expect(notes[0].total).toBe(2);
    expect(notes[0].repeat_count).toBe(2);
  });
});
