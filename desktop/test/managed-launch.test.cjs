const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { launchManagedDesktop, validateReadyPayload, READY_PREFIX } = require('../managed-launch.cjs');

function fakeChild(pid = 42) {
  const child = new EventEmitter();
  child.pid = pid; child.exitCode = null; child.signalCode = null;
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new EventEmitter();
  child.stdin.end = () => child.emit('stdin-end');
  child.kill = signal => { child.signalCode = signal; child.emit('exit', null, signal); };
  return child;
}

const payload = { type: 'desktop_ready', protocol: 1, pid: 42, project_root: '/repo', renderer_url: 'http://127.0.0.1:4321/desktop.html', backend_healthy: true, host_generation: 3 };

test('managed launcher accepts only the owned healthy source desktop', async () => {
  const child = fakeChild();
  const launch = launchManagedDesktop({ executable: 'electron', args: [], cwd: '/repo', env: {}, spawnImpl: () => child });
  child.stdout.emit('data', `${READY_PREFIX}${JSON.stringify(payload)}\n`);
  assert.deepEqual(await launch.ready, payload);
  child.once('stdin-end', () => { child.exitCode = 0; child.emit('exit', 0, null); });
  await launch.stop('test_cleanup');
});

test('managed launcher reports an abnormal exit before ready', async () => {
  const child = fakeChild();
  const events = [];
  const launch = launchManagedDesktop({ executable: 'electron', args: [], cwd: '/repo', env: {}, spawnImpl: () => child, onEvent: event => events.push(event) });
  child.exitCode = 9; child.emit('exit', 9, null);
  await assert.rejects(launch.ready, /before ready/);
  assert.equal(events.at(-1).expected, false);
});

test('parent cleanup closes stdin and records an expected exit', async () => {
  const child = fakeChild();
  const events = [];
  const launch = launchManagedDesktop({ executable: 'electron', args: [], cwd: '/repo', env: {}, spawnImpl: () => child, onEvent: event => events.push(event) });
  child.stdout.emit('data', `${READY_PREFIX}${JSON.stringify(payload)}\n`);
  await launch.ready;
  child.once('stdin-end', () => { child.exitCode = 0; child.emit('exit', 0, null); });
  await launch.stop('parent_end');
  assert.deepEqual(events.filter(event => ['cleanup', 'exit'].includes(event.type)).map(event => event.type), ['cleanup', 'exit']);
  assert.equal(events.at(-1).expected, true);
});

test('ready validation rejects the wrong repository or renderer', () => {
  assert.throws(() => validateReadyPayload({ ...payload, project_root: '/other' }, { pid: 42, projectRoot: '/repo' }), /project mismatch/);
  assert.throws(() => validateReadyPayload({ ...payload, renderer_url: 'https://example.com/desktop.html' }, { pid: 42, projectRoot: '/repo' }), /renderer/);
});
