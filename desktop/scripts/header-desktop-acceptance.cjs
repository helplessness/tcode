// Full real desktop/Agent acceptance fixture. No production config is modified.
// Launch using the project's Electron executable, not plain node.
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '../..');
const instances = require('../test-instance.cjs');
const original = instances.createTestInstance;
const python = path.join(root, '.desktop-venv/bin/python');
const mock = spawn(python, [path.join(root, 'scripts/header_desktop_mock.py')], {stdio: ['ignore', 'pipe', 'inherit']});
let fixtureRoot;
process.on('exit', () => {
  mock.kill('SIGTERM');
  // Remove only this fixture's copied model credentials, retain session/log evidence.
  if (fixtureRoot) {
    try { fs.unlinkSync(path.join(fixtureRoot, 'runtime/config/tcode.json')); }
    catch (error) { if (error.code !== 'ENOENT') console.error('Fixture credential cleanup failed:', error.code); }
  }
});
const lines = readline.createInterface({input: mock.stdout});
lines.on('line', line => {
  const event = JSON.parse(line);
  if (fixtureRoot) fs.appendFileSync(path.join(fixtureRoot, 'header-events.jsonl'), line + '\n');
  if (!event.ready) return;
  instances.createTestInstance = (...args) => {
    const instance = original(...args);
    fixtureRoot = instance.root;
    const configRoot = instance.environment.TCODE_CONFIG_ROOT;
    fs.mkdirSync(configRoot, {recursive: true});
    const source = JSON.parse(fs.readFileSync(path.join(root, '.tcode-local/tcode.json'), 'utf8'));
    const model = 'deepseek/deepseek-v4-flash';
    if (!source.provider?.deepseek) throw Error('Existing deepseek provider is required');
    // Only reuse the already-configured model, never copy plugin/service configuration.
    const config = {model, provider: {deepseek: source.provider.deepseek},
      agent: {build: {permission: {'*': 'deny', 'mcp_*': 'allow', question: 'allow'}}}};
    const write = (name, value) => fs.writeFileSync(path.join(configRoot, name), JSON.stringify(value, null, 2), {mode: 0o600});
    write('tcode.json', config);
    write('plugins.json', {plugins: []});
    write('mcp_agent_config.json', {agents: {build: {allowed_tools: ['query']}}});
    write('desktop-mcp.json', {
      $desktop: {managed_job_protocol: false, artifact_services: {}, artifact_default_service_id: '', artifact_target_services: {}},
      'job-protocol-mcp': {url: 'http://127.0.0.1:9/mcp', enabled: false},
      'local-header-echo': {url: `http://127.0.0.1:${event.port}/mcp`, enabled: true, invocation_mode: 'direct', timeout: 180000,
        dynamic_headers: {'X-Project-ID': {variable: 'project_id', description: '本次查询所属项目编号，用于选择项目结果', extraction_hint: '从用户明确指定的项目提取；缺失或有歧义时询问用户'}}}
    });
    Object.assign(instance.environment, {TCODE_AUTH_REQUIRED: '0', TCODE_DESKTOP_DEV_LOCAL_SERVICES: '1',
      TCODE_DESKTOP_PAUSE_REMOTE_SERVICES: '0', SYMPHONY_DIRECT_MCP_SERVERS: 'local-header-echo',
      SYMPHONY_NEVER_ROUTE_MCP_SERVERS: 'local-header-echo,job-protocol-mcp'});
    fs.writeFileSync(path.join(fixtureRoot, 'header-fixture.json'), JSON.stringify({port: event.port, model, runtime: path.join(fixtureRoot, 'runtime')}));
    console.log(JSON.stringify({header_desktop_fixture: fixtureRoot, mock_port: event.port}));
    return instance;
  };
  process.argv.push('--test-instance');
  require('../main.cjs');
});
