const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const PROTOCOL = 1;
const MAX_FRAME = 4 * 1024 * 1024;
const READ_ONLY = new Set(['capabilities', 'list_apps', 'list_windows', 'observe', 'status', 'query_action']);
const WRITE = new Set(['click_element', 'right_click', 'open_menu', 'select_option', 'set_value', 'click_at', 'double_click', 'scroll', 'type_text', 'press_key', 'activate_app']);
const pixelInput = (operation, payload) => ['type_text', 'press_key'].includes(operation)
  && ['x', 'y', 'coordinate_space', 'reference_width', 'reference_height'].some(key => payload[key] !== undefined);
const pixelAction = (operation, payload) => operation === 'click_at'
  || ['double_click', 'right_click'].includes(operation) && !payload.element_id || pixelInput(operation, payload);
const OPERATIONS = new Set([...READ_ONLY, ...WRITE, 'release_control']);
const INTERNAL_OPERATIONS = new Set(['end_task']);
const INTERACTIVE_TASK_OPERATIONS = Object.freeze([...WRITE, 'observe']);
// Legacy semantic profiles remain narrow; app_task below works with any exact
// discovered app and a user-approved, runtime-matched control allowlist.
const TASK_SCOPES = Object.freeze({
  media_search_play: { apps: new Set(['com.netease.163music']), operations: new Set(INTERACTIVE_TASK_OPERATIONS) },
  local_document_read: { apps: new Set(['com.apple.finder', 'com.apple.Preview']), operations: new Set(INTERACTIVE_TASK_OPERATIONS) },
  direct_message_send: { apps: new Set(['com.tencent.xinWeChat']), operations: new Set(INTERACTIVE_TASK_OPERATIONS) },
});
const DRIVER_TOOLS = Object.freeze({
  list_apps: 'list_apps', list_windows: 'list_windows', observe: 'get_window_state',
  click_element: 'click', right_click: 'right_click', open_menu: 'right_click', select_option: 'click', set_value: 'set_value', click_at: 'click', double_click: 'double_click', scroll: 'scroll',
  type_text: 'type_text', press_key: 'press_key', activate_app: 'bring_to_front',
});
const DRIVER_CONTRACTS = Object.freeze({
  capabilities: { identity: 'none', driver: null, readonly: true },
  list_apps: { identity: 'none', driver: 'list_apps', readonly: true },
  list_windows: { identity: 'app', driver: 'list_windows', readonly: true },
  observe: { identity: 'window', driver: 'get_window_state', readonly: true, requires: ['pid', 'window_id'] },
  activate_app: { identity: 'app_or_window', driver: 'launch_app|bring_to_front', readonly: false },
  click_element: { identity: 'observed_element', driver: 'click', readonly: false, token: true },
  right_click: { identity: 'observed_element_or_captured_window', driver: 'right_click', readonly: false, token: true, capture: true },
  open_menu: { identity: 'observed_element', driver: 'right_click(AXShowMenu)', readonly: false, token: true },
  select_option: { identity: 'observed_element', driver: 'click(action=pick)', readonly: false, token: true },
  set_value: { identity: 'observed_element', driver: 'set_value', readonly: false, token: true },
  type_text: { identity: 'window_or_observed_element', driver: 'type_text', readonly: false, optional_token: true },
  press_key: { identity: 'window_or_observed_element', driver: 'press_key', readonly: false, optional_token: true },
  scroll: { identity: 'window_or_observed_element', driver: 'scroll', readonly: false, optional_token: true },
  click_at: { identity: 'captured_window', driver: 'click', readonly: false, capture: true },
  double_click: { identity: 'observed_element_or_captured_window', driver: 'double_click', readonly: false, token: true, capture: true },
  status: { identity: 'none', driver: null, readonly: true },
  query_action: { identity: 'task_action', driver: null, readonly: true },
  release_control: { identity: 'none', driver: null, readonly: false },
  end_task: { identity: 'task_terminal', driver: 'end_session', readonly: false, internal: true },
});

function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
function now() { return Date.now(); }
function id(prefix) { return `${prefix}_${crypto.randomBytes(16).toString('hex')}`; }
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  return value;
}
function digest(value) { return crypto.createHash('sha256').update(JSON.stringify(stable(value))).digest('hex'); }
function observationSemantics(value) {
  const keys = ['role', 'subrole', 'title', 'label', 'description', 'value', 'placeholder',
    'enabled', 'selected', 'focused', 'expanded', 'checked', 'required', 'orientation'];
  const source = Array.isArray(value?.elements) ? value.elements
    : Array.isArray(value?.snapshot?.elements) ? value.snapshot.elements : [];
  const elements = source.map(item => Object.fromEntries(keys
    .filter(key => item?.[key] !== undefined && item?.[key] !== null)
    .map(key => [key, item[key]])))
    .filter(item => Object.keys(item).length)
    .sort((a, b) => JSON.stringify(stable(a)).localeCompare(JSON.stringify(stable(b))));
  const structuralRoles = new Set(['AXApplication', 'AXWindow', 'AXGroup', 'AXScrollArea', 'AXSplitGroup', 'AXUnknown']);
  const meaningful = elements.filter(item => item.role && !structuralRoles.has(String(item.role))
    && !/^AXMenu/.test(String(item.role)));
  return meaningful.length ? { ax_state: String(value?.ax_state || value?.accessibility_state || ''), elements } : null;
}
function semanticCoverage(value, semantics) {
  if (!semantics) return 'unknown';
  if (value?.elements_complete === false || value?.truncated === true || value?.degraded_reason
      || Number(value?.nodes_pending) > 0) return 'partial';
  if (value?.elements_complete === true) return 'complete';
  return 'unknown';
}

// A fresh target-window snapshot can check field contents, but never proves
// submission, playback, or that a web renderer accepted an AX write.
function driverRecoveryResult(structured) {
  const result = {};
  if (!structured || typeof structured !== 'object') return result;
  const recommended = structured.escalation?.recommended;
  if (['px', 'foreground', 'page', 'chunk', 'verify_state'].includes(recommended)) {
    result.escalation = { recommended };
  }
  for (const key of ['requested_chars', 'delivered_chars', 'synthesized_chars', 'max_chunk_chars', 'retry_from_character']) {
    if (Number.isSafeInteger(structured[key]) && structured[key] >= 0) result[key] = structured[key];
  }
  if (typeof structured.retryable === 'boolean') result.retryable = structured.retryable;
  return result;
}

const OBSERVATION_REF_PATTERN = /^obs_[a-f0-9]{32}$/;

function approvalPresentation(state, presented = '') {
  const pending = state?.pending_approval;
  const key = pending ? `${pending.approval_id}:${pending.revision || 0}` : '';
  return { key, should_present: Boolean(key && key !== presented) };
}

function resolvedApprovalPanelRequest(requestId, requestApprovalKey, resolvedApprovalKey) {
  const clear = Boolean(requestId && requestApprovalKey && requestApprovalKey === resolvedApprovalKey);
  return clear ? { request_id: 0, approval_key: '' } : {
    request_id: Number(requestId) || 0, approval_key: String(requestApprovalKey || ''),
  };
}
function safeEqual(a, b) {
  const left = Buffer.from(String(a || '')); const right = Buffer.from(String(b || ''));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function observationElementIds(result) {
  const ids = new Set();
  const pending = [result];
  let visited = 0;
  while (pending.length && visited < 10_000) {
    const value = pending.pop(); visited += 1;
    if (Array.isArray(value)) { pending.push(...value); continue; }
    if (!value || typeof value !== 'object') continue;
    for (const [key, child] of Object.entries(value)) {
      if (['id', 'element_id', 'element_token', 'token'].includes(key) && typeof child === 'string' && child) ids.add(child);
      else if (child && typeof child === 'object') pending.push(child);
    }
  }
  return ids;
}

function targetWindowElements(elements) {
  if (!Array.isArray(elements)) return [];
  const indexed = new Map(elements.map(item => [item.element_index, item]));
  return elements.filter(item => {
    const menu = /^AXMenu/.test(String(item.role || ''));
    const seen = new Set();
    let current = item;
    while (current && !seen.has(current)) {
      seen.add(current);
      if (current.role === 'AXMenuBar') return false;
      if (current.role === 'AXWindow') return true;
      current = Number.isInteger(current.parent_index) ? indexed.get(current.parent_index) : null;
    }
    // Detached menus may be global Recent Items. Keep only menu controls whose
    // ancestry proves membership of the exact window returned by the Driver.
    return !menu;
  });
}

function hostProcessStartIdentity(pid) {
  const numericPid = Number(pid);
  if (!Number.isInteger(numericPid) || numericPid <= 0 || process.platform !== 'darwin') return '';
  const probe = spawnSync('/bin/ps', ['-o', 'lstart=', '-p', String(numericPid)], {
    encoding: 'utf8', timeout: 1000, stdio: ['ignore', 'pipe', 'ignore'],
  });
  return probe.status === 0 ? String(probe.stdout || '').trim() : '';
}

const LOCALIZED_APP_NAMES = new Map();
function localizedAppNames(app) {
  const bundle = String(app?.launch_path || '');
  if (!path.isAbsolute(bundle) || !bundle.endsWith('.app')) return [];
  if (LOCALIZED_APP_NAMES.has(bundle)) return LOCALIZED_APP_NAMES.get(bundle);
  const resources = path.join(bundle, 'Contents', 'Resources');
  let locales;
  try { locales = fs.readdirSync(resources, { withFileTypes: true }); } catch { return []; }
  const names = new Set();
  for (const locale of locales) {
    if (!locale.isDirectory() || !locale.name.endsWith('.lproj') || names.size >= 16) continue;
    const strings = path.join(resources, locale.name, 'InfoPlist.strings');
    if (!fs.existsSync(strings)) continue;
    try {
      const data = fs.readFileSync(strings);
      const text = data[0] === 0xff && data[1] === 0xfe ? data.subarray(2).toString('utf16le') : data.toString('utf8');
      for (const match of text.matchAll(/"?(?:CFBundleDisplayName|CFBundleName)"?\s*=\s*"([^"]+)"/g)) names.add(match[1].slice(0, 256));
    } catch { /* malformed localization is not an app discovery failure */ }
  }
  const result = [...names].sort((a, b) => a.localeCompare(b));
  LOCALIZED_APP_NAMES.set(bundle, result);
  return result;
}

function classifyDriverError(response) {
  const code = String(response?.structuredContent?.code || response?.error?.code || '');
  if (['bring_to_front_exact_window_unverified', 'background_unavailable', 'foreground_required'].includes(code)) return code;
  const text = JSON.stringify(response || {}).toLowerCase();
  // A stale/exited target can mention the Accessibility API in its diagnostic.
  // Classify the concrete target failure before explicit TCC denial signals.
  if (/not found|no such (window|application|process)|process.*exited|target.*(missing|gone)|invalid pid/.test(text)) return 'target_not_found';
  if (/screen.?record(ing)?.*(denied|not (authorized|authorised)|permission)|accessibility.*(denied|not (authorized|authorised)|permission)|permission denied|not authorized|not authorised/.test(text)) return 'system_permission_denied';
  if (/windowserver|cgwindow/.test(text)) return 'window_server_error';
  if (/invalid (argument|parameter)|schema|additionalproperties/.test(text)) return 'driver_protocol_error';
  if (/session/.test(text)) return 'driver_session_error';
  return 'failed_known';
}

function driverErrorEvidence(response) {
  const structured = response?.structuredContent && typeof response.structuredContent === 'object'
    ? response.structuredContent : {};
  const text = response?.content?.find?.(item => item?.type === 'text' && typeof item.text === 'string')?.text;
  return {
    raw_code: String(structured.code || response?.error?.code || '').slice(0, 120),
    raw_message: String(structured.message || response?.error?.message || text || '').slice(0, 1000),
    effect: String(structured.effect || '').slice(0, 80),
    actual_delivery: String(structured.delivery_mode || structured.actual_delivery || '').slice(0, 40),
  };
}

class McpDriverAdapter {
  constructor({ executable, hostBundleId = 'com.tcode.desktop', expectedVersion = '0.35.2', sdkLoader = () => import('@trycua/cua-driver'), callTimeoutMs = 30_000 }) {
    this.executable = executable;
    this.hostBundleId = hostBundleId;
    this.expectedVersion = expectedVersion;
    this.sdkLoader = sdkLoader;
    this.callTimeoutMs = callTimeoutMs;
    this.discovery = null;
    this.host = null;
    this.driver = null;
    this.connection = null;
    this.stopping = false;
    this.lifecycleTail = Promise.resolve();
    this.lifecycleListener = () => {};
    this.enabled = true;
    this.enableRevision = 0;
    this.identity = null;
    this.activeCalls = new Map();
    this.callSequence = 0;
  }

  setLifecycleListener(listener) { this.lifecycleListener = typeof listener === 'function' ? listener : () => {}; }
  setEnabled(enabled) {
    if (enabled === this.enabled) return;
    this.enabled = enabled;
    this.enableRevision += 1;
  }

  #requireEnabled(revision) {
    if (!this.enabled || revision !== this.enableRevision) throw Object.assign(new Error('computer control is disabled'), { code: 'computer_disabled' });
  }

  async #locked(work) {
    const prior = this.lifecycleTail;
    let release;
    this.lifecycleTail = new Promise(resolve => { release = resolve; });
    await prior;
    try { return await work(); } finally { release(); }
  }

  async #startLocked(revision) {
    this.#requireEnabled(revision);
    if (this.driver && this.connection) return;
    if (!this.executable || !fs.existsSync(this.executable)) throw Object.assign(new Error('driver unavailable'), { code: 'driver_unavailable' });
    const sdk = await this.sdkLoader();
    this.#requireEnabled(revision);
    const host = new sdk.EmbeddedCuaDriverHost(this.executable, this.hostBundleId);
    this.host = host;
    try {
      const connection = await host.start();
      this.#requireEnabled(revision);
      const actualVersion = String(connection.driverVersion || '');
      if (actualVersion !== this.expectedVersion) throw Object.assign(new Error('unsupported driver version'), {
        code: 'driver_protocol_error', expectedVersion: this.expectedVersion, actualVersion: actualVersion || 'missing',
      });
      const driver = sdk.CuaDriver.connect(connection.socketPath);
      const metadata = await driver.metadata();
      if (!this.enabled || revision !== this.enableRevision) {
        driver.uniffiDestroy?.();
        this.#requireEnabled(revision);
      }
      if (!metadata?.embedded || Number(metadata.pid) !== Number(connection.pid)
        || String(metadata.driverVersion || '') !== actualVersion
        || String(metadata.hostBundleId || '') !== this.hostBundleId) {
        driver.uniffiDestroy?.();
        throw Object.assign(new Error('embedded driver identity mismatch'), { code: 'driver_protocol_error' });
      }
      this.connection = connection;
      this.driver = driver;
      this.identity = Object.freeze({ embedded: true, pid: Number(connection.pid), generation: String(connection.generation), host_bundle_id: this.hostBundleId, permission_mode: 'standard', binary_path: path.resolve(this.executable) });
      this.discovery = { serverInfo: { version: actualVersion }, contractVersion: connection.contractVersion };
      this.lifecycleListener({ type: 'ready', ...this.identity });
      this.#watchExit(host, connection.generation, driver);
    } catch (error) {
      await host.stop().catch(() => {});
      host.uniffiDestroy?.();
      if (this.host === host) this.host = null;
      throw error;
    }
  }

  async start() {
    const revision = this.enableRevision;
    return this.#locked(() => this.#startLocked(revision));
  }

  #watchExit(host, generation, driver) {
    host.waitForExit(generation).then(exit => {
      if (this.connection?.generation !== generation) return;
      driver.uniffiDestroy?.();
      this.driver = null;
      this.connection = null;
      this.identity = null;
      this.discovery = { ...this.discovery, exit };
      this.lifecycleListener({ type: 'unexpected_exit', generation: String(generation), exit });
    }).catch(() => {});
  }

  async call(tool, args) {
    const revision = this.enableRevision;
    const lease = await this.#locked(async () => {
      await this.#startLocked(revision);
      this.#requireEnabled(revision);
      const controller = new AbortController();
      let settle;
      const done = new Promise(resolve => { settle = resolve; });
      const id = ++this.callSequence;
      const value = { id, driver: this.driver, generation: String(this.connection.generation), controller, done, settle };
      this.activeCalls.set(id, value);
      return value;
    });
    const { driver, controller, generation } = lease;
    const timer = setTimeout(() => controller.abort(), this.callTimeoutMs);
    let response;
    try {
      this.#requireEnabled(revision);
      response = await driver.callTool(tool, JSON.stringify(args), { signal: controller.signal });
    }
    catch (error) {
      if (controller.signal.aborted) throw Object.assign(new Error('driver timeout'), { code: 'outcome_unknown' });
      throw Object.assign(error instanceof Error ? error : new Error('driver disconnected'), {
        code: error?.code || 'outcome_unknown', evidence: { raw_code: String(error?.code || ''), raw_message: String(error?.message || ''), effect: 'Unverifiable', actual_delivery: '' },
      });
    } finally {
      clearTimeout(timer);
      this.activeCalls.delete(lease.id);
      lease.settle();
    }
    if (controller.signal.aborted || revision !== this.enableRevision || this.connection?.generation !== generation || this.driver !== driver) {
      throw Object.assign(new Error('driver response arrived after lifecycle ended'), { code: 'outcome_unknown' });
    }
    let structured;
    try { structured = response.structuredJson ? JSON.parse(response.structuredJson) : undefined; } catch { structured = undefined; }
    let raw;
    try { raw = response.rawJson ? JSON.parse(response.rawJson) : undefined; } catch { raw = undefined; }
    const normalized = {
      isError: Boolean(response.isError),
      structuredContent: structured || raw?.structuredContent,
      content: [
        ...(response.text ? [{ type: 'text', text: response.text }] : []),
        ...(response.images || []).map(image => ({ type: 'image', data: image.dataBase64, mimeType: image.mimeType })),
      ],
      error: response.errorCode ? { code: response.errorCode, message: response.text } : undefined,
    };
    const effects = ['Confirmed', 'Partial', 'Unverifiable', 'SuspectedNoop', 'Refused'];
    const deliveries = ['background', 'foreground', 'not_applicable', 'unknown'];
    const effect = response.action ? effects[Number(response.action.effect)] || 'Unknown' : '';
    const actualDelivery = response.action?.delivery ? deliveries[Number(response.action.delivery.mode)] || 'unknown' : '';
    if (response.action && ['Refused', 'Unknown'].includes(effect)) {
      const refusedCode = String(response.action.error?.code || response.errorCode || '').slice(0, 120);
      throw Object.assign(new Error(`driver action ${effect.toLowerCase()}`), {
        code: effect === 'Unknown' ? 'outcome_unknown' : 'failed_known',
        safeCode: refusedCode || 'action_refused',
        evidence: { raw_code: refusedCode, raw_message: '', effect, actual_delivery: actualDelivery },
        detail: { action: response.action, verification: response.verification },
        publicResult: driverRecoveryResult(normalized.structuredContent),
      });
    }
    if (normalized.isError) {
      const evidence = driverErrorEvidence(normalized);
      const unknownEffect = /partial|unverifiable/i.test(evidence.effect);
      throw Object.assign(new Error('driver rejected action'), {
        code: unknownEffect ? 'outcome_unknown' : 'failed_known', safeCode: classifyDriverError(normalized), evidence, detail: normalized, publicResult: driverRecoveryResult(normalized.structuredContent),
      });
    }
    structured = normalized.structuredContent || normalized.content.find(item => item.type === 'text')?.text || raw || normalized;
    if (typeof structured === 'string') { try { structured = JSON.parse(structured); } catch { structured = { summary: structured }; } }
    const image = normalized.content.find(item => item.type === 'image' && typeof item.data === 'string');
    const result = { ...(structured || {}), ...(image ? { _image: { data: image.data, mime_type: image.mimeType || 'image/png' } } : {}) };
    if (response.action) {
      if (actualDelivery === 'unknown' && effect !== 'Confirmed') {
        throw Object.assign(new Error('native delivery is unknown'), { code: 'outcome_unknown' });
      }
      // Keep transport completion distinct from effect and goal verification.
      // Only closed, non-sensitive contract fields cross this boundary.
      result.action_receipt = {
        effect: effect === 'SuspectedNoop' ? 'suspected_noop' : effect.toLowerCase(), delivery: actualDelivery || 'not_applicable',
        verification: effect === 'Confirmed' ? 'native_confirmed' : 'pending',
      };
    }
    // Cua's PID-posted pixel route does not move the hardware pointer. Apps
    // that hit-test that pointer need the official foreground rung instead.
    if (result.route === 'synthetic_events' && result.action_receipt?.delivery === 'background'
      && result.action_receipt.verification === 'pending') {
      result.escalation = { recommended: 'foreground' };
    }
    if (tool === 'check_permissions') result._driver_identity = this.identity;
    return result;
  }

  async stop() {
    return this.#locked(async () => {
    this.stopping = true;
    try {
      const driver = this.driver; const host = this.host;
      const active = [...this.activeCalls.values()].filter(call => call.driver === driver);
      for (const call of active) call.controller.abort();
      this.driver = null; this.connection = null; this.host = null;
      this.identity = null;
      await host?.stop().catch(() => {});
      host?.uniffiDestroy?.();
      const settled = Promise.allSettled(active.map(call => call.done));
      const completed = await Promise.race([settled.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 2_000))]);
      if (completed) driver?.uniffiDestroy?.();
      else void settled.then(() => driver?.uniffiDestroy?.());
    } finally { this.stopping = false; }
    });
  }
}

class ComputerBroker {
  constructor({ runtimeRoot, driver, ledgerPath, enabled = true, maxActions = 30, maxActiveMs = 10 * 60_000, onStateChange = () => {}, onPermissionRequired = () => {}, hostPermissions = null, onTestEvidence = null }) {
    this.runtimeRoot = path.resolve(runtimeRoot);
    this.socketPath = path.join(this.runtimeRoot, 'computer', 'broker.sock');
    this.ledgerPath = ledgerPath || path.join(this.runtimeRoot, 'computer', 'ledger.jsonl');
    this.driver = driver;
    this.onTestEvidence = onTestEvidence;
    this.enabled = enabled;
    this.driver?.setEnabled?.(enabled);
    this.maxActions = maxActions;
    this.maxActiveMs = maxActiveMs;
    this.secret = crypto.randomBytes(32).toString('hex');
    this.brokerSessionId = id('cu_session');
    this.generation = 1;
    this.epoch = 1;
    this.server = null;
    this.control = 'paused';
    this.grants = new Map();
    this.taskGrants = new Map();
    this.captures = new Map();
    this.observations = new Map();
    this.targets = new Map();
    this.requests = new Map();
    this.rawActionResults = new Map();
    this.actionDigests = new Map();
    this.approvalResumes = new WeakSet();
    this.approvalRevision = 0;
    this.cancelledOwners = new Set();
    this.taskTerminals = new Map();
    this.taskFences = new Map();
    this.pendingApproval = null;
    this.taskBudgets = new Map();
    this.permissions = { status: 'checking', accessibility: null, screen_recording: null, identity_verified: false, attribution: 'unknown', host_bundle_id: '' };
    this.permissionsCheckedAt = 0;
    this.permissionsRefresh = null;
    this.inflight = new Map();
    this.dispatchTail = Promise.resolve();
    this.recovery = new Map();
    this.unknownDigests = new Set();
    this.onStateChange = onStateChange;
    this.onPermissionRequired = onPermissionRequired;
    this.hostPermissions = hostPermissions;
    this.driverGenerationKey = '';
    this.driver?.setLifecycleListener?.(event => this.#driverLifecycle(event));
    this.#loadLedger();
  }

  startupPayload() { return { socket_path: this.socketPath, secret: this.secret, enabled: true }; }
  async setEnabled(enabled) {
    if (typeof enabled !== 'boolean') throw new Error('invalid computer enabled state');
    if (enabled === this.enabled) {
      if (!enabled) await this.driverStop;
      return this.publicState();
    }
    this.enabled = enabled;
    this.driver?.setEnabled?.(enabled);
    if (!enabled) {
      this.revoke('computer_disabled');
      await Promise.all([this.driverStop, this.permissionsRefresh]);
    }
    this.onStateChange(this.publicState());
    return this.publicState();
  }
  publicState({ forModel = false } = {}) {
    const pending = this.pendingApproval;
    return {
      available: Boolean(this.driver), enabled: this.enabled, status: this.control, driver_generation: this.generation,
      control_epoch: this.epoch, pending_approval: pending ? {
        approval_id: pending.approval_id, task_id: pending.task_id,
        revision: pending.revision,
        operation: pending.operation, app: pending.app, window: pending.window,
        delivery_mode: pending.delivery_mode, parameter_digest: pending.parameter_digest,
        ...(forModel ? {} : { action_summary: pending.action_summary }), expires_at: pending.expires_at,
      } : null,
      action_budget: {
        used: Math.max(0, ...[...this.taskBudgets.values()].map(item => item.actions)),
        limit: this.maxActions,
      },
      active_scope: [...this.taskGrants.values()].find(scope => scope.expires_at > now()) ? this.#publicScope([...this.taskGrants.values()].find(scope => scope.expires_at > now())) : null,
      permissions: this.permissions,
    };
  }

  preview() {
    if (!this.enabled || ['taken_over', 'unavailable'].includes(this.control)) return null;
    const capture = this.latestPreview;
    if (capture?.epoch !== this.epoch || capture?.generation !== this.generation) return null;
    if (!capture) return null;
    try {
      const file = capture.model_image_path;
      const stat = fs.lstatSync(file);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 10 * 1024 * 1024) return null;
      return { bytes: fs.readFileSync(file), app: capture.app, observed_at: capture.at };
    } catch { return null; }
  }

  async start() {
    const dir = path.dirname(this.socketPath); fs.mkdirSync(dir, { recursive: true, mode: 0o700 }); fs.chmodSync(dir, 0o700);
    try {
      const stat = fs.lstatSync(this.socketPath);
      if (!stat.isSocket() || stat.isSymbolicLink()) throw new Error('unsafe existing computer broker path');
      fs.unlinkSync(this.socketPath);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    this.server = net.createServer(socket => this.#accept(socket));
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(this.socketPath, resolve); });
    fs.chmodSync(this.socketPath, 0o600);
    void this.refreshPermissions();
  }

  async refreshPermissions(force = false) {
    if (!this.enabled || !this.driver) return this.publicState();
    if (this.permissionsRefresh) { await this.permissionsRefresh; return this.publicState(); }
    if (!force && now() - this.permissionsCheckedAt < 5_000) return this.publicState();
    this.permissionsRefresh = this.driver.call('check_permissions', { prompt: false }).then(result => {
      if (!this.enabled) return;
      const source = result?.source && typeof result.source === 'object' ? result.source : {};
      const identity = result?._driver_identity && typeof result._driver_identity === 'object' ? result._driver_identity : {};
      const hostBundleId = String(source.host_bundle_id || '');
      const expectedHostBundleId = String(this.driver?.hostBundleId || '');
      const identityVerified = source.attribution === 'host' && source.embedded === true && identity.embedded === true
        && identity.permission_mode === 'standard' && Number(identity.pid) > 0 && Boolean(identity.generation)
        && Boolean(hostBundleId) && hostBundleId === String(identity.host_bundle_id || '')
        && (!expectedHostBundleId || hostBundleId === expectedHostBundleId)
        && path.resolve(String(identity.binary_path || '')) === path.resolve(String(this.driver?.executable || ''));
      this.permissions = {
        status: typeof result?.accessibility === 'boolean' && typeof result?.screen_recording === 'boolean' ? 'known' : 'error',
        accessibility: typeof result?.accessibility === 'boolean' ? result.accessibility : null,
        screen_recording: typeof result?.screen_recording === 'boolean' ? result.screen_recording : null, identity_verified: identityVerified,
        attribution: String(source.attribution || 'unknown'), host_bundle_id: hostBundleId,
        direct_capture_status: String(result?.direct_capture_status || 'unknown'),
      };
      if (this.hostPermissions) {
        try {
          const host = this.hostPermissions();
          this.permissions.host_accessibility = typeof host.accessibility === 'boolean' ? host.accessibility : null;
          this.permissions.host_screen_recording = typeof host.screen_recording === 'boolean' ? host.screen_recording : null;
          this.permissions.runtime_mismatch = identityVerified && (
            host.accessibility === true && this.permissions.accessibility === false
            || host.screen_recording === true && this.permissions.screen_recording === false);
        } catch { /* Host diagnostics never substitute for Driver permission checks. */ }
      }
    }, () => { if (this.enabled) this.permissions = { ...this.permissions, status: 'error' }; }).finally(() => {
      if (this.enabled) this.permissionsCheckedAt = now();
      this.permissionsRefresh = null;
    });
    await this.permissionsRefresh;
    return this.publicState();
  }

  #accept(socket) {
    socket.setEncoding('utf8'); socket.setTimeout(15_000); let buffer = ''; let authenticated = false;
    const owner = { id: id('cu_client'), settled: false };
    let disconnected = false;
    const cancelDisconnectedOwner = () => {
      if (disconnected) return;
      disconnected = true;
      if (!owner.settled) this.cancelOwner(owner.id);
    };
    socket.once('close', cancelDisconnectedOwner);
    socket.once('error', cancelDisconnectedOwner);
    const nonce = crypto.randomBytes(32).toString('hex');
    socket.write(JSON.stringify({ type: 'challenge', protocol: PROTOCOL, nonce }) + '\n');
    socket.on('data', chunk => {
      buffer += chunk;
      if (Buffer.byteLength(buffer) > MAX_FRAME) return socket.destroy();
      while (buffer.includes('\n')) {
        const index = buffer.indexOf('\n'); const line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
        let message; try { message = JSON.parse(line); } catch { return socket.destroy(); }
        if (!authenticated) {
          const expected = crypto.createHmac('sha256', Buffer.from(this.secret, 'hex')).update(`${PROTOCOL}:${nonce}`).digest('hex');
          if (message.type !== 'authenticate' || message.protocol !== PROTOCOL || !safeEqual(message.digest, expected)) return socket.destroy();
          authenticated = true; socket.setTimeout(0);
          socket.write(JSON.stringify({ type: 'welcome', broker_session_id: this.brokerSessionId, driver_generation: this.generation, control_epoch: this.epoch }) + '\n');
          continue;
        }
        void this.handle(message, owner).then(response => {
          owner.settled = true;
          socket.end(JSON.stringify(response) + '\n');
        }, () => {
          owner.settled = true;
          socket.end(JSON.stringify({ request_id: message.request_id, status: 'failed_known', error_code: 'internal_error' }) + '\n');
        });
      }
    });
  }

  grant(spec) {
    const operations = [...new Set(Array.isArray(spec.operations) ? spec.operations : [])].filter(op => OPERATIONS.has(op));
    const discoveryOnly = operations.every(op => ['capabilities', 'list_apps', 'list_windows'].includes(op));
    if (!spec.task_id || (!spec.app?.bundle_id && !discoveryOnly) || !operations.length || !/^[a-f0-9]{64}$/.test(String(spec.parameter_digest || ''))) throw new Error('invalid computer grant');
    const grant = Object.freeze({
      grant_id: id('cu_grant'), task_id: String(spec.task_id), broker_session_id: this.brokerSessionId,
      driver_generation: this.generation, control_epoch: this.epoch,
      app: { bundle_id: String(spec.app?.bundle_id || ''), pid: Number(spec.app?.pid) || 0, process_start_identity: String(spec.app?.process_start_identity || '') },
      window_handle: String(spec.window_handle || ''), operations,
      mode: spec.mode === 'foreground' ? 'foreground' : 'background',
      owner_id: String(spec.owner_id || ''),
      parameter_digest: String(spec.parameter_digest || ''), observation_id: String(spec.observation_id || ''),
      expires_at: Math.min(now() + 10 * 60_000, Number(spec.expires_at) || now() + 2 * 60_000),
    });
    this.grants.set(grant.grant_id, grant); this.control = 'running';
    return grant.grant_id;
  }

  grantPending(spec) {
    const pending = this.pendingApproval;
    if (!pending || !spec || String(spec.approval_id || '') !== pending.approval_id || String(spec.task_id || '') !== pending.task_id || String(spec.operation || '') !== pending.operation || String(spec.parameter_digest || '') !== pending.parameter_digest || pending.epoch !== this.epoch || pending.generation !== this.generation || pending.expires_at <= now()) throw new Error('pending computer approval changed');
    const singleMessage = spec.single_message;
    if (singleMessage && (!pending.scope?.continuous
      || typeof singleMessage.recipient !== 'string' || !singleMessage.recipient.trim() || singleMessage.recipient.length > 80
      || typeof singleMessage.text !== 'string' || !singleMessage.text || singleMessage.text.length > 1000
      || !pending.scope.private_task_text.includes(singleMessage.recipient)
      || !pending.scope.private_task_text.includes(singleMessage.text))) throw new Error('message intent does not match approved human task');
    clearTimeout(pending.timer);
    const grantId = this.grant({
      task_id: pending.task_id, app: pending.app, window_handle: pending.window_handle,
      operations: [pending.operation], mode: pending.delivery_mode,
      parameter_digest: pending.parameter_digest, observation_id: pending.observation_id, owner_id: pending.owner_id,
    });
    if (pending.scope) this.taskGrants.set(pending.task_id, {
      ...pending.scope, task_id: pending.task_id, broker_session_id: this.brokerSessionId,
      // Capability comes only from this bound, visible approval, never from
      // model tool arguments or a guessed application/keyword classification.
      navigation: pending.scope.continuous && spec.navigation === true,
      ...(singleMessage ? { navigation: true, recipient: singleMessage.recipient, message_text: singleMessage.text,
        commit_intent_id: digest({ task_id: pending.task_id, turn_id: pending.scope.turn_id,
          app: pending.app.bundle_id, recipient: singleMessage.recipient, text: singleMessage.text }) } : {}),
      owner_id: pending.owner_id,
      driver_generation: this.generation, control_epoch: this.epoch,
      process_identities: { [String(pending.app?.bundle_id || '')]: String(pending.app?.process_start_identity || '') },
      app_targets: { [String(pending.app?.bundle_id || '')]: { ...(pending.app || {}) } },
      last_observations: {},
      window_handle: pending.window_handle,
      mode: pending.delivery_mode,
      expires_at: Math.min(now() + 10 * 60_000, pending.expires_at), max_actions: this.maxActions,
    });
    const proposal = pending.action_summary.control_proposal;
    const scope = this.taskGrants.get(pending.task_id);
    if (scope && pending.message.payload.screenshot_service !== undefined) {
      // Keep all action/commit limits when approving a new screenshot recipient.
      scope.screenshot_service = String(pending.message.payload.screenshot_service);
    }
    if (spec.remember_control === true && proposal && scope?.turn_id && scope.expires_at > now()
      && scope.control_epoch === this.epoch && scope.driver_generation === this.generation
      && scope.window_handle === pending.window_handle && scope.controls.length < 20
      && scope.app_targets[pending.app.bundle_id]?.pid === pending.app.pid
      && scope.process_identities[pending.app.bundle_id] === pending.app.process_start_identity) {
      scope.controls = [...scope.controls, proposal];
    }
    this.pendingApproval = null;
    this.approvalResumes.add(pending.message);
    this.#ledger({ event: 'approval', approval_id: pending.approval_id, task_id: pending.task_id,
      approval_kind: pending.scope ? 'task' : 'action', navigation: Boolean(scope?.navigation),
      epoch: this.epoch, generation: this.generation, approved_at: new Date().toISOString() });
    pending.resolve(this.handle(pending.message, { id: pending.owner_id, settled: false }));
    return grantId;
  }

  cancelOwner(ownerId, reason = 'caller_disconnected') {
    const normalized = String(ownerId || '');
    if (!normalized) return false;
    this.cancelledOwners.add(normalized);
    while (this.cancelledOwners.size > 512) this.cancelledOwners.delete(this.cancelledOwners.values().next().value);
    let changed = false;
    const pending = this.pendingApproval;
    if (pending?.owner_id === normalized) {
      clearTimeout(pending.timer);
      this.pendingApproval = null; this.control = 'paused'; changed = true;
      pending.resolve(this.#response(pending.message, 'cancelled', reason, false, true));
    }
    for (const [grantId, grant] of this.grants) {
      if (grant.owner_id === normalized) { this.grants.delete(grantId); changed = true; }
    }
    for (const [taskId, scope] of this.taskGrants) {
      if (scope.owner_id === normalized) { this.taskGrants.delete(taskId); changed = true; }
    }
    for (const inflight of this.inflight.values()) {
      if (inflight.owner_id === normalized) { inflight.revoked = true; changed = true; }
    }
    if (changed) this.onStateChange(this.publicState());
    return changed;
  }

  async #endTask(message, payload) {
    const taskId = String(payload.task_id || '');
    const terminalId = String(payload.terminal_id || '');
    const prior = this.taskTerminals.get(taskId);
    if (prior === terminalId) return this.#response(message, 'completed_known', '', false, true, {
      task_id: taskId, terminal_id: terminalId, already_ended: true,
    });
    const driverSession = this.#driverSession(taskId);
    const fence = this.taskFences.get(taskId);
    if (fence) fence.ended = true;
    this.taskFences.delete(taskId);
    const pending = this.pendingApproval;
    if (pending?.task_id === taskId) {
      clearTimeout(pending.timer); this.pendingApproval = null;
      pending.resolve(this.#response(pending.message, 'cancelled', 'task_ended', false, true));
    }
    for (const [grantId, grant] of this.grants) if (grant.task_id === taskId) this.grants.delete(grantId);
    this.taskGrants.delete(taskId);
    for (const [ref, target] of this.targets) if (target.task_id === taskId) this.targets.delete(ref);
    for (const [ref, observation] of this.observations) if (observation.task_id === taskId) this.observations.delete(ref);
    const capturePaths = [...(fence?.observation_images || [])];
    if (this.latestPreview?.task_id === taskId) {
      capturePaths.push(this.latestPreview.model_image_path); this.latestPreview = null;
    }
    for (const [captureId, capture] of this.captures) if (capture.task_id === taskId) {
      if (capture.model_image_path) capturePaths.push(capture.model_image_path);
      this.captures.delete(captureId);
    }
    this.recovery.delete(taskId);
    this.taskBudgets.delete(taskId);
    for (const inflight of this.inflight.values()) if (inflight.task_id === taskId) inflight.revoked = true;
    this.taskTerminals.set(taskId, terminalId);
    while (this.taskTerminals.size > 2048) this.taskTerminals.delete(this.taskTerminals.keys().next().value);
    if (!this.pendingApproval && this.inflight.size === 0) this.control = 'paused';
    this.onStateChange(this.publicState());
    let captureCleanupFailures = 0;
    for (const file of capturePaths) {
      try { fs.unlinkSync(file); } catch (error) { if (error.code !== 'ENOENT') captureCleanupFailures += 1; }
    }
    let driverSessionEnded = false;
    let driverSessionCleanupFailed = false;
    if (this.enabled) {
      try {
        const ended = await this.driver.call('end_session', { session: driverSession });
        driverSessionEnded = ended?.active === false;
      } catch { driverSessionCleanupFailed = true; /* Logical revocation already completed. */ }
    }
    return this.#response(message, 'completed_known', '', false, true, {
      task_id: taskId, terminal_id: terminalId, already_ended: false, driver_session_ended: driverSessionEnded,
      cleanup: { capture_file_failures: captureCleanupFailures, driver_session_failed: driverSessionCleanupFailed },
    });
  }

  rejectPending(spec, reason = 'approval_denied') {
    const pending = this.pendingApproval;
    if (!pending || String(spec?.approval_id || '') !== pending.approval_id || String(spec?.parameter_digest || '') !== pending.parameter_digest) throw new Error('pending computer approval changed');
    clearTimeout(pending.timer); this.pendingApproval = null; this.control = 'paused';
    pending.resolve(this.#response(pending.message, 'rejected', reason, false, true));
    return this.publicState();
  }

  revoke(reason = 'control_revoked') {
    const pending = this.pendingApproval;
    if (pending) {
      clearTimeout(pending.timer);
      pending.resolve(this.#response(pending.message, 'rejected', 'control_revoked', false, true));
    }
    this.epoch += 1; this.control = reason === 'user_takeover' ? 'taken_over' : 'paused';
    this.grants.clear(); this.taskGrants.clear(); this.captures.clear(); this.observations.clear(); this.targets.clear(); this.latestPreview = null; this.pendingApproval = null;
    this.recovery.clear(); this.unknownDigests.clear(); this.taskBudgets.clear();
    for (const entry of this.inflight.values()) entry.revoked = true;
    this.generation += 1;
    this.driverStop = Promise.resolve(this.driver?.stop?.());
    void this.driverStop.catch(() => {});
    this.permissions = { ...this.permissions, status: 'checking' }; this.permissionsCheckedAt = 0;
    this.#cleanupCaptures();
    return this.publicState();
  }

  #driverLifecycle(event) {
    const generation = String(event?.generation || '');
    if (!generation || event.type === 'ready' && generation === this.driverGenerationKey) return;
    this.driverGenerationKey = event.type === 'ready' ? generation : '';
    const pending = this.pendingApproval;
    if (pending) {
      clearTimeout(pending.timer);
      pending.resolve(this.#response(pending.message, 'rejected', 'driver_generation_changed', false, true));
    }
    this.epoch += 1; this.generation += 1; this.control = 'paused';
    this.grants.clear(); this.taskGrants.clear(); this.captures.clear(); this.observations.clear(); this.targets.clear(); this.latestPreview = null;
    this.pendingApproval = null; this.recovery.clear(); this.unknownDigests.clear(); this.taskBudgets.clear();
    for (const entry of this.inflight.values()) entry.revoked = true;
    this.permissions = { ...this.permissions, status: 'checking', identity_verified: false };
    this.permissionsCheckedAt = 0; this.#cleanupCaptures(); this.onStateChange(this.publicState());
  }

  rotateSession(reason = 'backend_restart') {
    this.revoke(reason); this.secret = crypto.randomBytes(32).toString('hex'); this.brokerSessionId = id('cu_session');
    return this.publicState();
  }

  #findGrant(operation, payload) {
    for (const grant of this.grants.values()) {
      if (grant.task_id !== String(payload.task_id || '') || grant.control_epoch !== this.epoch || grant.driver_generation !== this.generation || grant.expires_at <= now()) continue;
      if (!grant.operations.includes(operation) || grant.mode !== (payload.delivery_mode || 'background')) continue;
      if (!grant.app.bundle_id && !['capabilities', 'list_apps', 'list_windows'].includes(operation)) continue;
      if (grant.app.bundle_id && payload.app?.bundle_id !== grant.app.bundle_id) continue;
      if (grant.app.pid && Number(payload.app?.pid) !== grant.app.pid) continue;
      if (grant.app.process_start_identity && payload.app?.process_start_identity !== grant.app.process_start_identity) continue;
      if (grant.window_handle && String(payload.window?.handle || payload.window?.window_id || '') !== grant.window_handle) continue;
      if (grant.parameter_digest !== this.#parameterDigest(payload)) continue;
      if (grant.observation_id && String(payload.observation_id || '') !== grant.observation_id) continue;
      return grant;
    }
    return null;
  }

  #scopeFromPayload(payload) {
    if (payload.task_intent) {
      const intent = payload.task_intent;
      const expected = crypto.createHmac('sha256', Buffer.from(this.secret, 'hex')).update(JSON.stringify(stable(intent))).digest('hex');
      if (!safeEqual(String(payload.task_intent_proof || ''), expected)
        || intent.task_id !== payload.task_id || typeof intent.user_message_id !== 'string' || !intent.user_message_id
        || intent.turn_id !== `turn-${intent.user_message_id}` || typeof intent.text !== 'string' || !intent.text.trim() || intent.text.length > 100_000) return null;
      return Object.freeze({ purpose: 'app_task', display_name: String(payload.app?.name || payload.app?.bundle_id || ''),
        screenshot_service: String(payload.screenshot_service || ''),
        target_label: '用户请求的电脑任务', private_task_text: intent.text, turn_id: intent.turn_id,
        allowed_apps: [String(payload.app?.bundle_id || '')], operations: [...INTERACTIVE_TASK_OPERATIONS],
        window_scope: 'same_app', continuous: true, controls: [],
      });
    }
    const raw = payload.authorization;
    if (!raw || typeof raw !== 'object') return null;
    const purpose = String(raw.purpose || '');
    const bundleId = String(payload.app?.bundle_id || '');
    const policy = TASK_SCOPES[purpose];
    if (purpose === 'app_task') {
      const controls = Array.isArray(raw.controls) ? raw.controls : [];
      const allowed = new Set(['click_element', 'double_click', 'right_click', 'open_menu', 'select_option', 'set_value', 'type_text']);
      if (!bundleId || !String(raw.target_label || '').trim() || controls.length > 20
        || controls.some(rule => !rule || typeof rule.label !== 'string' || !rule.label.trim() || rule.label.length > 160
          || !Array.isArray(rule.operations) || !rule.operations.length || rule.operations.some(op => !allowed.has(op))
          || rule.operations.some(op => ['set_value', 'type_text'].includes(op)) && typeof rule.text !== 'string'
          || String(rule.text || '').length > 1000)) return null;
      return Object.freeze({ purpose, display_name: String(payload.app?.name || bundleId),
        screenshot_service: String(payload.screenshot_service || ''),
        target_label: String(raw.target_label).trim().slice(0, 160), allowed_apps: [bundleId],
        operations: [...INTERACTIVE_TASK_OPERATIONS], window_scope: raw.window_scope === 'same_app' ? 'same_app' : 'current',
        controls: controls.map(rule => ({ label: rule.label.trim(), operations: [...new Set(rule.operations)],
          ...(typeof rule.text === 'string' ? { text: rule.text } : {}) })),
      });
    }
    if (!policy || !policy.apps.has(bundleId)) return null;
    const displayName = String(raw.display_name || '').trim().slice(0, 80);
    const targetLabel = String(raw.target_label || '').trim().slice(0, 160);
    let fileRoot = String(raw.file_root || '').trim().slice(0, 512);
    let filePath = String(raw.file_path || '').trim().slice(0, 1024);
    if (!displayName || !targetLabel) return null;
    if (purpose === 'local_document_read') {
      if (!path.isAbsolute(fileRoot) || !path.isAbsolute(filePath)) return null;
      try {
        fileRoot = fs.realpathSync(fileRoot);
        filePath = fs.realpathSync(filePath);
        if (!fs.statSync(fileRoot).isDirectory() || !fs.statSync(filePath).isFile()) return null;
      } catch { return null; }
      const relative = path.relative(fileRoot, filePath);
      if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return null;
    }
    const query = String(raw.query || '').trim().slice(0, 80);
    if (purpose === 'media_search_play' && !query) return null;
    const recipient = String(raw.recipient || '').trim().slice(0, 80);
    const messageText = String(raw.message_text || '').slice(0, 1000);
    if (purpose === 'direct_message_send' && (!recipient || !messageText)) return null;
    return Object.freeze({ purpose, screenshot_service: String(payload.screenshot_service || ''), display_name: displayName, target_label: targetLabel, file_root: fileRoot, file_path: filePath, query, recipient, message_text: messageText, allowed_apps: [...policy.apps], operations: [...policy.operations] });
  }

  #findTaskGrant(operation, payload) {
    const scope = this.taskGrants.get(String(payload.task_id || ''));
    if (!scope || scope.broker_session_id !== this.brokerSessionId || scope.control_epoch !== this.epoch || scope.driver_generation !== this.generation || scope.expires_at <= now()) return null;
    if (String(scope.screenshot_service || '') !== String(payload.screenshot_service || '')) return null;
    const bundleId = String(payload.app?.bundle_id || '');
    if (!scope.allowed_apps.includes(bundleId) || !scope.operations.includes(operation)) return null;
    if (!scope.continuous && scope.mode !== String(payload.delivery_mode || 'background')) return null;
    const processIdentity = String(payload.app?.process_start_identity || '');
    const knownIdentity = scope.process_identities[bundleId] || '';
    const knownPid = Number(scope.app_targets[bundleId]?.pid) || 0;
    if (knownPid && Number(payload.app?.pid) !== knownPid) return null;
    if (knownIdentity && processIdentity !== knownIdentity) return null;
    if (!knownIdentity && processIdentity) scope.process_identities[bundleId] = processIdentity;
    if (this.#actionBoundary(operation, payload, scope).requiresApproval) return null;
    if (scope.purpose === 'app_task') {
      const window = String(payload.window?.handle || payload.window?.window_id || '');
      if (scope.window_scope !== 'same_app' && (!scope.window_handle || window !== scope.window_handle)) return null;
      if (['observe', 'activate_app'].includes(operation)) return scope;
      const observed = scope.last_observations[bundleId];
      if (!observed || now() - observed.at > 60_000 || observed.window !== window) return null;
      if (!payload.observation_id) payload.observation_id = observed.observation_id;
      if (payload.observation_id !== observed.observation_id) return null;
      if (operation === 'scroll') return scope;
      if (scope.commit_intent_id && ['type_text', 'set_value'].includes(operation)) {
        if (payload.text === scope.recipient && !scope.message_attempted) return scope;
        if (payload.text === scope.message_text && !scope.message_attempted && scope.recipient_verified) return scope;
        return null;
      }
      if (scope.navigation) {
        if (operation === 'press_key') {
          const key = String(payload.key || '').toLowerCase();
          const modifiers = [...new Set(payload.modifiers || [])].sort().join('+');
          if (!modifiers && ['enter', 'return', 'space', 'escape', 'tab', 'left', 'right', 'up', 'down'].includes(key)
            || modifiers === 'cmd' && ['a', 'f'].includes(key)) return scope;
        }
        if (!payload.element_id && ['click_at', 'double_click', 'right_click'].includes(operation)) return scope;
        if (['type_text', 'set_value'].includes(operation) && payload.text
          && scope.private_task_text.includes(payload.text)
          && !/Secure/i.test(this.#observedControl(payload)?.role || '')) return scope;
      }
      if (operation === 'press_key') return !(payload.modifiers || []).length
        && ['escape', 'tab', 'left', 'right', 'up', 'down'].includes(String(payload.key || '').toLowerCase()) ? scope : null;
      // Coordinates and unmatched/ambiguous labels cannot inherit consent.
      const elements = this.observations.get(observed.observation_id)?.controls || [];
      const element = this.#observedControl(payload);
      if (!element || element.enabled === false || elements.filter(item => item.label === element.label).length !== 1) return null;
      if (scope.continuous) {
        if (['type_text', 'set_value'].includes(operation)
          && (!/TextField|TextArea|ComboBox/.test(element.role) || /Secure/i.test(element.role))) return null;
        return scope;
      }
      const rule = scope.controls.find(item => item.label === element.label && item.operations.includes(operation));
      if (!rule || ['set_value', 'type_text'].includes(operation) && payload.text !== rule.text) return null;
      return scope;
    }
    if ((operation === 'type_text' || operation === 'set_value') && scope.purpose === 'media_search_play') {
      if (String(payload.text || '') !== scope.query || scope.query.length > 80) return null;
    }
    if ((operation === 'type_text' || operation === 'set_value') && scope.purpose === 'local_document_read'
      && String(payload.text || '') !== scope.file_path) return null;
    if ((operation === 'type_text' || operation === 'set_value') && scope.purpose === 'direct_message_send') {
      const text = String(payload.text || '');
      if (text === scope.recipient && !scope.recipient_typed) scope.next_text_stage = 'recipient';
      else if (text === scope.message_text && scope.recipient_selected && !scope.message_typed) scope.next_text_stage = 'message';
      else return null;
    }
    if (operation === 'press_key') {
      const key = String(payload.key || '').toLowerCase();
      const modifiers = [...new Set((payload.modifiers || []).map(value => String(value).toLowerCase()))].sort().join('+');
      const mediaKey = modifiers === 'cmd' && key === 'f' || modifiers === '' && ['enter', 'return', 'escape', 'space', 'left', 'right', 'up', 'down'].includes(key);
      const documentKey = modifiers === 'cmd+shift' && key === 'g' || modifiers === '' && ['enter', 'return', 'escape', 'space', 'left', 'right', 'up', 'down'].includes(key);
      const messageKey = modifiers === 'cmd' && key === 'f'
        || modifiers === '' && ['escape', 'left', 'right', 'up', 'down'].includes(key)
        || modifiers === '' && ['enter', 'return'].includes(key) && scope.recipient_typed && !scope.message_typed;
      if (scope.purpose === 'media_search_play' ? !mediaKey : scope.purpose === 'local_document_read' ? !documentKey : !messageKey) return null;
    }
    if (['click_element', 'click_at', 'double_click', 'type_text', 'set_value', 'scroll', 'press_key'].includes(operation)) {
      const windowHandle = String(payload.window?.handle || payload.window?.window_id || '');
      const observed = scope.last_observations[bundleId];
      if (!observed || now() - observed.at > 60_000 || (observed.window && windowHandle && observed.window !== windowHandle)) return null;
      if (!payload.observation_id) payload.observation_id = observed.observation_id;
      if (String(payload.observation_id || '') !== observed.observation_id) return null;
    }
    if ((this.taskBudgets.get(String(payload.task_id || ''))?.actions || 0) >= scope.max_actions) return null;
    return scope;
  }

  #observedControl(payload) {
    const controls = this.observations.get(String(payload.observation_id || ''))?.controls || [];
    if (payload.element_id) return controls.find(item => item.token === payload.element_id);
    if (payload.x === undefined || payload.y === undefined) return null;
    const capture = [...this.captures.values()].find(item => item.observation_id === payload.observation_id);
    const x = payload.coordinate_space === 'normalized' ? payload.x * (capture?.delivered_width || 0) : payload.x;
    const y = payload.coordinate_space === 'normalized' ? payload.y * (capture?.delivered_height || 0) : payload.y;
    const matches = controls.filter(item => {
      const box = item.screenshot_frame;
      return box && !/^AX(Window|Group|Menu|ScrollArea|SplitGroup)$/.test(item.role)
        && item.enabled !== false && x >= box.x && y >= box.y
        && x < box.x + (box.w ?? box.width) && y < box.y + (box.h ?? box.height);
    });
    const depth = Math.max(...matches.map(item => item.depth));
    const deepest = matches.filter(item => item.depth === depth);
    return deepest.length === 1 ? deepest[0] : null;
  }

  #actionBoundary(operation, payload, scope) {
    const controls = this.observations.get(String(payload.observation_id || ''))?.controls || [];
    const element = this.#observedControl(payload);
    const click = ['click_element', 'click_at', 'double_click', 'right_click', 'open_menu', 'select_option'].includes(operation);
    const commitKey = operation === 'press_key' && ['enter', 'return', 'space'].includes(String(payload.key || '').toLowerCase());
    const knownCommit = element && /(发送|提交|删除|付款|支付|购买|转账|发布|send|submit|delete|pay|purchase|transfer|publish)/i.test(element.label);
    const messageSend = Boolean(scope?.commit_intent_id && scope.recipient_verified && scope.body_verified
      && (commitKey || click && (!element || /(发送|send)/i.test(element.label))));
    // This is not a semantic classifier: unnamed, ambiguous, coordinate and
    // legacy-profile clicks have no exact control consent and need approval.
    const unknownClick = click && (scope?.purpose !== 'app_task' || !element?.label || element.enabled === false
      || controls.filter(item => item.label === element.label).length !== 1);
    return { commit: Boolean(messageSend || knownCommit || commitKey && !scope?.navigation),
      requiresApproval: Boolean(knownCommit && !messageSend || commitKey && !scope?.navigation && !messageSend
        || unknownClick && !(scope?.navigation && !payload.element_id)),
      send: messageSend || scope?.purpose === 'direct_message_send' && Boolean(knownCommit
        || (scope.message_typed || scope.message_attempted) && (click || commitKey)) };
  }

  #publicScope(scope) {
    const displayName = scope?.purpose === 'app_task' ? scope.display_name : scope?.purpose === 'media_search_play' ? '网易云音乐' : scope?.purpose === 'local_document_read' ? 'Finder 与预览' : '微信';
    const targetLabel = scope?.purpose === 'app_task' ? scope.target_label : scope?.purpose === 'media_search_play' ? `搜索并播放“${scope.query}”` : scope?.purpose === 'local_document_read' ? `打开并阅读 ${path.basename(scope.file_path)}` : `给“${scope.recipient}”发送一条消息`;
    const used = this.taskBudgets.get(String(scope?.task_id || ''))?.actions || 0;
    return scope ? { purpose: scope.purpose, display_name: displayName, target_label: targetLabel, file_root: scope.file_root || '', expires_at: scope.expires_at, remaining_actions: Math.max(0, scope.max_actions - used) } : null;
  }

  #bindLatestCapture(payload) {
    if (payload.capture_id || payload.capture_binding) {
      const supplied = this.captures.get(String(payload.capture_id || ''));
      if (supplied && supplied.binding === String(payload.capture_binding || '') && !payload.observation_id) {
        payload.observation_id = supplied.observation_id;
      }
      return;
    }
    const taskId = String(payload.task_id || '');
    const appId = String(payload.app?.bundle_id || '');
    const latest = [...this.captures.entries()].reverse().find(([, item]) =>
      !item.consumed && item.task_id === taskId && item.app === appId
      && item.epoch === this.epoch && item.generation === this.generation);
    if (!latest) return;
    const [captureId, capture] = latest;
    let windowHandle = String(payload.window?.handle || payload.window?.window_id || '');
    if (!windowHandle && capture?.window) {
      payload.window = { window_id: capture.window };
      windowHandle = capture.window;
    }
    if (!capture || capture.consumed || capture.task_id !== String(payload.task_id || '')
      || capture.app !== String(payload.app?.bundle_id || '')
      || capture.process_start_identity !== String(payload.app?.process_start_identity || '')
      || capture.window !== windowHandle || capture.epoch !== this.epoch
      || capture.generation !== this.generation) return;
    // Security tokens are host-owned bookkeeping. Models may choose coordinates
    // from the last image, but cannot weaken or substitute its exact binding.
    payload.capture_id = captureId;
    payload.capture_binding = capture.binding;
    if (payload.observation_id && String(payload.observation_id) !== capture.observation_id) return;
    payload.observation_id = capture.observation_id;
  }

  #fillUnambiguousScopedTarget(payload) {
    const scope = this.taskGrants.get(String(payload.task_id || ''));
    if (!scope || scope.allowed_apps.length !== 1) return;
    const bundleId = scope.allowed_apps[0];
    const target = scope.app_targets[bundleId];
    if (!target?.bundle_id || payload.app?.bundle_id && payload.app.bundle_id !== bundleId) return;
    payload.app = { ...target, ...(payload.app || {}) };
  }

  #validate(message) {
    if (!message || message.type !== 'request' || typeof message.request_id !== 'string' || !/^[a-f0-9]{32}$/.test(message.request_id)) return 'protocol_error';
    if ((!OPERATIONS.has(message.operation) && !INTERNAL_OPERATIONS.has(message.operation)) || !message.payload || typeof message.payload !== 'object') return 'protocol_error';
    if (WRITE.has(message.operation) && (typeof message.action_id !== 'string' || !/^[a-f0-9]{32}$/.test(message.action_id))) return 'protocol_error';
    if (message.broker_session_id !== this.brokerSessionId || message.driver_generation !== this.generation || message.control_epoch !== this.epoch) return 'control_revoked';
    const p = message.payload;
    if (p.query !== undefined && (typeof p.query !== 'string' || p.query.length > 120)) return 'protocol_error';
    if (p.cursor !== undefined && (!Number.isInteger(p.cursor) || p.cursor < 0)) return 'protocol_error';
    if (p.limit !== undefined && (!Number.isInteger(p.limit) || p.limit < 1 || p.limit > 50)) return 'protocol_error';
    for (const key of ['x', 'y', 'to_x', 'to_y', 'delta_x', 'delta_y']) if (p[key] !== undefined && !finite(p[key])) return 'protocol_error';
    if (typeof p.text === 'string' && p.text.length > 100_000) return 'protocol_error';
    if (p.input_mode !== undefined && (!['insert', 'replace'].includes(p.input_mode) || message.operation !== 'type_text')) return 'protocol_error';
    if (p.input_mode === 'replace' && (p.delivery_mode !== 'foreground' || p.element_id || !pixelInput(message.operation, p))) return 'observation_contract_required';
    if (message.operation === 'end_task' && (typeof p.task_id !== 'string' || !p.task_id
      || typeof p.terminal_id !== 'string' || !/^[a-zA-Z0-9_.:-]{1,200}$/.test(p.terminal_id))) return 'protocol_error';
    if (message.operation === 'scroll' && (!finite(p.delta_x || 0) || !finite(p.delta_y || 0)
      || (!Number(p.delta_x || 0) && !Number(p.delta_y || 0)))) return 'protocol_error';
    if (message.operation === 'click_at' && (!['pixel', 'normalized'].includes(String(p.coordinate_space || ''))
      || !finite(p.x) || !finite(p.y) || !Number.isInteger(p.reference_width) || !Number.isInteger(p.reference_height)
      || (p.count !== undefined && (!Number.isInteger(p.count) || p.count < 1 || p.count > 3)))) return 'observation_contract_required';
    if (['double_click', 'right_click'].includes(message.operation) && !p.element_id && (!['pixel', 'normalized'].includes(String(p.coordinate_space || ''))
      || !finite(p.x) || !finite(p.y) || !Number.isInteger(p.reference_width) || !Number.isInteger(p.reference_height))) return 'observation_contract_required';
    if (pixelInput(message.operation, p) && (p.element_id || !p.observation_id
      || !['pixel', 'normalized'].includes(String(p.coordinate_space || '')) || !finite(p.x) || !finite(p.y)
      || !Number.isInteger(p.reference_width) || !Number.isInteger(p.reference_height))) return 'observation_contract_required';
    if (['type_text', 'press_key'].includes(message.operation) && ['to_x', 'to_y', 'count'].some(key => p[key] !== undefined)) return 'protocol_error';
    if (!['click_at', 'double_click', 'right_click', 'type_text', 'press_key'].includes(message.operation) && ['x', 'y', 'to_x', 'to_y', 'coordinate_space', 'reference_width', 'reference_height', 'count']
      .some(key => p[key] !== undefined)) return 'protocol_error';
    if (message.operation === 'double_click' && p.count !== undefined && p.count !== 1) return 'protocol_error';
    if (['set_value', 'type_text'].includes(message.operation) && typeof p.text !== 'string') return 'protocol_error';
    if (['click_element', 'open_menu', 'select_option', 'set_value'].includes(message.operation) && !p.element_id) return 'observation_contract_required';
    if (message.operation === 'press_key' && (typeof p.key !== 'string' || !p.key)) return 'protocol_error';
    if (p.element_id && !['click_element', 'right_click', 'open_menu', 'select_option', 'double_click', 'set_value', 'type_text', 'press_key', 'scroll'].includes(message.operation)) return 'protocol_error';
    if (p.element_id && !p.observation_id) return 'observation_contract_required';
    // observe always mints a fresh Host reference. Accepting a caller-provided
    // id would let an old observation masquerade as a new Driver read.
    if (message.operation === 'observe' && p.observation_id !== undefined
      && !this.approvalResumes.has(message)) return 'observation_contract_required';
    if (p.observation_id !== undefined && !OBSERVATION_REF_PATTERN.test(String(p.observation_id))) return 'observation_contract_required';
    return '';
  }

  async handle(message, owner = null) {
    if (!this.enabled && !['status', 'capabilities', 'query_action', 'release_control', 'end_task'].includes(message.operation)) {
      this.onPermissionRequired();
      return this.#response(message, 'rejected', 'computer_disabled', false, true);
    }
    const error = this.#validate(message); if (error) return this.#response(message, 'rejected', error, false, true);
    const operation = message.operation;
    const payload = {
      ...message.payload,
      ...(message.payload.app ? { app: { ...message.payload.app } } : {}),
      ...(message.payload.window ? { window: { ...message.payload.window } } : {}),
    };
    const taskId = String(payload.task_id || '');
    let taskFence = this.taskFences.get(taskId);
    if (!taskFence) { taskFence = { ended: false }; this.taskFences.set(taskId, taskFence); }
    if (payload.target_ref) {
      const target = this.targets.get(String(payload.target_ref));
      if (!target || target.task_id !== taskId || target.generation !== this.generation || target.epoch !== this.epoch) return this.#response(message, 'rejected', 'target_not_found', false, true);
      if (payload.app?.bundle_id && payload.app.bundle_id !== target.app.bundle_id) return this.#response(message, 'rejected', 'target_mismatch', false, true);
      if (operation === 'list_windows' && target.window) {
        return this.#response(message, 'rejected', 'target_kind_mismatch', false, true, {
          expected_target_kind: 'app', actual_target_kind: 'window',
          current_window_ref: String(payload.target_ref),
          next_operation: 'observe_or_action', preserve_observation_context: true,
        });
      }
      if (operation === 'observe' && !target.window) {
        return this.#response(message, 'rejected', 'target_kind_mismatch', false, true, {
          expected_target_kind: 'window', actual_target_kind: 'app',
          app_target_ref: String(payload.target_ref), next_operation: 'list_windows',
        });
      }
      payload.app = { ...target.app };
      if (target.window) payload.window = { ...target.window };
    }
    const tokenOperation = (DRIVER_CONTRACTS[operation]?.token && (!['double_click', 'right_click'].includes(operation) || payload.element_id))
      || (DRIVER_CONTRACTS[operation]?.optional_token && payload.element_id);
    if (tokenOperation && payload.target_ref) {
      const observation = this.observations.get(String(payload.observation_id || ''));
      if (!observation || observation.task_id !== taskId || observation.epoch !== this.epoch
        || observation.generation !== this.generation || observation.target_ref !== String(payload.target_ref || '')
        || !observation.element_ids.has(String(payload.element_id || ''))) {
        return this.#response(message, 'rejected', 'stale_observation', false, true);
      }
    }
    if (operation === 'query_action') {
      const actionId = String(payload.action_id || '');
      if (!/^[a-f0-9]{32}$/.test(actionId)) return this.#response(message, 'rejected', 'protocol_error', false, true);
      const prior = this.requests.get(actionId);
      if (prior && String(prior.audit?.task_id || '') !== taskId) return this.#response(message, 'rejected', 'action_not_found', false, true);
      return prior
        ? { ...prior, request_id: message.request_id, result: { ...(prior.result || {}), queried_action_id: actionId } }
        : this.#response(message, 'rejected', 'action_not_found', false, true);
    }
    const actionId = WRITE.has(operation) ? message.action_id : message.request_id;
    const knownDigest = this.actionDigests.get(actionId);
    const requestDigest = this.approvalResumes.has(message) && knownDigest
      ? knownDigest : digest({ operation, payload });
    if (knownDigest && knownDigest !== requestDigest) return this.#response(message, 'rejected', 'idempotency_conflict', false, true);
    const duplicate = this.requests.get(actionId);
    if (duplicate) return { ...duplicate, request_id: message.request_id };
    if (WRITE.has(operation)) this.actionDigests.set(actionId, requestDigest);
    if (!['capabilities', 'list_apps', 'list_windows', 'status', 'release_control', 'end_task'].includes(operation)) this.#fillUnambiguousScopedTarget(payload);
    if (!['capabilities', 'list_apps', 'list_windows', 'status', 'release_control', 'end_task'].includes(operation) && !payload.app?.bundle_id) {
      return this.#response(message, 'rejected', 'target_not_found', false, true);
    }
    if (operation === 'observe' && !payload.observation_id) payload.observation_id = id('obs');
    if (operation === 'status') return this.#response(message, 'completed_known', '', false, true, this.publicState({ forModel: true }));
    if (operation === 'release_control') { this.revoke(); return this.#response(message, 'completed_known', '', false, true, this.publicState()); }
    if (operation === 'end_task') return await this.#endTask(message, payload);
    if (operation === 'list_windows' && !(Number(payload.app?.pid) > 0)) {
      return this.#response(message, 'completed_known', '', false, true, this.#publicResult(operation, { windows: [] }, payload));
    }
    if (payload.target_ref && operation === 'observe' && !(Number(payload.app?.pid) > 0)) {
      return this.#response(message, 'rejected', 'target_not_found', false, true, { next_operation: 'activate_app' });
    }
    if (!['capabilities', 'status'].includes(operation)) await this.refreshPermissions(true);
    // Revoke can arrive while the native permission read awaits. Never mint a
    // temporary grant from that old epoch when the read resumes.
    if (message.control_epoch !== this.epoch || message.driver_generation !== this.generation) {
      return this.#response(message, 'cancelled', 'control_revoked', false, true);
    }
    const needsAccessibility = !['capabilities', 'status'].includes(operation);
    const needsScreen = operation === 'observe';
    if (needsAccessibility && (this.permissions.status !== 'known' || !this.permissions.identity_verified || !this.permissions.accessibility || (needsScreen && !this.permissions.screen_recording))) {
      this.onPermissionRequired();
      return this.#response(message, 'rejected', this.permissions.status === 'known' ? 'system_permission_denied' : 'system_permission_unknown', false, true, {
        computer_enabled: this.enabled,
        permissions: { status: this.permissions.status, accessibility: this.permissions.accessibility,
          screen_recording: this.permissions.screen_recording, identity_verified: this.permissions.identity_verified,
          host_bundle_id: this.permissions.host_bundle_id },
        runtime_mismatch: this.permissions.runtime_mismatch === true,
        required_permissions: { accessibility: needsAccessibility, screen_recording: needsScreen, identity_verified: true },
      });
    }
    if (operation === 'activate_app' && Number(payload.app?.pid) > 0) {
      try {
        const listed = await this.driver.call('list_apps', this.#driverArgs('list_apps', payload));
        const current = (listed?.apps || []).find(app => Number(app.pid) === Number(payload.app.pid)
          && String(app.bundle_id || '') === String(payload.app.bundle_id || '') && app.running !== false);
        const currentIdentity = current && String(current.process_start_identity || current.process_start_time || hostProcessStartIdentity(current.pid));
        if (!current || !currentIdentity || currentIdentity !== String(payload.app.process_start_identity || '')) {
          if (payload.target_ref) return this.#response(message, 'rejected', 'target_not_found', false, true, { next_operation: 'list_apps' });
          payload.app = { bundle_id: String(payload.app.bundle_id || '') }; delete payload.window;
        }
      } catch (preflightError) {
        return this.#response(message, 'failed_known', preflightError.safeCode || preflightError.code || 'driver_error', false, true);
      }
    }
    if (operation === 'activate_app' && Number(payload.app?.pid) > 0) {
      const scope = this.#findTaskGrant(operation, payload);
      const selected = scope?.selected_windows?.[payload.app.bundle_id];
      if (!payload.window && selected) payload.window = { ...selected.window };
      if (payload.window?.window_id) {
        try {
          const listed = await this.driver.call('list_windows', this.#driverArgs('list_windows', payload));
          if (!(listed?.windows || []).some(window => Number(window.pid) === Number(payload.app.pid)
            && Number(window.window_id) === Number(payload.window.window_id))) {
            return this.#response(message, 'rejected', 'target_not_found', false, true, { next_operation: 'list_windows', selected_window_closed: true });
          }
        } catch (preflightError) {
          return this.#response(message, 'failed_known', preflightError.safeCode || preflightError.code || 'driver_error', false, true);
        }
      }
    }
    if ((operation === 'observe' || operation === 'activate_app' && payload.delivery_mode === 'foreground') && Number(payload.app?.pid) > 0
      && !String(payload.window?.handle || payload.window?.window_id || '')) {
      try { await this.#resolveExactWindow(payload, { attempts: operation === 'activate_app' ? 3 : 1 }); }
      catch (preflightError) {
        return this.#response(message, 'failed_known', preflightError.safeCode || preflightError.code || 'driver_error', false, true, preflightError.publicResult || {});
      }
    }
    if (message.control_epoch !== this.epoch || message.driver_generation !== this.generation) {
      return this.#response(message, 'cancelled', 'control_revoked', false, true);
    }
    if (taskFence.ended) return this.#response(message, 'cancelled', 'task_ended', false, true);
    const activeScope = this.taskGrants.get(taskId);
    if (activeScope?.turn_id && payload.task_consent_required && !payload.task_intent) {
      return this.#response(message, 'rejected', 'task_intent_required', false, true);
    }
    if (payload.task_intent && (!this.#scopeFromPayload(payload)
      || activeScope?.turn_id && activeScope.turn_id !== payload.task_intent.turn_id)) {
      return this.#response(message, 'rejected', 'task_intent_invalid', false, true);
    }
    if (activeScope?.purpose === 'direct_message_send' && activeScope.sent && WRITE.has(operation)
      && !['activate_app', 'scroll'].includes(operation)) {
      return this.#response(message, 'rejected', 'message_already_sent', false, true);
    }
    if (activeScope?.continuous && activeScope.commit_attempted && WRITE.has(operation)
      && !['activate_app', 'scroll'].includes(operation)) {
      return this.#response(message, 'rejected', 'commit_already_attempted', false, true);
    }
    if (activeScope?.commit_intent_id && activeScope.message_attempted && WRITE.has(operation)
      && !['activate_app', 'scroll'].includes(operation)
      && !(activeScope.recipient_verified && activeScope.body_verified
        && this.#actionBoundary(operation, payload, activeScope).send)) {
      return this.#response(message, 'rejected', 'message_target_unverified', false, true);
    }
    if (pixelAction(operation, payload)) this.#bindLatestCapture(payload);
    if (owner?.id && this.cancelledOwners.has(String(owner.id))) {
      return this.#response(message, 'cancelled', 'caller_disconnected', false, true);
    }
    // A known but unverifiable action still ran. Reject a new write before
    // asking for approval; a fresh observation must precede replanning.
    if (WRITE.has(operation) && this.recovery.get(taskId)?.needsObserve) {
      return this.#response(message, 'rejected', 'recovery_observe_required', false, true);
    }
    let grant = this.#findGrant(operation, payload);
    const taskGrant = this.#findTaskGrant(operation, payload);
    if (!grant && taskGrant) {
      this.grant({ task_id: taskId, app: payload.app, window_handle: String(payload.window?.handle || payload.window?.window_id || ''), operations: [operation], mode: payload.delivery_mode, parameter_digest: this.#parameterDigest(payload), observation_id: String(payload.observation_id || ''), expires_at: Math.min(taskGrant.expires_at, now() + 60_000) });
      grant = this.#findGrant(operation, payload);
    }
    const scopedBundleId = String(payload.app?.bundle_id || '');
    const scopedKnownIdentity = activeScope?.process_identities[scopedBundleId] || '';
    const scopedText = String(payload.text ?? '');
    const scopedTextMatches = !['type_text', 'set_value'].includes(operation)
      || activeScope?.purpose === 'media_search_play' && scopedText === activeScope.query
      || activeScope?.purpose === 'local_document_read' && scopedText === activeScope.file_path
      || activeScope?.purpose === 'direct_message_send' && [activeScope.recipient, activeScope.message_text].includes(scopedText);
    if (!grant && activeScope?.allowed_apps.includes(scopedBundleId)
      && activeScope.operations.includes(operation) && activeScope.mode === String(payload.delivery_mode || 'background')
      && (!scopedKnownIdentity || scopedKnownIdentity === String(payload.app?.process_start_identity || ''))
      && scopedTextMatches
      && ['click_element', 'right_click', 'open_menu', 'select_option', 'click_at', 'double_click', 'type_text', 'set_value', 'scroll', 'press_key'].includes(operation)) {
      const observed = activeScope.last_observations[scopedBundleId];
      const windowHandle = String(payload.window?.handle || payload.window?.window_id || '');
      if (!observed || now() - observed.at > 60_000 || (observed.window && windowHandle && observed.window !== windowHandle)) {
        return this.#recoveryResponse(message, taskId, 'recovery_observe_required');
      }
    }
    if (!grant && ['capabilities', 'list_apps', 'list_windows'].includes(operation)) {
      grant = { grant_id: id('cu_discovery'), task_scope: true };
    }
    const supportedPolicy = Object.values(TASK_SCOPES).find(policy => policy.apps.has(String(payload.app?.bundle_id || '')));
    if (!grant && payload.task_consent_required && !activeScope && !this.#scopeFromPayload(payload)) {
      return this.#response(message, 'rejected', 'task_intent_required', false, true, {
        instruction: 'A direct human task context is required. No operation was dispatched. Do not construct permission fields; stop and ask the user to submit the task in the desktop chat.',
      });
    }
    if (!grant && !payload.task_intent && supportedPolicy && payload.authorization && !['activate_app', 'observe'].includes(operation)) {
      return this.#response(message, 'rejected', 'task_authorization_required', false, true);
    }
    if (!grant && !payload.task_intent && !this.taskGrants.has(taskId) && supportedPolicy && supportedPolicy.operations.has(operation) && !payload.authorization) {
      return this.#response(message, 'rejected', 'task_authorization_required', false, true);
    }
    if (!grant) {
      if (this.pendingApproval) return this.#response(message, 'rejected', 'approval_in_progress', false, true);
      const expiresAt = now() + 10 * 60_000;
      this.control = 'waiting_for_approval';
      return await new Promise(resolve => {
        const pending = {
          approval_id: id('cu_approval'), task_id: String(payload.task_id || ''), operation,
          revision: ++this.approvalRevision,
          app: payload.app || null, window: payload.window || null,
          window_handle: String(payload.window?.handle || payload.window?.window_id || ''),
          delivery_mode: payload.delivery_mode || 'background',
          parameter_digest: this.#parameterDigest(payload),
          observation_id: String(payload.observation_id || ''),
          message: Object.freeze({ ...message, payload: Object.freeze({ ...payload }) }),
          epoch: this.epoch, generation: this.generation, expires_at: expiresAt, resolve,
          scope: !activeScope ? this.#scopeFromPayload(payload) : null,
          action_summary: this.#actionSummary(operation, payload),
          owner_id: String(owner?.id || ''),
        };
        pending.timer = setTimeout(() => {
          if (this.pendingApproval !== pending) return;
          this.pendingApproval = null; this.control = 'paused';
          resolve(this.#response(message, 'rejected', 'approval_expired', false, true));
          this.onStateChange(this.publicState());
        }, expiresAt - now());
        this.pendingApproval = pending;
        this.onStateChange(this.publicState());
      });
    }
    let releaseDispatch = () => {}; let beforeFrontmostPid = null;
    if (WRITE.has(operation)) {
      const priorDispatch = this.dispatchTail;
      this.dispatchTail = new Promise(resolve => { releaseDispatch = resolve; });
      await priorDispatch;
      if (!this.enabled || message.control_epoch !== this.epoch || message.driver_generation !== this.generation) {
        releaseDispatch();
        return this.#response(message, 'cancelled', 'control_revoked', false, true);
      }
      const freshGrant = this.#findGrant(operation, payload);
      if (!freshGrant || freshGrant.grant_id !== grant.grant_id) {
        releaseDispatch();
        return this.#response(message, 'cancelled', 'grant_expired', false, true);
      }
      grant = freshGrant;
    }
    if (WRITE.has(operation) && operation !== 'activate_app' && payload.target_ref) {
      let running;
      try { running = await this.driver.call('list_apps', this.#driverArgs('list_apps', payload)); beforeFrontmostPid = running?.apps?.find(app => app.active)?.pid || null; }
      catch (preflightError) {
        releaseDispatch();
        return this.#response(message, 'failed_known', preflightError.safeCode || preflightError.code || 'driver_error', false, true);
      }
      const expectedPid = Number(payload.app?.pid);
      const expectedStart = String(payload.app?.process_start_identity || '');
      const current = (running?.apps || []).find(app => String(app.bundle_id || '') === String(payload.app?.bundle_id || '')
        && (!Number.isInteger(expectedPid) || Number(app.pid) === expectedPid));
      const currentStart = String(current?.process_start_identity || current?.process_start_time || hostProcessStartIdentity(current?.pid));
      if (!current || !expectedStart || !currentStart || currentStart !== expectedStart) {
        releaseDispatch();
        return this.#response(message, 'rejected', current && (!expectedStart || !currentStart) ? 'target_identity_unavailable' : 'target_not_found', false, true);
      }
    }
    const queuedReturn = response => { releaseDispatch(); return response; };
    const recovery = this.recovery.get(taskId);
    if (WRITE.has(operation) && recovery?.needsObserve) return queuedReturn(this.#response(message, 'rejected', 'recovery_observe_required', false, true));
    if (WRITE.has(operation) && this.unknownDigests.has(`${taskId}:${this.#parameterDigest(payload)}`)) return queuedReturn(this.#response(message, 'rejected', 'outcome_unknown_not_replayable', false, true));
    let taskBudget;
    if (WRITE.has(operation)) {
      taskBudget = this.taskBudgets.get(taskId);
      if (!taskBudget) {
        taskBudget = { actions: 0, started_at: now() };
        this.taskBudgets.set(taskId, taskBudget);
      }
      if (taskBudget.actions >= this.maxActions || now() - taskBudget.started_at > this.maxActiveMs) {
        return queuedReturn(this.#response(message, 'rejected', 'budget_exceeded', false, true));
      }
    }
    if (pixelAction(operation, payload)) {
      const capture = this.captures.get(String(payload.capture_id || ''));
      if (!capture || capture.consumed || capture.task_id !== taskId || capture.app !== String(payload.app?.bundle_id || '') || capture.process_start_identity !== String(payload.app?.process_start_identity || '') || capture.observation_id !== String(payload.observation_id || '') || capture.binding !== String(payload.capture_binding || '') || capture.epoch !== this.epoch || capture.generation !== this.generation || capture.window !== String(payload.window?.handle || payload.window?.window_id || '')) return queuedReturn(this.#recoveryResponse(message, taskId, 'stale_capture'));
      let listed;
      try { listed = await this.driver.call('list_windows', this.#driverArgs('list_windows', payload)); }
      catch (preflightError) {
        const code = preflightError.safeCode || preflightError.code || 'driver_error';
        return queuedReturn(this.#response(message, 'failed_known', code, false, true));
      }
      const currentWindow = (listed?.windows || []).find(item => String(item.window_id) === capture.window);
      if (!currentWindow || !capture.window_bounds_digest || capture.window_bounds_digest !== digest(currentWindow.bounds || null)) return queuedReturn(this.#recoveryResponse(message, taskId, 'stale_capture'));
      const coordinateError = this.#normalizeCoordinates(payload, capture);
      if (coordinateError) return queuedReturn(this.#recoveryResponse(message, taskId, coordinateError));
      capture.consumed = true;
    }
    if (this.onTestEvidence && WRITE.has(operation)) await this.onTestEvidence({ operation, payload, stage: 'before',
      frontmostPid: beforeFrontmostPid, capture: this.captures.get(payload.capture_id) }).catch(() => {});
    if (!this.enabled) return queuedReturn(this.#response(message, 'cancelled', 'computer_disabled', false, true));
    if (taskFence.ended) return queuedReturn(this.#response(message, 'cancelled', 'task_ended', false, true));
    if (owner?.id && this.cancelledOwners.has(String(owner.id))) return queuedReturn(this.#response(message, 'cancelled', 'caller_disconnected', false, true));
    if (message.control_epoch !== this.epoch || message.driver_generation !== this.generation) return queuedReturn(this.#response(message, 'cancelled', 'control_revoked', false, true));
    // The dispatch lock alone cannot fence end_task/cancelOwner while native
    // preflight awaits. Revalidate the authoritative grant after every await,
    // without re-hashing the now-normalized pixel coordinates.
    if (!grant.task_scope && (this.grants.get(grant.grant_id) !== grant || grant.expires_at <= now())) {
      return queuedReturn(this.#response(message, 'cancelled', 'grant_expired', false, true));
    }
    if (!grant.task_scope) this.grants.delete(grant.grant_id);
    const dispatchScope = this.taskGrants.get(taskId);
    if (dispatchScope?.continuous && this.#actionBoundary(operation, payload, dispatchScope).commit) {
      if (dispatchScope.commit_attempted) return queuedReturn(this.#response(message, 'rejected', 'commit_already_attempted', false, true));
      // Reserve before native dispatch, including unknown outcomes. A fresh
      // observation must not become a second send through a different route.
      dispatchScope.commit_attempted = { intent_id: dispatchScope.commit_intent_id || digest({ task_id: taskId,
        turn_id: dispatchScope.turn_id, app: payload.app.bundle_id, operation, text: payload.text || '', element: payload.element_id || '' }), action_id: actionId };
    }
    if (dispatchScope?.commit_intent_id && ['type_text', 'set_value'].includes(operation)) {
      if (payload.text === dispatchScope.recipient) dispatchScope.recipient_typed = true;
      if (payload.text === dispatchScope.message_text) dispatchScope.message_attempted = true;
    }
    if (dispatchScope?.purpose === 'direct_message_send' && ['type_text', 'set_value'].includes(operation)
      && payload.text === dispatchScope.message_text) dispatchScope.message_attempted = true;
    if (this.#actionBoundary(operation, payload, dispatchScope).send) {
      if (dispatchScope.sent) return queuedReturn(this.#response(message, 'rejected', 'message_already_sent', false, true));
      // Reserve before dispatch, including unknown/unverifiable receipts:
      // a different mouse/key route must never retry this one-message task.
      dispatchScope.sent = true;
    }
    const entry = { request_id: message.request_id, action_id: actionId, request_digest: requestDigest, task_id: taskId, operation, grant_id: grant.grant_id, epoch: this.epoch, generation: this.generation, status: 'dispatching', dispatched_at: new Date().toISOString() };
    const inflight = { revoked: false, owner_id: String(owner?.id || ''), task_id: taskId }; this.inflight.set(message.request_id, inflight);
    this.#ledger(entry); this.requests.set(actionId, this.#response(message, 'outcome_unknown', 'outcome_unknown', true, false));
    if (WRITE.has(operation)) taskBudget.actions += 1;
    try {
      let result;
      if (operation === 'capabilities') result = { enabled: this.enabled, summary: this.enabled ? 'Native macOS computer control is available.' : 'Computer Use is disabled. Ask the user to enable it in the desktop panel.', operations: this.enabled ? [...OPERATIONS] : ['status', 'capabilities'] };
      else {
        try {
          // Cua's foreground helpers restore the previous app. Keep the approved
          // target in front so that pixel focus, selection and typing do not
          // dismiss a transient menu or blur an embedded search field midway.
          if (['right_click', 'double_click', 'click_at', 'type_text', 'press_key'].includes(operation) && !payload.element_id && payload.delivery_mode === 'foreground') {
            await this.driver.call('bring_to_front', this.#driverArgs('activate_app', payload));
            if (taskFence.ended || inflight.revoked || !this.enabled || grant.expires_at <= now()
              || message.control_epoch !== this.epoch || message.driver_generation !== this.generation) {
              throw Object.assign(new Error('menu action revoked after activation'), { code: 'outcome_unknown' });
            }
            this.#ledger({ ...entry, status: 'dispatching', native_stage: 'exact_menu_target_foreground' });
          }
          if (operation === 'type_text' && payload.input_mode === 'replace') {
            await this.driver.call('press_key', this.#driverArgs('press_key', { ...payload, key: 'a', modifiers: ['cmd'] }));
            if (taskFence.ended || inflight.revoked || !this.enabled || grant.expires_at <= now()
              || message.control_epoch !== this.epoch || message.driver_generation !== this.generation) {
              throw Object.assign(new Error('replacement revoked after selection'), { code: 'outcome_unknown' });
            }
            const typing = this.#driverArgs('type_text', payload);
            delete typing.x; delete typing.y;
            result = { ...await this.driver.call('type_text', typing), input_mode: 'replace' };
          } else result = operation === 'activate_app'
            ? await this.#activateApp(payload, taskId, () => !taskFence.ended && !inflight.revoked
              && message.control_epoch === this.epoch && message.driver_generation === this.generation)
            : await this.driver.call(this.#driverTool(operation, payload), this.#driverArgs(operation, payload));
        } catch (driverError) {
          throw driverError;
        }
      }
      if (this.onTestEvidence) await this.onTestEvidence({ operation, payload, result, capture: this.captures.get(payload.capture_id) }).catch(() => {});
      if (inflight.revoked || taskFence.ended) throw Object.assign(new Error('control revoked during dispatch'), { code: 'outcome_unknown' });
      if (operation === 'observe') {
        if (!Array.isArray(result.elements) && Array.isArray(result.snapshot?.elements)) {
          result.elements = result.snapshot.elements;
        }
        if (Array.isArray(result.elements)) result.elements = targetWindowElements(result.elements);
        result.observation_id = String(payload.observation_id || '');
        this.observations.set(result.observation_id, {
          task_id: taskId, target_ref: String(payload.target_ref || ''), epoch: this.epoch, generation: this.generation,
          element_ids: observationElementIds(result),
          controls: (result.elements || []).map(item => ({
            token: String(item.element_token || item.element_id || item.id || ''),
            label: String(item.label || item.title || item.value || item.description || '').trim(), enabled: item.enabled,
            role: String(item.role || ''), depth: Number(item.depth) || 0, screenshot_frame: item.screenshot_frame,
            title: String(item.title || ''), description: String(item.description || ''),
          })).filter(item => item.token && item.label),
        });
        while (this.observations.size > 64) this.observations.delete(this.observations.keys().next().value);
        const scope = this.taskGrants.get(taskId);
        const bundleId = String(payload.app?.bundle_id || '');
        if (scope && scope.allowed_apps.includes(bundleId)) {
          if (scope.commit_intent_id) {
            const labels = (result.elements || []).map(item => String(item.label || item.title || item.value || '').trim());
            scope.recipient_verified = Boolean(scope.recipient_typed && labels.filter(label => label === scope.recipient).length === 1);
            scope.body_verified = Boolean(scope.message_attempted && (result.elements || []).some(item =>
              /TextArea|TextField/.test(String(item.role || '')) && !/Secure/.test(String(item.role || ''))
              && String(item.value || item.label || '').trim() === scope.message_text));
          }
          scope.last_observations[bundleId] = {
            at: now(), window: String(payload.window?.handle || payload.window?.window_id || ''),
            observation_id: String(payload.observation_id || ''),
          };
          // Only a successful exact-window read within this grant selects a
          // window. Discovery order, size and names never imply a selection.
          if (payload.target_ref && payload.window?.window_id) {
            scope.selected_windows ||= {};
            scope.selected_windows[bundleId] = { window: { ...payload.window }, target_ref: payload.target_ref };
          }
        }
      }
      this.#registerTargets(operation, payload, result);
      if (operation === 'observe' && result.capture_id && !this.#validImageContract(result)) {
        result.image_contract_error = 'screenshot_dimensions_mismatch';
        delete result.capture_id;
      }
      if (operation === 'observe' && result.capture_id) {
        const binding = digest({
          task_id: String(payload.task_id || ''), app: payload.app || {}, window: payload.window || {},
          observation_id: String(payload.observation_id || ''), coordinate_transform: result.coordinate_transform || result.transform || null,
          geometry: result.window_bounds || result.geometry || result.bounds || null,
          display: result.display || { width: result.screenshot_width, height: result.screenshot_height, backing_scale: result.screenshot_scale },
        });
        result.capture_binding = binding;
        this.captures.set(result.capture_id, {
          at: now(), consumed: false, task_id: String(payload.task_id || ''), app: String(payload.app?.bundle_id || ''),
          process_start_identity: String(payload.app?.process_start_identity || ''), observation_id: String(payload.observation_id || ''),
          binding, epoch: this.epoch, generation: this.generation,
          window: String(payload.window?.handle || payload.window?.window_id || ''),
          geometry_digest: this.#geometryDigest(result),
          window_bounds_digest: digest(result.window_bounds || result.geometry || result.bounds || null),
          delivered_width: Number(result.screenshot_width || 0), delivered_height: Number(result.screenshot_height || 0),
        });
        while (this.captures.size > 64) this.captures.delete(this.captures.keys().next().value);
        const scope = this.taskGrants.get(taskId);
        if (scope && scope.allowed_apps.includes(String(payload.app?.bundle_id || ''))) {
          scope.last_capture_id = String(result.capture_id);
        }
      }
      if (operation === 'observe') {
        const currentRecovery = this.recovery.get(taskId);
        if (currentRecovery && currentRecovery.target === this.#recoveryTarget(payload)) {
          this.recovery.set(taskId, { ...currentRecovery, needsObserve: false });
        }
      }
      if (inflight.revoked) throw Object.assign(new Error('control revoked during dispatch'), { code: 'outcome_unknown' });
      if (WRITE.has(operation)) {
        this.rawActionResults.set(actionId, result);
        while (this.rawActionResults.size > 256) this.rawActionResults.delete(this.rawActionResults.keys().next().value);
      }
      const publicResult = this.#publicResult(operation, result, payload);
      if (operation === 'observe') {
        // Native Driver output is not authority to grant a model access.
        // Reaching here requires the exact task/action approval above.
        publicResult.screenshot_service_authorized = String(payload.screenshot_service || '');
      }
      if (operation === 'observe' && publicResult.model_image_path) {
        const fence = this.taskFences.get(taskId);
        if (fence) { fence.observation_images ||= new Set(); fence.observation_images.add(publicResult.model_image_path); }
        this.latestPreview = { task_id: taskId, app: String(payload.app?.bundle_id || ''), at: now(),
          epoch: this.epoch, generation: this.generation, model_image_path: publicResult.model_image_path };
      }
      if (operation === 'observe' && result.capture_id && publicResult.model_image_path) {
        const capture = this.captures.get(String(result.capture_id));
        if (capture) capture.model_image_path = publicResult.model_image_path;
      }
      const response = this.#response(message, 'completed_known', '', WRITE.has(operation), true, publicResult);
      const waiting = WRITE.has(operation) && result.action_receipt?.verification === 'pending';
      if (WRITE.has(operation)) {
        if (waiting) {
          this.recovery.set(taskId, { attempts: 0, needsObserve: true, target: this.#recoveryTarget(payload) });
          if (['unverifiable', 'partial'].includes(result.action_receipt.effect)) {
            this.unknownDigests.add(`${taskId}:${this.#parameterDigest(payload)}`);
          }
        } else this.recovery.delete(taskId);
        const scope = this.taskGrants.get(taskId);
        if (scope) {
          if (scope.purpose === 'direct_message_send' && !waiting) {
            if (scope.next_text_stage === 'recipient') { scope.recipient_typed = true; scope.next_text_stage = ''; }
            else if (scope.next_text_stage === 'message') { scope.message_typed = true; scope.next_text_stage = ''; }
            else if (scope.recipient_typed && !scope.message_typed && (['click_element', 'click_at', 'double_click'].includes(operation)
              || operation === 'press_key' && ['enter', 'return'].includes(String(payload.key || '').toLowerCase()))) scope.recipient_selected = true;
            else if (scope.message_typed && operation === 'press_key' && ['enter', 'return'].includes(String(payload.key || '').toLowerCase())) scope.sent = true;
          }
          delete scope.last_observations[String(payload.app?.bundle_id || '')];
        }
      }
      this.requests.set(actionId, response); this.#ledger({ ...entry, status: 'completed_known',
        ...(result.action_receipt ? { action_receipt: result.action_receipt } : {}), completed_at: new Date().toISOString() }); return response;
    } catch (driverError) {
      if (this.onTestEvidence) await this.onTestEvidence({ operation, payload, error: driverError, capture: this.captures.get(payload.capture_id) }).catch(() => {});
      // An activation approval is provisional until the exact process/window
      // has been launched and foreground-confirmed.  Do not retain a broader
      // task grant after that boundary fails.
      if (operation === 'activate_app') this.taskGrants.delete(taskId);
      if (WRITE.has(operation)) {
        this.rawActionResults.set(actionId, driverError.detail || driverError.evidence || { code: driverError.code || 'driver_error' });
        while (this.rawActionResults.size > 256) this.rawActionResults.delete(this.rawActionResults.keys().next().value);
      }
      const unknown = driverError.code === 'outcome_unknown' || (WRITE.has(operation) && driverError.code !== 'failed_known'); const status = unknown ? 'outcome_unknown' : 'failed_known';
      if ((unknown || driverError.nonReplayable) && WRITE.has(operation)) this.unknownDigests.add(`${taskId}:${this.#parameterDigest(payload)}`);
      if (WRITE.has(operation)) {
        const prior = this.recovery.get(taskId) || { attempts: 0 };
        this.recovery.set(taskId, { attempts: prior.attempts + 1, needsObserve: true, target: this.#recoveryTarget(payload) });
      }
      const executed = WRITE.has(operation) && driverError.evidence?.effect !== 'Refused';
      const response = this.#response(message, status, unknown ? 'outcome_unknown' : (driverError.safeCode || driverError.code || 'driver_error'), executed, !unknown, driverError.publicResult || {});
      if (driverError.evidence) response.result = { ...(response.result || {}), driver_error: {
        ...driverError.evidence, requested_delivery: String(payload.delivery_mode || ''),
      } };
      const persistentEvidence = driverError.evidence ? {
        raw_code: driverError.evidence.raw_code, effect: driverError.evidence.effect,
        actual_delivery: driverError.evidence.actual_delivery,
      } : undefined;
      this.requests.set(actionId, response); this.#ledger({ ...entry, status, error_code: response.error_code, driver_error: persistentEvidence, completed_at: new Date().toISOString() }); return response;
    } finally {
      this.inflight.delete(message.request_id);
      releaseDispatch();
    }
  }

  #loadLedger() {
    let lines;
    try { lines = fs.readFileSync(this.ledgerPath, 'utf8').split('\n').filter(Boolean); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
    const latest = new Map();
    for (const line of lines.slice(-2000)) {
      try {
        const item = JSON.parse(line); const actionId = String(item?.action_id || item?.request_id || '');
        if (actionId) latest.set(actionId, item);
      } catch { /* A torn final line is ignored; the prior dispatch remains unknown. */ }
    }
    for (const [actionId, item] of latest) {
      const status = ['completed_known', 'failed_known', 'cancelled', 'rejected'].includes(item.status) ? item.status : 'outcome_unknown';
      const receipt = item.action_receipt;
      const publicReceipt = receipt
        && ['confirmed', 'partial', 'unverifiable', 'suspected_noop'].includes(receipt.effect)
        && ['background', 'foreground', 'not_applicable'].includes(receipt.delivery)
        && ['native_confirmed', 'pending'].includes(receipt.verification)
        ? { effect: receipt.effect, delivery: receipt.delivery, verification: receipt.verification } : null;
      if (item.request_digest) this.actionDigests.set(actionId, String(item.request_digest));
      this.requests.set(actionId, {
        request_id: String(item.request_id || actionId), action_id: actionId, status, executed: ['dispatching', 'dispatched'].includes(item.status) || status === 'completed_known' || status === 'outcome_unknown',
        outcome_known: status !== 'outcome_unknown', error_code: status === 'outcome_unknown' ? 'outcome_unknown' : String(item.error_code || ''),
        result: { recovered: true, ...(publicReceipt ? { action_receipt: publicReceipt } : {}) }, audit: { action_id: actionId, task_id: String(item.task_id || ''), operation: String(item.operation || 'unknown'), status, error_code: status === 'outcome_unknown' ? 'outcome_unknown' : String(item.error_code || ''), epoch: Number(item.epoch || 0), generation: Number(item.generation || 0) },
      });
    }
  }

  #driverArgs(operation, payload) {
    // Driver schemas are closed (`additionalProperties:false`).  Keep broker
    // identity fields (bundle_id/process_start_identity/handle) at this trust
    // boundary and translate only the exact arguments each Cua tool accepts.
    const args = { session: this.#driverSession(payload.task_id) };
    const pid = Number(payload.app?.pid);
    const windowId = Number(payload.window?.window_id ?? payload.window?.handle);
    if (operation === 'list_apps') return args;
    if (Number.isInteger(pid) && pid > 0) args.pid = pid;
    if (operation === 'list_windows') return args;
    if (Number.isInteger(windowId) && windowId > 0) args.window_id = windowId;
    // Let Cua resize and retain its native pixel transform together; never
    // resize only the model attachment while leaving the capture contract intact.
    if (operation === 'observe') return { ...args, max_dimension: 1024 };
    if (operation === 'activate_app' && !(Number.isInteger(pid) && pid > 0)) {
      args.bundle_id = String(payload.app?.bundle_id || '');
      delete args.pid;
      return args;
    }
    if (payload.element_id) args.element_token = payload.element_id;
    if (['click_element', 'right_click', 'open_menu', 'select_option', 'click_at', 'double_click', 'scroll', 'drag', 'type_text', 'press_key'].includes(operation)) {
      args.delivery_mode = payload.delivery_mode || 'background';
    }
    if (operation === 'click_at') {
      for (const key of ['capture_id', 'x', 'y', 'count']) if (payload[key] !== undefined) args[key] = payload[key];
    } else if (operation === 'right_click') {
      for (const key of ['x', 'y']) if (payload[key] !== undefined) args[key] = payload[key];
    } else if (operation === 'select_option') {
      args.action = 'pick';
    } else if (operation === 'double_click') {
      for (const key of ['x', 'y']) if (payload[key] !== undefined) args[key] = payload[key];
    } else if (operation === 'set_value') {
      args.value = String(payload.text ?? '');
    } else if (operation === 'scroll') {
      const horizontal = Number(payload.delta_x || 0);
      const vertical = Number(payload.delta_y || 0);
      const dominant = Math.abs(horizontal) > Math.abs(vertical) ? horizontal : vertical;
      args.direction = Math.abs(horizontal) > Math.abs(vertical)
        ? (dominant < 0 ? 'left' : 'right') : (dominant < 0 ? 'up' : 'down');
      args.amount = Math.max(1, Math.min(50, Math.ceil(Math.abs(dominant) || 1)));
    } else if (operation === 'type_text') {
      args.text = String(payload.text ?? '');
    } else if (operation === 'press_key') {
      const parts = String(payload.key || '').toLowerCase().split('+').map(part => part.trim()).filter(Boolean);
      const modifierAliases = { command: 'cmd', control: 'ctrl', alt: 'option' };
      const explicit = Array.isArray(payload.modifiers) ? payload.modifiers : [];
      const modifiers = [...explicit, ...parts.slice(0, -1)]
        .map(part => modifierAliases[String(part).toLowerCase()] || String(part).toLowerCase())
        .filter((part, index, values) => ['cmd', 'ctrl', 'option', 'shift'].includes(part) && values.indexOf(part) === index);
      args.key = parts.at(-1) || '';
      if (modifiers.length) args.modifiers = modifiers;
    }
    if (pixelInput(operation, payload)) { args.x = payload.x; args.y = payload.y; }
    return args;
  }

  #driverSession(taskId) {
    let fence = this.taskFences.get(String(taskId || ''));
    if (!fence) { fence = { ended: false }; this.taskFences.set(String(taskId || ''), fence); }
    // Driver end_session is terminal. A later human turn in the same chat must
    // get a fresh native session, not reopen a closed label or retain grants.
    fence.driver_session ||= `tcode-${digest(String(taskId || '')).slice(0, 16)}-${crypto.randomBytes(8).toString('hex')}`;
    return fence.driver_session;
  }

  async #resolveExactWindow(payload, { attempts = 1 } = {}) {
    const pid = Number(payload.app?.pid);
    if (!Number.isInteger(pid) || pid <= 0) throw Object.assign(new Error('target process unavailable'), {
      code: 'failed_known', safeCode: 'target_not_found',
    });
    let candidates = [];
    for (let attempt = 0; attempt < Math.max(1, attempts); attempt += 1) {
      const listed = await this.driver.call('list_windows', this.#driverArgs('list_windows', payload));
      candidates = (listed?.windows || []).filter(window => Number(window.pid) === pid
        && Number(window.window_id) > 0 && Number(window.bounds?.width) > 0 && Number(window.bounds?.height) > 0);
      if (candidates.length) break;
      if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (candidates.length === 1) {
      payload.window = { window_id: Number(candidates[0].window_id) };
      return candidates[0];
    }
    const publicCandidates = candidates.map(window => ({
      target_ref: this.#registerTarget(payload.task_id, payload.app, window),
      title: String(window.title || '').slice(0, 512),
      bounds: Object.fromEntries(['x', 'y', 'width', 'height']
        .filter(key => finite(Number(window.bounds?.[key]))).map(key => [key, Number(window.bounds[key])])),
      is_on_screen: window.is_on_screen !== false,
      on_current_space: window.on_current_space !== false,
      minimized: window.minimized === true,
    }));
    throw Object.assign(new Error(candidates.length ? 'multiple target windows' : 'target window not ready'), {
      code: 'failed_known', safeCode: candidates.length ? 'target_ambiguous' : 'window_not_ready',
      publicResult: {
        candidates: publicCandidates, candidate_count: publicCandidates.length,
        next_operation: candidates.length ? 'select_window_target_ref' : 'list_windows',
        ...(candidates.length ? { recovery: {
          tool: 'computer_control', operation: 'activate_app',
          instruction: 'Select one candidate using task evidence, pass its exact target_ref to computer_control activate_app, then computer_observe that same window. Do not retry the ambiguous app ref. If the task cannot distinguish the candidates, ask the user.',
          candidates: publicCandidates.map(candidate => ({ operation: 'activate_app', target_ref: candidate.target_ref, delivery_mode: payload.delivery_mode || 'foreground' })),
        } } : {}),
      },
    });
  }

  async #activateApp(payload, taskId, authorized) {
    const bundleId = String(payload.app?.bundle_id || '');
    const check = () => { if (!authorized()) throw Object.assign(new Error('activation revoked'), { code: 'outcome_unknown' }); };
    let result;
    if (Number(payload.app?.pid) > 0 && payload.delivery_mode !== 'foreground') {
      check();
      result = { pid: Number(payload.app.pid), background_ready: true, foreground_confirmed: false };
    } else if (Number(payload.app?.pid) > 0) {
      try {
        check();
        result = await this.driver.call('bring_to_front', this.#driverArgs('activate_app', payload));
      } catch (error) {
        if (error.safeCode !== 'target_not_found') throw error;
        if (payload.target_ref) throw error; // Never relaunch/rebind a selected target after a dispatch race.
        payload.app = { bundle_id: bundleId }; delete payload.window;
      }
    }
    if (!result) {
      check();
      result = await this.driver.call('launch_app', this.#driverArgs('activate_app', payload));
      const pid = Number(result?.pid);
      if (!Number.isInteger(pid) || pid <= 0) throw Object.assign(new Error('launch returned no process'), {
        code: 'failed_known', safeCode: 'window_not_ready', publicResult: { next_operation: 'list_apps' },
      });
      const processStartIdentity = String(
        result?.process_start_identity || result?.process_start_time || hostProcessStartIdentity(pid),
      );
      if (!processStartIdentity) throw Object.assign(new Error('launched process identity unavailable'), {
        code: 'failed_known', safeCode: 'process_identity_unavailable',
      });
      payload.app = { bundle_id: bundleId, pid, process_start_identity: processStartIdentity };
      delete payload.window;
      // The launch succeeded even if subsequent foreground confirmation fails.
      // Bind only the previously unbound installed-app ref; never rebind an old PID.
      const launchedTarget = this.targets.get(payload.target_ref);
      if (launchedTarget && !launchedTarget.app.pid && launchedTarget.app.bundle_id === bundleId) {
        launchedTarget.app = { ...launchedTarget.app, ...payload.app };
      }
      if (payload.delivery_mode === 'foreground') {
        await this.#resolveExactWindow(payload, { attempts: 5 });
        check();
        await this.driver.call('bring_to_front', this.#driverArgs('activate_app', payload));
        result.foreground_confirmed = true;
        result.window_id = Number(payload.window.window_id);
      }
    } else if (payload.delivery_mode === 'foreground') {
      result.foreground_confirmed = true;
      result.window_id = Number(payload.window?.window_id) || null;
    }
    const scope = this.taskGrants.get(taskId);
    if (scope?.allowed_apps.includes(bundleId)) {
      scope.process_identities[bundleId] = String(payload.app.process_start_identity || '');
      scope.app_targets[bundleId] = { ...payload.app };
      if (scope.purpose === 'app_task' && !scope.window_handle) scope.window_handle = String(payload.window?.window_id || '');
      delete scope.last_observations[bundleId];
    }
    return result;
  }

  #driverTool(operation, payload) {
    if (operation === 'activate_app' && !(Number.isInteger(Number(payload.app?.pid)) && Number(payload.app?.pid) > 0)) {
      return 'launch_app';
    }
    return DRIVER_TOOLS[operation];
  }

  #registerTargets(operation, payload, result) {
    if (operation === 'activate_app') {
      // Activation has verified the launched process. Bind an installed-app
      // discovery reference once, without rebinding references to old processes.
      const target = this.targets.get(payload.target_ref);
      if (target && !target.app.pid && target.app.bundle_id === payload.app?.bundle_id) {
        target.app = { ...target.app, ...payload.app };
      }
      result.app_target_ref = this.#registerTarget(payload.task_id, payload.app);
      if (payload.window?.window_id) {
        result.window_target_ref = this.#registerTarget(payload.task_id, payload.app, payload.window);
        const scope = this.#findTaskGrant('activate_app', payload);
        if (scope) {
          scope.selected_windows ||= {};
          scope.selected_windows[payload.app.bundle_id] = { window: { ...payload.window }, target_ref: result.window_target_ref };
        }
        if (result.launch_state) result.launch_state.window_ready = true;
      }
    } else if (operation === 'list_apps' && Array.isArray(result?.apps)) {
      result.apps = result.apps.map(app => ({ ...app, target_ref: this.#registerTarget(payload.task_id, app) }));
    } else if (operation === 'list_windows' && Array.isArray(result?.windows)) {
      const expectedPid = Number(payload.app?.pid);
      result.windows = result.windows.filter(window => !Number.isInteger(expectedPid) || Number(window.pid) === expectedPid).map(window => ({
        ...window,
        target_ref: this.#registerTarget(payload.task_id, {
          bundle_id: payload.app?.bundle_id || window.bundle_id || '', pid: window.pid,
          process_start_identity: payload.app?.process_start_identity || '',
        }, window),
      }));
    } else if (operation === 'observe') {
      result.target_ref = String(payload.target_ref || this.#registerTarget(payload.task_id, payload.app, payload.window));
      result.observation_ref = String(result.observation_id || payload.observation_id || '');
    }
  }

  #registerTarget(taskId, app, window = null) {
    if (!app?.bundle_id) return '';
    const processStartIdentity = String(app.process_start_identity || app.process_start_time || hostProcessStartIdentity(app.pid));
    const targetRef = `target_${digest({
      task_id: String(taskId || ''), generation: this.generation, epoch: this.epoch,
      // Stable within this task lease, never resurrect an ended turn's ref.
      driver_session: this.#driverSession(taskId),
      bundle_id: String(app.bundle_id), pid: Number(app.pid) || 0,
      process_start_identity: processStartIdentity, window_id: Number(window?.window_id) || 0,
    }).slice(0, 32)}`;
    this.targets.set(targetRef, {
      task_id: String(taskId || ''),
      app: {
        bundle_id: String(app.bundle_id), ...(Number(app.pid) > 0 ? { pid: Number(app.pid) } : {}),
        ...(app.name ? { name: String(app.name).slice(0, 80) } : {}),
        ...(processStartIdentity ? { process_start_identity: processStartIdentity } : {}),
      },
      window: window && Number(window.window_id) > 0 ? { window_id: Number(window.window_id) } : null,
      generation: this.generation, epoch: this.epoch,
    });
    while (this.targets.size > 512) this.targets.delete(this.targets.keys().next().value);
    return targetRef;
  }

  #parameterDigest(payload) {
    const exact = { ...payload }; delete exact.task_id; delete exact.call_id;
    return digest(exact);
  }

  #geometryDigest(result) {
    const geometry = result?.geometry || result?.bounds || result?.window_bounds || null;
    const display = result?.display || (
      result?.screenshot_width && result?.screenshot_height && result?.screenshot_scale
        ? { width: result.screenshot_width, height: result.screenshot_height, scale: result.screenshot_scale }
        : null
    );
    const transform = result?.coordinate_transform || result?.transform || null;
    return geometry && display ? digest({ geometry, display, transform }) : '';
  }

  #normalizeCoordinates(payload, capture) {
    const width = Number(capture.delivered_width); const height = Number(capture.delivered_height);
    if (!(width > 0 && height > 0)) return 'coordinate_contract_unknown';
    const referenceWidth = Number(payload.reference_width); const referenceHeight = Number(payload.reference_height);
    if (!(referenceWidth > 0 && referenceHeight > 0) || referenceWidth !== width || referenceHeight !== height) return 'coordinate_contract_mismatch';
    if (payload.coordinate_space === 'normalized') {
      if (!(payload.x >= 0 && payload.x <= 1 && payload.y >= 0 && payload.y <= 1)) return 'coordinate_out_of_bounds';
      payload.x *= width; payload.y *= height;
    }
    if (!(payload.x >= 0 && payload.x < width && payload.y >= 0 && payload.y < height)) return 'coordinate_out_of_bounds';
    delete payload.coordinate_space; delete payload.reference_width; delete payload.reference_height;
    return '';
  }

  #validImageContract(result) {
    const image = result?._image;
    if (!image || image.mime_type !== 'image/png' || typeof image.data !== 'string') return false;
    let data; try { data = Buffer.from(image.data, 'base64'); } catch { return false; }
    if (data.length < 24 || !data.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) return false;
    const width = data.readUInt32BE(16); const height = data.readUInt32BE(20);
    return width > 0 && height > 0 && width === Number(result.screenshot_width) && height === Number(result.screenshot_height);
  }

  #recoveryTarget(payload) {
    const target = this.targets.get(String(payload.target_ref || ''));
    const app = payload.app || target?.app || {};
    const window = payload.window || target?.window || {};
    return digest({ bundle: app.bundle_id, pid: app.pid, start: app.process_start_identity,
      window: window.window_id, epoch: this.epoch, generation: this.generation });
  }

  #recoveryResponse(message, taskId, code) {
    const prior = this.recovery.get(taskId) || { attempts: 0 };
    const attempts = prior.attempts + 1;
    this.recovery.set(taskId, { attempts, needsObserve: true, target: this.#recoveryTarget(message.payload || {}) });
    return this.#response(message, 'rejected', attempts >= 2 ? 'recovery_budget_exceeded' : code, false, true);
  }

  #actionSummary(operation, payload) {
    const activeScope = this.taskGrants.get(String(payload.task_id || ''));
    const scope = activeScope ? null : this.#scopeFromPayload(payload);
    const element = this.#observedControl(payload);
    const appName = payload.app?.name || '目标应用';
    const controlName = element?.label ? `“${element.label}”` : payload.element_id ? '当前未识别名称的控件' : '截图中的指定位置';
    const actions = {
      observe: '查看当前窗口', activate_app: payload.delivery_mode === 'foreground' ? '切换到该应用' : '准备后台操作该应用',
      click_element: `点击${controlName}`, click_at: `${payload.count === 2 ? '双击' : payload.count === 3 ? '三击' : '点击'}${controlName}`,
      double_click: `双击${controlName}`, right_click: `右键打开${controlName}的菜单`, open_menu: `打开${controlName}的菜单`,
      select_option: `选择${controlName}`, set_value: `填写${controlName}`, type_text: '在当前输入位置输入正文',
      press_key: '按下以下按键', scroll: '滚动当前窗口',
    };
    const exact = [
      `允许在“${appName}”中${actions[operation] || '执行当前操作'}。`,
      ...(payload.delivery_mode === 'foreground' && operation !== 'activate_app' ? ['操作时会切换到该应用。'] : []),
      ...(payload.input_mode === 'replace' ? ['将指定输入框的全部内容替换；聚焦、全选后输入，不再次点击。'] : []),
      ...(['type_text', 'set_value'].includes(operation) ? [`精确文本：${JSON.stringify(payload.text)}`] : []),
      ...(operation === 'press_key' ? [`按键：${[...(payload.modifiers || []), payload.key].join(' + ')}`] : []),
      ...(payload.x !== undefined ? ['此操作按截图位置执行，系统不能可靠判断该位置的控件含义。'] : []),
    ].join('\n');
    // Local approval UI only. Never returned by model-facing status or ledger.
    const detail = { approval_kind: scope ? 'task' : 'action', exact_action: exact };
    const screenshotNotice = payload.screenshot_service
      ? `目标窗口截图将发送给 ${payload.screenshot_service} 服务（当前模型：${payload.screenshot_model || '未指定'}）。同一服务内切换模型沿用授权，换供应商时重新确认；不开放其他应用。`
      : '目标窗口截图会交给已获准的视觉模型；不开放其他应用。';
    if (activeScope && String(activeScope.screenshot_service || '') !== String(payload.screenshot_service || '')) {
      return { ...detail, title: '确认更换截图接收供应商', application: appName,
        purpose: `从 ${activeScope.screenshot_service || '未指定服务'} 切换到 ${payload.screenshot_service || '未指定服务'}`,
        allowed: '仅更新本任务的截图接收服务；原有应用、操作范围、期限和提交次数限制继续生效。',
        screenshot: screenshotNotice };
    }
    if (activeScope?.purpose === 'app_task' && !activeScope.continuous && activeScope.turn_id && element
      && !this.#actionBoundary(operation, payload, activeScope).requiresApproval
      && ['click_element', 'double_click', 'right_click', 'open_menu', 'select_option', 'set_value', 'type_text'].includes(operation)
      && String(payload.text || '').length <= 1000 && activeScope.controls.length < 20) {
      detail.control_proposal = { label: element.label, operations: [operation],
        ...(['set_value', 'type_text'].includes(operation) ? { text: payload.text } : {}) };
    }
    if (scope?.purpose === 'app_task') {
      const labels = { click_element: '点击', double_click: '双击', right_click: '右键', open_menu: '打开菜单', select_option: '选择', set_value: '填写', type_text: '输入' };
      return { ...detail, title: `允许在 ${scope.display_name} 完成这项任务？`, application: scope.display_name,
        purpose: scope.target_label,
        allowed: `任务：${scope.private_task_text || scope.target_label}。${scope.continuous ? `允许在该应用中连续${scope.mode === 'foreground' ? '切换前台并' : '后台'}观察、滚动、导航、点击、双击、打开菜单、选择选项和填写普通输入框；无需逐个确认已识别控件。${scope.mode === 'background' ? '不包含切换前台；需要前台时另行请求。' : ''}` : `允许观察、滚动和方向键导航；${scope.controls.map(rule => `${rule.operations.map(op => labels[op]).join('/')}“${rule.label}”${rule.text !== undefined ? `（精确输入“${rule.text}”）` : ''}`).join('；') || '其他操作需另行批准'}。`}`,
        navigation_proposal: Boolean(scope.continuous),
        ask_again: '发送、删除、支付等可识别提交动作、敏感输入及超出应用范围的操作仍需确认。搜索/导航能力须在下方明确选择；系统不能可靠识别未知位置或回车的语义，允许的操作也可能产生实际影响。',
        duration: `仅${scope.window_scope === 'same_app' ? '该应用同一进程的窗口' : '当前精确窗口'}，本轮结束、取消或停止即失效；最多 10 分钟 / 30 个动作。`,
        screenshot: screenshotNotice };
    }
    if (!scope && this.#actionBoundary(operation, payload, activeScope).send) {
      return {
        ...detail, title: `允许这一次可能发送消息的操作？`, application: '微信',
        purpose: `发送精确正文：“${activeScope.message_text}”`, allowed: '仅发送这一次',
        ask_again: '派发后不允许以另一按键或点击重发，即使回执无法确认结果', duration: '单次操作，完成后立即失效',
        screenshot: `${screenshotNotice} 请核对当前聊天是“${activeScope.recipient}”，正文是“${activeScope.message_text}”；未知控件不保证执行发送`, recipient: activeScope.recipient,
        message_text: activeScope.message_text,
      };
    }
    const application = scope?.purpose === 'media_search_play' ? '网易云音乐' : scope?.purpose === 'local_document_read' ? 'Finder 与预览' : '微信';
    const purpose = scope?.purpose === 'media_search_play'
      ? `搜索并播放“${scope.query}”`
      : scope?.purpose === 'local_document_read'
        ? `打开并阅读 ${path.basename(scope?.file_path || '')}（仅限 ${scope?.file_root || ''}）`
        : `查找联系人“${scope?.recipient || ''}”并编辑精确正文“${scope?.message_text || ''}”；发送前另行确认`;
    const summary = scope ? {
      ...detail,
      title: `允许 TCode 完成：在${application}${purpose}`,
      application, purpose,
      allowed: scope.purpose === 'media_search_play' ? '观察界面、搜索、输入、翻页并播放' : scope.purpose === 'local_document_read' ? '在指定测试目录中打开并阅读文档' : '查找并核实联系人、进入会话、编辑这一条正文；不含最终发送',
      ask_again: '控件点击、坐标点击、Enter/Space 提交键和可识别的提交动作另行确认；系统不能可靠识别所有提交控件。',
      duration: '最多 10 分钟或 30 个动作，可随时停止',
      screenshot: screenshotNotice,
    } : { ...detail, operation, target: String(payload.app?.bundle_id || 'discovery'), delivery_mode: payload.delivery_mode || 'background' };
    if (operation === 'press_key') {
      summary.key = String(payload.key || '').slice(0, 64);
      if (Array.isArray(payload.modifiers) && payload.modifiers.length) summary.modifiers = payload.modifiers.slice(0, 4);
    }
    if (operation === 'type_text' || operation === 'set_value') summary.text_length = String(payload.text ?? '').length;
    if (operation === 'click_at') summary.click_count = Number(payload.count || 1);
    if (operation === 'double_click') summary.click_count = 2;
    if (operation === 'right_click') summary.mouse_button = 'right';
    if (operation === 'open_menu') summary.element_action = 'show_menu';
    if (operation === 'select_option') summary.element_action = 'pick';
    if (payload.element_id) summary.element = String(payload.element_id).slice(0, 96);
    if (payload.window?.window_id || payload.window?.handle) summary.window = String(payload.window.window_id || payload.window.handle).slice(0, 96);
    return summary;
  }

  #publicResult(operation, result, payload = {}) {
    const value = result && typeof result === 'object' ? { ...result } : { summary: String(result || '') };
    const image = value._image; delete value._image;
    const query = String(payload.query || '').trim().toLocaleLowerCase();
    const cursor = Math.max(0, Number(payload.cursor) || 0);
    const limit = Math.max(1, Math.min(50, Number(payload.limit) || 25));
    if (operation === 'list_apps' && Array.isArray(value.apps)) {
      const candidates = value.apps
      .map(app => ({ ...app, aliases: query ? localizedAppNames(app) : [] }))
      .filter(app => query
        ? `${app.name || ''}\n${app.bundle_id || ''}\n${app.aliases.join('\n')}`.toLocaleLowerCase().includes(query)
        : app.running !== false)
      .sort((a, b) => Number(Boolean(b.running)) - Number(Boolean(a.running))
        || Number(Boolean(b.active)) - Number(Boolean(a.active))
        || String(a.name || '').localeCompare(String(b.name || ''))
        || String(a.bundle_id || '').localeCompare(String(b.bundle_id || '')))
      .map(app => ({
        target_ref: app.target_ref, bundle_id: String(app.bundle_id || '').slice(0, 256), name: String(app.name || '').slice(0, 256),
        ...(app.aliases.length ? { aliases: app.aliases } : {}),
        running: app.running !== false, active: Boolean(app.active),
        ...(Array.isArray(app.windows) ? { window_count: app.windows.length } : {}),
      }));
      value.apps = candidates.slice(cursor, cursor + limit);
      value.page = { cursor, limit, returned: value.apps.length, total: candidates.length,
        next_cursor: cursor + limit < candidates.length ? cursor + limit : null, query: String(payload.query || '') };
      value.discovery = query
        ? { scope: 'installed_and_running', selection_required: value.apps.length !== 1 }
        : {
            scope: 'running_only', selection_required: true, installed_search_available: true,
            next_call: { operation: 'list_apps', query: '<requested application name>', limit },
            note: 'Absence here does not mean an application is not installed. Search once by the user-visible application name before concluding that.',
          };
    }
    if (operation === 'list_windows' && Array.isArray(value.windows)) {
      const candidates = value.windows
        .filter(window => !query || String(window.title || '').toLocaleLowerCase().includes(query))
        .sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')) || Number(a.window_id || 0) - Number(b.window_id || 0))
        .map(window => ({
          target_ref: window.target_ref, title: String(window.title || '').slice(0, 512),
          is_on_screen: window.is_on_screen !== false, on_current_space: window.on_current_space !== false,
        }));
      value.windows = candidates.slice(cursor, cursor + limit);
      value.page = { cursor, limit, returned: value.windows.length, total: candidates.length,
        next_cursor: cursor + limit < candidates.length ? cursor + limit : null, query: String(payload.query || '') };
    }
    if (operation === 'observe') {
      // Cua's macOS snapshot may include the global menu bar and Recent Items,
      // which are outside the requested target window. Keep target controls but
      // never forward unrelated global menu history to the model.
      if (Array.isArray(value.elements)) value.elements = targetWindowElements(value.elements);
      value.actionable_element_count = Array.isArray(value.elements) ? value.elements.length : 0;
      value.actionable_elements_available = value.actionable_element_count > 0;
      if (value.capture_id) value.coordinate_contract = {
        coordinate_space: 'normalized', reference_width: value.screenshot_width, reference_height: value.screenshot_height,
        observation_ref: value.observation_ref,
        note: 'Prefer normalized x/y in 0..1 relative to this entire image, independent of preview size. Pixel coordinates are also accepted, but must use the exact delivered image dimensions, never a resized preview or screen points. The Driver handles Retina and image scaling; do not apply scaling yourself.',
      };
      // Host retains native geometry for identity/bounds checks. The model gets
      // one coordinate system only, avoiding AX points vs screenshot pixels.
      if (Array.isArray(value.elements)) value.elements = value.elements.map(({ frame: _frame, ...item }) => item);
      for (const key of ['window_bounds', 'geometry', 'bounds', 'display', 'coordinate_transform', 'transform', 'screenshot_scale']) delete value[key];
      value._note = value.actionable_elements_available
        ? 'Listed elements are actionable evidence, not proof that the AX tree is complete. You may act on an exact listed element_token with this target_ref and observation_ref; do not repeat observe merely to rediscover it. After any action, use a fresh observe to verify the requested state change.'
        : 'AX coverage may be partial or unknown. Use the delivered screenshot when available; take another fresh observe only when evidence needed for the next bounded action is absent.';
      if (!value.coordinate_contract && !value.actionable_elements_available) {
        value._note = 'This observation has no actionable elements or valid coordinate contract. A one-shot image may depict a hidden or previous surface and does not authorize pixel input. Rediscover the app and list_windows, select its current task-relevant window, then observe that new target. Do not keep clicking or observing the old surface.';
      }
      delete value.tree_markdown;
      delete value.observation_id;
      delete value.capture_id;
      delete value.capture_binding;
      const semantics = observationSemantics(value);
      if (semantics) {
        value.result_fingerprint = digest({ operation, semantics });
        value.progress_evidence = { kind: 'ax_target_semantics', semantic_coverage: semanticCoverage(value, semantics) };
      } else {
        value.progress_evidence = { kind: 'unknown', semantic_coverage: 'unknown' };
      }
    }
    if (payload.target_ref && !value.target_ref) value.target_ref = String(payload.target_ref);
    if (operation === 'observe' && image
      && !(value.background_input?.observation?.frame_freshness === 'unknown'
        && !value.coordinate_contract && !value.actionable_elements_available)) {
      const data = Buffer.from(String(image.data || ''), 'base64');
      const mime = image.mime_type === 'image/jpeg' ? 'image/jpeg' : 'image/png';
      const valid = data.length > 8 && data.length <= 10 * 1024 * 1024 && (mime === 'image/png' ? data.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) : data.subarray(0, 3).equals(Buffer.from([255,216,255])));
      if (valid) {
        const dir = path.join(this.runtimeRoot, 'computer', 'captures'); fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
        const file = path.join(dir, `${id('capture')}.${mime === 'image/png' ? 'png' : 'jpg'}`);
        fs.writeFileSync(file, data, { mode: 0o600 });
        value.model_image = { path: file, mime_type: mime, size: data.length, sha256: crypto.createHash('sha256').update(data).digest('hex') };
      }
    }
    for (const key of Object.keys(value)) if (/path|secret|token|markdown|raw/i.test(key)) delete value[key];
    // The only path allowed out is one created above in the private capture directory.
    if (operation === 'observe' && value.model_image?.path) value.model_image_path = value.model_image.path;
    if (['list_apps', 'list_windows'].includes(operation)) {
      const withoutRefs = items => items?.map(({ target_ref: _targetRef, ...item }) => item);
      value.result_fingerprint = digest({ operation, apps: withoutRefs(value.apps), windows: withoutRefs(value.windows), page: value.page });
    }
    value.operation = operation; return value;
  }

  #cleanupCaptures() {
    const dir = path.join(this.runtimeRoot, 'computer', 'captures');
    let entries; try { entries = fs.readdirSync(dir); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
    for (const entry of entries) if (/^capture_[a-f0-9]{32}\.(png|jpg)$/.test(entry)) {
      try { fs.unlinkSync(path.join(dir, entry)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }

  #response(message, status, errorCode, executed, outcomeKnown, result = {}) {
    const response = { request_id: message.request_id, action_id: message.action_id || '', status, executed, outcome_known: outcomeKnown, error_code: errorCode || '', result, audit: { request_id: message.request_id, action_id: message.action_id || '', task_id: String(message.payload?.task_id || ''), target_ref: String(result?.target_ref || ''), operation: message.operation, status, error_code: errorCode || '', epoch: this.epoch, generation: this.generation } };
    if (status !== 'outcome_unknown') this.requests.set(message.request_id, response);
    return response;
  }

  #ledger(value) {
    fs.mkdirSync(path.dirname(this.ledgerPath), { recursive: true, mode: 0o700 });
    fs.appendFileSync(this.ledgerPath, JSON.stringify(value) + '\n', { encoding: 'utf8', mode: 0o600 });
  }

  async stop() {
    this.enabled = false;
    this.driver?.setEnabled?.(false);
    this.revoke('shutdown'); this.generation += 1; this.brokerSessionId = id('cu_session');
    if (this.server) await new Promise(resolve => this.server.close(resolve));
    this.server = null; try { fs.unlinkSync(this.socketPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await this.driverStop;
  }
}

module.exports = { ComputerBroker, McpDriverAdapter, OPERATIONS, READ_ONLY, WRITE, INTERNAL_OPERATIONS, DRIVER_CONTRACTS, DRIVER_TOOLS, OBSERVATION_REF_PATTERN, classifyDriverError, observationSemantics, semanticCoverage, approvalPresentation, resolvedApprovalPanelRequest };
