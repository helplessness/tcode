import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';

it('keeps Wiki controls unclipped and adapts to inspector width, not window width', () => {
  const source = readFileSync(new URL('./PaperWikiWorkspace.tsx', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../interface.css', import.meta.url), 'utf8');
  expect(source).not.toContain('max-h-[20%]');
  expect(source).toContain('class="wiki-tools"');
  expect(source).toContain('aria-label="自然语言操作 Wiki"');
  expect(source).toContain('dismissDetailsOnOutsideClick(() => wikiTools)');
  expect(source.match(/paper-wiki-body flex min-h-0/g)).toHaveLength(2);
  expect(css).toContain('.paper-wiki-workspace{container-type:inline-size}');
  expect(css).toContain('@container (max-width:560px)');
  expect(css).toContain('form>input{flex-basis:180px}');
});

it('reserves desktop note editing space without changing the web editor', () => {
  const source = readFileSync(new URL('./PaperWikiWorkspace.tsx', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../interface.css', import.meta.url), 'utf8');
  expect(source).toContain('aria-controls="wiki-page-directory"');
  expect(source).toContain('hidden={desktopSurface() && (directoryHidden() || editing())}');
  expect(source).toContain('minHeight={320}');
  expect(source).toContain('aria-label="编辑 Wiki 用户笔记"');
  expect(source).toContain("height: 'min(860px, 92vh)'");
  expect(css).toContain('.wiki-notes-editor-actions{position:sticky;bottom:0');
  expect(css).toContain('@container (max-width:480px)');
});
