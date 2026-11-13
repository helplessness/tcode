export type FileMentionTrigger = {
  query: string;
  start: number;
  end: number;
};

/** Find an @file token immediately before the caret. */
export function findFileMentionTrigger(
  value: string,
  caretPosition?: number,
): FileMentionTrigger | null {
  const text = String(value || '');
  const cursor = Math.max(0, Math.min(caretPosition ?? text.length, text.length));
  const beforeCursor = text.slice(0, cursor);
  const match = beforeCursor.match(/(^|\s)@([^\s@]*)$/);
  if (!match) return null;
  const tokenLength = 1 + String(match[2] || '').length;
  return {
    query: String(match[2] || ''),
    start: cursor - tokenLength,
    end: cursor,
  };
}

export function removeFileMentionTrigger(
  value: string,
  trigger: FileMentionTrigger,
): { value: string; cursor: number } {
  const text = String(value || '');
  const start = Math.max(0, Math.min(trigger.start, text.length));
  const end = Math.max(start, Math.min(trigger.end, text.length));
  return {
    value: `${text.slice(0, start)}${text.slice(end)}`,
    cursor: start,
  };
}
