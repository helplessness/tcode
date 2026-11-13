import { createSignal, createEffect, onCleanup, type Setter } from 'solid-js';
import { searchWorkspaceFiles, type WorkspaceFileSearchItem } from '../../api/client';
import { type AttachedFile } from '../../stores/composer';
import { sessionStore } from '../../stores/session';
import { findFileMentionTrigger, removeFileMentionTrigger } from '../../utils/fileMentions';
import { generateId } from '../../utils/id';

/** Own debounced file search, attachment selection and menu scrolling. */
export function createFileMentions(options: {
  workspaceId: () => string;
  input: () => string;
  setInput: (value: string) => unknown;
  attachedFiles: () => AttachedFile[];
  setAttachedFiles: Setter<AttachedFile[]>;
  textarea: () => HTMLTextAreaElement | undefined;
  autoSizeTextarea: (textarea: HTMLTextAreaElement) => void;
  closeCommandMenu: () => void;
}) {
  const [fileMentionItems, setFileMentionItems] = createSignal<WorkspaceFileSearchItem[]>([]);
  const [highlightedFileMentionIndex, setHighlightedFileMentionIndex] = createSignal(0);
  const [isFileMentionMenuOpen, setIsFileMentionMenuOpen] = createSignal(false);
  const [fileMentionLoading, setFileMentionLoading] = createSignal(false);
  const [fileMentionError, setFileMentionError] = createSignal('');
  let fileMentionSearchGeneration = 0;
  let fileMentionSearchTimer: ReturnType<typeof setTimeout> | null = null;
  let fileMentionListRef: HTMLDivElement | undefined;

  const closeFileMentionMenu = () => {
    fileMentionSearchGeneration += 1;
    if (fileMentionSearchTimer !== null) {
      clearTimeout(fileMentionSearchTimer);
      fileMentionSearchTimer = null;
    }
    setIsFileMentionMenuOpen(false);
    setFileMentionLoading(false);
    setFileMentionError('');
    setFileMentionItems([]);
    setHighlightedFileMentionIndex(0);
  };

  const refreshFileMentionMenu = (value: string, caretPosition?: number) => {
    const trigger = findFileMentionTrigger(value, caretPosition);
    if (!trigger) {
      closeFileMentionMenu();
      return;
    }

    options.closeCommandMenu();
    setIsFileMentionMenuOpen(true);
    setHighlightedFileMentionIndex(0);

    const workspaceId = options.workspaceId();
    if (!workspaceId) {
      setFileMentionItems([]);
      setFileMentionLoading(false);
      setFileMentionError('请先选择一个 Workspace 会话');
      return;
    }

    const generation = ++fileMentionSearchGeneration;
    if (fileMentionSearchTimer !== null) {
      clearTimeout(fileMentionSearchTimer);
    }
    setFileMentionItems([]);
    setFileMentionLoading(true);
    setFileMentionError('');
    fileMentionSearchTimer = setTimeout(async () => {
      fileMentionSearchTimer = null;
      try {
        const items = await searchWorkspaceFiles(trigger.query, workspaceId, 40);
        if (generation !== fileMentionSearchGeneration) return;
        const attachedPaths = new Set(
          options.attachedFiles()
            .map(file => String(file.path || '').trim())
            .filter(Boolean),
        );
        setFileMentionItems(
          items.filter(item => !attachedPaths.has(item.path)).slice(0, 12),
        );
      } catch (error) {
        if (generation !== fileMentionSearchGeneration) return;
        setFileMentionItems([]);
        setFileMentionError(
          error instanceof Error ? error.message : 'Workspace 文件搜索失败',
        );
      } finally {
        if (generation === fileMentionSearchGeneration) {
          setFileMentionLoading(false);
        }
      }
    }, 140);
  };

  const applyFileMention = (item: WorkspaceFileSearchItem) => {
    const current = options.input();
    const textareaRef = options.textarea();
    const cursor = textareaRef?.selectionStart ?? current.length;
    const trigger = findFileMentionTrigger(current, cursor);
    if (!trigger) {
      closeFileMentionMenu();
      return;
    }

    const updated = removeFileMentionTrigger(current, trigger);
    const isAlreadyAttached = options.attachedFiles().some(
      file => String(file.path || '').trim() === item.path,
    );
    if (!isAlreadyAttached) {
      options.setAttachedFiles(prev => [
        ...prev,
        {
          id: generateId(),
          name: item.name,
          path: item.path,
          mode: 'path',
          source: 'mention',
        },
      ]);
    } else {
      sessionStore.setStatus(`${item.path} 已在文件引用中`);
    }

    options.setInput(updated.value);
    closeFileMentionMenu();
    queueMicrotask(() => {
      const textareaRef = options.textarea();
      if (!textareaRef) return;
      textareaRef.focus();
      textareaRef.value = updated.value;
      textareaRef.selectionStart = updated.cursor;
      textareaRef.selectionEnd = updated.cursor;
      options.autoSizeTextarea(textareaRef);
    });
  };


  createEffect(() => {
    const menuOpen = isFileMentionMenuOpen();
    const itemIndex = highlightedFileMentionIndex();
    const itemCount = fileMentionItems().length;
    if (!menuOpen || itemCount === 0) return;
    queueMicrotask(() => {
      const list = fileMentionListRef;
      const item = list?.querySelector<HTMLElement>(
        `[data-file-mention-index="${itemIndex}"]`,
      );
      if (!list || !item) return;
      const itemTop = item.offsetTop;
      const itemBottom = itemTop + item.offsetHeight;
      if (itemTop < list.scrollTop) {
        list.scrollTop = itemTop;
      } else if (itemBottom > list.scrollTop + list.clientHeight) {
        list.scrollTop = itemBottom - list.clientHeight;
      }
    });
  });

  onCleanup(closeFileMentionMenu);
  const setFileMentionListRef = (element: HTMLDivElement) => { fileMentionListRef = element; };
  return {
    fileMentionItems,
    highlightedFileMentionIndex,
    setHighlightedFileMentionIndex,
    isFileMentionMenuOpen,
    fileMentionLoading,
    fileMentionError,
    closeFileMentionMenu,
    refreshFileMentionMenu,
    applyFileMention,
    setFileMentionListRef,
  };
}
