const test = require('node:test');
const assert = require('node:assert/strict');
const { assertSender, downloadRoute, reloadDesktopPage } = require('../native.cjs');
const { isResearchMapPopup } = require('../native.cjs');

test('research map popup only allows a named blank window from the trusted desktop entry', () => {
  const origin = 'http://127.0.0.1:9000';
  const options = {url:'about:blank',frameName:'tcode-research-map',referrer:{url:`${origin}/desktop.html#/research`}};
  assert.equal(isResearchMapPopup(options, origin), true);
  for (const url of ['https://example.com', `${origin}/api/files/preview`, `${origin}/desktop.html`]) assert.equal(isResearchMapPopup({...options,url},origin),false);
  for (const url of ['https://example.com/desktop.html', `${origin}/api/files/preview`, `${origin}/`]) assert.equal(isResearchMapPopup({...options,referrer:{url}},origin),false);
  assert.equal(isResearchMapPopup({...options,frameName:'anything'},origin),false);
});

test('refresh really reloads the current document, preserves its route, and can recover a status page', () => {
  const origin = 'http://127.0.0.1:9000';
  for (const url of [`${origin}/desktop.html#/settings`, `${origin}/desktop.html`]) {
    let reloads = 0;
    reloadDesktopPage({getURL: () => url, reloadIgnoringCache: () => reloads++,
      loadURL: () => assert.fail('must reload instead of navigating to the base URL')}, origin);
    assert.equal(reloads, 1);
  }
  let loaded;
  reloadDesktopPage({getURL: () => 'data:text/html,status', loadURL: url => loaded = url,
    reloadIgnoringCache: () => assert.fail('must leave the error page')}, origin);
  assert.equal(loaded, `${origin}/desktop.html`);
});

test('native actions require the owning main frame at the desktop entry', () => {
  const frame = { url: 'http://127.0.0.1:9000/desktop.html#assets' };
  const window = { webContents: { mainFrame: frame } };
  const event = { sender: window.webContents, senderFrame: frame };
  assert.doesNotThrow(() => assertSender(event, window, 'http://127.0.0.1:9000'));
  assert.throws(() => assertSender({...event, senderFrame: {...frame}}, window, 'http://127.0.0.1:9000'));
  for (const url of ['http://127.0.0.1:9000/', 'http://127.0.0.1:9001/desktop.html', 'http://127.0.0.1:9000/api/files/preview', 'https://example.com/']) {
    frame.url = url;
    assert.throws(() => assertSender(event, window, 'http://127.0.0.1:9000'));
  }
});

test('save requests use only fixed authenticated API routes', () => {
  assert.equal(downloadRoute({path:'upload/a & b.txt', workspaceId:'w'}), '/api/files/download?path=upload%2Fa+%26+b.txt&workspace_id=w');
  assert.equal(downloadRoute({path:'x',jobId:'a/b',root:'r'}), '/api/jobs/a%2Fb/artifacts/download?path=x&root=r');
  assert.throws(() => downloadRoute({path:'x'}));
  assert.throws(() => downloadRoute({path:'x',jobId:'j'}));
});
