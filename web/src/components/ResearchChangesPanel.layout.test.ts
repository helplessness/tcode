import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';

it('keeps comparison first and maps on demand without nested list scrolling',()=>{
  const source=readFileSync(new URL('./ResearchChangesPanel.tsx',import.meta.url),'utf8');
  const css=readFileSync(new URL('./ResearchChangesPanel.css',import.meta.url),'utf8');
  expect(source).toContain('基线版本 · 比较前');
  expect(source).toContain('按时间排序');
  expect(source).toContain('展开前后内容');
  expect(source).toContain('<Show when={mapTarget()}>');
  expect(source).toContain('比较前地图');
  expect(source).toContain('比较后地图');
  expect(source).toContain('收起地图');
  expect(source).toContain("base()!==head()");
  expect(css).toContain('.research-changes-list { overflow:visible; }');
  expect(css).not.toContain('minmax(260px,320px)');
});

it('preserves map semantics and vertical scroll chaining in the white workspace',()=>{
  const source=readFileSync(new URL('./ResearchMap.tsx',import.meta.url),'utf8');
  const css=readFileSync(new URL('./ResearchMap.css',import.meta.url),'utf8');
  expect(source).toContain('const id = createUniqueId()');
  expect(source).toContain('marker-end={analysis()?');
  expect(source).toContain('研究对象类型');
  expect(source).not.toContain('切换深色画布');
  expect(css).toContain('overscroll-behavior:contain auto');
  expect(css).toContain('--map-canvas:#fff');
  expect(css).not.toContain('--map-canvas:#020617');
  expect(css).toContain('24px 24px');
  expect(css).toContain('[data-kind=conflict]');
});
