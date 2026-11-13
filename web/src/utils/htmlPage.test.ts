import { describe, expect, it } from 'vitest';
import { htmlPageDocument, themeHtmlCss } from './htmlPage';
describe('HTML isolated document', () => {
 it('projects model hardcoded UI colors while preserving theme tokens and geometry', () => {
  expect(themeHtmlCss('.a{color:#51435f;background:#fdf6fb;border:1px solid rgba(81,67,95,.14);border-radius:26px;accent-color:#c4b5fd}'))
   .toBe('.a{color:var(--ui-text);background:var(--ui-surface);border:1px solid var(--ui-border);border-radius:26px;accent-color:var(--ui-accent)}');
  expect(themeHtmlCss('.a{color:var(--ui-muted);background:var(--ui-surface,#fff)}')).toContain('var(--ui-surface,#fff)');
 });
 it('puts deny-by-default policy before content and only runs platform nonce script', () => {
  const doc = htmlPageDocument('<p>页面</p>', '', 'clay', 'test-nonce');
  expect(doc.indexOf('Content-Security-Policy')).toBeLessThan(doc.indexOf('<body>'));
  expect(doc).toContain("default-src 'none'");
  expect(doc).toContain("connect-src 'none'");
  expect(doc).toContain("script-src 'nonce-test-nonce'");
  expect(doc.match(/<script/g)?.length).toBe(1);
  expect(doc).toContain("if(event.key==='Escape')parent.postMessage({type:'tcode:html-close'},'*')");
  expect(doc).not.toContain('fetch(');
  expect(doc).toContain('target.textContent=');
 });
 it('inherits theme tokens and reduced-motion rules', () => {
  expect(htmlPageDocument('<p>x</p>', '', 'clay', 'n')).toContain('--ui-accent:#76518c');
  expect(htmlPageDocument('<p>x</p>', '', 'manual', 'n')).toContain('--ui-accent:#2563eb');
  expect(htmlPageDocument('<p>x</p>', '', 'manual', 'n')).toContain('prefers-reduced-motion');
 });
});
