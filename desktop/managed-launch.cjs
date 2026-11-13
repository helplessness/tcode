const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');

const READY_PREFIX = 'TCODE_DESKTOP_READY ';

function validateReadyPayload(payload, expected = {}) {
  if (!payload || payload.type !== 'desktop_ready' || payload.protocol !== 1) throw new Error('invalid desktop ready protocol');
  if (!Number.isInteger(payload.pid) || payload.pid < 1) throw new Error('invalid desktop pid');
  if (expected.pid && payload.pid !== expected.pid) throw new Error('desktop ready pid mismatch');
  if (expected.projectRoot && payload.project_root !== expected.projectRoot) throw new Error('desktop ready project mismatch');
  const renderer = new URL(payload.renderer_url);
  if (renderer.hostname !== '127.0.0.1' || renderer.pathname !== '/desktop.html') throw new Error('invalid desktop renderer');
  if (payload.backend_healthy !== true) throw new Error('desktop backend is not healthy');
  if (!Number.isInteger(payload.host_generation) || payload.host_generation < 1) throw new Error('invalid computer host generation');
  return payload;
}

function launchManagedDesktop({ executable, args, cwd, env, spawnImpl = spawn, timeoutMs = 120000, onEvent = () => {} }) {
  // Keep terminal signals on the owning harness. The child observes owner death
  // through stdin EOF and performs its own Electron/backend/driver cleanup.
  const child = spawnImpl(executable, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'], detached: true });
  let settled = false;
  let stopped = false;
  let buffer = '';
  let timer;
  const events = new EventEmitter();
  const ready = new Promise((resolve, reject) => {
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error); else resolve(value);
    };
    timer = setTimeout(() => finish(new Error('desktop ready timeout')), timeoutMs);
    child.stdout.on('data', chunk => {
      buffer += chunk.toString('utf8');
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        onEvent({ type: 'stdout', line });
        if (!line.startsWith(READY_PREFIX)) continue;
        try { finish(null, validateReadyPayload(JSON.parse(line.slice(READY_PREFIX.length)), { pid: child.pid, projectRoot: cwd })); }
        catch (error) { finish(error); }
      }
    });
    child.stderr.on('data', chunk => onEvent({ type: 'stderr', line: chunk.toString('utf8') }));
    child.once('error', finish);
    child.once('exit', (code, signal) => {
      const event = { type: 'exit', pid: child.pid, code, signal, expected: stopped };
      onEvent(event); events.emit('exit', event);
      if (!settled) finish(new Error(`desktop exited before ready (${signal || code})`));
    });
  });
  const stop = async reason => {
    if (stopped) return;
    stopped = true;
    onEvent({ type: 'cleanup', pid: child.pid, reason });
    if (child.exitCode !== null || child.signalCode !== null) return;
    await new Promise(resolve => {
      const kill = setTimeout(() => {
        try { process.kill(-child.pid, 'SIGTERM'); }
        catch (error) { if (error.code !== 'ESRCH') throw error; }
      }, 10000);
      child.once('exit', () => { clearTimeout(kill); resolve(); });
      child.stdin.end();
    });
  };
  return { child, ready, stop, events };
}

module.exports = { READY_PREFIX, validateReadyPayload, launchManagedDesktop };
