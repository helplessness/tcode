const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const test = require('node:test');
const { ComputerBroker, McpDriverAdapter, OPERATIONS, READ_ONLY, WRITE, INTERNAL_OPERATIONS, DRIVER_CONTRACTS, DRIVER_TOOLS, OBSERVATION_REF_PATTERN, classifyDriverError, observationSemantics, semanticCoverage, approvalPresentation, resolvedApprovalPanelRequest } = require('../computer.cjs');
const windowState034 = JSON.parse(fs.readFileSync(path.join(__dirname, '../../tests/fixtures/computer-use/cua-0.34.0-window-state.json'), 'utf8'));
const windows034 = JSON.parse(fs.readFileSync(path.join(__dirname, '../../tests/fixtures/computer-use/cua-0.34.0-list-windows.json'), 'utf8'));
const runtimeShape034 = JSON.parse(fs.readFileSync(path.join(__dirname, '../../tests/fixtures/computer-use/cua-0.34.0-runtime-shape.json'), 'utf8'));

test('every operation has one coherent read/write/control contract', () => {
  const contracts = new Set(Object.keys(DRIVER_CONTRACTS));
  assert.deepEqual(contracts, new Set([...OPERATIONS, ...INTERNAL_OPERATIONS]));
  assert.deepEqual(new Set([...READ_ONLY, ...WRITE, 'release_control']), OPERATIONS);
  assert.equal([...READ_ONLY].some(operation => WRITE.has(operation)), false);
  for (const operation of READ_ONLY) assert.equal(DRIVER_CONTRACTS[operation].readonly, true, operation);
  for (const operation of WRITE) {
    assert.equal(DRIVER_CONTRACTS[operation].readonly, false, operation);
    assert.ok(DRIVER_TOOLS[operation], `${operation} needs a Driver mapping`);
  }
  assert.equal(DRIVER_CONTRACTS.release_control.readonly, false);
  assert.equal(DRIVER_CONTRACTS.release_control.driver, null);
  assert.equal(DRIVER_CONTRACTS.end_task.internal, true);
});

test('every write operation requires an action id before dispatch', async () => {
  for (const operation of WRITE) {
    const broker = ready(new ComputerBroker({ enabled: true, runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-cu-class-')), expectedDriverVersion: '0.34.0' }));
    broker.refreshPermissions = async () => {};
    const request = message(broker, operation, { task_id: 'classification' });
    delete request.action_id;
    const response = await broker.handle(request);
    assert.equal(response.error_code, 'protocol_error', operation);
    assert.equal(response.executed, false, operation);
    assert.equal(broker.publicState().action_budget.used, 0, operation);
  }
});

function fakePng(width, height) {
  const data = Buffer.alloc(24); Buffer.from([137,80,78,71,13,10,26,10]).copy(data); data.writeUInt32BE(width, 16); data.writeUInt32BE(height, 20);
  return { mime_type: 'image/png', data: data.toString('base64') };
}

function message(broker, operation, payload = {}) {
  return {
    type: 'request', request_id: 'a'.repeat(32), action_id: crypto.randomBytes(16).toString('hex'), operation, payload,
    broker_session_id: broker.brokerSessionId, driver_generation: broker.generation, control_epoch: broker.epoch,
  };
}
function ready(broker, screenRecording = true) {
  broker.permissions = { status: 'known', accessibility: true, screen_recording: screenRecording, identity_verified: true, attribution: 'host', host_bundle_id: 'com.tcode.desktop' };
  return broker;
}
function approve(broker, taskId, operation) {
  const pending = broker.publicState().pending_approval;
  broker.grantPending({ approval_id: pending.approval_id, task_id: taskId, operation, parameter_digest: pending.parameter_digest });
}
async function requestAndApprove(broker, request, taskId, operation) {
  const result = broker.handle(request);
  await new Promise(resolve => setImmediate(resolve));
  if (broker.publicState().pending_approval) approve(broker, taskId, operation);
  return result;
}
const permissions = { accessibility: true, screen_recording: true, direct_capture_status: 'not_checked', source: { attribution: 'host', embedded: true, host_bundle_id: 'com.tcode.desktop' }, _driver_identity: { embedded: true, pid: 77, generation: 'driver-generation', host_bundle_id: 'com.tcode.desktop', permission_mode: 'standard', binary_path: '' } };

test('one task confirmation covers screenshots; model switches reuse it and supplier changes require consent', async () => {
  let captures = 0;
  const app = { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' };
  const bounds = { x: 0, y: 0, width: 50, height: 50 };
  const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-service-consent-')),
    driver: { call: async tool => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
      if (tool === 'get_window_state') return { capture_id: `cap-${++captures}`, elements: [],
        window_bounds: bounds, screenshot_width: 100, screenshot_height: 100, _image: fakePng(100, 100),
        screenshot_service_authorized: 'forged-native-service' };
      return {};
    } } }));
  const intent = { task_id: 'task', text: '只读查看窗口', turn_id: 'turn-human', user_message_id: 'human' };
  const proof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(intent)).digest('hex');
  const request = (service, model) => ({ ...message(broker, 'observe', {
    task_id: 'task', app, window: { window_id: 7 }, task_intent: intent, task_intent_proof: proof,
    screenshot_service: service, screenshot_model: `${service}/${model}`, delivery_mode: 'background',
  }), request_id: crypto.randomBytes(16).toString('hex') });
  const first = broker.handle(request('kimi', 'k2.6'));
  await new Promise(resolve => setImmediate(resolve));
  let pending = broker.publicState().pending_approval;
  assert.match(pending.action_summary.screenshot, /kimi.*k2.6/);
  assert.equal(captures, 0);
  broker.grantPending(pending);
  assert.equal((await first).result.screenshot_service_authorized, 'kimi');
  const scope = broker.taskGrants.get('task');
  scope.commit_attempted = { intent_id: 'already-committed' };
  const expiry = scope.expires_at;
  const sameService = await broker.handle(request('kimi', 'k2.5'));
  assert.equal(sameService.result.screenshot_service_authorized, 'kimi');
  assert.equal(broker.publicState().pending_approval, null);
  const refused = broker.handle(request('other', 'vision'));
  await new Promise(resolve => setImmediate(resolve));
  pending = broker.publicState().pending_approval;
  assert.equal(pending.action_summary.title, '确认更换截图接收供应商');
  assert.match(pending.action_summary.screenshot, /other/);
  broker.rejectPending(pending);
  assert.equal((await refused).error_code, 'approval_denied');
  assert.equal(captures, 2);
  const changed = broker.handle(request('other', 'vision'));
  await new Promise(resolve => setImmediate(resolve));
  broker.grantPending(broker.publicState().pending_approval);
  assert.equal((await changed).result.screenshot_service_authorized, 'other');
  assert.equal(scope.commit_attempted.intent_id, 'already-committed');
  assert.equal(scope.expires_at, expiry);
  assert.equal((await broker.handle(request('other', 'vision-next'))).status, 'completed_known');
  assert.equal(broker.publicState().pending_approval, null);
});

test('concurrent permission refreshes all return state and distinguish host/driver disagreement', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-permission-refresh-'));
  let release;
  const broker = new ComputerBroker({ runtimeRoot: root,
    hostPermissions: () => ({ accessibility: true, screen_recording: true }),
    driver: { call: async () => new Promise(resolve => { release = resolve; }) } });
  try {
    const first = broker.refreshPermissions(true);
    const second = broker.refreshPermissions(true);
    release({ ...permissions, accessibility: false, screen_recording: false });
    const results = await Promise.all([first, second]);
    for (const result of results) {
      assert.equal(result.enabled, true);
      assert.equal(result.permissions.runtime_mismatch, true);
      assert.equal(result.permissions.accessibility, false); // Never bypass the Driver gate.
    }
    const malformed = broker.refreshPermissions(true);
    release({ source: permissions.source, _driver_identity: permissions._driver_identity });
    const result = await malformed;
    assert.equal(result.permissions.status, 'error');
    assert.equal(result.permissions.accessibility, null);
  } finally { await broker.stop(); fs.rmSync(root, { recursive: true, force: true }); }
});

test('Driver adapter delegates lifecycle and calls to the official embedded SDK', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-embedded-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  let starts = 0; let stops = 0; let destroyed = 0; let resolveExit;
  const exit = new Promise(resolve => { resolveExit = resolve; });
  const driver = {
    metadata: async () => ({ embedded: true, pid: 77, driverVersion: '0.35.2', hostBundleId: 'com.tcode.test' }),
    callTool: async (tool, json) => ({
      text: 'ok', images: [{ mimeType: 'image/png', dataBase64: 'image' }],
      structuredJson: JSON.stringify({ tool, args: JSON.parse(json) }), isError: false, degraded: false, rawJson: '{}',
    }),
    uniffiDestroy: () => { destroyed += 1; },
  };
  class EmbeddedCuaDriverHost {
    constructor(binary, bundle) { assert.equal(binary, executable); assert.equal(bundle, 'com.tcode.test'); }
    async start() { starts += 1; return { socketPath: '/tmp/fixture.sock', pid: 77, generation: 'generation', driverVersion: '0.35.2', contractVersion: '0.8.0' }; }
    waitForExit() { return exit; }
    async stop() { stops += 1; resolveExit({ generation: 'generation', success: true }); }
    uniffiDestroy() { destroyed += 1; }
  }
  const adapter = new McpDriverAdapter({ executable, hostBundleId: 'com.tcode.test', sdkLoader: async () => ({
    EmbeddedCuaDriverHost, CuaDriver: { connect: socket => { assert.equal(socket, '/tmp/fixture.sock'); return driver; } },
  }) });
  const [first, second] = await Promise.all([adapter.call('list_apps', {}), adapter.call('list_windows', { pid: 7 })]);
  assert.equal(starts, 1); assert.equal(first.tool, 'list_apps'); assert.equal(second.args.pid, 7);
  assert.deepEqual(first._image, { data: 'image', mime_type: 'image/png' });
  await adapter.stop();
  assert.equal(stops, 1); assert.equal(destroyed, 2);
});

test('Driver adapter rejects incompatible embedded daemon versions before connecting', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-embedded-version-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  let connected = false; let stopped = false;
  const adapter = new McpDriverAdapter({ executable, sdkLoader: async () => ({
    EmbeddedCuaDriverHost: class {
      async start() { return { driverVersion: '0.35.0' }; }
      async stop() { stopped = true; }
      uniffiDestroy() {}
    },
    CuaDriver: { connect: () => { connected = true; } },
  }) });
  await assert.rejects(adapter.start(), error => error.code === 'driver_protocol_error' && error.actualVersion === '0.35.0');
  assert.equal(connected, false); assert.equal(stopped, true);
});

test('Driver adapter preserves native action effects instead of treating delivery as success', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-effects-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  const responses = [
    { effect: 1, delivery: { mode: 0 } },
    { effect: 2, delivery: { mode: 1 } },
    { effect: 3, delivery: { mode: 0 } },
    { effect: 4, delivery: { mode: 0 }, error: { code: 'stale_snapshot' } },
    { effect: 0, delivery: { mode: 0 } },
    { effect: 2, delivery: { mode: 3 } },
  ];
  let exitResolve;
  const adapter = new McpDriverAdapter({ executable, sdkLoader: async () => ({
    EmbeddedCuaDriverHost: class {
      async start() { return { socketPath: '/tmp/effects.sock', pid: 88, generation: 'effects', driverVersion: '0.35.2', contractVersion: '0.8.0' }; }
      waitForExit() { return new Promise(resolve => { exitResolve = resolve; }); }
      async stop() { exitResolve?.({ generation: 'effects', success: true }); }
      uniffiDestroy() {}
    },
    CuaDriver: { connect: () => ({
      metadata: async () => ({ embedded: true, pid: 88, driverVersion: '0.35.2', hostBundleId: 'com.tcode.desktop' }),
      callTool: async () => ({ text: '', images: [], isError: false, degraded: false, rawJson: '{}', action: responses.shift() }),
      uniffiDestroy() {},
    }) },
  }) });
  assert.deepEqual((await adapter.call('click', {})).action_receipt, { effect: 'partial', delivery: 'background', verification: 'pending' });
  assert.deepEqual((await adapter.call('click', {})).action_receipt, { effect: 'unverifiable', delivery: 'foreground', verification: 'pending' });
  assert.deepEqual((await adapter.call('click', {})).action_receipt, { effect: 'suspected_noop', delivery: 'background', verification: 'pending' });
  await assert.rejects(adapter.call('click', {}), error => error.code === 'failed_known' && error.safeCode === 'stale_snapshot');
  assert.equal((await adapter.call('click', {})).action_receipt.verification, 'native_confirmed');
  await assert.rejects(adapter.call('click', {}), error => error.code === 'outcome_unknown');
  await adapter.stop();
});

test('Driver adapter serializes start, stop, and a subsequent restart', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-lifecycle-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  let starts = 0; let firstStartRelease;
  const firstStart = new Promise(resolve => { firstStartRelease = resolve; });
  const events = [];
  class Host {
    constructor() { this.number = ++starts; }
    async start() { if (this.number === 1) await firstStart; return { socketPath: `/tmp/${this.number}.sock`, pid: 90 + this.number, generation: `g${this.number}`, driverVersion: '0.35.2', contractVersion: '0.8.0' }; }
    waitForExit() { return new Promise(() => {}); }
    async stop() { events.push(`stop:${this.number}`); }
    uniffiDestroy() {}
  }
  const adapter = new McpDriverAdapter({ executable, sdkLoader: async () => ({
    EmbeddedCuaDriverHost: Host,
    CuaDriver: { connect: socket => ({
      metadata: async () => ({ embedded: true, pid: 90 + Number(socket.match(/\d+/)[0]), driverVersion: '0.35.2', hostBundleId: 'com.tcode.desktop' }),
      callTool: async () => ({ text: socket, images: [], structuredJson: JSON.stringify({ socket }), isError: false, degraded: false, rawJson: '{}' }),
      uniffiDestroy() {},
    }) },
  }) });
  const first = adapter.call('list_apps', {});
  const firstRejected = assert.rejects(first, error => error.code === 'outcome_unknown');
  const stopping = adapter.stop();
  const second = adapter.call('list_apps', {});
  firstStartRelease();
  await firstRejected;
  await stopping;
  assert.equal((await second).socket, '/tmp/2.sock');
  assert.deepEqual(events, ['stop:1']);
  await adapter.stop();
});

test('stop aborts an active call promptly and a late callback cannot revive the old generation', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-abort-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  let starts = 0; let releaseLate; let firstSignal;
  class Host {
    async start() { this.number = ++starts; return { socketPath: `/tmp/abort-${this.number}.sock`, pid: 110 + this.number, generation: `abort-g${this.number}`, driverVersion: '0.35.2', contractVersion: '0.8.0' }; }
    waitForExit() { return new Promise(() => {}); }
    async stop() {}
    uniffiDestroy() {}
  }
  const adapter = new McpDriverAdapter({ executable, sdkLoader: async () => ({
    EmbeddedCuaDriverHost: Host,
    CuaDriver: { connect: socket => ({
      metadata: async () => ({ embedded: true, pid: 110 + Number(socket.match(/(\d+)\.sock$/)[1]), driverVersion: '0.35.2', hostBundleId: 'com.tcode.desktop' }),
      callTool: async (_tool, _args, options) => {
        if (socket.endsWith('1.sock')) {
          firstSignal = options.signal;
          await new Promise(resolve => { releaseLate = resolve; });
        }
        return { text: '', images: [], structuredJson: JSON.stringify({ socket }), isError: false, degraded: false, rawJson: '{}' };
      },
      uniffiDestroy() {},
    }) },
  }) });
  const oldCall = adapter.call('click', {});
  const oldRejected = assert.rejects(oldCall, error => error.code === 'outcome_unknown');
  while (!firstSignal) await new Promise(resolve => setImmediate(resolve));
  const stop = adapter.stop();
  while (!firstSignal.aborted) await new Promise(resolve => setImmediate(resolve));
  assert.equal(firstSignal.aborted, true);
  releaseLate();
  await oldRejected;
  await stop;
  assert.equal((await adapter.call('list_apps', {})).socket, '/tmp/abort-2.sock');
  await adapter.stop();
});

test('a dispatched SDK timeout is outcome unknown and not a known action effect', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-timeout-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  const adapter = new McpDriverAdapter({ executable, callTimeoutMs: 5, sdkLoader: async () => ({
    EmbeddedCuaDriverHost: class {
      async start() { return { socketPath: '/tmp/timeout.sock', pid: 121, generation: 'timeout-g', driverVersion: '0.35.2', contractVersion: '0.8.0' }; }
      waitForExit() { return new Promise(() => {}); }
      async stop() {}
      uniffiDestroy() {}
    },
    CuaDriver: { connect: () => ({
      metadata: async () => ({ embedded: true, pid: 121, driverVersion: '0.35.2', hostBundleId: 'com.tcode.desktop' }),
      callTool: async (_tool, _args, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })),
      uniffiDestroy() {},
    }) },
  }) });
  await assert.rejects(adapter.call('click', {}), error => error.code === 'outcome_unknown' && !error.evidence);
  await adapter.stop();
});

test('an unexpected official host exit is reported and the next call gets a fresh generation', async () => {
  const executable = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-exit-')), 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  let starts = 0; const exits = new Map(); const events = [];
  class Host {
    async start() {
      this.number = ++starts;
      return { socketPath: `/tmp/exit-${this.number}.sock`, pid: 100 + this.number, generation: `exit-g${this.number}`, driverVersion: '0.35.2', contractVersion: '0.8.0' };
    }
    waitForExit(generation) { return new Promise(resolve => exits.set(generation, resolve)); }
    async stop() {}
    uniffiDestroy() {}
  }
  const adapter = new McpDriverAdapter({ executable, sdkLoader: async () => ({
    EmbeddedCuaDriverHost: Host,
    CuaDriver: { connect: socket => ({
      metadata: async () => ({ embedded: true, pid: 100 + Number(socket.match(/(\d+)\.sock$/)[1]), driverVersion: '0.35.2', hostBundleId: 'com.tcode.desktop' }),
      callTool: async () => ({ text: '', images: [], structuredJson: JSON.stringify({ socket }), isError: false, degraded: false, rawJson: '{}' }),
      uniffiDestroy() {},
    }) },
  }) });
  adapter.setLifecycleListener(event => events.push(event));
  assert.equal((await adapter.call('list_apps', {})).socket, '/tmp/exit-1.sock');
  exits.get('exit-g1')({ generation: 'exit-g1', success: false, code: 9 });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(events.at(-1).type, 'unexpected_exit');
  assert.equal((await adapter.call('list_apps', {})).socket, '/tmp/exit-2.sock');
  assert.deepEqual(events.filter(event => event.type === 'ready').map(event => event.generation), ['exit-g1', 'exit-g2']);
  await adapter.stop();
});

test('an official driver generation event fences Broker grants and observations', () => {
  let listener;
  const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-generation-')), driver: {
    setLifecycleListener: value => { listener = value; }, call: async () => permissions, stop: async () => {},
  } }));
  broker.grants.set('grant', {}); broker.observations.set('observation', {}); broker.captures.set('capture', {}); broker.targets.set('target', {});
  const before = broker.generation;
  listener({ type: 'ready', generation: 'official-1' });
  assert.equal(broker.generation, before + 1);
  assert.equal(broker.grants.size, 0); assert.equal(broker.observations.size, 0); assert.equal(broker.captures.size, 0); assert.equal(broker.targets.size, 0);
  broker.grants.set('grant', {});
  listener({ type: 'unexpected_exit', generation: 'official-1' });
  assert.equal(broker.generation, before + 2); assert.equal(broker.grants.size, 0); assert.equal(broker.permissions.identity_verified, false);
});

test('fixed Cua 0.34.0 contract fixtures use real upstream field names', () => {
  assert.equal(windowState034.driver_version, '0.34.0');
  assert.deepEqual(Object.keys(windowState034.window_bounds), ['x', 'y', 'width', 'height']);
  assert.equal(windowState034.screenshot_width, 1280); assert.equal(windowState034.screenshot_scale, 2);
  assert.equal(windows034.windows[0].window_id, 17); assert.deepEqual(windows034.windows[0].bounds, windowState034.window_bounds);
  assert.equal('geometry' in windowState034, false); assert.equal('coordinate_transform' in windowState034, false);
});

test('progress semantics reject menu-only or structural AX coverage', () => {
  assert.equal(observationSemantics({ elements: [
    { role: 'AXMenuBar', title: 'Application menu' },
    { role: 'AXWindow', title: 'Visual-only content' },
    { role: 'AXGroup' },
  ] }), null);
  assert.deepEqual(observationSemantics({ elements: [
    { role: 'AXWindow', title: 'Fixture' },
    { role: 'AXButton', title: 'Apply', enabled: true },
  ] }), {
    ax_state: '',
    elements: [
      { role: 'AXButton', title: 'Apply', enabled: true },
      { role: 'AXWindow', title: 'Fixture' },
    ],
  });
  const semantics = observationSemantics({ elements: [{ role: 'AXButton', title: 'Apply' }] });
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], element_count: 1 }, semantics), 'unknown');
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], elements_complete: true }, semantics), 'complete');
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], elements_complete: true, truncated: true }, semantics), 'partial');
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], elements_complete: true, nodes_pending: 2 }, semantics), 'partial');
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], element_count: 2 }, semantics), 'unknown');
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], truncated: true, element_count: 1 }, semantics), 'partial');
  assert.equal(semanticCoverage({ elements: [{ role: 'AXButton' }], degraded_reason: 'ax_window_unresolved' }, semantics), 'partial');
});

test('local AppKit runtime fixture records the pinned Driver image contract without private content', () => {
  assert.equal(runtimeShape034.driver_version, '0.34.0'); assert.equal(runtimeShape034.app.bundle_id, 'com.tcode.computer-fixture');
  assert.equal(runtimeShape034.state.has_image, true); assert.equal(runtimeShape034.state.screenshot_scale, 2);
  assert.equal(runtimeShape034.state.screenshot_width, runtimeShape034.window.bounds.width * 2);
  assert.equal(runtimeShape034.state.screenshot_height, runtimeShape034.window.bounds.height * 2);
  assert.equal(runtimeShape034.state.keys.includes('snapshot_id'), true); assert.equal(runtimeShape034.state.keys.includes('capture_id'), true);
  assert.equal(JSON.stringify(runtimeShape034).includes('tree_markdown":'), false);
});

test('disabled computer use blocks discovery and writes and cancels pending approval', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-toggle-'));
  let calls = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, enabled: false, driver: { call: async () => { calls += 1; return permissions; } } }));
  for (const operation of ['list_apps', 'observe', 'type_text']) {
    assert.equal((await broker.handle(message(broker, operation, { task_id: 'task' }))).error_code, 'computer_disabled');
  }
  assert.equal(calls, 0);
  broker.setEnabled(true);
  const pending = broker.handle({ ...message(broker, 'type_text', { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'text', delivery_mode: 'background' }), request_id: 'b'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(broker.publicState().pending_approval);
  broker.setEnabled(false);
  assert.equal((await pending).error_code, 'control_revoked');
  assert.equal(broker.publicState().enabled, false);
  assert.equal(broker.publicState().pending_approval, null);
  assert.equal(broker.grants.size, 0);
});

test('driver errors are reduced to fixed non-sensitive enums', () => {
  assert.equal(classifyDriverError({ content: [{ text: 'Screen recording permission denied for Secret Window' }] }), 'system_permission_denied');
  assert.equal(classifyDriverError({ content: [{ text: 'CGWindowListCopyWindowInfo failed' }] }), 'window_server_error');
  assert.equal(classifyDriverError({ content: [{ text: 'private unexpected detail' }] }), 'failed_known');
  assert.equal(classifyDriverError({ content: [{ text: 'Accessibility target process exited; pid not found' }] }), 'target_not_found');
});

test('computer actions fail closed, revoke invalidates grants, and audit omits content', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => { if (tool === 'check_permissions') return permissions; calls.push({ tool, args }); return { summary: 'ok' }; } } });
  ready(broker);
  const pending = broker.handle(message(broker, 'type_text', { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'secret', delivery_mode: 'background' }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(broker.publicState().status, 'waiting_for_approval'); assert.equal(calls.length, 0);
  approve(broker, 'task', 'type_text');
  let result = await pending;
  assert.equal(result.status, 'completed_known'); assert.equal(calls.length, 1);
  assert.equal(JSON.stringify(result.audit).includes('secret'), false);
  broker.revoke();
  result = await broker.handle({ ...message(broker, 'type_text', { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'again', delivery_mode: 'background' }), request_id: 'b'.repeat(32), control_epoch: broker.epoch - 1 });
  assert.equal(result.error_code, 'control_revoked'); assert.equal(calls.length, 1);
});

test('capture is consumed before dispatch and unknown effects are never replayed', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  let calls = 0; let clickArgs;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls += 1;
    if (tool === 'get_window_state') return {
      summary: 'window', capture_id: 'capture_one',
      window_bounds: { x: 0, y: 0, width: 100, height: 100 },
      screenshot_width: 100, screenshot_height: 100, screenshot_scale: 1,
      _image: fakePng(100, 100),
    };
    if (tool === 'list_windows') return { windows: [{ window_id: 1, bounds: { x: 0, y: 0, width: 100, height: 100 } }] };
    clickArgs = args;
    throw Object.assign(new Error('lost'), { code: 'outcome_unknown' });
  } } });
  ready(broker);
  const window = { window_id: '1' }; const app = { bundle_id: 'fixture.app' };
  const observation = { task_id: 'task', app, window, delivery_mode: 'background' };
  const observed = await requestAndApprove(broker, message(broker, 'observe', observation), 'task', 'observe');
  assert.equal(observed.result.capture_id, undefined); assert.equal(observed.result.capture_binding, undefined);
  const action = { ...message(broker, 'click_at', { task_id: 'task', app, window, x: 0.02, y: 0.03, coordinate_space: 'normalized', reference_width: 100, reference_height: 100, delivery_mode: 'background' }), request_id: 'c'.repeat(32) };
  const first = await requestAndApprove(broker, action, 'task', 'click_at');
  const duplicate = await broker.handle(action);
  assert.equal(first.status, 'outcome_unknown'); assert.deepEqual(duplicate, first); assert.equal(calls, 3);
  assert.equal(clickArgs.capture_id, 'capture_one');
  assert.equal(clickArgs.x, 2); assert.equal(clickArgs.y, 3);
  const retry = { ...action, request_id: 'd'.repeat(32), action_id: 'd'.repeat(32) };
  const second = await broker.handle(retry);
  assert.equal(broker.publicState().pending_approval, null);
  assert.equal(second.error_code, 'recovery_observe_required'); assert.equal(calls, 3);
});

test('pointer contract preserves delivered pixels at 1x, Retina, and resized capture scales', async () => {
  for (const [width, height, scale, hasAX] of [[520, 452, 1, true], [1040, 904, 2, true], [780, 678, 2, true], [1040, 904, 2, false], [1568, 1100, 2, false]]) {
    const writes = [];
    let capture = 0;
    const bounds = { x: 496, y: 137, width: 520, height: 452 };
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-pixel-contract-')),
      driver: { call: async (tool, args) => {
        if (tool === 'check_permissions') return permissions;
        if (tool === 'list_windows') return { windows: [{ window_id: 7, bounds }] };
        if (tool === 'get_window_state') {
          assert.equal(args.max_dimension, 1024);
          return {
          capture_id: `capture-${++capture}`, window_bounds: bounds,
          screenshot_width: width, screenshot_height: height, screenshot_scale: scale,
          _image: fakePng(width, height), elements: hasAX ? [{ role: 'AXButton', label: 'Item',
            frame: { x: 535, y: 326, w: 162, h: 34 }, screenshot_frame: { x: 78, y: 378, w: 324, h: 68 } }] : [],
          };
        }
        writes.push({ tool, args }); return {};
      } } }));
    const payload = { task_id: 'pixels', app: { bundle_id: 'fixture.app' }, window: { window_id: 7 }, delivery_mode: 'foreground' };
    const request = (operation, args) => ({ ...message(broker, operation, args), request_id: crypto.randomBytes(16).toString('hex') });
    const observe = async () => (await requestAndApprove(broker, request('observe', payload), 'pixels', 'observe')).result;
    const first = await observe();
    assert.equal(first.coordinate_contract.reference_width, width);
    assert.equal(first.coordinate_contract.coordinate_space, 'normalized');
    assert.equal(first.coordinate_contract.reference_height, height);
    assert.equal(first.coordinate_contract.observation_ref, first.observation_ref);
    assert.equal(first.window_bounds, undefined);
    assert.equal(first.screenshot_scale, undefined);
    assert.equal(first.actionable_elements_available, hasAX);
    if (hasAX) {
      assert.equal(first.elements[0].frame, undefined);
      assert.ok(first.elements[0].screenshot_frame);
    }
    const wrong = await requestAndApprove(broker, request('double_click', { ...payload,
      observation_id: first.observation_ref, x: 0.2308, y: 0.4558, coordinate_space: 'normalized',
      reference_width: width === 1040 ? 520 : width - 1, reference_height: height === 904 ? 452 : height,
    }), 'pixels', 'double_click');
    assert.equal(wrong.error_code, 'coordinate_contract_mismatch');
    assert.equal(writes.length, 0);
    for (const coordinate_space of ['normalized', 'pixel']) {
      const fresh = await observe();
      const result = await requestAndApprove(broker, request('double_click', { ...payload,
        observation_id: fresh.observation_ref, x: coordinate_space === 'pixel' ? width * 0.2308 : 0.2308,
        y: coordinate_space === 'pixel' ? height * 0.4558 : 0.4558,
        coordinate_space, reference_width: width, reference_height: height,
      }), 'pixels', 'double_click');
      assert.equal(result.status, 'completed_known');
      assert.equal(writes.at(-1).args.x, width * 0.2308);
      assert.equal(writes.at(-1).args.y, height * 0.4558);
      assert.equal(writes.at(-1).args.window_id, 7);
      assert.equal(writes.at(-1).args.reference_width, undefined);
    }
    const stale = first.observation_ref;
    await observe();
    const refused = await requestAndApprove(broker, request('double_click', { ...payload,
      observation_id: stale, x: 1, y: 1, coordinate_space: 'pixel', reference_width: width, reference_height: height,
    }), 'pixels', 'double_click');
    assert.equal(refused.error_code, 'stale_capture');
    assert.deepEqual(writes.map(call => call.tool), ['bring_to_front', 'double_click', 'bring_to_front', 'double_click']);
  }
});

test('known unverifiable receipt requires a fresh observation but does not halt later planning', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-unverifiable-'));
  let writes = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [7, 8].map(window_id => ({ pid: 42, window_id, title: 'Fixture', bounds: { x: 0, y: 0, width: 100, height: 100 }, is_on_screen: true })) };
    if (tool === 'get_window_state') return { elements: [{ element_token: 's00000001:1', label: 'Fixture' }] };
    if (tool === 'press_key') {
      writes += 1;
      return { action_receipt: { effect: 'unverifiable', delivery: 'background', verification: 'pending' } };
    }
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: '4'.repeat(32) });
  const targetRef = windows.result.windows[0].target_ref;
  const observed = await requestAndApprove(broker, { ...message(broker, 'observe', { task_id: 'task', target_ref: targetRef }), request_id: '5'.repeat(32) }, 'task', 'observe');
  const firstPayload = { task_id: 'task', target_ref: targetRef, observation_id: observed.result.observation_ref, key: 'escape' };
  const first = await requestAndApprove(broker, { ...message(broker, 'press_key', firstPayload), request_id: '6'.repeat(32) }, 'task', 'press_key');
  assert.equal(first.status, 'completed_known'); assert.equal(first.error_code, ''); assert.equal(first.outcome_known, true);
  assert.equal(first.result.action_receipt.verification, 'pending');
  assert.equal(broker.recovery.get('task').attempts, 0);
  const premature = await broker.handle(message(broker, 'press_key', { ...firstPayload, key: 'tab' }));
  assert.equal(premature.error_code, 'recovery_observe_required');
  assert.equal(broker.publicState().pending_approval, null);
  assert.equal(writes, 1);
  const other = await requestAndApprove(broker, message(broker, 'observe', { task_id: 'task', target_ref: windows.result.windows[1].target_ref }), 'task', 'observe');
  assert.equal(other.status, 'rejected');
  assert.equal(other.executed, false);
  assert.equal(broker.recovery.get('task').needsObserve, true);
  const fresh = await requestAndApprove(broker, { ...message(broker, 'observe', { task_id: 'task', target_ref: targetRef }), request_id: '7'.repeat(32) }, 'task', 'observe');
  assert.equal(fresh.status, 'completed_known');
  const continued = await requestAndApprove(broker, { ...message(broker, 'press_key', { task_id: 'task', target_ref: targetRef, observation_id: fresh.result.observation_ref, key: 'tab' }), request_id: '8'.repeat(32) }, 'task', 'press_key');
  assert.equal(continued.status, 'completed_known', JSON.stringify(continued)); assert.equal(writes, 2);
  assert.equal(broker.recovery.get('task').attempts, 0);
  const restored = new ComputerBroker({ runtimeRoot: root });
  assert.deepEqual(restored.requests.get(first.action_id).result.action_receipt, first.result.action_receipt);
});

async function readLine(socket) {
  return new Promise((resolve, reject) => {
    let text = '';
    socket.on('data', chunk => { text += chunk; if (text.includes('\n')) resolve(JSON.parse(text.slice(0, text.indexOf('\n')))); });
    socket.once('error', reject); socket.once('close', () => { if (!text.includes('\n')) reject(new Error('closed')); });
  });
}

test('socket challenge is per-connection and a replayed digest is rejected', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async () => ({}) } });
  try { await broker.start(); }
  catch (error) { if (error.code === 'EPERM') return t.skip('sandbox blocks Unix socket listen'); throw error; }
  const first = net.createConnection(broker.socketPath); const challenge = await readLine(first);
  const digest = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(`1:${challenge.nonce}`).digest('hex');
  first.write(JSON.stringify({ type: 'authenticate', protocol: 1, digest }) + '\n');
  assert.equal((await readLine(first)).type, 'welcome'); first.destroy();
  const replay = net.createConnection(broker.socketPath); await readLine(replay);
  replay.write(JSON.stringify({ type: 'authenticate', protocol: 1, digest }) + '\n');
  await assert.rejects(readLine(replay));
  await broker.stop();
});

test('invalid coordinates fail before dispatch and a crash leaves dispatched work unknown', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const ledgerPath = path.join(root, 'ledger.jsonl');
  let calls = 0; const broker = new ComputerBroker({ runtimeRoot: root, ledgerPath, driver: { call: async () => { calls += 1; return {}; } } });
  ready(broker);
  const invalid = await broker.handle(message(broker, 'click_at', { task_id: 'task', app: { bundle_id: 'fixture.app' }, x: NaN, y: 1, delivery_mode: 'background' }));
  assert.equal(invalid.error_code, 'protocol_error'); assert.equal(calls, 0);
  fs.mkdirSync(path.dirname(ledgerPath), { recursive: true });
  fs.appendFileSync(ledgerPath, JSON.stringify({ request_id: 'f'.repeat(32), action_id: 'f'.repeat(32), task_id: 'task', operation: 'type_text', status: 'dispatching', epoch: 1, generation: 1 }) + '\n');
  const recovered = new ComputerBroker({ runtimeRoot: root, ledgerPath, driver: { call: async () => { throw new Error('must not replay'); } } });
  const response = await recovered.handle({ ...message(recovered, 'type_text', { task_id: 'task', text: 'already-dispatched' }), request_id: 'f'.repeat(32), action_id: 'f'.repeat(32) });
  assert.equal(response.status, 'outcome_unknown'); assert.equal(response.result.recovered, true);
});

test('discovery is implicit read-only but cannot authorize app actions', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); let calls = 0; let driverArgs;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => { if (tool === 'check_permissions') return permissions; calls += 1; driverArgs = args; return { apps: [] }; } } });
  ready(broker);
  const discovery = { task_id: 'task', delivery_mode: 'background' };
  const listed = await broker.handle(message(broker, 'list_apps', discovery));
  assert.equal(listed.status, 'completed_known'); assert.equal(calls, 1);
  assert.deepEqual(Object.keys(driverArgs), ['session']);
  const action = await broker.handle({ ...message(broker, 'click_element', { task_id: 'task', app: { bundle_id: 'fixture.app' }, element_id: 'x', delivery_mode: 'background' }), request_id: '9'.repeat(32) });
  assert.equal(action.error_code, 'observation_contract_required');
  assert.equal(broker.publicState().pending_approval, null); assert.equal(calls, 1);
});

test('supported task apps reject unscoped first actions instead of prompting per action', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } });
  ready(broker);
  const result = await broker.handle(message(broker, 'activate_app', { task_id: 'task', app: { bundle_id: 'com.netease.163music' }, delivery_mode: 'background' }));
  assert.equal(result.error_code, 'task_authorization_required');
  assert.equal(broker.publicState().pending_approval, null);
});

test('driver arguments exclude broker identity fields and translate action schemas', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args }); return {};
  } } });
  ready(broker);
  const base = {
    task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' },
    window: { handle: '7', title: 'Private title' }, delivery_mode: 'background',
  };
  for (const [index, [operation, extra]] of [
    ['type_text', { text: 'value' }],
    ['scroll', { delta_y: -3 }],
  ].entries()) {
    const payload = { ...base, ...extra };
    await requestAndApprove(broker, { ...message(broker, operation, payload), request_id: String(index + 1).repeat(32) }, 'task', operation);
  }
  assert.deepEqual(calls[0], { tool: 'type_text', args: {
    session: calls[0].args.session, pid: 42, window_id: 7,
    delivery_mode: 'background', text: 'value',
  } });
  assert.deepEqual(calls[1], { tool: 'scroll', args: {
    session: calls[1].args.session, pid: 42, window_id: 7,
    delivery_mode: 'background', direction: 'up', amount: 3,
  } });
  assert.equal(JSON.stringify(calls).includes('process_start_identity'), false);
  assert.equal(JSON.stringify(calls).includes('Private title'), false);
});

test('background activation launches once and leaves a running app in the background', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, running: true, process_start_identity: 'start-42' }] };
    if (tool === 'launch_app') return { pid: 41, process_start_identity: 'start-41' };
    return {};
  } } });
  ready(broker);
  for (const [index, app] of [{ bundle_id: 'fixture.app', pid: 0 }, { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-42' }].entries()) {
    const taskId = `task-${index}`;
    const payload = { task_id: taskId, app, delivery_mode: 'background' };
    await requestAndApprove(broker, { ...message(broker, 'activate_app', payload), request_id: String(index + 1).repeat(32) }, taskId, 'activate_app');
  }
  assert.deepEqual(calls[0], { tool: 'launch_app', args: {
    session: calls[0].args.session, bundle_id: 'fixture.app',
  } });
  assert.equal(calls[1].tool, 'list_apps');
  assert.equal(calls.some(call => call.tool === 'bring_to_front'), false);
});

test('activate_app binds the sole visible window before foreground dispatch', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, running: true, process_start_identity: 'start-42' }] };
    if (tool === 'list_windows') return { windows: [
      { pid: 42, window_id: 7, is_on_screen: true, on_current_space: true, bounds: { x: 0, y: 0, width: 520, height: 292 } },
      { pid: 99, window_id: 8, is_on_screen: true, on_current_space: true, bounds: { x: 0, y: 0, width: 100, height: 100 } },
    ] };
    return {};
  } } });
  ready(broker);
  const pending = broker.handle(message(broker, 'activate_app', {
    task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-42' }, delivery_mode: 'foreground',
  }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(broker.publicState().pending_approval.action_summary.window, '7');
  approve(broker, 'task', 'activate_app');
  assert.equal((await pending).status, 'completed_known');
  assert.deepEqual(calls.map(call => call.tool), ['list_apps', 'list_windows', 'list_apps', 'list_windows', 'bring_to_front']);
  assert.equal(calls.at(-1).args.pid, 42);
  assert.equal(calls.at(-1).args.window_id, 7);
});

test('activate_app refuses genuinely ambiguous windows with an actionable read', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool) => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, running: true, process_start_identity: 'start-42' }] };
    if (tool === 'list_windows') return { windows: [7, 8].map(window_id => ({
      pid: 42, window_id, is_on_screen: true, on_current_space: true,
      bounds: { x: 0, y: 0, width: 100, height: 100 },
    })) };
    throw new Error('write must not dispatch');
  } } });
  ready(broker);
  const result = await broker.handle(message(broker, 'activate_app', {
    task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-42' }, delivery_mode: 'foreground',
  }));
  assert.equal(result.status, 'failed_known');
  assert.equal(result.error_code, 'target_ambiguous');
  assert.equal(result.result.candidate_count, 2);
  assert.equal(result.result.next_operation, 'select_window_target_ref');
  assert.equal(result.result.candidates.length, 2);
  assert.equal(result.result.recovery.tool, 'computer_control');
  assert.deepEqual(result.result.recovery.candidates.map(candidate => candidate.target_ref), result.result.candidates.map(candidate => candidate.target_ref));
  assert.equal(broker.publicState().pending_approval, null);
});

test('selected A/B window persists only within the live approved process/task, never silently switches', async () => {
  for (const chosen of [7, 8]) {
    let app = { bundle_id: 'generic.app', pid: 42, process_start_identity: 'start-1' };
    let ids = [7, 8]; const writes = []; let revokeOnWindows = false; let closeOnFront = false;
    const bounds = { x: 0, y: 0, width: 50, height: 50 };
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-window-selection-')), driver: { call: async (tool, args) => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') {
        if (revokeOnWindows) broker.revoke();
        return { windows: ids.map(window_id => ({ pid: 42, window_id, bounds })) };
      }
      if (tool === 'get_window_state') return { capture_id: crypto.randomBytes(8).toString('hex'), window_id: args.window_id, elements: [], window_bounds: bounds,
        screenshot_width: 100, screenshot_height: 100, _image: fakePng(100, 100) };
      if (closeOnFront && tool === 'bring_to_front') throw Object.assign(new Error('window closed'), { code: 'failed_known', safeCode: 'target_not_found' });
      writes.push({ tool, args }); return {};
    } } }));
    const intent = { task_id: 'task', text: '在选定窗口完成任务', turn_id: 'turn-human', user_message_id: 'human' };
    const proof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(intent)).digest('hex');
    const request = (operation, args = {}) => ({ ...message(broker, operation, { task_id: 'task', task_consent_required: true,
      task_intent: intent, task_intent_proof: proof, delivery_mode: 'foreground', ...args }), request_id: crypto.randomBytes(16).toString('hex') });
    const appRef = (await broker.handle(request('list_apps'))).result.apps[0].target_ref;
    const ambiguous = await broker.handle(request('activate_app', { target_ref: appRef }));
    assert.equal(ambiguous.error_code, 'target_ambiguous'); assert.equal(writes.length, 0);
    const windowRef = ambiguous.result.candidates[chosen === 7 ? 0 : 1].target_ref;
    const observing = broker.handle(request('observe', { target_ref: windowRef }));
    await new Promise(resolve => setImmediate(resolve));
    broker.grantPending({ ...broker.publicState().pending_approval, navigation: true });
    assert.equal((await observing).status, 'completed_known');
    assert.equal((await broker.handle(request('activate_app', { target_ref: appRef }))).status, 'completed_known');
    assert.equal(writes.at(-1).args.window_id, chosen);
    assert.equal(broker.publicState().pending_approval, null);
    closeOnFront = true;
    assert.equal((await broker.handle(request('activate_app', { target_ref: windowRef }))).error_code, 'target_not_found');
    assert.equal(writes.length, 1); closeOnFront = false;
    assert.equal(broker.taskGrants.has('task'), false);
    assert.equal((await requestAndApprove(broker, request('observe', { target_ref: windowRef }), 'task', 'observe')).status, 'completed_known');
    ids = ids.filter(id => id !== chosen);
    assert.equal((await broker.handle(request('activate_app', { target_ref: appRef }))).error_code, 'target_not_found');
    assert.equal(writes.length, 1);
    ids = [7, 8]; app = { ...app, process_start_identity: 'start-2' };
    assert.equal((await broker.handle(request('activate_app', { target_ref: windowRef }))).error_code, 'target_not_found');
    assert.equal(writes.length, 1);
    app = { ...app, process_start_identity: 'start-1' };
    const changedIntent = { ...intent, turn_id: 'turn-other', user_message_id: 'other' };
    const changedProof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(changedIntent)).digest('hex');
    assert.equal((await broker.handle(request('activate_app', { target_ref: appRef,
      task_intent: changedIntent, task_intent_proof: changedProof }))).error_code, 'task_intent_invalid');
    const scope = broker.taskGrants.get('task');
    broker.taskGrants.delete('task');
    assert.equal((await broker.handle(request('activate_app', { target_ref: appRef }))).error_code, 'target_ambiguous');
    broker.taskGrants.set('task', scope); revokeOnWindows = true;
    assert.equal((await broker.handle(request('activate_app', { target_ref: appRef }))).error_code, 'control_revoked');
    assert.equal(broker.publicState().pending_approval, null);
    assert.equal(broker.grants.size, 0);
    assert.equal((await broker.handle(request('activate_app', { target_ref: windowRef }))).error_code, 'target_not_found');
    assert.equal(writes.length, 1);
  }
});

test('foreground activation accepts one trusted hidden or cross-space window', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-hidden-window-'));
  const calls = [];
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, running: true, process_start_identity: 'start-42' }] };
    if (tool === 'list_windows') return { windows: [{
      pid: 42, window_id: 7, is_on_screen: false, on_current_space: false, minimized: true,
      bounds: { x: 0, y: 0, width: 520, height: 292 },
    }] };
    return {};
  } } }));
  const request = message(broker, 'activate_app', {
    task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-42' }, delivery_mode: 'foreground',
  });
  const result = await requestAndApprove(broker, request, 'task', 'activate_app');
  assert.equal(result.status, 'completed_known');
  assert.equal(calls.at(-1).tool, 'bring_to_front');
  assert.equal(calls.at(-1).args.window_id, 7);
});

test('cold launch waits boundedly for its exact window before foregrounding', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-delayed-window-'));
  let windowReads = 0;
  const calls = [];
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-42' };
    if (tool === 'list_windows') {
      windowReads += 1;
      return windowReads < 3 ? { windows: [] } : { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 520, height: 292 } }] };
    }
    return {};
  } } }));
  const result = await requestAndApprove(broker, message(broker, 'activate_app', {
    task_id: 'task', app: { bundle_id: 'fixture.app' }, delivery_mode: 'foreground',
  }), 'task', 'activate_app');
  assert.equal(result.status, 'completed_known');
  assert.equal(windowReads, 3);
  assert.equal(calls.at(-1).tool, 'bring_to_front');
  assert.equal(calls.at(-1).args.window_id, 7);
});

test('activate_app recovers a definitely stale pid through the same approved bundle only', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [] };
    if (tool === 'launch_app') return { pid: 43, process_start_identity: 'start-43' };
    return {};
  } } });
  ready(broker);
  await requestAndApprove(broker, message(broker, 'activate_app', { task_id: 'task', app: { bundle_id: 'fixture.app', pid: 999, process_start_identity: 'old' }, delivery_mode: 'background' }), 'task', 'activate_app');
  assert.deepEqual(calls.map(call => call.tool), ['list_apps', 'launch_app']);
  assert.equal(calls[1].args.bundle_id, 'fixture.app');
});

test('foreground bundle activation promotes the launched pid for the next scoped observe', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-42' };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 520, height: 292 } }] };
    if (tool === 'get_window_state') return { summary: 'visible' };
    return {};
  } } });
  ready(broker);
  const authorization = { purpose: 'media_search_play', display_name: 'untrusted', target_label: 'untrusted', query: '山丘' };
  const first = broker.handle(message(broker, 'activate_app', {
    task_id: 'scope-task', app: { bundle_id: 'com.netease.163music' }, authorization, delivery_mode: 'foreground',
  }));
  await new Promise(resolve => setImmediate(resolve)); approve(broker, 'scope-task', 'activate_app');
  assert.equal((await first).status, 'completed_known');
  const observed = await broker.handle({ ...message(broker, 'observe', {
    task_id: 'scope-task', app: { bundle_id: 'com.netease.163music' }, window: { window_id: 7 }, delivery_mode: 'foreground',
  }), request_id: 'c'.repeat(32) });
  assert.equal(observed.status, 'completed_known');
  assert.deepEqual(calls.map(call => call.tool), ['launch_app', 'list_windows', 'bring_to_front', 'get_window_state']);
  assert.equal(calls[2].args.pid, 42);
  assert.equal(calls[3].args.pid, 42);
});

test('installed discovery reference gains verified process identity after foreground activation', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-installed-ref-'));
  const calls = [];
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', name: 'Fixture', running: false }] };
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-42', launch_state: { window_ready: false } };
    if (tool === 'list_windows') return { windows: [
      { pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 520, height: 292 } },
      { pid: 43, window_id: 8, bounds: { x: 0, y: 0, width: 520, height: 292 } },
    ] };
    if (tool === 'get_window_state') return { summary: 'visible' };
    return {};
  } } }));
  let sequence = 0;
  const request = (operation, payload) => ({ ...message(broker, operation, { task_id: 'task', ...payload }), request_id: (++sequence).toString(16).padStart(32, '0') });
  const discovered = await broker.handle(request('list_apps', { query: 'Fixture' }));
  const target_ref = discovered.result.apps[0].target_ref;
  const activated = await requestAndApprove(broker, request('activate_app', { target_ref, delivery_mode: 'foreground' }), 'task', 'activate_app');
  assert.equal(activated.status, 'completed_known');
  assert.equal(activated.result.launch_state.window_ready, true);
  assert.match(activated.result.window_target_ref, /^target_/);
  const windows = await broker.handle(request('list_windows', { target_ref }));
  assert.equal(windows.result.windows.length, 1);
  assert.equal(calls.at(-1).args.pid, 42);
  assert.equal(windows.result.windows[0].target_ref, activated.result.window_target_ref);
  const observed = await requestAndApprove(broker, request('observe', { target_ref: windows.result.windows[0].target_ref }), 'task', 'observe');
  assert.equal(observed.status, 'completed_known');
  assert.equal(calls.at(-1).args.pid, 42);
  assert.equal(calls.at(-1).args.window_id, 7);
  assert.equal((await broker.handle(request('observe', { target_ref }))).error_code, 'target_kind_mismatch');
  assert.equal((await broker.handle(request('list_windows', { target_ref, task_id: 'other-task' }))).error_code, 'target_not_found');
});

test('known foreground verification failure does not report activation complete', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-42' };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 520, height: 292 } }] };
    if (tool === 'bring_to_front') throw Object.assign(new Error('not confirmed'), { code: 'failed_known', safeCode: 'failed_known' });
    if (tool === 'get_window_state') return { summary: 'visible' };
    return {};
  } } });
  ready(broker);
  const authorization = { purpose: 'media_search_play', display_name: 'untrusted', target_label: 'untrusted', query: '山丘' };
  const first = broker.handle(message(broker, 'activate_app', {
    task_id: 'scope-task', app: { bundle_id: 'com.netease.163music' }, authorization, delivery_mode: 'foreground',
  }));
  await new Promise(resolve => setImmediate(resolve)); approve(broker, 'scope-task', 'activate_app');
  const activated = await first;
  assert.equal(activated.status, 'failed_known');
  assert.equal(activated.error_code, 'failed_known');
  assert.equal(broker.taskGrants.has('scope-task'), false);
});

test('task authorization cannot start on a write without an observation', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } });
  ready(broker);
  const result = await broker.handle(message(broker, 'press_key', {
    task_id: 'scope-task', app: { bundle_id: 'com.netease.163music' }, key: 'f', modifiers: ['cmd'], delivery_mode: 'foreground',
    authorization: { purpose: 'media_search_play', display_name: 'untrusted', target_label: 'untrusted', query: '山丘' },
  }));
  assert.equal(result.error_code, 'task_authorization_required');
  assert.equal(broker.publicState().pending_approval, null);
});

test('app-bound operations never prompt against an unbound discovered target', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } });
  ready(broker);
  const result = await broker.handle(message(broker, 'click_at', { task_id: 'task', x: 10, y: 20, coordinate_space: 'pixel', reference_width: 100, reference_height: 100, delivery_mode: 'foreground' }));
  assert.equal(result.error_code, 'target_not_found');
  assert.equal(broker.publicState().pending_approval, null);
});

test('stale pid recovery keeps the approved task scope for a fresh foreground observe', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [] };
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-42' };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 520, height: 292 } }] };
    if (tool === 'get_window_state') return { summary: 'fresh window' };
    return {};
  } } });
  ready(broker);
  const authorization = { purpose: 'media_search_play', display_name: 'untrusted', target_label: 'untrusted', query: '山丘' };
  const first = broker.handle(message(broker, 'activate_app', {
    task_id: 'scope-task', app: { bundle_id: 'com.netease.163music', pid: 999, process_start_identity: 'old-start' },
    authorization, delivery_mode: 'foreground',
  }));
  await new Promise(resolve => setImmediate(resolve)); approve(broker, 'scope-task', 'activate_app');
  assert.equal((await first).status, 'completed_known');
  const observed = await broker.handle({ ...message(broker, 'observe', {
    task_id: 'scope-task', delivery_mode: 'foreground',
  }), request_id: 'b'.repeat(32) });
  assert.equal(observed.status, 'completed_known');
  assert.equal(broker.publicState().pending_approval, null);
  assert.deepEqual(calls.map(call => call.tool), ['list_apps', 'launch_app', 'list_windows', 'bring_to_front', 'list_windows', 'get_window_state']);
  assert.equal(calls[5].args.pid, 42);
});

test('press_key normalizes a chord to the driver key and modifiers schema', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args }); return {};
  } } });
  ready(broker);
  const payload = { task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42 }, window: { window_id: 7 }, key: 'cmd+f', delivery_mode: 'foreground' };
  await requestAndApprove(broker, { ...message(broker, 'press_key', payload), request_id: 'a'.repeat(32) }, 'task', 'press_key');
  assert.equal(calls[0].tool, 'bring_to_front');
  assert.deepEqual(calls[1], { tool: 'press_key', args: {
    session: calls[1].args.session, pid: 42, window_id: 7,
    delivery_mode: 'foreground', key: 'f', modifiers: ['cmd'],
  } });
});

test('press_key preserves explicit modifiers from the public tool schema and approval summary', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args }); return {};
  } } });
  ready(broker);
  const payload = { task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42 }, window: { window_id: 7 }, key: 'f', modifiers: ['cmd'], delivery_mode: 'background' };
  const pending = broker.handle({ ...message(broker, 'press_key', payload), request_id: 'b'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve));
  const state = broker.publicState();
  assert.deepEqual(state.pending_approval.action_summary.modifiers, ['cmd']);
  broker.grantPending({ task_id: 'task', operation: 'press_key', approval_id: state.pending_approval.approval_id, parameter_digest: state.pending_approval.parameter_digest });
  await pending;
  assert.deepEqual(calls[0].args.modifiers, ['cmd']);
  assert.equal(calls[0].args.key, 'f');
});

test('legacy task scope reuses reads/text but unknown controls and coordinates need exact approval', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'com.netease.163music', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, is_on_screen: true, on_current_space: true, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-1' };
    return tool === 'get_window_state' ? { capture_id: 'cap', elements: [{ id: 'result-row' }], window_bounds: { x: 0, y: 0, width: 50, height: 50 }, screenshot_width: 100, screenshot_height: 100, screenshot_scale: 2, _image: fakePng(100, 100) } : {};
  } } });
  ready(broker);
  const app = { bundle_id: 'com.netease.163music', process_start_identity: 'start-1' };
  const authorization = { purpose: 'media_search_play', display_name: '网易云音乐', target_label: '中搜索并播放《山丘》', query: '山丘' };
  const discovered = await broker.handle({ ...message(broker, 'list_apps', { task_id: 'scope-task' }), request_id: '0f'.repeat(16) });
  const taskTargetRef = discovered.result.apps[0].target_ref;
  const firstPromise = broker.handle(message(broker, 'activate_app', { task_id: 'scope-task', target_ref: taskTargetRef, authorization, delivery_mode: 'background' }));
  await new Promise(resolve => setImmediate(resolve));
  const approval = broker.publicState().pending_approval;
  assert.match(approval.action_summary.title, /网易云音乐.*山丘/);
  approve(broker, 'scope-task', 'activate_app');
  const activated = await firstPromise;
  assert.equal(activated.status, 'completed_known');
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'scope-task', target_ref: taskTargetRef, delivery_mode: 'background' }), request_id: 'af'.repeat(16) });
  const windowTargetRef = windows.result.windows[0].target_ref;
  const exactAction = request => requestAndApprove(broker, request, 'scope-task', request.operation);
  const observation = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', target_ref: windowTargetRef, delivery_mode: 'background' }), request_id: 'b'.repeat(32) });
  assert.equal(observation.status, 'completed_known');
  assert.equal(calls.at(-1).args.window_id, 7);
  assert.equal(broker.publicState().pending_approval, null);
  const typed = await broker.handle({ ...message(broker, 'type_text', { task_id: 'scope-task', app, window: { window_id: 7 }, text: '山丘', delivery_mode: 'background' }), request_id: 'c'.repeat(32) });
  assert.equal(typed.status, 'completed_known');
  assert.equal(broker.publicState().pending_approval, null);
  const beforeClick = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', target_ref: windowTargetRef, delivery_mode: 'background' }), request_id: 'd'.repeat(32) });
  assert.equal(beforeClick.status, 'completed_known');
  assert.equal(beforeClick.result.target_ref, windowTargetRef);
  assert.match(beforeClick.result.observation_ref, /^obs_[a-f0-9]{32}$/);
  const clicked = await exactAction({ ...message(broker, 'click_element', { task_id: 'scope-task', target_ref: beforeClick.result.target_ref, observation_id: beforeClick.result.observation_ref, element_id: 'result-row', delivery_mode: 'background' }), request_id: 'e'.repeat(32) });
  assert.equal(clicked.status, 'completed_known', JSON.stringify(clicked));
  assert.equal(clicked.result.target_ref, beforeClick.result.target_ref);
  assert.equal(clicked.audit.target_ref, beforeClick.result.target_ref);
  assert.equal(broker.publicState().pending_approval, null);
  const beforeDouble = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', app, window: { window_id: 7 }, delivery_mode: 'background' }), request_id: '1'.repeat(32) });
  assert.equal(beforeDouble.status, 'completed_known');
  const doubled = await exactAction({ ...message(broker, 'double_click', { task_id: 'scope-task', app, window: { window_id: 7 }, observation_id: beforeDouble.result.observation_ref, element_id: 'result-row', delivery_mode: 'background' }), request_id: '2'.repeat(32) });
  assert.equal(doubled.status, 'completed_known');
  assert.equal(calls.at(-1).tool, 'double_click');
  assert.equal(calls.at(-1).args.element_token, 'result-row');
  const beforeCoordinateDouble = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', app, window: { window_id: 7 }, delivery_mode: 'background' }), request_id: '3'.repeat(32) });
  assert.equal(beforeCoordinateDouble.status, 'completed_known');
  const coordinateDouble = await exactAction({ ...message(broker, 'double_click', { task_id: 'scope-task', app, x: 12, y: 14, coordinate_space: 'pixel', reference_width: 100, reference_height: 100, delivery_mode: 'background' }), request_id: '4'.repeat(32) });
  assert.equal(coordinateDouble.status, 'completed_known');
  assert.equal(calls.at(-1).tool, 'double_click');
  // Dedicated Cua double_click validates against the Host capture but its
  // upstream schema accepts coordinates, not capture_id.
  assert.equal(calls.at(-1).args.capture_id, undefined);
  assert.equal(calls.at(-1).args.x, 12);
  assert.equal(calls.at(-1).args.y, 14);
  assert.equal(calls.at(-1).args.element_token, undefined);
  const beforeRight = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', app, window: { window_id: 7 }, delivery_mode: 'background' }), request_id: '67'.repeat(16) });
  const right = await exactAction({ ...message(broker, 'right_click', { task_id: 'scope-task', app, window: { window_id: 7 }, observation_id: beforeRight.result.observation_ref, element_id: 'result-row', delivery_mode: 'background' }), request_id: '78'.repeat(16) });
  assert.equal(right.status, 'completed_known');
  assert.equal(calls.at(-1).tool, 'right_click');
  assert.equal(calls.at(-1).args.element_token, 'result-row');
  assert.equal(calls.at(-1).args.capture_id, undefined);
  const beforeMenu = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', app, window: { window_id: 7 }, delivery_mode: 'background' }), request_id: '89'.repeat(16) });
  const menu = await exactAction({ ...message(broker, 'open_menu', { task_id: 'scope-task', app, window: { window_id: 7 }, observation_id: beforeMenu.result.observation_ref, element_id: 'result-row', delivery_mode: 'background' }), request_id: 'ab'.repeat(16) });
  assert.equal(menu.status, 'completed_known');
  assert.equal(calls.at(-1).tool, 'right_click');
  assert.equal(calls.at(-1).args.element_token, 'result-row');
  assert.equal(calls.at(-1).args.action, undefined);
  const beforeSelect = await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', app, window: { window_id: 7 }, delivery_mode: 'background' }), request_id: 'bc'.repeat(16) });
  const selected = await exactAction({ ...message(broker, 'select_option', { task_id: 'scope-task', app, window: { window_id: 7 }, observation_id: beforeSelect.result.observation_ref, element_id: 'result-row', delivery_mode: 'background' }), request_id: 'cd'.repeat(16) });
  assert.equal(selected.status, 'completed_known');
  assert.equal(calls.at(-1).tool, 'click');
  assert.equal(calls.at(-1).args.action, 'pick');
  assert.equal((await broker.handle({ ...message(broker, 'observe', { task_id: 'scope-task', app, window: { window_id: 7 }, delivery_mode: 'background' }), request_id: '5'.repeat(32) })).status, 'completed_known');
  const coordinateClick = await exactAction({ ...message(broker, 'click_at', { task_id: 'scope-task', app, x: 12, y: 14, coordinate_space: 'pixel', reference_width: 100, reference_height: 100, count: 2, delivery_mode: 'background' }), request_id: 'f'.repeat(32) });
  assert.equal(coordinateClick.status, 'completed_known');
  assert.equal(calls.at(-1).args.capture_id, 'cap');
  assert.equal(calls.at(-1).args.window_id, 7);
  assert.equal(calls.at(-1).args.count, 2);
  assert.equal(broker.publicState().pending_approval, null);
  const skippedObservation = await broker.handle({ ...message(broker, 'type_text', { task_id: 'scope-task', app, window: { window_id: 7 }, text: '山丘', delivery_mode: 'background' }), request_id: '9'.repeat(32) });
  assert.equal(skippedObservation.error_code, 'recovery_observe_required');
  assert.equal(broker.publicState().pending_approval, null);
  broker.revoke();
  const stale = await broker.handle({ ...message(broker, 'press_key', { task_id: 'scope-task', app, key: 'return', delivery_mode: 'background' }), request_id: '0'.repeat(32), control_epoch: broker.epoch - 1 });
  assert.equal(stale.error_code, 'control_revoked');
});

test('generic task consent reuses exact controls for arbitrary apps and fails closed at boundaries', async () => {
  for (const bundle of ['org.example.editor', 'org.example.other-app']) {
    let writes = 0;
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-generic-consent-'));
    const app = { bundle_id: bundle, name: 'Example editor', pid: 42, process_start_identity: 'start-1' };
    let elements = [
      { element_token: 's1:1', label: 'Item', enabled: true },
      { element_token: 's1:2', label: 'Query', enabled: true },
      { element_token: 's1:3', label: '发送', enabled: true },
    ];
    const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'get_window_state') return { elements };
      writes += 1; return {};
    } } }));
    const payload = { task_id: 'generic', app, window: { window_id: 7 }, delivery_mode: 'foreground' };
    const request = (operation, args) => ({ ...message(broker, operation, args), request_id: crypto.randomBytes(16).toString('hex') });
    const authorization = { purpose: 'app_task', target_label: '检查选项并输入指定查询', controls: [
      { label: 'Item', operations: ['double_click', 'right_click', 'open_menu', 'select_option'] },
      { label: 'Query', operations: ['set_value', 'type_text'], text: 'bounded query' },
      { label: '发送', operations: ['click_element'] },
    ] };
    const unscoped = await broker.handle(request('observe', { ...payload, task_consent_required: true }));
    assert.equal(unscoped.error_code, 'task_intent_required');
    assert.equal(broker.publicState().pending_approval, null);
    assert.equal(writes, 0);
    const first = broker.handle(request('observe', { ...payload, authorization }));
    await new Promise(resolve => setImmediate(resolve));
    assert.match(broker.publicState().pending_approval.action_summary.title, /Example editor/);
    assert.match(broker.publicState().pending_approval.action_summary.allowed, /bounded query/);
    approve(broker, 'generic', 'observe'); await first;
    assert.equal(broker.taskGrants.get('generic').window_handle, '7');
    const observe = async () => {
      const response = await broker.handle(request('observe', payload));
      assert.equal(response.status, 'completed_known');
      assert.equal(broker.publicState().pending_approval, null);
      return response.result.observation_ref;
    };
    for (const operation of ['double_click', 'right_click', 'open_menu', 'select_option', 'set_value', 'type_text']) {
      const observation_id = await observe();
      const input = ['set_value', 'type_text'].includes(operation);
      const response = await broker.handle(request(operation, { ...payload, observation_id,
        element_id: input ? 's1:2' : 's1:1', ...(input ? { text: 'bounded query' } : {}) }));
      assert.equal(response.status, 'completed_known', operation);
      assert.equal(broker.publicState().pending_approval, null);
    }
    assert.equal(writes, 6);
    const rejectBoundary = async (operation, extra) => {
      const observation_id = await observe();
      const before = writes;
      const pending = broker.handle(request(operation, { ...payload, ...(operation === 'observe' ? {} : { observation_id }), ...extra }));
      await new Promise(resolve => setImmediate(resolve));
      assert.ok(broker.publicState().pending_approval, JSON.stringify(extra));
      assert.equal(writes, before);
      const approval = broker.publicState().pending_approval;
      broker.rejectPending({ approval_id: approval.approval_id, parameter_digest: approval.parameter_digest });
      await pending;
    };
    await rejectBoundary('set_value', { element_id: 's1:2', text: 'different query' });
    await rejectBoundary('click_element', { element_id: 's1:3' });
    await rejectBoundary('press_key', { key: 'return' });
    await rejectBoundary('observe', { window: { window_id: 8 } });
    await rejectBoundary('observe', { app: { ...app, bundle_id: 'outside.app' } });
    await rejectBoundary('observe', { app: { ...app, process_start_identity: 'start-2' } });
    await rejectBoundary('observe', { app: { ...app, pid: 43 } });
    await rejectBoundary('observe', { delivery_mode: 'background' });
    await rejectBoundary('observe', { task_id: 'different-task' });
    elements = [...elements, { element_token: 's1:4', label: 'Item', enabled: true }];
    await rejectBoundary('double_click', { element_id: 's1:1' });
    await broker.handle(request('end_task', { task_id: 'generic', terminal_id: 'done' }));
    assert.equal(broker.taskGrants.size, 0);
    assert.equal(broker.observations.size, 0);
  }
});

test('task scope does not cross task, app, process identity, mode, or bounded query', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'start-1' };
    return {};
  } } });
  ready(broker);
  const app = { bundle_id: 'com.netease.163music', process_start_identity: 'start-1' };
  const authorization = { purpose: 'media_search_play', display_name: '网易云音乐', target_label: '中搜索并播放《山丘》', query: '山丘' };
  const observationId = `obs_${'1'.repeat(32)}`;
  const first = broker.handle(message(broker, 'activate_app', { task_id: 'scope-task', app, authorization, delivery_mode: 'background' }));
  await new Promise(resolve => setImmediate(resolve)); approve(broker, 'scope-task', 'activate_app'); await first;
  const crossTask = await broker.handle({ ...message(broker, 'type_text', { task_id: 'other-task', app, observation_id: observationId, text: '山丘', delivery_mode: 'background' }), request_id: '1'.repeat(32) });
  assert.equal(crossTask.error_code, 'task_authorization_required');
  assert.equal(broker.publicState().pending_approval, null);
  for (const [index, payload] of [
    { task_id: 'scope-task', app: { ...app, bundle_id: 'other.app' }, observation_id: observationId, text: '山丘', delivery_mode: 'background' },
    { task_id: 'scope-task', app: { ...app, process_start_identity: 'start-2' }, observation_id: observationId, text: '山丘', delivery_mode: 'background' },
    { task_id: 'scope-task', app, observation_id: observationId, text: '购买会员', delivery_mode: 'background' },
    { task_id: 'scope-task', app, observation_id: observationId, text: '山丘', delivery_mode: 'foreground' },
  ].entries()) {
    const pending = broker.handle({ ...message(broker, 'type_text', payload), request_id: String(index + 2).repeat(32) });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(broker.publicState().status, 'waiting_for_approval');
    broker.rejectPending({ approval_id: broker.publicState().pending_approval.approval_id, parameter_digest: broker.publicState().pending_approval.parameter_digest });
    await pending;
  }
});

test('document scope canonicalizes one root, covers Finder and Preview once, and rejects outside paths', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const fixtureRoot = path.join(root, 'fixtures'); fs.mkdirSync(fixtureRoot);
  const fixture = path.join(fixtureRoot, 'acceptance.pdf'); fs.writeFileSync(fixture, 'safe fixture');
  const outside = path.join(root, 'private.pdf'); fs.writeFileSync(outside, 'outside');
  const calls = [];
  const broker = new ComputerBroker({ runtimeRoot: path.join(root, 'runtime'), driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, args });
    if (tool === 'launch_app') return {
      pid: args.bundle_id === 'com.apple.finder' ? 41 : 42,
      process_start_identity: args.bundle_id === 'com.apple.finder' ? 'finder-1' : 'preview-1',
    };
    return tool === 'get_window_state' ? { capture_id: `cap-${calls.length}`, geometry: { x: 0, y: 0, width: 50, height: 50 }, display: { id: 'd', scale: 2 }, coordinate_transform: { source: 'capture_pixels', target: 'screen_points' } } : {};
  } } });
  ready(broker);
  const finder = { bundle_id: 'com.apple.finder', pid: 41, process_start_identity: 'finder-1' };
  const preview = { bundle_id: 'com.apple.Preview', pid: 42, process_start_identity: 'preview-1' };
  const authorization = { purpose: 'local_document_read', display_name: 'untrusted', target_label: 'untrusted', file_root: fixtureRoot, file_path: fixture };
  const first = broker.handle(message(broker, 'activate_app', { task_id: 'doc-task', app: finder, authorization, delivery_mode: 'background' }));
  await new Promise(resolve => setImmediate(resolve));
  const summary = broker.publicState().pending_approval.action_summary;
  assert.match(summary.title, /Finder.*预览.*acceptance\.pdf/);
  assert.equal(summary.title.includes('untrusted'), false);
  approve(broker, 'doc-task', 'activate_app'); assert.equal((await first).status, 'completed_known');
  const previewActivation = await broker.handle({ ...message(broker, 'activate_app', { task_id: 'doc-task', app: preview, delivery_mode: 'background' }), request_id: '7'.repeat(32) });
  assert.equal(previewActivation.status, 'completed_known');
  assert.equal(broker.publicState().pending_approval, null);
  await broker.handle({ ...message(broker, 'observe', { task_id: 'doc-task', app: finder, window: { window_id: 4 }, delivery_mode: 'background' }), request_id: '8'.repeat(32) });
  const denied = broker.handle({ ...message(broker, 'type_text', { task_id: 'doc-task', app: finder, window: { window_id: 4 }, text: outside, delivery_mode: 'background' }), request_id: '9'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(broker.publicState().status, 'waiting_for_approval');
  broker.rejectPending({ approval_id: broker.publicState().pending_approval.approval_id, parameter_digest: broker.publicState().pending_approval.parameter_digest });
  assert.equal((await denied).error_code, 'approval_denied');
});

test('message scope binds recipient and body, then requires one exact send approval and cannot resend', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); let capture = 0;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'launch_app') return { pid: 42, process_start_identity: 'wx-1' };
    return tool === 'get_window_state' ? { capture_id: `wx-${++capture}`, elements: [{ id: 'unique-human-contact' }], geometry: { x: 0, y: 0, width: 50, height: 50 }, display: { id: 'd', scale: 2 }, coordinate_transform: { source: 'capture_pixels', target: 'screen_points' } } : {};
  } } });
  ready(broker);
  const app = { bundle_id: 'com.tencent.xinWeChat', process_start_identity: 'wx-1' };
  const window = { window_id: 9 };
  const authorization = { purpose: 'direct_message_send', display_name: 'untrusted', target_label: 'untrusted', recipient: '宝贝', message_text: '你好' };
  const first = broker.handle(message(broker, 'activate_app', { task_id: 'wx-task', app, authorization, delivery_mode: 'background' }));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(broker.publicState().pending_approval.action_summary.title, /微信.*宝贝.*你好/);
  approve(broker, 'wx-task', 'activate_app'); await first;
  const observe = async requestId => broker.handle({ ...message(broker, 'observe', { task_id: 'wx-task', app, window, delivery_mode: 'background' }), request_id: requestId.repeat(32) });
  await observe('1');
  assert.equal((await broker.handle({ ...message(broker, 'type_text', { task_id: 'wx-task', app, window, text: '宝贝', delivery_mode: 'background' }), request_id: '2'.repeat(32) })).status, 'completed_known');
  const contactObservation = await observe('3');
  assert.equal((await requestAndApprove(broker, { ...message(broker, 'click_element', { task_id: 'wx-task', app, window, observation_id: contactObservation.result.observation_ref, element_id: 'unique-human-contact', delivery_mode: 'background' }), request_id: '4'.repeat(32) }, 'wx-task', 'click_element')).status, 'completed_known');
  await observe('5');
  assert.equal((await broker.handle({ ...message(broker, 'type_text', { task_id: 'wx-task', app, window, text: '你好', delivery_mode: 'background' }), request_id: '6'.repeat(32) })).status, 'completed_known');
  await observe('7');
  const send = broker.handle({ ...message(broker, 'press_key', { task_id: 'wx-task', app, window, key: 'return', delivery_mode: 'background' }), request_id: '8'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve));
  const pending = broker.publicState().pending_approval;
  assert.match(pending.action_summary.screenshot, /宝贝/);
  assert.equal(pending.action_summary.message_text, '你好');
  approve(broker, 'wx-task', 'press_key');
  assert.equal((await send).status, 'completed_known');
  assert.equal((await broker.handle({ ...message(broker, 'press_key', { task_id: 'wx-task', app, window, key: 'return', delivery_mode: 'background' }), request_id: '9'.repeat(32) })).error_code, 'message_already_sent');
});

test('observed images use private files and revoke removes them', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const png = fakePng(100, 100).data;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : ({
    capture_id: 'capture_image', _image: { mime_type: 'image/png', data: png },
    elements: [{ role: 'AXTextField', title: 'fixture query' }, { role: 'AXMenuItem', title: 'Private Recent Item' }],
    tree_markdown: 'Private Recent Item',
    geometry: { x: 0, y: 0, width: 100, height: 100 },
    display: { id: 'display-1', scale: 2 },
    coordinate_transform: { source: 'capture_pixels', target: 'screen_points' },
    screenshot_width: 100, screenshot_height: 100, screenshot_scale: 1,
  }) } });
  ready(broker);
  const observation = { task_id: 'task', app: { bundle_id: 'fixture.app' }, window: { window_id: '1' }, delivery_mode: 'background' };
  const result = await requestAndApprove(broker, message(broker, 'observe', observation), 'task', 'observe');
  const file = result.result.model_image_path;
  assert.equal(result.status, 'completed_known'); assert.ok(file.startsWith(path.join(root, 'computer', 'captures')));
  assert.deepEqual(result.result.elements, [{ role: 'AXTextField', title: 'fixture query' }]);
  assert.equal(JSON.stringify(result).includes('Private Recent Item'), false);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600); assert.equal(JSON.stringify(result).includes(png), false);
  await broker.handle({ ...message(broker, 'end_task', { task_id: 'task', terminal_id: 'e'.repeat(64) }), request_id: 'e'.repeat(32) });
  assert.equal(fs.existsSync(file), false);
  assert.equal(broker.epoch, 1);
});

test('window-owned context menu tokens survive while global menu history is removed', async () => {
  const elements = [
    { element_index: 0, role: 'AXWindow', label: 'Fixture' },
    { element_index: 1, parent_index: 0, role: 'AXButton', label: 'Selectable item', element_token: 's1:1' },
    { element_index: 2, parent_index: 1, role: 'AXMenu' },
    { element_index: 3, parent_index: 2, role: 'AXMenuItem', label: 'Choose enabled', element_token: 's1:3' },
    { element_index: 4, role: 'AXMenuBar' },
    { element_index: 5, parent_index: 4, role: 'AXMenuItem', label: 'Private Recent Item', element_token: 's1:5' },
    { element_index: 6, role: 'AXMenuItem', label: 'Detached private menu', element_token: 's1:6' },
  ];
  const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-window-menu-')),
    driver: { call: async tool => tool === 'check_permissions' ? permissions : { elements } } }));
  const observed = await requestAndApprove(broker, message(broker, 'observe', {
    task_id: 'task', app: { bundle_id: 'fixture.app' }, window: { window_id: 7 },
  }), 'task', 'observe');
  assert.deepEqual(observed.result.elements.map(item => item.element_index), [0, 1, 2, 3]);
  const stored = broker.observations.get(observed.result.observation_ref);
  assert.ok(stored.element_ids.has('s1:3'));
  assert.equal(stored.element_ids.has('s1:5'), false);
  assert.equal(stored.element_ids.has('s1:6'), false);
});

test('a mismatched PNG header cannot mint a coordinate capture', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : ({
    capture_id: 'capture_mismatch', _image: fakePng(99, 100),
    screenshot_width: 100, screenshot_height: 100, screenshot_scale: 1,
    window_bounds: { x: 0, y: 0, width: 100, height: 100 },
  }) } });
  ready(broker);
  const observation = { task_id: 'task', app: { bundle_id: 'fixture.app' }, window: { window_id: '1' }, delivery_mode: 'background' };
  const result = await requestAndApprove(broker, message(broker, 'observe', observation), 'task', 'observe');
  assert.equal(result.status, 'completed_known');
  assert.equal(result.result.image_contract_error, 'screenshot_dimensions_mismatch');
  assert.equal(result.result.capture_id, undefined);
  assert.equal(broker.captures.has('capture_mismatch'), false);
});

test('permission state is host-attributed, non-prompting, and public', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); let args;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, value) => {
    assert.equal(tool, 'check_permissions'); args = value;
    return { ...permissions, screen_recording: false, ignored: 'private' };
  } } });
  await broker.refreshPermissions();
  assert.deepEqual(args, { prompt: false });
  assert.deepEqual(broker.publicState().permissions, { status: 'known', accessibility: true, screen_recording: false, identity_verified: true, attribution: 'host', host_bundle_id: 'com.tcode.desktop', direct_capture_status: 'not_checked' });
});

test('permission identity fails closed when driver reports a different host', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-'));
  const driver = {
    hostBundleId: 'com.github.Electron',
    call: async () => ({
      accessibility: true, screen_recording: true,
      source: { attribution: 'host', embedded: true, host_bundle_id: 'unexpected.host' },
      _driver_identity: { ...permissions._driver_identity, host_bundle_id: 'unexpected.host' },
    }),
  };
  const broker = new ComputerBroker({ runtimeRoot: root, driver });
  await broker.refreshPermissions();
  assert.equal(broker.publicState().permissions.identity_verified, false);
});

test('an exact grant cannot be reused with changed parameters, window, or process identity', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); let calls = 0;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => { if (tool === 'check_permissions') return permissions; calls += 1; return {}; } } });
  ready(broker);
  const original = {
    task_id: 'task', app: { bundle_id: 'fixture.app', process_start_identity: 'pid-1' },
    window: { window_id: 'window-1' }, text: 'approved text', delivery_mode: 'background',
  };
  await requestAndApprove(broker, message(broker, 'type_text', original), 'task', 'type_text');
  for (const [index, changed] of [
    { ...original, text: 'different text' },
    { ...original, window: { window_id: 'window-2' } },
    { ...original, app: { ...original.app, process_start_identity: 'pid-2' } },
  ].entries()) {
    const waiting = broker.handle({ ...message(broker, 'type_text', changed), request_id: String(index + 1).repeat(32) });
    await new Promise(resolve => setImmediate(resolve));
    const pending = broker.publicState().pending_approval;
    broker.rejectPending({ approval_id: pending.approval_id, parameter_digest: pending.parameter_digest });
    assert.equal((await waiting).error_code, 'approval_denied');
  }
  assert.equal(calls, 1);
});

test('approval is bound to its digest and revoke prevents old approval resume', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); let calls = 0;
  const broker = new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => { if (tool === 'check_permissions') return permissions; calls += 1; return {}; } } });
  const first = { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'first', delivery_mode: 'background' };
  const second = { ...first, text: 'second' };
  const waiting = broker.handle(message(broker, 'type_text', first));
  await new Promise(resolve => setImmediate(resolve));
  const stale = broker.publicState().pending_approval;
  assert.equal((await broker.handle({ ...message(broker, 'type_text', second), request_id: '2'.repeat(32) })).error_code, 'approval_in_progress');
  assert.throws(() => broker.grantPending({ approval_id: stale.approval_id, task_id: 'task', operation: 'type_text', parameter_digest: '0'.repeat(64) }));
  broker.revoke('emergency_stop');
  assert.equal((await waiting).error_code, 'control_revoked');
  assert.throws(() => broker.grantPending({ approval_id: stale.approval_id, task_id: 'task', operation: 'type_text', parameter_digest: stale.parameter_digest }));
  assert.equal(calls, 0);
});

test('missing TCC fails closed and revoke makes in-flight writes unknown', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')); let release;
  let guidanceRequests = 0;
  const denied = new ComputerBroker({ runtimeRoot: root, onPermissionRequired: () => { guidanceRequests += 1; }, driver: { call: async tool => tool === 'check_permissions' ? { ...permissions, accessibility: false } : (() => { throw new Error('must not dispatch'); })() } });
  const payload = { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'text', delivery_mode: 'background' };
  const rejection = await denied.handle(message(denied, 'type_text', payload));
  assert.equal(rejection.error_code, 'system_permission_denied');
  assert.equal(rejection.result.computer_enabled, true);
  assert.equal(rejection.result.permissions.accessibility, false);
  assert.equal(guidanceRequests, 1);
  assert.equal(denied.publicState().pending_approval, null);

  const broker = new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')), driver: {
    call: async tool => tool === 'check_permissions' ? permissions : new Promise(resolve => { release = resolve; }), stop: async () => {},
  } });
  const dispatched = requestAndApprove(broker, message(broker, 'type_text', payload), 'task', 'type_text');
  await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve));
  broker.revoke('emergency_stop'); release({});
  assert.equal((await dispatched).status, 'outcome_unknown');
});

test('backend lifecycle rotation invalidates the prior secret and generation', () => {
  const broker = new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-')), driver: null });
  const before = { ...broker.startupPayload(), session: broker.brokerSessionId, generation: broker.generation, epoch: broker.epoch };
  broker.rotateSession(); const after = broker.startupPayload();
  assert.notEqual(after.secret, before.secret); assert.notEqual(broker.brokerSessionId, before.session);
  assert.ok(broker.generation > before.generation); assert.ok(broker.epoch > before.epoch);
});

test('write action ids are idempotent, conflict closed, and remain queryable', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-idempotency-')); let calls = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    calls += 1; return { summary: 'done' };
  } } }));
  const action = message(broker, 'type_text', { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'fixture', delivery_mode: 'background' });
  const first = await requestAndApprove(broker, action, 'task', 'type_text');
  const duplicate = await broker.handle({ ...action, request_id: 'b'.repeat(32) });
  assert.equal(first.status, 'completed_known'); assert.equal(duplicate.status, 'completed_known'); assert.equal(calls, 1);
  const conflict = await broker.handle({ ...action, request_id: 'c'.repeat(32), payload: { ...action.payload, text: 'changed' } });
  assert.equal(conflict.error_code, 'idempotency_conflict'); assert.equal(calls, 1);
  const queried = await broker.handle(message(broker, 'query_action', { task_id: 'task', action_id: action.action_id }));
  assert.equal(queried.status, 'completed_known'); assert.equal(queried.result.queried_action_id, action.action_id);
  const crossTask = await broker.handle(message(broker, 'query_action', { task_id: 'other', action_id: action.action_id }));
  assert.equal(crossTask.error_code, 'action_not_found');
});

test('host-issued target refs carry app and window identity without model-supplied pids', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-target-ref-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', name: 'Fixture', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') { assert.equal(args.pid, 42); return { windows: [
      { pid: 99, window_id: 6, title: 'Wrong process', bounds: { x: 0, y: 0, width: 50, height: 50 } },
      { pid: 42, window_id: 7, title: 'Fixture', bounds: { x: 0, y: 0, width: 50, height: 50 } },
    ] }; }
    if (tool === 'get_window_state') { assert.equal(args.pid, 42); assert.equal(args.window_id, 7); return { window_bounds: { x: 0, y: 0, width: 50, height: 50 } }; }
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task', delivery_mode: 'background' }));
  assert.equal('pid' in apps.result.apps[0], false); const appRef = apps.result.apps[0].target_ref;
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: appRef, delivery_mode: 'background' }), request_id: 'b'.repeat(32) });
  assert.equal(windows.result.windows.length, 1);
  const windowRef = windows.result.windows[0].target_ref; assert.equal('pid' in windows.result.windows[0], false);
  assert.equal(broker.targets.get(windowRef).app.process_start_identity, 'start-1');
  const observation = broker.handle({ ...message(broker, 'observe', { task_id: 'task', target_ref: windowRef, delivery_mode: 'background' }), request_id: 'c'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve)); approve(broker, 'task', 'observe');
  const observed = await observation;
  assert.equal(observed.status, 'completed_known'); assert.equal(observed.result.target_ref, windowRef); assert.ok(observed.result.observation_ref);
});

test('operation target kinds fail early with an actionable recovery instead of implicit window guessing', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-target-kind-'));
  let driverCalls = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    driverCalls += 1;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, title: 'Fixture', bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const appRef = apps.result.apps[0].target_ref;
  const wrongObserve = await broker.handle({ ...message(broker, 'observe', { task_id: 'task', target_ref: appRef }), request_id: 'b'.repeat(32) });
  assert.equal(wrongObserve.error_code, 'target_kind_mismatch');
  assert.deepEqual(wrongObserve.result, { expected_target_kind: 'window', actual_target_kind: 'app', app_target_ref: appRef, next_operation: 'list_windows' });
  assert.equal(broker.publicState().pending_approval, null);
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: appRef }), request_id: 'c'.repeat(32) });
  const windowRef = windows.result.windows[0].target_ref;
  const wrongList = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: windowRef }), request_id: 'd'.repeat(32) });
  assert.equal(wrongList.error_code, 'target_kind_mismatch');
  assert.deepEqual(wrongList.result, {
    expected_target_kind: 'app', actual_target_kind: 'window', current_window_ref: windowRef,
    next_operation: 'observe_or_action', preserve_observation_context: true,
  });
  assert.equal(driverCalls, 2);
});

test('observe separates actionable evidence from unknown tree completeness', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-actionable-projection-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, title: 'Fixture', bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'get_window_state') return {
      elements: [{ role: 'AXWindow', label: 'Fixture' }, { role: 'AXButton', label: 'Selectable item', element_token: 's1:1' }],
      returned_element_count: 2, total_element_count: 2, nodes_pending: 0, truncated: false,
    };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: '4'.repeat(32) });
  const observing = broker.handle({ ...message(broker, 'observe', { task_id: 'task', target_ref: windows.result.windows[0].target_ref }), request_id: '5'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve));
  approve(broker, 'task', 'observe');
  const observed = await observing;
  assert.equal(observed.result.actionable_element_count, 2);
  assert.equal(observed.result.actionable_elements_available, true);
  assert.equal(observed.result.actionable_elements_complete, undefined);
  assert.equal(observed.result.progress_evidence.semantic_coverage, 'unknown');
  assert.match(observed.result._note, /not proof.*complete/i);
});

test('Host observation refs have one format and observe rejects caller ids', async () => {
  assert.equal(OBSERVATION_REF_PATTERN.test(`obs_${'a'.repeat(32)}`), true);
  assert.equal(OBSERVATION_REF_PATTERN.test(`obs_${'a'.repeat(33)}`), false);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-observation-ref-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } }));
  const supplied = await broker.handle(message(broker, 'observe', {
    task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' },
    window: { window_id: 7 }, observation_id: `obs_${'b'.repeat(32)}`,
  }));
  assert.equal(supplied.error_code, 'observation_contract_required');
});

test('pending approval snapshots have monotonic revisions and terminal decisions clear them', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-approval-revision-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } }));
  const payload = { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'first', delivery_mode: 'background' };
  const first = broker.handle(message(broker, 'type_text', payload));
  await new Promise(resolve => setImmediate(resolve));
  const firstSnapshot = broker.publicState().pending_approval;
  assert.equal(firstSnapshot.revision, 1);
  broker.rejectPending({ approval_id: firstSnapshot.approval_id, parameter_digest: firstSnapshot.parameter_digest });
  assert.equal((await first).error_code, 'approval_denied');
  assert.equal(broker.publicState().pending_approval, null);

  const second = broker.handle({ ...message(broker, 'type_text', { ...payload, text: 'second' }), request_id: '2'.repeat(32), action_id: '3'.repeat(32) });
  await new Promise(resolve => setImmediate(resolve));
  const secondSnapshot = broker.publicState().pending_approval;
  assert.equal(secondSnapshot.revision, 2);
  broker.rejectPending({ approval_id: secondSnapshot.approval_id, parameter_digest: secondSnapshot.parameter_digest });
  assert.equal((await second).error_code, 'approval_denied');
  assert.equal(broker.publicState().pending_approval, null);
});

test('pending approval presentation is replayable after missed events and deduplicated after display', () => {
  const pending = { pending_approval: { approval_id: 'approval-1', revision: 7 } };
  assert.deepEqual(approvalPresentation(pending, ''), { key: 'approval-1:7', should_present: true });
  assert.deepEqual(approvalPresentation(pending, 'approval-1:7'), { key: 'approval-1:7', should_present: false });
  assert.deepEqual(approvalPresentation({ pending_approval: null }, 'approval-1:7'), { key: '', should_present: false });
  assert.deepEqual(approvalPresentation({ pending_approval: { approval_id: 'approval-2', revision: 8 } }, ''), { key: 'approval-2:8', should_present: true });
});

test('resolved approval clears only its unacknowledged automatic panel request', () => {
  assert.deepEqual(resolvedApprovalPanelRequest(12, 'approval-1:7', 'approval-1:7'), {
    request_id: 0, approval_key: '',
  });
  assert.deepEqual(resolvedApprovalPanelRequest(13, '', 'approval-1:7'), {
    request_id: 13, approval_key: '',
  });
  assert.deepEqual(resolvedApprovalPanelRequest(14, 'approval-2:8', 'approval-1:7'), {
    request_id: 14, approval_key: 'approval-2:8',
  });
  assert.deepEqual(resolvedApprovalPanelRequest(0, '', 'approval-1:7'), {
    request_id: 0, approval_key: '',
  });
});

test('element tokens stay bound to their explicit observation and process epoch', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-element-observation-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'get_window_state') return { elements: [{ id: 'field-token' }] };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: 'b'.repeat(32) });
  const targetRef = windows.result.windows[0].target_ref;
  const observed = await requestAndApprove(broker, { ...message(broker, 'observe', { task_id: 'task', target_ref: targetRef }), request_id: 'c'.repeat(32) }, 'task', 'observe');
  const missing = await broker.handle({ ...message(broker, 'set_value', { task_id: 'task', target_ref: targetRef, element_id: 'field-token', text: 'x' }), request_id: 'd'.repeat(32) });
  assert.equal(missing.error_code, 'observation_contract_required');
  const wrong = await broker.handle({ ...message(broker, 'set_value', { task_id: 'task', target_ref: targetRef, observation_id: observed.result.observation_ref, element_id: 'other-token', text: 'x' }), request_id: 'e'.repeat(32) });
  assert.equal(wrong.error_code, 'stale_observation');
});

test('all token-capable operations require explicit observation ownership', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-token-contract-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } }));
  for (const [index, [operation, extra]] of [
    ['type_text', { text: 'x' }], ['press_key', { key: 'escape' }], ['scroll', { delta_y: 1 }],
  ].entries()) {
    const result = await broker.handle({ ...message(broker, operation, {
      task_id: 'task', app: { bundle_id: 'fixture.app' }, element_id: 'token-1', ...extra,
    }), request_id: String(index + 1).repeat(32) });
    assert.equal(result.error_code, 'observation_contract_required');
  }
});

test('element tokens are collected from nested driver snapshots', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-nested-observation-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'get_window_state') return { snapshot: { elements: [{ element_token: 's00000001:1' }] } };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: 'b'.repeat(32) });
  const targetRef = windows.result.windows[0].target_ref;
  const observed = await requestAndApprove(broker, { ...message(broker, 'observe', { task_id: 'task', target_ref: targetRef }), request_id: 'c'.repeat(32) }, 'task', 'observe');
  const written = requestAndApprove(broker, { ...message(broker, 'set_value', {
    task_id: 'task', target_ref: targetRef, observation_id: observed.result.observation_ref,
    element_id: 's00000001:1', text: 'value',
  }), request_id: 'd'.repeat(32) }, 'task', 'set_value');
  assert.equal((await written).status, 'completed_known');
});

test('write preflight rejects a reused pid with a changed process-start identity', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-process-reuse-'));
  let start = 'start-1';
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: start, running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: 'b'.repeat(32) });
  start = 'start-2';
  const action = { ...message(broker, 'press_key', { task_id: 'task', target_ref: windows.result.windows[0].target_ref, key: 'escape' }), request_id: 'c'.repeat(32) };
  const result = await requestAndApprove(broker, action, 'task', 'press_key');
  assert.equal(result.error_code, 'target_not_found');
});

test('write preflight rejects a running target when no trusted process-start identity exists', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-process-identity-missing-'));
  const missingPid = 999999;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: missingPid, running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: missingPid, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: 'b'.repeat(32) });
  const result = await requestAndApprove(broker, { ...message(broker, 'press_key', {
    task_id: 'task', target_ref: windows.result.windows[0].target_ref, key: 'escape',
  }), request_id: 'c'.repeat(32) }, 'task', 'press_key');
  assert.equal(result.error_code, 'target_identity_unavailable');
});

test('unknown app authorization intent falls back to an exact per-action approval', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-exact-approval-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'get_window_state') return { snapshot: { elements: [{ element_token: 's00000001:1' }] } };
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: 'b'.repeat(32) });
  const targetRef = windows.result.windows[0].target_ref;
  const observed = await requestAndApprove(broker, { ...message(broker, 'observe', { task_id: 'task', target_ref: targetRef }), request_id: 'c'.repeat(32) }, 'task', 'observe');
  const request = { ...message(broker, 'set_value', {
    task_id: 'task', target_ref: targetRef, observation_id: observed.result.observation_ref,
    element_id: 's00000001:1', text: 'hello', authorization: {
      purpose: 'application_interaction', display_name: 'Fixture', target_label: 'fill form',
    },
  }), request_id: 'd'.repeat(32) };
  const pending = broker.handle(request);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(broker.publicState().status, 'waiting_for_approval');
  assert.equal(broker.publicState().pending_approval.operation, 'set_value');
  approve(broker, 'task', 'set_value');
  assert.equal((await pending).status, 'completed_known');
  assert.equal(broker.taskGrants.has('task'), false);
});

test('caller disconnect cancels a pending observe and makes late approval invalid', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-disconnect-observe-'));
  let observes = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] };
    if (tool === 'get_window_state') { observes += 1; return {}; }
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const windows = await broker.handle({ ...message(broker, 'list_windows', { task_id: 'task', target_ref: apps.result.apps[0].target_ref }), request_id: 'e'.repeat(32) });
  const owner = { id: 'client-observe', settled: false };
  const waiting = broker.handle({ ...message(broker, 'observe', { task_id: 'task', target_ref: windows.result.windows[0].target_ref }), request_id: 'b'.repeat(32) }, owner);
  await new Promise(resolve => setImmediate(resolve));
  const approval = broker.publicState().pending_approval;
  assert.equal(broker.cancelOwner(owner.id), true);
  assert.equal((await waiting).error_code, 'caller_disconnected');
  assert.equal(observes, 0);
  assert.throws(() => broker.grantPending({
    approval_id: approval.approval_id, task_id: 'task', operation: 'observe', parameter_digest: approval.parameter_digest,
  }), /changed/);
});

test('disconnect after approval but before dispatch cancels with zero write dispatches', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-disconnect-before-dispatch-'));
  let writes = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'press_key') writes += 1;
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const owner = { id: 'client-write', settled: false };
  const waiting = broker.handle({ ...message(broker, 'press_key', {
    task_id: 'task', target_ref: apps.result.apps[0].target_ref, key: 'escape',
  }), request_id: 'b'.repeat(32) }, owner);
  await new Promise(resolve => setImmediate(resolve));
  approve(broker, 'task', 'press_key');
  broker.cancelOwner(owner.id);
  assert.equal((await waiting).error_code, 'caller_disconnected');
  assert.equal(writes, 0);
});

test('disconnect after write dispatch records unknown without replay', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-disconnect-after-dispatch-'));
  let writes = 0; let releaseWrite;
  const blocked = new Promise(resolve => { releaseWrite = resolve; });
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
    if (tool === 'press_key') { writes += 1; await blocked; }
    return {};
  } } }));
  const apps = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const owner = { id: 'client-dispatched', settled: false };
  const request = { ...message(broker, 'press_key', {
    task_id: 'task', target_ref: apps.result.apps[0].target_ref, key: 'escape',
  }), request_id: 'b'.repeat(32) };
  const waiting = broker.handle(request, owner);
  await new Promise(resolve => setImmediate(resolve)); approve(broker, 'task', 'press_key');
  while (writes === 0) await new Promise(resolve => setImmediate(resolve));
  broker.cancelOwner(owner.id); releaseWrite({});
  const result = await waiting;
  assert.equal(result.status, 'outcome_unknown');
  assert.equal(writes, 1);
  const duplicate = await broker.handle(request, owner);
  assert.equal(duplicate.status, 'outcome_unknown');
  assert.equal(writes, 1);
});

test('end_task is idempotent, task-scoped, and closes only its driver session', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-task-end-'));
  const ended = [];
  let failEndSession = false;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'launch_app') return { pid: tool.length + ended.length + 40, process_start_identity: `start-${ended.length}` };
    if (tool === 'end_session') {
      ended.push(args.session);
      if (failEndSession) throw new Error('cleanup failed');
      return { active: false };
    }
    return {};
  } } }));
  for (const taskId of ['task-a', 'task-b']) {
    const pending = broker.handle(message(broker, 'activate_app', {
      task_id: taskId, app: { bundle_id: 'com.netease.163music' }, delivery_mode: 'background',
      authorization: { purpose: 'media_search_play', display_name: 'Music', target_label: 'search', query: 'one' },
    }));
    await new Promise(resolve => setImmediate(resolve)); approve(broker, taskId, 'activate_app');
    assert.equal((await pending).status, 'completed_known');
  }
  assert.equal(broker.taskGrants.size, 2);
  broker.captures.set('bad-cleanup', { task_id: 'task-a', model_image_path: root });
  broker.enabled = false;
  failEndSession = true;
  const terminal = { task_id: 'task-a', terminal_id: 'a'.repeat(64) };
  const first = await broker.handle({ ...message(broker, 'end_task', terminal), request_id: 'c'.repeat(32) });
  const repeated = await broker.handle({ ...message(broker, 'end_task', terminal), request_id: 'd'.repeat(32) });
  assert.equal(first.status, 'completed_known');
  assert.equal(first.result.driver_session_ended, false);
  assert.deepEqual(first.result.cleanup, { capture_file_failures: 1, driver_session_failed: true });
  assert.equal(repeated.result.already_ended, true);
  assert.equal(ended.length, 1);
  assert.equal(broker.taskGrants.has('task-a'), false);
  assert.equal(broker.taskGrants.has('task-b'), true);
  assert.equal(broker.captures.has('bad-cleanup'), false);
  assert.equal(broker.epoch, 1);
});

test('same chat gets a fresh native session after end, including overlapping old cleanup', async () => {
  const closed = new Set(); const calls = []; let reached; let resume;
  const entered = new Promise(resolve => { reached = resolve; });
  const barrier = new Promise(resolve => { resume = resolve; });
  const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-session-turn-')), driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    calls.push({ tool, session: args.session });
    if (tool === 'end_session') { closed.add(args.session); reached(); await barrier; return { active: false }; }
    assert(!closed.has(args.session), 'closed native labels cannot be reused');
    return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
  } } }));
  const request = (operation, fields = {}) => ({ ...message(broker, operation, { task_id: 'same-chat', ...fields }), request_id: crypto.randomBytes(16).toString('hex') });
  const first = await broker.handle(request('list_apps'));
  assert.equal(first.status, 'completed_known');
  const oldRef = first.result.apps[0].target_ref;
  const oldSession = calls.at(-1).session;
  const ending = broker.handle(request('end_task', { terminal_id: 'turn-one' })); await entered;
  const next = await broker.handle(request('list_apps'));
  assert.equal(next.status, 'completed_known');
  const nextRef = next.result.apps[0].target_ref;
  assert.notEqual(nextRef, oldRef);
  const nextSession = calls.at(-1).session; assert.notEqual(nextSession, oldSession);
  const expired = await broker.handle(request('activate_app', { target_ref: oldRef }));
  assert.equal(expired.error_code, 'target_not_found');
  assert.equal(expired.executed, false);
  resume(); await ending;
  assert.equal((await broker.handle(request('list_apps'))).result.apps[0].target_ref, nextRef);
  assert.equal(calls.at(-1).session, nextSession);
  const duplicate = await broker.handle(request('end_task', { terminal_id: 'turn-one' }));
  assert.equal(duplicate.result.already_ended, true);
  assert.equal((await broker.handle(request('list_apps'))).status, 'completed_known');
  assert.equal(calls.at(-1).session, nextSession);
  assert.equal(broker.grants.size, 0); assert.equal(broker.taskGrants.size, 0);
});

test('computer action and active-time budgets restart after a task ends', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-budget-window-'));
  let writes = 0;
  const broker = ready(new ComputerBroker({
    runtimeRoot: root,
    maxActions: 2,
    maxActiveMs: 10,
    driver: { call: async tool => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1', running: true }] };
      if (tool === 'press_key') writes += 1;
      if (tool === 'end_session') return { active: false };
      return {};
    } },
  }));
  const taskId = 'same-chat-task';
  const discover = async requestId => broker.handle({
    ...message(broker, 'list_apps', { task_id: taskId }), request_id: requestId.repeat(32),
  });
  const press = async (targetRef, requestId) => requestAndApprove(broker, {
    ...message(broker, 'press_key', { task_id: taskId, target_ref: targetRef, key: 'escape' }),
    request_id: requestId.repeat(32),
  }, taskId, 'press_key');

  let apps = await discover('1');
  assert.equal((await press(apps.result.apps[0].target_ref, '2')).status, 'completed_known');
  broker.taskBudgets.get(taskId).started_at = 0;
  assert.equal((await press(apps.result.apps[0].target_ref, '3')).error_code, 'budget_exceeded');
  assert.equal(writes, 1);

  await broker.handle({
    ...message(broker, 'end_task', { task_id: taskId, terminal_id: 'f'.repeat(64) }),
    request_id: '4'.repeat(32),
  });
  assert.equal(broker.publicState().action_budget.used, 0);
  apps = await discover('5');
  assert.equal((await press(apps.result.apps[0].target_ref, '6')).status, 'completed_known');
  assert.equal(writes, 2);
});

test('app discovery exposes running apps only so targets are not buried in installed inventory', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-running-apps-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [
      { bundle_id: 'fixture.app', name: 'Fixture', pid: 42, running: true },
      { bundle_id: 'unused.app', name: 'Unused', running: false },
    ] };
    return {};
  } } }));
  const result = await broker.handle(message(broker, 'list_apps', { task_id: 'task', delivery_mode: 'background' }));
  assert.deepEqual(result.result.apps.map(app => app.bundle_id), ['fixture.app']);
  assert.equal('window_count' in result.result.apps[0], false);
  assert.equal(result.result.discovery.scope, 'running_only');
  assert.equal(result.result.discovery.installed_search_available, true);
  assert.equal(result.result.discovery.next_call.query, '<requested application name>');
});

test('discovery refs and semantic fingerprints are stable while real state changes remain visible', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-stable-discovery-'));
  let active = false;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', name: 'Fixture', pid: 42, process_start_identity: 'start-1', running: true, active }] };
    return {};
  } } }));
  const first = await broker.handle(message(broker, 'list_apps', { task_id: 'task' }));
  const second = await broker.handle({ ...message(broker, 'list_apps', { task_id: 'task' }), request_id: 'b'.repeat(32) });
  assert.equal(first.result.apps[0].target_ref, second.result.apps[0].target_ref);
  assert.equal(first.result.result_fingerprint, second.result.result_fingerprint);
  active = true;
  const changed = await broker.handle({ ...message(broker, 'list_apps', { task_id: 'task' }), request_id: 'c'.repeat(32) });
  assert.notEqual(first.result.result_fingerprint, changed.result.result_fingerprint);
});

test('discovery is sorted, bounded, pageable, and can find a non-running installed app', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-paged-discovery-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [
      { bundle_id: 'z.app', name: 'Zulu', pid: 2, running: true },
      { bundle_id: 'fixture.app', name: 'TCode Computer Fixture', running: false },
      { bundle_id: 'a.app', name: 'Alpha', pid: 1, running: true },
    ] };
    return {};
  } } }));
  const page = await broker.handle(message(broker, 'list_apps', { task_id: 'task', limit: 1 }));
  assert.equal(page.result.apps[0].name, 'Alpha');
  assert.equal(page.result.page.next_cursor, 1);
  const installed = await broker.handle({ ...message(broker, 'list_apps', { task_id: 'task', query: 'computer fixture' }), request_id: 'b'.repeat(32) });
  assert.equal(installed.result.apps[0].bundle_id, 'fixture.app');
  assert.equal(installed.result.apps[0].running, false);
});

test('installed app search matches its localized display name without exposing its path', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-localized-discovery-'));
  const bundle = path.join(root, 'Example.app');
  const localization = path.join(bundle, 'Contents', 'Resources', 'zh-Hans.lproj');
  fs.mkdirSync(localization, { recursive: true });
  fs.writeFileSync(path.join(localization, 'InfoPlist.strings'), '"CFBundleDisplayName" = "示例应用";\n', 'utf8');
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'example.app', name: 'Example', launch_path: bundle, running: false }] };
    return {};
  } } }));
  const result = await broker.handle(message(broker, 'list_apps', { task_id: 'task', query: '示例应用' }));
  assert.equal(result.result.apps[0].bundle_id, 'example.app');
  assert.deepEqual(result.result.apps[0].aliases, ['示例应用']);
  assert.equal('launch_path' in result.result.apps[0], false);
});

test('host-issued target refs cannot cross task boundaries', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-target-owner-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' }] };
    return {};
  } } }));
  const discovery = await broker.handle(message(broker, 'list_apps', { task_id: 'owner', delivery_mode: 'background' }));
  const targetRef = discovery.result.apps[0].target_ref;
  const stolen = await broker.handle(message(broker, 'list_windows', { task_id: 'other', target_ref: targetRef, delivery_mode: 'background' }));
  assert.equal(stolen.error_code, 'target_not_found');
});

test('driver evidence is returned for the task but persisted through a safe whitelist', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-evidence-')); const ledgerPath = path.join(root, 'ledger.jsonl');
  const broker = ready(new ComputerBroker({ runtimeRoot: root, ledgerPath, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    throw Object.assign(new Error('rejected'), { code: 'failed_known', safeCode: 'failed_known', evidence: {
      raw_code: 'focus_lost', raw_message: 'private window title', effect: 'Refused', actual_delivery: 'foreground',
    } });
  } } }));
  const action = message(broker, 'type_text', { task_id: 'task', app: { bundle_id: 'fixture.app' }, text: 'fixture', delivery_mode: 'background' });
  const result = await requestAndApprove(broker, action, 'task', 'type_text');
  assert.equal(result.result.driver_error.raw_code, 'focus_lost');
  const persisted = fs.readFileSync(ledgerPath, 'utf8');
  assert.equal(persisted.includes('focus_lost'), true); assert.equal(persisted.includes('private window title'), false);
});

test('explicit navigation capability reuses one approval across search, results, selection and playback', async () => {
  for (const bundle_id of ['generic.browser', 'generic.player']) {
    const app = { bundle_id, pid: 42, process_start_identity: 'start-1' };
    const bounds = { x: 0, y: 0, width: 50, height: 50 };
    let writes = 0; let phase = 'search';
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-navigation-')), driver: { call: async tool => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
      if (tool === 'get_window_state') return { capture_id: crypto.randomBytes(8).toString('hex'), elements: [
        { element_token: 'root', role: 'AXWindow' }, { element_token: 'send', role: 'AXButton', label: 'Send' },
        { element_token: 'phase', role: 'AXStaticText', label: phase },
      ], window_bounds: bounds, screenshot_width: 100, screenshot_height: 100, _image: fakePng(100, 100) };
      if (tool === 'press_key') phase = 'results';
      if (tool === 'click') { assert.equal(phase, 'results'); phase = 'selected'; }
      if (tool === 'double_click') { assert.equal(phase, 'selected'); phase = 'playing'; }
      writes++; return {};
    } } }));
    const intent = { task_id: 'task', text: '搜索山丘并播放', turn_id: 'turn-human', user_message_id: 'human' };
    const proof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(intent)).digest('hex');
    const request = (operation, payload = {}) => ({ ...message(broker, operation, { task_id: 'task', task_consent_required: true,
      task_intent: intent, task_intent_proof: proof, delivery_mode: 'foreground', ...payload }), request_id: crypto.randomBytes(16).toString('hex') });
    const apps = await broker.handle(request('list_apps'));
    const first = broker.handle(request('activate_app', { target_ref: apps.result.apps[0].target_ref }));
    await new Promise(resolve => setImmediate(resolve));
    const approval = broker.publicState().pending_approval;
    broker.grantPending({ ...approval, navigation: true });
    const target_ref = (await first).result.window_target_ref;
    for (const [operation, args] of [
      ['type_text', { text: '山丘' }], ['press_key', { key: 'return' }],
      ['click_at', { x: 20, y: 20, coordinate_space: 'pixel', reference_width: 100, reference_height: 100 }],
      ['double_click', { x: 30, y: 30, coordinate_space: 'pixel', reference_width: 100, reference_height: 100 }],
    ]) {
      const observed = await broker.handle(request('observe', { target_ref }));
      const pending = broker.handle(request(operation, { target_ref, observation_id: observed.result.observation_ref, ...args }));
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(broker.publicState().pending_approval, null, operation);
      assert.equal((await pending).status, 'completed_known', operation);
    }
    assert.equal(broker.taskGrants.get('task').commit_attempted, undefined);
    assert.equal(phase, 'playing');
    assert.equal(fs.readFileSync(broker.ledgerPath, 'utf8').split('\n').filter(line => line.includes('"event":"approval"')).length, 1);
    assert.equal((await broker.handle(request('observe', { target_ref, task_id: 'other' }))).error_code, 'target_not_found');
    const observed = await broker.handle(request('observe', { target_ref }));
    const send = broker.handle(request('click_element', { target_ref, observation_id: observed.result.observation_ref, element_id: 'send' }));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(broker.publicState().pending_approval.operation, 'click_element');
    broker.rejectPending(broker.publicState().pending_approval);
    assert.equal((await send).status, 'rejected');
    const count = writes;
    broker.revoke('test');
    assert.equal((await broker.handle(request('click_at', { target_ref, observation_id: observed.result.observation_ref,
      x: 20, y: 20, coordinate_space: 'pixel', reference_width: 100, reference_height: 100 }))).status, 'rejected');
    assert.equal(writes, count);
  }
});

test('explicit single-message capability binds intent and consumes Enter/click equivalently before an unknown receipt', async () => {
  for (const firstOperation of ['press_key', 'click_element']) {
    const app = { bundle_id: 'generic.messenger', pid: 42, process_start_identity: 'start-1' };
    const bounds = { x: 0, y: 0, width: 50, height: 50 };
    let body = ''; let sends = 0;
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-message-capability-')), driver: { call: async (tool, args) => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
      if (tool === 'get_window_state') return { elements: [
        { element_token: 'recipient', role: 'AXStaticText', label: '宝贝' },
        { element_token: 'body', role: 'AXTextArea', label: body },
        { element_token: 'send', role: 'AXButton', label: 'Send' },
      ] };
      if (tool === 'type_text' && args.text === '你好') body = args.text;
      if (tool === 'press_key' || tool === 'click' && args.element_token === 'send') {
        sends++; throw Object.assign(new Error('unknown native send'), { code: 'outcome_unknown' });
      }
      return {};
    } } }));
    const intent = { task_id: 'task', text: '给宝贝发送你好一次', turn_id: 'turn-human', user_message_id: 'human' };
    const proof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(intent)).digest('hex');
    const request = (operation, payload = {}) => ({ ...message(broker, operation, { task_id: 'task', task_consent_required: true,
      task_intent: intent, task_intent_proof: proof, delivery_mode: 'foreground', ...payload }), request_id: crypto.randomBytes(16).toString('hex') });
    const apps = await broker.handle(request('list_apps'));
    const first = broker.handle(request('activate_app', { target_ref: apps.result.apps[0].target_ref }));
    await new Promise(resolve => setImmediate(resolve));
    const approval = broker.publicState().pending_approval;
    assert.throws(() => broker.grantPending({ ...approval, single_message: { recipient: '别人', text: '你好' } }));
    broker.grantPending({ ...approval, single_message: { recipient: '宝贝', text: '你好' } });
    const target_ref = (await first).result.window_target_ref;
    const observe = () => broker.handle(request('observe', { target_ref }));
    await observe();
    assert.equal((await broker.handle(request('type_text', { target_ref, text: '宝贝' }))).status, 'completed_known');
    await observe();
    assert.equal((await broker.handle(request('type_text', { target_ref, text: '你好' }))).status, 'completed_known');
    const observed = await observe();
    const sendArgs = firstOperation === 'press_key' ? { key: 'return' } : { element_id: 'send' };
    const pending = broker.handle(request(firstOperation, { target_ref, observation_id: observed.result.observation_ref, ...sendArgs }));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(broker.publicState().pending_approval, null);
    assert.equal((await pending).status, 'outcome_unknown');
    const fresh = await observe();
    const alternate = firstOperation === 'press_key' ? 'click_element' : 'press_key';
    const result = await broker.handle(request(alternate, { target_ref, observation_id: fresh.result.observation_ref,
      ...(alternate === 'press_key' ? { key: 'return' } : { element_id: 'send' }) }));
    assert.equal(result.error_code, 'commit_already_attempted');
    assert.equal(sends, 1);
    assert.equal(broker.taskGrants.get('task').commit_attempted.intent_id, broker.taskGrants.get('task').commit_intent_id);
    assert.equal(fs.readFileSync(broker.ledgerPath, 'utf8').split('\n').filter(line => line.includes('"event":"approval"')).length, 1);
  }
});

test('trusted human intent enters Host approval and resumes the original call without model permission fields', async () => {
  for (const bundle_id of ['fixture.app', 'com.netease.163music', 'com.tencent.xinWeChat']) {
    const app = { bundle_id, pid: 42, process_start_identity: 'start-1' };
    const writes = [];
    const bounds = { x: 0, y: 0, width: 50, height: 50 };
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-human-intent-')), driver: { call: async (tool, args) => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
      if (tool === 'get_window_state') return { capture_id: crypto.randomBytes(8).toString('hex'), elements: [
        { element_token: 's1:0', label: 'Fixture window', role: 'AXWindow', depth: 0, screenshot_frame: { x: 0, y: 0, w: 100, h: 100 } },
        { element_token: 's1:1', label: 'Item', role: 'AXButton', depth: 1, screenshot_frame: { x: 10, y: 10, w: 20, h: 20 } },
        { element_token: 's1:2', label: 'Send' },
      ], window_bounds: bounds, screenshot_width: 100, screenshot_height: 100, _image: fakePng(100, 100) };
      writes.push({ tool, args }); return {};
    } } }));
    const intent = { task_id: 'task', text: 'PRIVATE-HUMAN-TASK', turn_id: 'turn-human', user_message_id: 'human' };
    const proof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(intent)).digest('hex');
    const request = (operation, payload = {}) => ({ ...message(broker, operation, { task_id: 'task', task_consent_required: true,
      task_intent: intent, task_intent_proof: proof, delivery_mode: 'foreground', ...payload }), request_id: crypto.randomBytes(16).toString('hex') });
    const apps = await broker.handle(request('list_apps'));
    const target_ref = apps.result.apps[0].target_ref;
    assert.equal((await broker.handle(request('activate_app', { target_ref, task_intent_proof: '0'.repeat(64) }))).error_code, 'task_intent_invalid');
    assert.equal(broker.publicState().pending_approval, null); assert.equal(writes.length, 0);
    assert.equal((await broker.handle(request('activate_app', { target_ref, task_id: 'other-task' }))).error_code, 'target_not_found');
    const first = request('activate_app', { target_ref });
    const pendingResult = broker.handle(first);
    await new Promise(resolve => setImmediate(resolve));
    const pending = broker.publicState().pending_approval;
    assert.equal(pending.operation, 'activate_app'); assert.equal(writes.length, 0);
    assert.match(pending.action_summary.allowed, /PRIVATE-HUMAN-TASK/);
    assert.equal(JSON.stringify(broker.publicState({ forModel: true })).includes(intent.text), false);
    approve(broker, 'task', 'activate_app');
    const activated = await pendingResult;
    assert.equal(activated.action_id, first.action_id); assert.equal(activated.status, 'completed_known');
    assert.equal(writes.length, 1);
    assert.equal((await broker.handle(first)).action_id, first.action_id); assert.equal(writes.length, 1);
    const windows = await broker.handle(request('list_windows', { target_ref }));
    const windowRef = windows.result.windows[0].target_ref;
    const observe = () => broker.handle(request('observe', { target_ref: windowRef }));
    const observed = await observe(); assert.equal(broker.publicState().pending_approval, null);
    const action = request('double_click', { target_ref: windowRef, observation_id: observed.result.observation_ref, element_id: 's1:1' });
    const actionResult = broker.handle(action);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(broker.publicState().pending_approval, null);
    assert.equal((await actionResult).status, 'completed_known');
    const next = await observe();
    assert.equal((await broker.handle(request('double_click', { target_ref: windowRef, observation_id: next.result.observation_ref, element_id: 's1:1' }))).status, 'completed_known');
    assert.equal(broker.publicState().pending_approval, null);
    for (const operation of ['right_click', 'open_menu', 'select_option', 'click_element']) {
      const current = await observe();
      assert.equal((await broker.handle(request(operation, { target_ref: windowRef, observation_id: current.result.observation_ref, element_id: 's1:1' }))).status, 'completed_known');
      assert.equal(broker.publicState().pending_approval, null);
    }
    const pixelObservation = await observe();
    assert.equal((await broker.handle(request('right_click', { target_ref: windowRef, observation_id: pixelObservation.result.observation_ref,
      x: 20, y: 20, coordinate_space: 'pixel', reference_width: 100, reference_height: 100 }))).status, 'completed_known');
    assert.equal(broker.publicState().pending_approval, null);
    const forged = await broker.handle(request('observe', { target_ref: windowRef, task_intent_proof: '0'.repeat(64) }));
    assert.equal(forged.error_code, 'task_intent_invalid');
    const missing = await broker.handle(request('observe', { target_ref: windowRef, task_intent: undefined }));
    assert.equal(missing.error_code, 'task_intent_required');
    assert.equal(fs.readFileSync(broker.ledgerPath, 'utf8').includes(intent.text), false);
    const lastObserved = await observe();
    const late = broker.handle(request('click_element', { target_ref: windowRef, observation_id: lastObserved.result.observation_ref, element_id: 's1:2' }));
    await new Promise(resolve => setImmediate(resolve));
    const latePending = broker.publicState().pending_approval;
    await broker.handle(request('end_task', { terminal_id: 'finished' }));
    assert.equal((await late).status, 'cancelled');
    assert.throws(() => broker.grantPending({ approval_id: latePending.approval_id, task_id: 'task', operation: 'click_element', parameter_digest: latePending.parameter_digest }));
    assert.equal(writes.some(call => call.args.element_token === 's1:2'), false);
    assert.equal(broker.taskGrants.size, 0);
  }
});

test('continuous task reserves an approved commit even when its native outcome is unknown', async () => {
  const app = { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' };
  const bounds = { x: 0, y: 0, width: 50, height: 50 };
  let sends = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-task-commit-')), driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [app] };
    if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
    if (tool === 'get_window_state') return { elements: [{ element_token: 's1:1', label: 'Send' }], window_bounds: bounds };
    sends++; throw Object.assign(new Error('unknown'), { code: 'outcome_unknown' });
  } } }));
  const task_intent = { task_id: 'task', text: 'Send exactly one fixture message', turn_id: 'turn-human', user_message_id: 'human' };
  const task_intent_proof = crypto.createHmac('sha256', Buffer.from(broker.secret, 'hex')).update(JSON.stringify(task_intent)).digest('hex');
  const request = (operation, args = {}) => ({ ...message(broker, operation, { task_id: 'task', task_intent, task_intent_proof, delivery_mode: 'foreground', ...args }), request_id: crypto.randomBytes(16).toString('hex') });
  const apps = await broker.handle(request('list_apps'));
  const windows = await broker.handle(request('list_windows', { target_ref: apps.result.apps[0].target_ref }));
  const target_ref = windows.result.windows[0].target_ref;
  const observed = await requestAndApprove(broker, request('observe', { target_ref }), 'task', 'observe');
  const result = await requestAndApprove(broker, request('click_element', { target_ref, observation_id: observed.result.observation_ref, element_id: 's1:1' }), 'task', 'click_element');
  assert.equal(result.status, 'outcome_unknown'); assert.equal(sends, 1);
  const fresh = await broker.handle(request('observe', { target_ref }));
  const retry = await broker.handle(request('press_key', { target_ref, observation_id: fresh.result.observation_ref, key: 'return' }));
  assert.equal(retry.error_code, 'commit_already_attempted'); assert.equal(sends, 1);
  assert.equal(broker.publicState().pending_approval, null);
});

test('foreground menus and atomic input retain the exact target without upgrading background or bypassing cancellation', async () => {
  for (const mode of ['foreground', 'double', 'click', 'type', 'replace', 'replace_revoked', 'key', 'background', 'token', 'open_menu', 'ended', 'revoked']) {
    const calls = [];
    const app = { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' };
    const bounds = { x: 0, y: 0, width: 50, height: 50 };
    let broker;
    const request = (operation, payload = {}) => ({ ...message(broker, operation, { task_id: 'task', ...payload }), request_id: crypto.randomBytes(16).toString('hex') });
    broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-menu-focus-')), driver: { call: async (tool, args) => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
      if (tool === 'get_window_state') return { capture_id: 'cap', elements: [{ element_token: 's1:1', label: 'Item' }], window_bounds: bounds, screenshot_width: 100, screenshot_height: 100, _image: fakePng(100, 100) };
      if (tool === 'end_session') return {};
      calls.push({ tool, args });
      if (tool === 'press_key' && mode === 'replace_revoked') broker.revoke('test');
      if (tool === 'bring_to_front') {
        if (mode === 'ended') await broker.handle(request('end_task', { terminal_id: 'finished' }));
        if (mode === 'revoked') broker.revoke('test');
      }
      return {};
    } } }));
    const delivery_mode = mode === 'background' ? 'background' : 'foreground';
    const apps = await broker.handle(request('list_apps'));
    const windows = await broker.handle(request('list_windows', { target_ref: apps.result.apps[0].target_ref }));
    const target_ref = windows.result.windows[0].target_ref;
    const observed = await requestAndApprove(broker, request('observe', { target_ref, delivery_mode,
      authorization: { purpose: 'app_task', target_label: 'open Item menu', controls: [{ label: 'Item', operations: ['right_click', 'open_menu'] }] },
    }), 'task', 'observe');
    const operation = mode === 'open_menu' ? 'open_menu' : mode === 'double' ? 'double_click' : mode === 'click' ? 'click_at' : ['type', 'replace', 'replace_revoked'].includes(mode) ? 'type_text' : mode === 'key' ? 'press_key' : 'right_click';
    const token = mode === 'token' || mode === 'open_menu';
    const action = request(operation, { target_ref, delivery_mode, observation_id: observed.result.observation_ref,
      ...(['type', 'replace', 'replace_revoked'].includes(mode) ? { text: '山丘', ...(['replace', 'replace_revoked'].includes(mode) ? { input_mode: 'replace' } : {}) } : mode === 'key' ? { key: 'a', modifiers: ['cmd'] } : {}),
      ...(token ? { element_id: 's1:1' } : { x: 20, y: 20, coordinate_space: 'pixel', reference_width: 100, reference_height: 100 }),
    });
    const result = await requestAndApprove(broker, action, 'task', operation);
    if (mode === 'ended' || mode === 'revoked' || mode === 'replace_revoked') {
      assert.equal(result.status, 'outcome_unknown', mode);
      assert.deepEqual(calls.map(call => call.tool), mode === 'replace_revoked' ? ['bring_to_front', 'press_key'] : ['bring_to_front'], mode);
    } else {
      assert.equal(result.status, 'completed_known', JSON.stringify(result));
      const driverTool = operation === 'double_click' ? 'double_click' : operation === 'click_at' ? 'click' : ['type_text', 'press_key'].includes(operation) ? operation : 'right_click';
      assert.deepEqual(calls.map(call => call.tool), mode === 'replace' ? ['bring_to_front', 'press_key', 'type_text'] : ['foreground', 'double', 'click', 'type', 'key'].includes(mode) ? ['bring_to_front', driverTool] : [driverTool], mode);
      if (mode === 'replace') {
        assert.equal(calls[1].args.key, 'a'); assert.deepEqual(calls[1].args.modifiers, ['cmd']);
        assert.equal(calls[1].args.x, 20); assert.equal(calls[2].args.x, undefined);
        assert.equal(calls[2].args.y, undefined); assert.equal(calls[2].args.text, '山丘');
        assert.equal(calls[2].args.input_mode, undefined);
      }
      assert.equal(calls.at(-1).args.delivery_mode, delivery_mode);
      assert.equal(calls.at(-1).args.action, undefined);
    }
    for (const call of calls) { assert.equal(call.args.pid, 42); assert.equal(call.args.window_id, 7); }
    if (mode === 'foreground') {
      const ledger = fs.readFileSync(broker.ledgerPath, 'utf8');
      assert.equal(ledger.includes('exact_menu_target_foreground'), true);
    }
  }
});

test('atomic keyboard input consumes the exact image contract, scales once and rejects stale/moved captures', async () => {
  for (const scale of [1, 2]) for (const operation of ['type_text', 'press_key']) {
    let capture = 0; const writes = []; let moved = false;
    const app = { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' };
    const bounds = { x: 0, y: 0, width: 100, height: 50 };
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-atomic-')), driver: { call: async (tool, args) => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds: moved ? { ...bounds, x: 10 } : bounds }] };
      if (tool === 'get_window_state') return { capture_id: `cap-${++capture}`, elements: [], window_bounds: bounds,
        screenshot_width: 100 * scale, screenshot_height: 50 * scale, _image: fakePng(100 * scale, 50 * scale) };
      writes.push({ tool, args }); return {};
    } } }));
    const request = (op, fields = {}) => ({ ...message(broker, op, { task_id: 'task', delivery_mode: 'foreground', ...fields }), request_id: crypto.randomBytes(16).toString('hex') });
    const apps = await broker.handle(request('list_apps'));
    const windows = await broker.handle(request('list_windows', { target_ref: apps.result.apps[0].target_ref }));
    const target_ref = windows.result.windows[0].target_ref;
    const observe = () => requestAndApprove(broker, request('observe', { target_ref }), 'task', 'observe');
    const observed = await observe();
    const input = { target_ref, observation_id: observed.result.observation_ref, x: 0.25, y: 0.5,
      coordinate_space: 'normalized', reference_width: 100 * scale, reference_height: 50 * scale,
      ...(operation === 'type_text' ? { text: '山丘' } : { key: 'return' }) };
    assert.equal((await broker.handle(request(operation, { ...input, y: undefined }))).executed, false);
    assert.equal((await broker.handle(request(operation, { ...input, element_id: 's1:1' }))).executed, false);
    assert.equal(writes.length, 0);
    const result = await requestAndApprove(broker, request(operation, input), 'task', operation);
    assert.equal(result.status, 'completed_known', JSON.stringify(result));
    assert.equal(writes.length, 2); assert.equal(writes[0].tool, 'bring_to_front');
    assert.equal(writes[1].tool, operation);
    assert.equal(writes[1].args.x, 25 * scale); assert.equal(writes[1].args.y, 25 * scale);
    assert.equal(writes[1].args.window_id, 7); assert.equal(writes[1].args.reference_width, undefined);
    assert.equal(writes[1].args.capture_binding, undefined);
    const newer = await observe();
    assert.equal((await requestAndApprove(broker, request(operation, input), 'task', operation)).executed, false);
    assert.equal(writes.length, 2);
    const current = await observe(); moved = true;
    assert.equal((await requestAndApprove(broker, request(operation, { ...input, observation_id: current.result.observation_ref }), 'task', operation)).executed, false);
    assert.equal(writes.length, 2); assert.notEqual(newer.result.observation_ref, current.result.observation_ref);
  }
});

test('end_task during asynchronous native preflight fences already-approved writes', async () => {
  for (const terminal of ['end_task', 'revoke']) {
  for (const inputOperation of ['double_click', 'type_text', 'press_key']) {
  for (const blockedTool of ['check_permissions', 'list_apps', 'list_windows']) {
    let block = false; let entered; let resume; let writes = 0;
    const reached = new Promise(resolve => { entered = resolve; });
    const barrier = new Promise(resolve => { resume = resolve; });
    const app = { bundle_id: 'fixture.app', pid: 42, process_start_identity: 'start-1' };
    const bounds = { x: 0, y: 0, width: 50, height: 50 };
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-task-fence-')), driver: { call: async tool => {
      if (block && tool === blockedTool) { block = false; entered(); await barrier; }
      if (tool === 'check_permissions') return permissions;
      if (tool === 'list_apps') return { apps: [app] };
      if (tool === 'list_windows') return { windows: [{ pid: 42, window_id: 7, bounds }] };
      if (tool === 'get_window_state') return { capture_id: 'cap', elements: [{ element_token: 's1:1', label: 'Item' }], window_bounds: bounds, screenshot_width: 100, screenshot_height: 100, _image: fakePng(100, 100) };
      if (tool === 'end_session') return { active: false };
      writes += 1; return {};
    } } }));
    const request = (operation, payload) => ({ ...message(broker, operation, { task_id: 'task', ...payload }), request_id: crypto.randomBytes(16).toString('hex') });
    const apps = await broker.handle(request('list_apps', {}));
    const windows = await broker.handle(request('list_windows', { target_ref: apps.result.apps[0].target_ref }));
    const target_ref = windows.result.windows[0].target_ref;
    const observed = await requestAndApprove(broker, request('observe', { target_ref, authorization: { purpose: 'app_task', target_label: 'open Item', controls: [{ label: 'Item', operations: ['double_click'] }] } }), 'task', 'observe');
    const pixel = blockedTool === 'list_windows';
    const action = request(inputOperation, { target_ref, observation_id: observed.result.observation_ref,
      ...(inputOperation === 'type_text' ? { text: 'query' } : inputOperation === 'press_key' ? { key: 'return' } : {}),
      ...(pixel ? { x: 20, y: 20, coordinate_space: 'pixel', reference_width: 100, reference_height: 100 } : { element_id: 's1:1' }) });
    block = true;
    const result = requestAndApprove(broker, action, 'task', inputOperation);
    await reached;
    if (terminal === 'revoke') broker.revoke('test');
    else await broker.handle(request('end_task', { terminal_id: 'finished' }));
    resume();
    const response = await result;
    assert.equal(response.executed, false, blockedTool);
    assert.equal(response.status, 'cancelled', `${terminal}/${inputOperation}/${blockedTool}`);
    assert.equal(writes, 0, blockedTool);
    assert.equal(broker.publicState().pending_approval, null);
    assert.equal(broker.grants.size, 0);
  }
  }
  }
});

test('every task purpose requires exact approval for commit keys, controls and unknown coordinates', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-commit-boundaries-'));
  const file = path.join(root, 'fixture.txt'); fs.writeFileSync(file, 'fixture');
  for (const [bundle_id, authorization] of [
    ['fixture.app', { purpose: 'app_task', target_label: 'task', controls: [{ label: 'Send', operations: ['click_element'] }] }],
    ['com.netease.163music', { purpose: 'media_search_play', query: '山丘' }],
    ['com.apple.finder', { purpose: 'local_document_read', file_root: root, file_path: file }],
    ['com.tencent.xinWeChat', { purpose: 'direct_message_send', recipient: '宝贝', message_text: '你好' }],
  ]) {
    let writes = 0;
    const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'get_window_state') return { elements: [{ element_token: 's1:1', label: 'Send' }] };
      writes += 1; return {};
    } } }));
    const base = { task_id: 'task', app: { bundle_id, pid: 42, process_start_identity: 'same-second' }, window: { window_id: 7 } };
    const request = (operation, extra) => ({ ...message(broker, operation, { ...base, ...extra }), request_id: crypto.randomBytes(16).toString('hex') });
    const observed = await requestAndApprove(broker, request('observe', { authorization: { display_name: 'fixture', target_label: 'bounded task', ...authorization } }), 'task', 'observe');
    assert.ok(broker.taskGrants.get('task'));
    for (const [operation, extra] of [
      ['click_element', { element_id: 's1:1' }], ['press_key', { key: 'return' }], ['press_key', { key: 'space' }],
      ['click_at', { x: 0.5, y: 0.5, coordinate_space: 'normalized', reference_width: 100, reference_height: 100 }],
    ]) {
      const result = broker.handle(request(operation, { observation_id: observed.result.observation_ref, ...extra }));
      await new Promise(resolve => setImmediate(resolve));
      const pending = broker.publicState().pending_approval;
      assert.ok(pending, `${authorization.purpose}:${operation}`);
      assert.equal(pending.action_summary.approval_kind, 'action');
      assert.equal(writes, 0);
      if (operation === 'click_element') assert.match(pending.action_summary.exact_action, /点击“Send”/);
      if (operation === 'press_key') assert.match(pending.action_summary.exact_action, new RegExp(extra.key));
      if (operation === 'click_at') assert.match(pending.action_summary.exact_action, /按截图位置执行/);
      assert.doesNotMatch(pending.action_summary.exact_action, /PID|s1:1|观察：|normalized|foreground|click_element|click_at/);
      broker.rejectPending(pending); await result;
    }
  }
});

test('mouse send is single-use across routes even after an unknown delivery', async () => {
  for (const unknown of [false, true]) {
    let writes = 0;
    const broker = ready(new ComputerBroker({ runtimeRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-send-once-')), driver: { call: async tool => {
      if (tool === 'check_permissions') return permissions;
      if (tool === 'get_window_state') return { elements: [{ element_token: 's1:1', label: '发送' }] };
      writes += 1;
      if (unknown) throw Object.assign(new Error('timeout'), { code: 'outcome_unknown' });
      return {};
    } } }));
    const base = { task_id: 'task', app: { bundle_id: 'com.tencent.xinWeChat', pid: 42, process_start_identity: 'wx' }, window: { window_id: 7 } };
    const request = (operation, extra) => ({ ...message(broker, operation, { ...base, ...extra }), request_id: crypto.randomBytes(16).toString('hex') });
    const observed = await requestAndApprove(broker, request('observe', { authorization: { purpose: 'direct_message_send', display_name: '微信', target_label: '单次消息', recipient: '宝贝', message_text: '你好' } }), 'task', 'observe');
    // A previously verified draft is the precondition under test, not a real send.
    broker.taskGrants.get('task').message_typed = true;
    const payload = { observation_id: observed.result.observation_ref, element_id: 's1:1' };
    const result = await requestAndApprove(broker, request('click_element', payload), 'task', 'click_element');
    assert.equal(result.status, unknown ? 'outcome_unknown' : 'completed_known');
    assert.equal(broker.taskGrants.get('task').sent, true);
    for (const [operation, extra] of [['press_key', { key: 'return' }], ['click_element', payload], ['double_click', payload]]) {
      assert.equal((await broker.handle(request(operation, extra))).error_code, 'message_already_sent');
    }
    assert.equal(writes, 1);
  }
});

test('exact sensitive approval text is local-only and cannot leak through model status or audit', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-private-approval-'));
  const secret = 'private-text-你好-<script>\n' + 'long'.repeat(1000);
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => tool === 'check_permissions' ? permissions : {} } }));
  const action = message(broker, 'type_text', { task_id: 'task', app: { bundle_id: 'fixture.app', pid: 42 }, text: secret });
  const waiting = broker.handle(action);
  await new Promise(resolve => setImmediate(resolve));
  const pending = broker.publicState().pending_approval;
  assert.ok(pending.action_summary.exact_action.includes(JSON.stringify(secret)));
  const status = await broker.handle({ ...message(broker, 'status', { task_id: 'task' }), request_id: 'b'.repeat(32) });
  assert.equal(status.result.pending_approval.action_summary, undefined);
  assert.equal(JSON.stringify(status).includes('private-text'), false);
  approve(broker, 'task', 'type_text');
  const result = await waiting;
  assert.equal(JSON.stringify(result).includes('private-text'), false);
  assert.equal(fs.readFileSync(broker.ledgerPath, 'utf8').includes('private-text'), false);
});

test('revoke while a write waits in the host queue causes zero late Driver dispatches', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-computer-queue-revoke-'));
  let writeCalls = 0; let releaseFirst;
  const firstBlocked = new Promise(resolve => { releaseFirst = resolve; });
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: {
    call: async tool => {
      if (tool === 'check_permissions') return permissions;
      writeCalls += 1;
      if (writeCalls === 1) await firstBlocked;
      return { summary: 'done' };
    },
    stop: async () => {},
  } }));
  const app = { bundle_id: 'fixture.app' };
  const first = requestAndApprove(broker, message(broker, 'type_text', { task_id: 'one', app, text: 'one', delivery_mode: 'background' }), 'one', 'type_text');
  while (writeCalls === 0) await new Promise(resolve => setImmediate(resolve));
  const secondRequest = message(broker, 'type_text', { task_id: 'two', app, text: 'two', delivery_mode: 'background' });
  const second = requestAndApprove(broker, secondRequest, 'two', 'type_text');
  await new Promise(resolve => setImmediate(resolve));
  broker.revoke('user_stop');
  releaseFirst();
  assert.equal((await first).status, 'outcome_unknown');
  const secondResult = await second;
  assert.equal(secondResult.status, 'cancelled'); assert.equal(secondResult.error_code, 'control_revoked');
  assert.equal(writeCalls, 1);
  const ledger = fs.readFileSync(broker.ledgerPath, 'utf8');
  assert.equal(ledger.includes(secondRequest.action_id), false);
});

test('official Driver refusal preserves bounded recovery without exposing native text', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-upstream-refusal-'));
  const executable = path.join(root, 'cua-driver');
  fs.writeFileSync(executable, 'fixture');
  let reply;
  let resolveExit;
  const exit = new Promise(resolve => { resolveExit = resolve; });
  class EmbeddedCuaDriverHost {
    async start() { return { socketPath: '/tmp/fixture.sock', pid: 77, generation: 'generation', driverVersion: '0.35.2', contractVersion: '0.8.0' }; }
    waitForExit() { return exit; }
    async stop() { resolveExit({ success: true }); }
    uniffiDestroy() {}
  }
  const adapter = new McpDriverAdapter({ executable, sdkLoader: async () => ({
    EmbeddedCuaDriverHost, CuaDriver: { connect: () => ({
      metadata: async () => ({ embedded: true, pid: 77, driverVersion: '0.35.2', hostBundleId: 'com.tcode.desktop' }),
      callTool: async () => reply, uniffiDestroy() {},
    }) },
  }) });
  try {
    for (const action of [undefined, { effect: 4, delivery: { mode: 0 }, error: { code: 'background_unavailable' } }]) {
      reply = { isError: true, text: 'private native text', action, images: [],
        structuredJson: JSON.stringify({ effect: 'refused', escalation: { recommended: 'px', reason: 'private native text' }, delivered_chars: 0, retryable: true }), rawJson: '{}' };
      await assert.rejects(adapter.call('type_text', { text: 'query' }), error => {
        assert.deepEqual(error.publicResult, { escalation: { recommended: 'px' }, delivered_chars: 0, retryable: true });
        assert.equal(JSON.stringify(error.publicResult).includes('private'), false);
        return true;
      });
    }
    reply = { isError: false, images: [], structuredJson: JSON.stringify({ effect: 'unverifiable', escalation: { recommended: 'px' } }), rawJson: '{}' };
    assert.equal((await adapter.call('type_text', { text: 'query' })).escalation.recommended, 'px');
    reply = { isError: false, images: [], action: { effect: 2, delivery: { mode: 0 } },
      structuredJson: JSON.stringify({ route: 'synthetic_events' }), rawJson: '{}' };
    const pointer = await adapter.call('click', { x: 20, y: 30, delivery_mode: 'background' });
    assert.equal(pointer.escalation.recommended, 'foreground');
    assert.equal(pointer.action_receipt.verification, 'pending');
    reply.action.delivery.mode = 1;
    assert.equal((await adapter.call('click', { x: 20, y: 30, delivery_mode: 'foreground' })).escalation, undefined);
  } finally { await adapter.stop(); fs.rmSync(root, { recursive: true, force: true }); }
});


test('launched installed-app ref survives a known foreground confirmation failure', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-launch-recovery-'));
  let launches = 0;
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async (tool, args) => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_apps') return { apps: [{ bundle_id: 'fixture.app', pid: 0, running: false }] };
    if (tool === 'launch_app') { launches++; return { pid: 42, process_start_identity: 'start-42' }; }
    if (tool === 'list_windows') return { windows: args.pid === 42 ? [{ pid: 42, window_id: 7, bounds: { x: 0, y: 0, width: 50, height: 50 } }] : [] };
    if (tool === 'bring_to_front') throw Object.assign(new Error('focus changed'), { code: 'failed_known', safeCode: 'bring_to_front_exact_window_unverified' });
    return {};
  } } }));
  try {
    const request = (operation, payload) => ({ ...message(broker, operation, payload), request_id: crypto.randomBytes(16).toString('hex') });
    const apps = await broker.handle(request('list_apps', { task_id: 'task', query: 'fixture' }));
    const ref = apps.result.apps[0].target_ref;
    const activation = await requestAndApprove(broker, request('activate_app', { task_id: 'task', target_ref: ref, delivery_mode: 'foreground' }), 'task', 'activate_app');
    assert.equal(activation.error_code, 'bring_to_front_exact_window_unverified');
    const windows = await broker.handle(request('list_windows', { task_id: 'task', target_ref: ref }));
    assert.equal(windows.result.windows.length, 1);
    assert.equal(launches, 1);
  } finally { await broker.stop(); fs.rmSync(root, { recursive: true, force: true }); }
});

test('preview uses only current observed capture and disappears on revocation', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-preview-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async () => permissions } }));
  const file = path.join(root, 'preview.png'); fs.writeFileSync(file, 'fixture');
  broker.latestPreview = { model_image_path: file, task_id: 'task', app: 'fixture.app', at: 123, epoch: broker.epoch, generation: broker.generation };
  assert.equal(broker.preview().observed_at, 123);
  broker.revoke('user_takeover');
  assert.equal(broker.preview(), null);
  fs.rmSync(root, { recursive: true, force: true });
});


test('observation without a coordinate contract directs rediscovery, not pixel retries', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-hidden-surface-'));
  const broker = ready(new ComputerBroker({ runtimeRoot: root, driver: { call: async tool => {
    if (tool === 'check_permissions') return permissions;
    if (tool === 'list_windows') return { windows: [{ window_id: 7 }] };
    return { screenshot_width: 100, screenshot_height: 100, elements: [], _image: fakePng(100, 100),
      background_input: { observation: { frame_freshness: 'unknown' } } };
  } } }));
  try {
    const result = await requestAndApprove(broker, message(broker, 'observe', {
      task_id: 'hidden', app: { bundle_id: 'fixture.app' }, window: { window_id: 7 },
    }), 'hidden', 'observe');
    assert.equal(result.result.coordinate_contract, undefined);
    assert.equal(result.result.actionable_elements_available, false);
    assert.equal(result.result.model_image_path, undefined);
    assert.match(result.result._note, /Rediscover the app and list_windows/);
    assert.match(result.result._note, /does not authorize pixel input/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
