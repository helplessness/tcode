import { createEffect, createMemo, createSignal, onCleanup, onMount } from 'solid-js';

import { closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
  redo,
  undo,
} from '@codemirror/commands';
import { bracketMatching, defaultHighlightStyle, foldGutter, indentOnInput, syntaxHighlighting } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { openSearchPanel, searchKeymap } from '@codemirror/search';
import { Compartment, EditorSelection, EditorState } from '@codemirror/state';
import {
  drawSelection,
  dropCursor,
  EditorView,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from '@codemirror/view';

import MarkdownContent from './MarkdownContent';

type Props = {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  minHeight?: number;
  renderKey?: string;
};

type WrapAction = {
  label: string;
  title: string;
  before: string;
  after?: string;
  placeholder?: string;
};

const wrapActions: WrapAction[] = [
  { label: 'B', title: '粗体', before: '**', after: '**', placeholder: '粗体文字' },
  { label: 'I', title: '斜体', before: '*', after: '*', placeholder: '斜体文字' },
  { label: '`<>`', title: '行内代码', before: '`', after: '`', placeholder: 'code' },
  { label: '链接', title: '插入链接', before: '[', after: '](https://)', placeholder: '链接文字' },
];

export default function MarkdownEditor(props: Props) {
  let host!: HTMLDivElement;
  let editor: EditorView | undefined;
  const editability = new Compartment();
  let previewTimer: ReturnType<typeof setTimeout> | undefined;
  const [splitPreview, setSplitPreview] = createSignal(true);
  const [previewContent, setPreviewContent] = createSignal(props.value || '');
  const minHeight = () => Math.max(280, Number(props.minHeight || 480));
  const lineCount = createMemo(() => (props.value ? props.value.split(/\r?\n/).length : 1));
  const characterCount = createMemo(() => [...(props.value || '')].length);

  const focus = () => editor?.focus();

  const replaceSelection = (action: WrapAction) => {
    if (!editor || props.disabled) return;
    const range = editor.state.selection.main;
    const selected = editor.state.sliceDoc(range.from, range.to);
    const body = selected || action.placeholder || '';
    const after = action.after || '';
    const inserted = `${action.before}${body}${after}`;
    const selectionStart = range.from + action.before.length;
    editor.dispatch({
      changes: { from: range.from, to: range.to, insert: inserted },
      selection: selected
        ? { anchor: range.from + inserted.length }
        : { anchor: selectionStart, head: selectionStart + body.length },
      scrollIntoView: true,
    });
    focus();
  };

  const prefixLines = (prefix: string) => {
    if (!editor || props.disabled) return;
    const range = editor.state.selection.main;
    const startLine = editor.state.doc.lineAt(range.from);
    const endLine = editor.state.doc.lineAt(range.to);
    const changes = [];
    for (let number = startLine.number; number <= endLine.number; number += 1) {
      changes.push({ from: editor.state.doc.line(number).from, insert: prefix });
    }
    editor.dispatch({ changes, scrollIntoView: true });
    focus();
  };

  const insertBlock = (content: string) => {
    if (!editor || props.disabled) return;
    const range = editor.state.selection.main;
    const line = editor.state.doc.lineAt(range.from);
    const prefix = line.text.trim() ? '\n\n' : '';
    editor.dispatch({
      changes: { from: range.from, to: range.to, insert: `${prefix}${content}` },
      selection: EditorSelection.cursor(range.from + prefix.length + content.length),
      scrollIntoView: true,
    });
    focus();
  };

  onMount(() => {
    const state = EditorState.create({
      doc: props.value || '',
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightSpecialChars(),
        history(),
        foldGutter(),
        drawSelection(),
        dropCursor(),
        EditorState.allowMultipleSelections.of(true),
        indentOnInput(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        bracketMatching(),
        closeBrackets(),
        markdown(),
        keymap.of([
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...searchKeymap,
          ...historyKeymap,
          indentWithTab,
        ]),
        EditorView.lineWrapping,
        editability.of([EditorState.readOnly.of(Boolean(props.disabled)), EditorView.editable.of(!props.disabled)]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) props.onChange(update.state.doc.toString());
        }),
        EditorView.theme({
          '&': {
            height: `${minHeight()}px`,
            color: '#353b2e',
            backgroundColor: '#fffdf7',
            fontSize: '13px',
          },
          '&.cm-focused': { outline: 'none' },
          '.cm-scroller': {
            overflow: 'auto',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
            lineHeight: '1.65',
          },
          '.cm-gutters': {
            backgroundColor: '#f5f1e8',
            color: '#737367',
            borderRight: '1px solid #dcd6c8',
          },
          '.cm-activeLineGutter': { backgroundColor: '#e8ecdc', color: '#586641' },
          '.cm-activeLine': { backgroundColor: 'rgba(56, 189, 248, 0.035)' },
          '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
            backgroundColor: 'rgba(56, 189, 248, 0.22) !important',
          },
          '.cm-cursor': { borderLeftColor: '#586641' },
          '.cm-foldPlaceholder': {
            backgroundColor: '#ede9de',
            borderColor: '#dcd6c8',
            color: '#737367',
          },
          '.cm-panels': { backgroundColor: '#f5f1e8', color: '#353b2e' },
          '.cm-searchMatch': { backgroundColor: 'rgba(241, 160, 43, 0.28)' },
          '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: 'rgba(56, 189, 248, 0.3)' },
        }),
      ],
    });
    editor = new EditorView({ state, parent: host });
  });

  createEffect(() => {
    const next = props.value || '';
    const disabled = Boolean(props.disabled);
    if (editor) editor.dispatch({ effects: editability.reconfigure([EditorState.readOnly.of(disabled), EditorView.editable.of(!disabled)]) });
    if (editor && editor.state.doc.toString() !== next) {
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: next } });
    }
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => setPreviewContent(next), 280);
  });

  onCleanup(() => {
    if (previewTimer) clearTimeout(previewTimer);
    editor?.destroy();
    editor = undefined;
  });

  return (
    <div class="markdown-editor overflow-hidden rounded-xl border border-[#303539] bg-[#0b1014] focus-within:border-[#7bd0ff]/50">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#252c31] bg-[#11171b] px-2 py-1.5">
        <div class="flex flex-wrap items-center gap-1">
          <ForActions actions={wrapActions} onAction={replaceSelection} />
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="二级标题" onClick={() => prefixLines('## ')}>H2</button>
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="无序列表" onClick={() => prefixLines('- ')}>• 列表</button>
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="引用" onClick={() => prefixLines('> ')}>❯ 引用</button>
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="代码块" onClick={() => insertBlock('```\ncode\n```')}>代码块</button>
          <span class="mx-1 h-4 w-px bg-[#30383e]" />
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="撤销" onClick={() => { if (editor) undo(editor); }}>撤销</button>
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="重做" onClick={() => { if (editor) redo(editor); }}>重做</button>
          <button class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e]" title="查找与替换" onClick={() => { if (editor) openSearchPanel(editor); }}>查找</button>
        </div>
        <div class="flex items-center gap-3 text-[9px] text-[#596873]">
          <span>{lineCount()} 行 · {characterCount()} 字符</span>
          <button
            class={`rounded border px-2 py-1 ${splitPreview() ? 'border-[#38bdf8]/30 text-[#8ed5ff]' : 'border-[#30383e] text-[#8b99a4]'}`}
            onClick={() => setSplitPreview((value) => !value)}
          >
            {splitPreview() ? '关闭预览' : '分栏预览'}
          </button>
        </div>
      </div>
      <div class={splitPreview() ? 'grid min-w-0 lg:grid-cols-2' : 'block'}>
        <div ref={host} class="min-w-0" />
        <ShowPreview
          visible={splitPreview()}
          content={previewContent()}
          renderKey={`${props.renderKey || 'markdown-editor'}:${previewContent().length}`}
          minHeight={minHeight()}
        />
      </div>
    </div>
  );
}

function ForActions(props: { actions: WrapAction[]; onAction: (action: WrapAction) => void }) {
  return (
    <>
      {props.actions.map((action) => (
        <button
          class="rounded px-2 py-1 text-[10px] text-[#9faeb7] hover:bg-[#20282e] hover:text-[#d8dee4]"
          title={action.title}
          onClick={() => props.onAction(action)}
        >
          {action.label}
        </button>
      ))}
    </>
  );
}

function ShowPreview(props: { visible: boolean; content: string; renderKey: string; minHeight: number }) {
  return props.visible ? (
    <div
      class="min-w-0 overflow-auto border-t border-[#252c31] bg-[#11171b] p-4 lg:border-l lg:border-t-0"
      style={{ height: `${props.minHeight}px` }}
    >
      <div class="mb-3 text-[9px] font-semibold uppercase tracking-[0.16em] text-[#596873]">Markdown Preview</div>
      <MarkdownContent content={props.content || '*暂无内容*'} role="assistant" renderKey={props.renderKey} />
    </div>
  ) : null;
}
