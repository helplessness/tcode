const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { startBackend, validateReady, isLocalNavigation, isExternalLink } = require('../backend.cjs');

test('readiness only accepts a valid loopback port and current protocol', () => {
  assert.equal(validateReady({ type: 'ready', protocol: 1, port: 49152 }), 'http://127.0.0.1:49152');
  for (const port of [0, 65536, '8080', 1.5]) {
    assert.throws(() => validateReady({ type: 'ready', protocol: 1, port }));
  }
  assert.throws(() => validateReady({ type: 'ready', protocol: 2, port: 8080 }));
});

test('untrusted navigation cannot inherit desktop privileges', () => {
  const origin = 'http://127.0.0.1:49152';
  assert.equal(isLocalNavigation(`${origin}/?page=wiki`, origin), true);
  for (const url of ['http://127.0.0.1:1234', 'http://127.0.0.1.evil.test:49152', 'file:///etc/passwd', 'javascript:alert(1)']) {
    assert.equal(isLocalNavigation(url, origin), false);
  }
  assert.equal(isExternalLink('https://example.com/paper'), true);
  assert.equal(isExternalLink('mailto:research@example.com'), true);
  assert.equal(isExternalLink('file:///etc/passwd'), false);
  assert.equal(isExternalLink('javascript:alert(1)'), false);
});

test('backend readiness and shutdown work with a real owned process', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-desktop-test-'));
  fs.mkdirSync(path.join(root, 'desktop'));
  fs.writeFileSync(path.join(root, 'desktop/backend.sh'), '#!/bin/bash\ntrap "exit 0" TERM\nprintf \'{"type":"ready","protocol":1,"port":54321}\\n\' >&3\nsleep 60 &\nwait\n');
  const backend = startBackend({ projectRoot: root, runtimeRoot: root, logPath: path.join(root, 'backend.log'), python: '/unused' });
  try {
    assert.equal(await backend.ready, 'http://127.0.0.1:54321');
    const pid = backend.child.pid;
    await backend.stop();
    await backend.stop();
    assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
  } finally {
    await backend.stop();
    fs.rmSync(root, { recursive: true });
  }
});

test('a backend that exits before ready fails promptly', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-desktop-test-'));
  fs.mkdirSync(path.join(root, 'desktop'));
  fs.writeFileSync(path.join(root, 'desktop/backend.sh'), '#!/bin/bash\nexit 7\n');
  const backend = startBackend({ projectRoot: root, runtimeRoot: root, logPath: path.join(root, 'backend.log'), python: '/unused' });
  try {
    await assert.rejects(backend.ready, /7/);
  } finally {
    await backend.stop();
    fs.rmSync(root, { recursive: true });
  }
});

test('packaged backend executable starts without bash or Python', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-packaged-backend-test-'));
  const executable = path.join(root, 'backend');
  fs.writeFileSync(executable, '#!/bin/bash\nprintf \'{"type":"ready","protocol":1,"port":54322}\\n\' >&3\nsleep 60 &\nwait\n', { mode: 0o755 });
  const backend = startBackend({
    projectRoot: '/path/that/does/not/exist',
    runtimeRoot: root,
    logPath: path.join(root, 'backend.log'),
    python: '/missing/python',
    backendExecutable: executable,
  });
  assert.equal(await backend.ready, 'http://127.0.0.1:54322');
  await backend.stop();
});
