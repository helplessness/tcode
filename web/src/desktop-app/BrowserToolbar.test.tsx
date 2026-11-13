import { renderToString } from 'solid-js/web';
import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import BrowserToolbar from './BrowserToolbar';

it('renders the compact desktop address bar and guards the sidebar from web clients', () => {
  const html = renderToString(() => <BrowserToolbar state={{ url: 'https://www.baidu.com/', title: '百度', loading: false, zoom: 1, canGoBack: false, canGoForward: false }} command={async () => {}} />);
  expect(html).toContain('aria-label="浏览器网址"');
  expect(html).toContain('aria-label="浏览器更多选项"');
  expect(html).toContain('aria-haspopup="menu"');
  expect(html).not.toContain('desktop-browser-menu');
  expect(html).toContain('https://www.baidu.com/');
  const source = readFileSync(new URL('../components/BrowserLivePanel.tsx', import.meta.url), 'utf8');
  expect(source).toContain('desktop?.browserPanel && sidePanelOpen()');
  expect(source).toContain('if (polling || document.hidden) return;');
  expect(source).toContain("document.getElementById('studio-browser-host')");
  expect(source).toContain('props.running || takenOverSession()');
  expect(source).toContain('browserLiveStore.setHidden(item.key, true)');
  expect(source).not.toContain('desktop-browser-resize');
});

it('announces and disables duplicate navigation while a page is loading', () => {
  const html = renderToString(() => <BrowserToolbar state={{ url: 'https://www.baidu.com/', title: '百度', loading: true, zoom: 1, canGoBack: true, canGoForward: true }} command={async () => {}} />);
  expect(html).toContain('aria-busy="true"');
  expect(html).toContain('正在加载页面，请稍候');
  expect(html).toContain('加载中');
  expect(html).toContain('disabled');
});
