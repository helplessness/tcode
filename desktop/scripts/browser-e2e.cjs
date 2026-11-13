const assert = require('node:assert/strict');
const { app, BrowserWindow, WebContentsView, session } = require('electron');
const { DesktopBrowser } = require('../browser.cjs');

app.whenReady().then(async () => {
  const deadline = setTimeout(() => { console.error('FAIL: browser integration exceeded 90 seconds'); app.exit(1); }, 90000);
  const browser = new DesktopBrowser({ BrowserWindow, WebContentsView, session });
  let call = 0;
  const execute = args => browser.execute({ owner: 'browser-e2e', call_id: String(++call), args: { session: 'e2e', ...args } });
  try {
    await browser.start();
    await execute({ action: 'open', url: 'https://www.baidu.com/' });
    const page = [...browser.pages.values()][0];
    await browser.control('e2e', 'takeover');
    const host = new BrowserWindow({ show: false });
    await host.loadURL('data:text/html,<main>Browser host</main>');
    await browser.panel('e2e', host, { action: 'attach', bounds: { x: 0, y: 0, width: 800, height: 600 } });
    assert.ok(host.contentView.children.includes(page.view));
    host.webContents.setZoomFactor(1.25);
    await browser.panel('e2e', host, { action: 'attach', bounds: { x: 40, y: 80, width: 400, height: 300 } });
    assert.deepEqual(page.view.getBounds(), { x: 50, y: 100, width: 500, height: 375 });
    await browser.panel('e2e', host, { action: 'attach', bounds: { x: 0, y: 0, width: 800, height: 600 } });
    const [hostWidth, hostHeight] = host.getContentSize();
    assert.deepEqual(page.view.getBounds(), { x: 0, y: 0, width: hostWidth, height: hostHeight });
    const fitted = await browser.panel('e2e', host, { action: 'fit' });
    assert.ok(fitted.zoom >= .25 && fitted.zoom <= 1);
    assert.equal(fitted.fitWidth, true);
    assert.ok(fitted.history.some(item => item.url.includes('baidu.com')));
    assert.equal(fitted.downloadsEnabled, false);
    await browser.panel('e2e', host, { action: 'downloads-enabled', enabled: true });
    assert.equal(page.downloadsEnabled, true);
    await browser.panel('e2e', host, { action: 'find', text: '百度' });
    await browser.panel('e2e', host, { action: 'find', text: '' });
    await browser.panel('e2e', host, { action: 'zoom', factor: 1.25 });
    assert.equal(page.contents.getZoomFactor(), 1.25);
    const point = await browser.evaluate(page, `(()=>{const r=document.querySelector('#kw').getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})()`);
    await browser.input('e2e', { action: 'click', ...point });
    await browser.input('e2e', { action: 'text', text: 'python' });
    assert.equal(await browser.evaluate(page, `document.querySelector('#kw').value`), 'python');
    const search = await browser.input('e2e', { action: 'key', key: 'Enter' });
    assert.ok(search.screenshot.length > 1000);
    await browser.control('e2e', 'release');
    assert.ok(!host.contentView.children.includes(page.view));
    host.destroy();
    for (const pn of [0, 10, 20]) {
      const result = await execute({ action: 'navigate', url: `https://www.baidu.com/s?wd=python&pn=${pn}` });
      assert.ok(result.url.includes('baidu.com/s'));
      const titles = await browser.evaluate(page, `Array.from(document.querySelectorAll('h3')).map(el=>el.innerText).filter(Boolean)`);
      assert.ok(titles.length > 0, 'search results missing');
      await execute({ action: 'scroll', delta_y: 600 });
      console.log(JSON.stringify({ page: pn / 10 + 1, titles }));
    }
    await execute({ action: 'close' });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(browser.pages.size, 0);
    console.log('PASS: scaled bounds, clipping limits, fit width, history, download opt-in, find, native input, three result pages, cleanup');
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally { clearTimeout(deadline); await browser.stop(); app.exit(process.exitCode || 0); }
});
