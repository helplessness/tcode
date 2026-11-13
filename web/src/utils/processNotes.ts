import type { ProcessNote } from '../stores/session';

export type CompactProcessNote = ProcessNote & {
  repeat_count?: number;
  source_count?: number;
};

const noteMessage = (note: ProcessNote) => String(note.message || '').trim();

const noteCompactKey = (note: ProcessNote) => {
  const phase = String(note.phase || '').trim().toLowerCase();
  const status = String(note.status || '').trim().toLowerCase();
  const toolName = String(note.tool_name || '').trim();
  const callId = String(note.call_id || '').trim();

  if (phase === 'tool_progress' && (callId || toolName)) {
    return ['progress', phase, toolName, callId].join(':');
  }

  return ['exact', phase, status, toolName, callId, noteMessage(note)].join(':');
};

export function compactProcessNotes(
  notes: ProcessNote[],
  maxItems = Number.POSITIVE_INFINITY,
): CompactProcessNote[] {
  const groups: Array<{
    key: string;
    latest: ProcessNote;
    count: number;
  }> = [];

  for (const note of Array.isArray(notes) ? notes : []) {
    if (!note || !noteMessage(note)) continue;
    const key = noteCompactKey(note);
    const previous = groups[groups.length - 1];
    if (previous && previous.key === key) {
      previous.latest = note;
      previous.count += 1;
      continue;
    }
    groups.push({ key, latest: note, count: 1 });
  }

  const visibleGroups = Number.isFinite(maxItems)
    ? groups.slice(Math.max(0, groups.length - Math.max(0, maxItems)))
    : groups;

  return visibleGroups.map((group) => ({
    ...group.latest,
    repeat_count: group.count > 1 ? group.count : undefined,
    source_count: group.count,
  }));
}
