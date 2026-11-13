import { sanitizeMessageVerification, type Message } from '../stores/session';
import { sanitizeMessageContentBlocks } from './contentBlocks';

export function shouldApplyHydratedSession(
  activeSessionId: string | null | undefined,
  hydratedSessionId: string | null | undefined,
): boolean {
  const hydrated = String(hydratedSessionId || '').trim();
  if (!hydrated) return false;
  const active = String(activeSessionId || '').trim();
  return !active || active === hydrated;
}

export function shouldApplySessionSelection(
  activeSessionId: string | null | undefined,
  hydratedSessionId: string | null | undefined,
  requestRevision: number,
  latestRequestRevision: number,
): boolean {
  return requestRevision === latestRequestRevision
    && shouldApplyHydratedSession(activeSessionId, hydratedSessionId);
}

const messageSnapshotEqual = (left: Message, right: Message) => (
  String(left.id || '') === String(right.id || '') &&
  String(left.role || '') === String(right.role || '') &&
  String(left.content || '') === String(right.content || '') &&
  String(left.presentation || '') === String(right.presentation || '') &&
  JSON.stringify(left.content_blocks || []) === JSON.stringify(right.content_blocks || []) &&
  JSON.stringify(left.tool_calls || []) === JSON.stringify(right.tool_calls || []) &&
  JSON.stringify(left.process_notes || []) === JSON.stringify(right.process_notes || []) &&
  JSON.stringify(left.usage || null) === JSON.stringify(right.usage || null) &&
  JSON.stringify(left.research_grounding || null) === JSON.stringify(right.research_grounding || null) &&
  JSON.stringify(left.verification || null) === JSON.stringify(right.verification || null) &&
  JSON.stringify(left.metadata || null) === JSON.stringify(right.metadata || null)
);

const hasProcessNotes = (message: any) =>
  Array.isArray(message?.process_notes) && message.process_notes.length > 0;

const processNotesEqual = (left: any, right: any) =>
  JSON.stringify(left?.process_notes || []) === JSON.stringify(right?.process_notes || []);

const messageSourceRuntime = (message: any): string =>
  String(message?.metadata?.source_runtime_id || '').trim();

const canTransferProcessNotes = (source: any, target: any): boolean => {
  const sourceRuntime = messageSourceRuntime(source);
  const targetRuntime = messageSourceRuntime(target);
  return !sourceRuntime || !targetRuntime || sourceRuntime === targetRuntime;
};

const preserveSourceRuntime = (upstream: any, local: any): any => {
  const upstreamRuntime = messageSourceRuntime(upstream);
  const localRuntime = messageSourceRuntime(local);
  if (upstreamRuntime || !localRuntime) return upstream;
  return {
    ...upstream,
    metadata: {
      ...(local?.metadata || {}),
      ...(upstream?.metadata || {}),
      source_runtime_id: localRuntime,
    },
  };
};

const mergeProcessNotes = (baseNotes: any[] | undefined, localNotes: any[] | undefined) => {
  const mergedNotes = Array.isArray(baseNotes) ? [...baseNotes] : [];
  const seen = new Set(mergedNotes.map((note) => String(note?.id || JSON.stringify(note))));
  for (const note of Array.isArray(localNotes) ? localNotes : []) {
    const key = String(note?.id || JSON.stringify(note));
    if (seen.has(key)) continue;
    seen.add(key);
    mergedNotes.push(note);
  }
  return mergedNotes.length > 0 ? mergedNotes : undefined;
};

export const toVisibleMessages = (messages: any[]): Message[] =>
  (Array.isArray(messages) ? messages : [])
    .filter((message) => {
      const role = String(message?.role || '').trim().toLowerCase();
      return role === 'user' || role === 'assistant' || role === 'system';
    })
    .map((message: any) => ({
      id: String(message.id || ''),
      created_at: Number.isFinite(Number(message.created_at))
        ? (Number(message.created_at) < 1_000_000_000_000 ? Number(message.created_at) * 1000 : Number(message.created_at))
        : undefined,
      role: (message.role || 'assistant') as 'user' | 'assistant' | 'system',
      content: String(message.content || ''),
      presentation: typeof message.presentation === 'string' ? message.presentation : undefined,
      content_blocks: sanitizeMessageContentBlocks(message.content_blocks).length > 0
        ? sanitizeMessageContentBlocks(message.content_blocks)
        : undefined,
      tool_calls: Array.isArray(message.tool_calls) ? message.tool_calls : undefined,
      process_notes: Array.isArray(message.process_notes) ? message.process_notes : undefined,
      usage: message.usage || undefined,
      research_grounding:
        message?.research_grounding && typeof message.research_grounding === 'object'
          && !Array.isArray(message.research_grounding)
          ? message.research_grounding
          : undefined,
      verification: sanitizeMessageVerification(message.verification),
      metadata:
        message?.metadata && typeof message.metadata === 'object' && !Array.isArray(message.metadata)
          ? message.metadata
          : undefined,
    }));

/**
 * Merge local synthetic messages with upstream server messages.
 * When server snapshot is shorter, prefer local tool/process payloads so UI-only
 * continuation cards and recent streaming state do not disappear during refresh.
 */
export const mergeWithLocalSyntheticMessages = (
  current: Message[],
  next: Message[],
): Message[] => {
  const merged = Array.isArray(next) ? [...next] : [];
  const currentList = Array.isArray(current) ? current : [];
  const currentById = new Map(
    currentList.map((item: any) => [String(item?.id || ''), item]),
  );
  // Recovered messages keep their timeline position, even if the server
  // snapshot omits an old async receipt while already containing newer turns.
  const insertLocalMessage = (message: Message) => {
    const position = currentList.indexOf(message);
    for (let index = position + 1; index < currentList.length; index += 1) {
      const id = currentList[index].id;
      const anchor = id ? merged.findIndex(item => item.id === id) : -1;
      if (anchor >= 0) { merged.splice(anchor, 0, message); return; }
    }
    for (let index = position - 1; index >= 0; index -= 1) {
      const id = currentList[index].id;
      const anchor = id ? merged.findIndex(item => item.id === id) : -1;
      if (anchor >= 0) { merged.splice(anchor + 1, 0, message); return; }
    }
    merged.push(message);
  };
  const hasSameSemanticMessage = (candidate: any) =>
    merged.some((message: any) => {
      const sameRole = String(message?.role || '') === String(candidate?.role || '');
      if (!sameRole) return false;

      const contentA = String(message?.content || '').trim();
      const contentB = String(candidate?.content || '').trim();
      if (!contentA || !contentB || contentA !== contentB) return false;

      const simpleA = !message?.tool_calls && !message?.process_notes && !message?.content_blocks && !message?.usage;
      const simpleB = !candidate?.tool_calls && !candidate?.process_notes && !candidate?.content_blocks && !candidate?.usage;
      return simpleA && simpleB;
    });
  const shouldPreserveRecentLocalMessage = (candidate: any) => {
    const role = String(candidate?.role || '').trim().toLowerCase();
    if (role === 'user') {
      return Boolean(String(candidate?.content || '').trim());
    }
    if (role !== 'assistant') {
      return false;
    }

    const content = String(candidate?.content || '');
    const hasToolCalls = Array.isArray(candidate?.tool_calls) && candidate.tool_calls.length > 0;
    const hasProcessNotes = Array.isArray(candidate?.process_notes) && candidate.process_notes.length > 0;
    const hasContentBlocks = Array.isArray(candidate?.content_blocks) && candidate.content_blocks.length > 0;
    return !content.trim() || hasToolCalls || hasProcessNotes || hasContentBlocks;
  };

  if (merged.length < currentList.length * 0.8 && currentList.length > 0) {
    for (let index = 0; index < merged.length; index += 1) {
      const nextItem = merged[index];
      const nextId = String(nextItem?.id || '');
      const currentItem = currentById.get(nextId);
      if (!currentItem) continue;

      const mergedItem = { ...preserveSourceRuntime(nextItem, currentItem) };
      if (!nextItem?.tool_calls && currentItem?.tool_calls) {
        mergedItem.tool_calls = currentItem.tool_calls;
      }
      if (!nextItem?.process_notes && currentItem?.process_notes) {
        mergedItem.process_notes = currentItem.process_notes;
      }
      if (!nextItem?.content_blocks && currentItem?.content_blocks) {
        mergedItem.content_blocks = currentItem.content_blocks;
      }
      if (!nextItem?.content && currentItem?.content) {
        mergedItem.content = currentItem.content;
      }
      merged[index] = mergedItem;
    }

    for (const [itemId, currentItem] of currentById) {
      const existsInMerged =
        merged.some((message: any) => String(message?.id || '') === itemId) ||
        hasSameSemanticMessage(currentItem);
      if (!existsInMerged) {
        const localIdx = currentList.findIndex((message: any) => String(message?.id || '') === itemId);
        if (localIdx >= currentList.length - 3 && localIdx >= 0) {
          insertLocalMessage(currentItem);
        }
      }
    }
  } else {
    for (let index = 0; index < merged.length; index += 1) {
      const nextItem = merged[index];
      const nextId = String(nextItem?.id || '');
      const currentItem = currentById.get(nextId);
      if (!currentItem) continue;

      const nextWithRuntime = preserveSourceRuntime(nextItem, currentItem);

      const nextContent = String(nextWithRuntime?.content || '');
      const currentContent = String(currentItem?.content || '');
      const sameRole = String(nextWithRuntime?.role || '') === String(currentItem?.role || '');
      const shouldPreserveLocalPayload =
        sameRole &&
        (
          (!nextWithRuntime?.tool_calls && currentItem?.tool_calls) ||
          (!nextWithRuntime?.process_notes && currentItem?.process_notes) ||
          (!nextWithRuntime?.content_blocks && currentItem?.content_blocks) ||
          (!nextWithRuntime?.usage && currentItem?.usage) ||
          (!nextContent && currentContent)
        );
      if (shouldPreserveLocalPayload) {
        merged[index] = {
          ...nextWithRuntime,
          content: nextContent || currentContent,
          tool_calls: nextWithRuntime?.tool_calls || currentItem?.tool_calls,
          process_notes: nextWithRuntime?.process_notes || currentItem?.process_notes,
          content_blocks: nextWithRuntime?.content_blocks || currentItem?.content_blocks,
          usage: nextWithRuntime?.usage || currentItem?.usage,
        };
      } else if (nextWithRuntime !== nextItem) {
        merged[index] = nextWithRuntime;
      }
    }
  }

  if (currentList.length > merged.length) {
    const tailStart = Math.max(0, currentList.length - 4);
    for (let localIdx = tailStart; localIdx < currentList.length; localIdx += 1) {
      const currentItem = currentList[localIdx];
      if (!shouldPreserveRecentLocalMessage(currentItem)) continue;

      const itemId = String(currentItem?.id || '');
      const existsInMerged =
        merged.some((message: any) => String(message?.id || '') === itemId) ||
        hasSameSemanticMessage(currentItem);
      if (!existsInMerged) {
        insertLocalMessage(currentItem);
      }
    }
  }

  const processTailStart = Math.max(0, currentList.length - 6);
  for (let localIdx = processTailStart; localIdx < currentList.length; localIdx += 1) {
    const currentItem = currentList[localIdx];
    const role = String(currentItem?.role || '').trim().toLowerCase();
    if (role !== 'assistant' || !hasProcessNotes(currentItem)) continue;

    const itemId = String(currentItem?.id || '');
    const exactIndex = itemId
      ? merged.findIndex((message: any) => String(message?.id || '') === itemId)
      : -1;
    if (exactIndex >= 0) {
      merged[exactIndex] = {
        ...merged[exactIndex],
        process_notes: mergeProcessNotes(merged[exactIndex]?.process_notes, currentItem.process_notes),
      };
      continue;
    }

    const alreadyHasSameNotes = merged.some((message: any) => processNotesEqual(message, currentItem));
    if (alreadyHasSameNotes) continue;

    let targetIndex = -1;
    const samePosition = merged[localIdx];
    if (String(samePosition?.role || '').trim().toLowerCase() === 'assistant') {
      targetIndex = localIdx;
    } else {
      const scanStart = Math.min(localIdx, merged.length - 1);
      for (let index = scanStart; index >= 0; index -= 1) {
        const candidate = merged[index];
        if (String(candidate?.role || '').trim().toLowerCase() === 'user') break;
        if (String(candidate?.role || '').trim().toLowerCase() === 'assistant') {
          targetIndex = index;
          break;
        }
      }
    }

    if (targetIndex >= 0 && canTransferProcessNotes(currentItem, merged[targetIndex])) {
      merged[targetIndex] = {
        ...merged[targetIndex],
        process_notes: mergeProcessNotes(merged[targetIndex]?.process_notes, currentItem.process_notes),
      };
    } else if (shouldPreserveRecentLocalMessage(currentItem)) {
      insertLocalMessage(currentItem);
    }
  }

  const seenIds = new Set(merged.map((item: any) => String(item?.id || '')));
  const seenContents = new Set(merged.map((item: any) => String(item?.content || '')));

  for (const item of currentList) {
    const content = String(item?.content || '');
    if (!content.includes('[Symphony:')) continue;

    const itemId = String(item?.id || '');
    if ((itemId && seenIds.has(itemId)) || seenContents.has(content)) continue;
    insertLocalMessage(item);
  }

  return merged;
};

export const stabilizeMessages = (current: Message[], next: Message[]): Message[] => {
  const currentList = Array.isArray(current) ? current : [];
  const nextList = Array.isArray(next) ? next : [];
  const sameSnapshot =
    nextList.length === currentList.length &&
    nextList.every((message, index) => {
      const currentMessage = currentList[index];
      return currentMessage ? messageSnapshotEqual(message, currentMessage) : false;
    });
  if (sameSnapshot) return currentList;

  const currentById = new Map(
    currentList.map((message) => [String(message.id || ''), message]),
  );
  return nextList.map((message) => {
    const previous = currentById.get(String(message.id || ''));
    if (!previous) return message;
    return messageSnapshotEqual(message, previous) ? previous : message;
  });
};

export const mergeAndStabilizeVisibleMessages = (
  current: Message[],
  upstream: any[],
): Message[] => stabilizeMessages(
  current,
  mergeWithLocalSyntheticMessages(current, toVisibleMessages(upstream)),
);
