// Run with the repository's Electron binary. No private config is read.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const instances = require('../test-instance.cjs');
const root = path.resolve(__dirname, '../..');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'boltz-mock-'));
const python = path.join(root, '.desktop-venv/bin/python');
const mock = spawn(python, [path.join(root, 'scripts/fixtures/boltz_desktop_mock.py'), fixture],
  { stdio: ['ignore', 'pipe', 'inherit'] });
process.on('exit', () => mock.kill());
const original = instances.createTestInstance;
const lines = readline.createInterface({ input: mock.stdout });
lines.once('line', port => {
  if (!/^\d+$/.test(port)) throw new Error('invalid mock port');
  const base = 'http://127.0.0.1:' + port;
  instances.readTestLogin = () => ({ email: 'fixture@example.test', password: 'Fixture-only-73942', userId: 'desktop-test' });
  instances.createTestInstance = (...args) => {
    const instance = original(...args);
    const prior = process.env.TCODE_BOLTZ_FIXTURE_RUNTIME;
    if (prior) {
      const resolved = fs.realpathSync(prior);
      if (!path.basename(path.dirname(resolved)).startsWith('tcode-isolated-') ||
          !fs.existsSync(path.join(resolved, '../test-instance.json'))) throw Error('Not an isolated fixture runtime');
      fs.cpSync(resolved, path.join(instance.root, 'runtime'), { recursive: true });
    }
    const folder = instance.environment.TCODE_CONFIG_ROOT;
    fs.mkdirSync(folder, { recursive: true });
    const write = (name, value) => fs.writeFileSync(path.join(folder, name), JSON.stringify(value), { mode: 0o600 });
    write('tcode.json', { model: 'smoke/smoke-model', provider: { smoke: {
      npm: '@ai-sdk/openai-compatible', options: { baseURL: base + '/v1', apiKey: 'local-fixture' },
      models: { 'smoke-model': { name: 'Mock model' } },
    } } });
    write('desktop-mcp.json', { $desktop: { managed_job_protocol: false,
      artifact_services: { fixture: { url: base } }, artifact_default_service_id: 'fixture' },
      'job-protocol-mcp': { url: base + '/mcp', transport: 'streamable-http' } });
    write('human_review.json', { enabled: true, required_review_tools: {
      'boltz-mcp': { tools: ['boltz_mcp_server'] },
    } });
    write('mcp_agent_config.json', { agents: { build: {
      allowed_tools: ['job-protocol-mcp'], allowed_routed_servers: ['boltz-mcp'], allow_plugin_async_targets: false,
    } } });
    write('mcp_exec_only.json', {
      'boltz-mcp': { url: base + '/mcp', transport: 'streamable-http', invocation_mode: 'async' },
    });
    Object.assign(instance.environment, {
      TCODE_DESKTOP_DEV_LOCAL_SERVICES: '1', TCODE_ASYNC_EXECUTION_MODE: 'bridge',
      TCODE_ASYNC_WATCHER_ENABLED: '1', HUMAN_REVIEW_ENABLED: 'true',
      TCODE_LAZY_ARTIFACT_MCP_SERVERS: 'fixture-unused',
    });
    console.log(JSON.stringify({ runtime: path.join(instance.root, 'runtime'), fixture, base }));
    return instance;
  };
  lines.on('line', line => console.log(line));
  process.argv.push('--test-instance');
  require('../main.cjs');
});
