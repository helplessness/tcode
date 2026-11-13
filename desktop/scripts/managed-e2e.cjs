#!/usr/bin/env node
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { launchManagedDesktop } = require('../managed-launch.cjs');

const projectRoot = path.resolve(__dirname, '../..');
const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-desktop-e2e-'));
const logPath = path.join(runtimeRoot, 'lifecycle.jsonl');
const electron = path.join(projectRoot, 'desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron');
const append = event => fs.appendFileSync(logPath, `${JSON.stringify({ at: new Date().toISOString(), ...event })}\n`, { mode: 0o600 });
const launch = launchManagedDesktop({
  executable: electron,
  args: [path.join(projectRoot, 'desktop')],
  cwd: projectRoot,
  env: { ...process.env, TCODE_DESKTOP_MANAGED_E2E: '1', TCODE_DESKTOP_LIFECYCLE_LOG: logPath },
  onEvent: event => { append(event); if (event.type === 'stderr') process.stderr.write(event.line); },
});
let cleaning = false;
async function cleanup(reason) {
  if (cleaning) return;
  cleaning = true;
  await launch.stop(reason);
}
process.once('SIGINT', () => void cleanup('parent_sigint'));
process.once('SIGTERM', () => void cleanup('parent_sigterm'));
process.once('beforeExit', () => void cleanup('parent_before_exit'));
launch.ready.then(payload => {
  process.stdout.write(`${JSON.stringify({ managed_desktop: 'ready', runtime_root: runtimeRoot, lifecycle_log: logPath, ...payload })}\n`);
}).catch(async error => {
  append({ type: 'launch_error', reason: error.message });
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
  await cleanup('launch_error');
});
