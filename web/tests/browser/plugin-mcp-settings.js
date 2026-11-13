// Real settings components; all API writes intercepted, never user services.
let active = false;
let fail = true;
const actions = [];
await page.route('**/api/**', async route => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  if (!path.startsWith('/api/')) { await route.continue(); return; }
  if (path === '/api/plugins/async-targets') {
    await route.fulfill({json: {gateway_url: 'http://127.0.0.1:10121', config_file: '/fixture/.tcode/plugin-async-targets.sqlite', management_configured: true,
      targets: [{plugin: 'community-predict', target: 'compute', server: 'plugin-fixture', active, version: active ? 'v1' : '',
        execution: {mode: 'remote-job'}, tools: ['predict'], url: '', token_env: '', declaration_files: ['async-targets.json']}]}}); return;
  }
  if (path.startsWith('/api/plugins/community-predict/async-targets/compute/')) {
    actions.push({path, body: route.request().postDataJSON()});
    if (path.endsWith('/check') && fail) {fail = false; await route.fulfill({status: 502, json: {detail: '测试：远端连接失败，请检查地址'}}); return;}
    if (path.endsWith('/activate')) active = true;
    if (path.endsWith('/deactivate')) active = false;
    await route.fulfill({json: {ok: true, status: active ? 'active' : 'inactive'}}); return;
  }
  if (path === '/api/mcp/config') {await route.fulfill({json: {servers: [], can_edit: true, config_file: '/fixture/mcp.json'}}); return;}
  await route.fulfill({json: {servers: [], agents: [], models: [], tools: [], sessions: [], style: 'default', default_style: 'clay'}});
});
await page.goto('http://127.0.0.1:5178/tests/acceptance/plugin-mcp-settings.html', {waitUntil: 'domcontentloaded'});
await expect(page.getByRole('button', {name: '添加常规 MCP', exact: true})).toBeVisible();
await expect(page.getByText('常规 MCP 配置文件：', {exact: false})).toBeVisible();
const section = page.getByRole('region', {name: '插件异步 MCP 配置'});
await expect(section.getByText('http://127.0.0.1:10121', {exact: true})).toBeVisible();
const enable = section.getByRole('button', {name: '启用／更新异步目标'});
await expect(enable).toBeDisabled();
await section.getByLabel('计算 MCP 地址').fill('https://compute.example.test/mcp');
await section.getByLabel('Bearer Token 环境变量名', {exact: false}).fill('TCODE_PLUGIN_TOKEN_DEMO');
await section.getByRole('button', {name: '检查连接'}).click();
await expect(section.getByRole('alert')).toContainText('远端连接失败');
await section.getByRole('button', {name: '检查连接'}).click();
await expect(section.getByRole('status')).toContainText('连接与工具检查通过');
await expect(enable).toBeDisabled();
await section.getByRole('checkbox').check();
await enable.click();
await expect(section.getByRole('heading', {name: /community-predict.*已启用/})).toBeVisible();
await section.getByRole('button', {name: '停用新任务'}).click();
await expect(section.getByRole('heading', {name: /community-predict.*未启用/})).toBeVisible();
assert(actions.filter(a => a.path.endsWith('/activate')).length === 1);
assert(actions.find(a => a.path.endsWith('/activate')).body.approved === true);
await page.getByRole('button', {name: '添加常规 MCP', exact: true}).click();
await expect(page.getByLabel('调用方式')).toHaveValue('direct');
await page.getByLabel('调用方式').selectOption('async');
await expect(page.getByText('此选项只改变路由，不会向 Job Protocol 注册新工具。', {exact: false})).toBeVisible();
await page.setViewportSize({width: 390, height: 844});
assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
return {directDefault: true, asyncApprovalRequired: true, failureRetry: true, activateDeactivate: true, configPathsVisible: true, narrowViewport: true};
