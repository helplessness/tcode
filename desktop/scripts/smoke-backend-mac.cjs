const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const executable = process.env.TCODE_PACKAGED_BACKEND || path.join(root, 'build/release/backend/tcode-backend');
if (!fs.existsSync(executable)) throw new Error(`Packaged backend not found: ${executable}`);

const runtime = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-packaged-backend-'));
const token = 'd'.repeat(64);
const env = {
  ...process.env,
  TCODE_DESKTOP_RUNTIME: runtime,
  TCODE_BOOTSTRAP_EMAIL: 'packaged-smoke@example.test',
  TCODE_BOOTSTRAP_PASSWORD: 'packaged-smoke-password',
};
for (const key of ['PYTHONPATH', 'PYTHONHOME', 'TCODE_CONFIG_ROOT', 'TCODE_PROJECT_ROOT']) delete env[key];

const child = spawn(executable, [], { cwd: os.tmpdir(), env, stdio: ['pipe', 'pipe', 'pipe', 'pipe'] });
let stderr = '';
child.stderr.on('data', data => { stderr += data; });
child.stdin.write(JSON.stringify({ token }) + '\n');

const timeout = setTimeout(() => {
  child.kill('SIGTERM');
  console.error(`Packaged backend did not become ready:\n${stderr}`);
  process.exit(2);
}, 90_000);

child.stdio[3].once('data', async data => {
  try {
    const ready = JSON.parse(String(data).trim().split('\n')[0]);
    const origin = `http://127.0.0.1:${ready.port}`;
    const headers = { Cookie: `tcode_desktop_gate=${token}` };
    const [health, desktop] = await Promise.all([
      fetch(`${origin}/api/health`, { headers }),
      fetch(`${origin}/desktop.html`, { headers }),
    ]);
    const html = await desktop.text();
    if (!health.ok || !desktop.ok || !/<html/i.test(html)) {
      throw new Error(`Unexpected response: health=${health.status}, desktop=${desktop.status}`);
    }
    if (html.includes(root)) throw new Error('Packaged frontend leaked a source-tree path');
    console.log(JSON.stringify({ status: 'passed', architecture: process.arch, runtime }));
  } catch (error) {
    console.error(error.message, stderr);
    process.exitCode = 1;
  } finally {
    clearTimeout(timeout);
    child.kill('SIGTERM');
  }
});

child.once('exit', code => {
  if (code && !process.exitCode) {
    clearTimeout(timeout);
    console.error(stderr);
    process.exitCode = code;
  }
});
