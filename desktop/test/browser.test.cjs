const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');
const { DesktopBrowser, publicAddress, allowedURL } = require('../browser.cjs');

test('browser menu anchors to its toolbar button at renderer zoom', async () => {
  let popup;
  const browser = new DesktopBrowser({ Menu: { buildFromTemplate: () => ({ popup(options) { popup = options; options.callback(); } }) } });
  const host = { webContents: { getZoomFactor: () => 1.25 }, getContentBounds: () => ({ width: 800, height: 600 }) };
  browser.pages.set('menu', { session: 'menu', closed: false, takenOver: true, view: {}, history: [], downloads: [], contents: {
    getURL: () => 'about:blank', getTitle: () => '', isLoading: () => false, getZoomFactor: () => 1,
    navigationHistory: { canGoBack: () => false, canGoForward: () => false },
  } });
  try {
    await browser.panel('menu', host, { action: 'menu', bounds: { x: 400, y: 120 } });
    assert.equal(popup.x, 500);
    assert.equal(popup.y, 150);
    await browser.panel('menu', host, { action: 'menu', bounds: { x: 1000, y: -10 } });
    assert.equal(popup.x, 800);
    assert.equal(popup.y, 0);
  } finally { browser.pages.clear(); require('node:fs').rmSync(browser.root, { recursive: true, force: true }); }
});

test('desktop browser denies local, metadata, non-web and encoded IP destinations', async () => {
  for (const url of ['file:///etc/passwd', 'http://localhost', 'http://127.1', 'http://2130706433', 'http://[::1]', 'http://169.254.169.254', 'https://user:secret@example.com']) {
    await assert.rejects(allowedURL(url));
  }
  for (const ip of ['10.1.2.3', '172.16.1.1', '192.168.1.2', '100.64.1.2', '::ffff:127.0.0.1', '2001:db8::1']) assert.equal(publicAddress(ip), false);
  assert.equal(publicAddress('8.8.8.8'), true);
  assert.equal(publicAddress('2606:4700:4700::1111'), true);
});

test('target blank links request a new browser sidebar', () => {
  const browser = new DesktopBrowser({});
  const sent = [];
  const page = { host: { isDestroyed: () => false, webContents: { send: (...args) => sent.push(args) } } };
  try {
    assert.deepEqual(browser.openInNewPage(page, 'https://www.baidu.com/link?url=result'), { action: 'deny' });
    assert.deepEqual(sent, [['desktop:browser-new-tab', 'https://www.baidu.com/link?url=result']]);
  } finally { require('node:fs').rmSync(browser.root, { recursive: true, force: true }); }
});

test('private browser socket rejects missing credentials without executing', async () => {
  const browser = new DesktopBrowser({});
  let executed = false;
  browser.execute = async () => { executed = true; return {}; };
  await browser.start();
  try {
    const result = await new Promise((resolve, reject) => {
      const socket = net.connect(browser.socket);
      socket.on('connect', () => socket.write(JSON.stringify({ token: 'wrong', args: { action: 'open' } }) + '\n'));
      socket.on('data', data => resolve(JSON.parse(data.toString())));
      socket.on('error', reject);
    });
    assert.equal(result.error, 'unauthorized'); assert.equal(executed, false);
  } finally { await browser.stop(); }
});

test('browser session cannot be selected across owners or replay an input', async () => {
  const browser = new DesktopBrowser({});
  const page = { closed: false, window: { isDestroyed: () => false }, results: new Map(), tail: Promise.resolve() };
  const crypto = require('node:crypto');
  const key = crypto.createHash('sha256').update('owner:session').digest('hex');
  browser.pages.set(key, page);
  let inputs = 0; browser.action = async () => { inputs++; return { text: 'result' }; };
  const request = { owner: 'owner', call_id: 'call', args: { session: 'session', action: 'fill' } };
  try {
    assert.deepEqual(await browser.execute(request), { text: 'result' });
    assert.deepEqual(await browser.execute(request), { text: 'result' });
    assert.equal(inputs, 1);
    await assert.rejects(browser.execute({ ...request, owner: 'other' }), /browser_session_missing/);
    await assert.rejects(browser.execute({ ...request, args: { ...request.args, session: '../escape' } }), /invalid_browser_scope/);
  } finally { browser.pages.clear(); require('node:fs').rmSync(browser.root, { recursive: true, force: true }); }
});

test('desktop browser serializes sidebar takeover and accepts input only while controlled', async () => {
  const browser = new DesktopBrowser({});
  const calls = [];
  browser.action = async () => ({});
  const window = {
    isDestroyed: () => false,
    webContents: {
      sendInputEvent: event => calls.push(event.type),
      insertText: text => calls.push(`text:${text}`),
    },
  };
  let finishAction;
  const activeAction = new Promise(resolve => { finishAction = resolve; });
  const page = { session: 'session-1', closed: false, takenOver: false, revision: 1, context: {}, tail: activeAction, window };
  browser.pages.set('page', page);
  try {
    const takeover = browser.control('session-1', 'takeover');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(page.takenOver, false);
    finishAction();
    assert.deepEqual(await takeover, { success: true, control_mode: 'human' });
    assert.equal(page.takenOver, true);
    assert.deepEqual(await browser.input('session-1', { action: 'click', x: 12, y: 24 }), { success: true });
    assert.deepEqual(await browser.input('session-1', { action: 'text', text: 'python' }), { success: true });
    assert.deepEqual(await browser.control('session-1', 'release'), { success: true, control_mode: 'agent' });
    assert.equal(page.takenOver, false);
    await assert.rejects(browser.input('session-1', { action: 'key', key: 'Enter' }), /browser_human_control_required/);
    assert.equal(page.revision, 2);
    assert.equal(page.context, null);
    assert.deepEqual(calls, ['mouseDown', 'mouseUp', 'text:python']);
  } finally {
    browser.pages.clear(); require('node:fs').rmSync(browser.root, { recursive: true, force: true });
  }
});

test('native panel converts renderer zoom, clamps bounds and skips unchanged resize', async () => {
  const browser = new DesktopBrowser({});
  let bounds = { x: 0, y: 0, width: 1, height: 1 }; let resizes = 0;
  const host = { isDestroyed: () => false, webContents: { getZoomFactor: () => 1.25 }, getContentSize: () => [800, 600], contentView: { addChildView() {}, removeChildView() {} } };
  const page = { session: 'test', closed: false, takenOver: true, host, fitWidth: false, history: [], downloads: [], contents: { getURL: () => '', getTitle: () => '', isLoading: () => false, getZoomFactor: () => 1, navigationHistory: { canGoBack: () => false, canGoForward: () => false } }, view: { getBounds: () => bounds, setBounds: next => { bounds = next; resizes++; }, setVisible() {} } };
  browser.pages.set('test', page);
  try {
    const command = { action: 'attach', bounds: { x: 40, y: 80, width: 800, height: 600 } };
    await browser.panel('test', host, command);
    assert.deepEqual(bounds, { x: 50, y: 100, width: 750, height: 500 });
    await browser.panel('test', host, command);
    assert.equal(resizes, 1);
    await assert.rejects(browser.panel('test', host, { action: 'zoom', factor: Infinity }), /invalid_browser_zoom/);
    page.takenOver = false;
    await browser.panel('test', host, { action: 'state' });
    await browser.panel('test', host, { action: 'detach' });
    await assert.rejects(browser.panel('test', host, command), /browser_human_control_required/);
  } finally { browser.pages.clear(); require('node:fs').rmSync(browser.root, { recursive: true, force: true }); }
});

test('agent browser action waits for human release before continuing', async () => {
  const browser = new DesktopBrowser({});
  let navigated = false;
  const page = { session: 'session-1', closed: false, takenOver: true, revision: 0, context: {},
    agentControl: null, resumeAgent: null, contents: { isLoading: () => false, getURL: () => 'https://example.com', getTitle: () => 'Example' },
    window: { isDestroyed: () => false } };
  page.agentControl = new Promise(resolve => { page.resumeAgent = resolve; });
  browser.navigate = async () => { navigated = true; };
  browser.evaluate = async () => ({ headings: [], text: '', elements: [] });
  browser.command = async () => ({ data: '' });
  browser.pages.set('page', page);
  try {
    const action = browser.action(page, { action: 'navigate', url: 'https://example.com' });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(navigated, false);
    await browser.control('session-1', 'release');
    await action;
    assert.equal(navigated, true);
  } finally { browser.pages.clear(); require('node:fs').rmSync(browser.root, { recursive: true, force: true }); }
});

test('versioned refs work without a duplicate revision argument but stale refs still fail', async () => {
  const browser = new DesktopBrowser({});
  const page = { revision: 7, takenOver: false, contents: {} };
  browser.evaluate = async () => { throw new Error('reached_target_validation'); };
  try {
    await assert.rejects(browser.action(page, { action: 'click', ref: 'r7-e12' }), /reached_target_validation/);
    await assert.rejects(browser.action(page, { action: 'click', ref: 'r6-e12' }), /stale_ref/);
    await assert.rejects(browser.action(page, { action: 'click', ref: 'r7-e12', page_revision: 6 }), /stale_ref/);
    await assert.rejects(browser.action(page, { action: 'click', ref: 'e12' }), /stale_ref/);
  } finally { require('node:fs').rmSync(browser.root, { recursive: true, force: true }); }
});
