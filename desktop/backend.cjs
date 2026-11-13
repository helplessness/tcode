const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

function validateReady(message) {
  if (message.type !== 'ready' || message.protocol !== 1 ||
      !Number.isInteger(message.port) || message.port < 1 || message.port > 65535) {
    throw new Error('本地后端返回了不兼容的启动协议');
  }
  return `http://127.0.0.1:${message.port}`;
}

function isLocalNavigation(url, origin) {
  try { return new URL(url).origin === origin; } catch { return false; }
}

function isExternalLink(url) {
  try { return ['https:', 'http:', 'mailto:'].includes(new URL(url).protocol); }
  catch { return false; }
}

function startBackend({ projectRoot, runtimeRoot, logPath, python, backendExecutable, environment = process.env, computer, browser, onExit }) {
  const token = randomBytes(32).toString('hex');
  fs.mkdirSync(runtimeRoot, { recursive: true });
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  const log = fs.openSync(logPath, 'a', 0o600);
  fs.chmodSync(logPath, 0o600);
  const executable = backendExecutable || '/bin/bash';
  const args = backendExecutable ? [] : [path.join(projectRoot, 'desktop/backend.sh')];
  const child = spawn(executable, args, {
    cwd: backendExecutable ? runtimeRoot : projectRoot,
    env: {
      ...environment,
      TCODE_DESKTOP_PYTHON: python,
      // Reuse the shipped Node runtime; never expose the host PATH to jobs.
      TCODE_DESKTOP_NODE: process.versions.electron ? process.execPath : '',
      TCODE_DESKTOP_RUNTIME: runtimeRoot,
      PYTHONUNBUFFERED: '1',
    },
    detached: true,
    // fd 3 is a private startup channel, never parsed from application logs.
    stdio: ['pipe', log, log, 'pipe'],
  });
  fs.closeSync(log);
  let stopped = false;
  let stopPromise;
  child.stdin.on('error', () => {}); // A startup failure can close stdin first.
  // Leave stdin open: EOF also lets the backend detect a crashed desktop parent.
  child.stdin.write(JSON.stringify({ token, computer, browser }) + '\n');
  const ready = new Promise((resolve, reject) => {
    // The authoritative session projection alone can take ~58s on a cold
    // 2.7GB runtime. Keep the host alive long enough for the subsequent
    // bounded startup hooks instead of killing a healthy backend at 60s.
    const timer = setTimeout(() => reject(new Error('本地后端在 120 秒内未就绪，请查看启动日志')), 120000);
    const lines = readline.createInterface({ input: child.stdio[3] });
    const finish = (error, value) => {
      clearTimeout(timer);
      lines.close();
      if (error) reject(error); else resolve(value);
    };
    lines.once('line', line => {
      try { finish(null, validateReady(JSON.parse(line))); }
      catch (error) { finish(error); }
    });
    child.once('error', error => finish(error));
    child.once('exit', (code, signal) => {
      finish(new Error(`本地后端已退出（${signal || code}），请查看启动日志`));
      if (!stopped) {
        void stop();
        onExit?.(code, signal);
      }
    });
  });

  function stop() {
    if (stopPromise) return stopPromise;
    stopped = true;
    stopPromise = new Promise(resolve => {
      const signalGroup = signal => {
        if (!child.pid) return;
        try { process.kill(-child.pid, signal); }
        catch (error) {
          // The process may already be reaped, or macOS may reject signalling
          // its former process group during teardown. Either means there is
          // nothing left for this owned launcher to wait on.
          if (!['ESRCH', 'EPERM'].includes(error.code)) throw error;
        }
      };
      const finish = () => {
        clearTimeout(timer);
        // A parent can exit before a stubborn child. Reap its remaining group.
        signalGroup('SIGKILL');
        child.stdin.destroy();
        resolve();
      };
      // Kill only this launch's process group, never processes selected by name.
      const timer = setTimeout(() => { signalGroup('SIGKILL'); finish(); }, 10000);
      child.once('exit', finish);
      signalGroup('SIGTERM');
      if (child.exitCode !== null || child.signalCode !== null || !child.pid) finish();
    });
    return stopPromise;
  }
  return { ready, stop, token, child };
}

module.exports = { startBackend, validateReady, isLocalNavigation, isExternalLink };
