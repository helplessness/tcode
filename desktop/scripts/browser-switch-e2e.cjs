const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { app, BrowserWindow, WebContentsView, session } = require('electron');
const { DesktopBrowser } = require('../browser.cjs');

app.whenReady().then(async () => {
  const browser = new DesktopBrowser({ BrowserWindow, WebContentsView, session });
  const host = new BrowserWindow({ show: true, width: 1000, height: 700 });
  const deadline = setTimeout(() => { console.error('FAIL: switching exceeded 30 seconds'); app.exit(1); }, 30000);
  try {
    await host.loadURL('data:text/html,<h1>Browser switch test</h1>');
    await browser.openBlank('manual-browser-switch');
    const manual = [...browser.pages.values()][0];
    await browser.evaluate(manual, `document.title='Manual page';document.body.innerHTML='<input id="note" value="keep me">'`);
    const key = crypto.createHash('sha256').update('test-owner:task-switch').digest('hex');
    const task = await browser.create(key, { session: 'task-switch' });
    await task.contents.loadURL('about:blank');
    await browser.evaluate(task, `document.title='Agent page';document.body.innerHTML='<h1>Agent task</h1>'`);
    const attach = id => browser.panel(id, host, { action: 'attach', bounds: { x: 400, y: 0, width: 600, height: 600 } });
    await browser.control(manual.session, 'takeover'); await attach(manual.session);
    await browser.panel(manual.session, host, { action: 'detach' });
    assert.equal(host.contentView.children.includes(manual.view), false);
    assert.equal(manual.takenOver, true);
    await browser.panel(manual.session, host, { action: 'attach', bounds: { x: 650, y: 350, width: 350, height: 300 } });
    assert.deepEqual(manual.view.getBounds(), { x: 650, y: 350, width: 350, height: 300 });
    await browser.openBlank('manual-browser-second');
    const second = [...browser.pages.values()].find(page=>page.session==='manual-browser-second');
    await browser.control(second.session, 'takeover'); await attach(second.session);
    assert.equal(manual.host, null);
    assert.equal(await browser.evaluate(manual, 'document.querySelector("#note").value'), 'keep me');
    await browser.panel(second.session, host, {action:'close'});
    assert.equal(second.closed, true); assert.equal(manual.closed, false);
    await attach(manual.session);
    await browser.control(task.session, 'takeover'); await attach(task.session);
    assert.equal(host.contentView.children.filter(view => view === manual.view || view === task.view).length, 1);
    assert.equal(manual.host, null); assert.equal(task.host, host);
    assert.equal(task.contents.getTitle(), 'Agent page');
    await browser.control(task.session, 'release'); await attach(manual.session);
    assert.equal(task.takenOver, false); assert.equal(manual.host, host);
    assert.equal(await browser.evaluate(manual, 'document.querySelector("#note").value'), 'keep me');
    await browser.action(task, { action: 'close' });
    assert.equal(manual.closed, false); assert.equal(manual.host, host);
    assert.equal(host.contentView.children.includes(task.view), false);
    console.log('PASS: task takeover switches one native view, release restores unchanged manual page, task cleanup preserves manual browser');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { clearTimeout(deadline); await browser.reset(); host.destroy(); require('node:fs').rmSync(browser.root, { recursive: true, force: true }); app.exit(process.exitCode || 0); }
});
