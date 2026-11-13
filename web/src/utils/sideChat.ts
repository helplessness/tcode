import type { Message } from '../stores/session';
import { composerIdentity } from '../stores/composer';

export const sideChatSelectionKey = (parentId: string) => `tcode-side-chat-v1:${JSON.stringify([composerIdentity(), parentId])}`;
export const sideChatDraftKey = (parentId: string, sideId: string) => JSON.stringify([composerIdentity(), sideId || `side-new:${parentId}`]);

export function readSideChatSelection(parentId: string): string | null {
  try { return localStorage.getItem(sideChatSelectionKey(parentId)); } catch { return null; }
}
export function saveSideChatSelection(parentId: string, sideId: string): void {
  try { localStorage.setItem(sideChatSelectionKey(parentId), sideId); } catch { /* Keep working when storage is unavailable. */ }
}

/** Reconcile live public text without putting side messages into the main store. */
export function applySideChatText(messages: Message[], event: Record<string, any>): Message[] {
  if (event.kind === 'model_output.superseded') return messages.filter(m => m.id !== event.message_id);
  if (event.kind !== 'model_output' || !event.message_id) return messages;
  const index = messages.findIndex(m => m.id === event.message_id);
  const previous = index >= 0 ? messages[index] : undefined;
  const content = typeof event.content === 'string' ? event.content : (previous?.content || '') + String(event.delta || '');
  const message: Message = {
    ...previous, id: event.message_id, role: 'assistant', content,
    presentation: typeof event.presentation === 'string' ? event.presentation : previous?.presentation,
  };
  return index < 0 ? [...messages, message] : messages.map((m, i) => i === index ? message : m);
}
