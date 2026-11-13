// Real desktop backend, fresh profile and original login; no production writes.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { startBackend } = require('../backend.cjs');
const { createTestInstance, readTestLogin } = require('../test-instance.cjs');
const projectRoot = path.resolve(__dirname, '../..');
const credentials = readTestLogin(path.join(projectRoot, '.tcode-local/symphony_route.env'));
const instance = createTestInstance(os.tmpdir(), process.env, credentials);
// Deliberately reproduce a missing gateway without touching any running service.
instance.environment.TCODE_DESKTOP_PAUSE_REMOTE_SERVICES = '1';
const runtimeRoot = path.join(instance.root, 'runtime');
const python = path.join(projectRoot, '.desktop-venv/bin/python');
const backend = startBackend({ projectRoot, runtimeRoot, python, environment: instance.environment,
  logPath: path.join(instance.root, 'backend.log') });
(async () => {
  try {
    const origin = await backend.ready;
    let cookie = `tcode_desktop_gate=${backend.token}`;
    const api = async (route, method = 'GET', body) => {
      const response = await fetch(origin + route, { method, headers: { Cookie: cookie, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body) });
      const value = await response.json();
      assert(response.ok, `${method} ${route}: ${JSON.stringify(value)}`);
      if (response.headers.get('set-cookie')) cookie += '; ' + response.headers.get('set-cookie').split(';')[0];
      return value;
    };
    await api('/api/auth/login', 'POST', { email: credentials.email, password: credentials.password });
    const create = async name => (await api('/api/workdirs', 'POST', { name })).workdir;
    const seed = (workspace, status) => {
      assert(fs.realpathSync(workspace.path).startsWith(fs.realpathSync(runtimeRoot) + path.sep));
      execFileSync(python, ['-c', `
import json,sys
from tcode_agent.runtime.shared_tracker import SharedTracker
w,status=json.loads(sys.argv[1]),sys.argv[2]
t=SharedTracker.bind_workspace_owner({'id':'fixture-'+status,'identifier':'FIXTURE-'+status},tenant_id=w['tenant_id'],user_id=w['owner_user_id'],workspace_id=w['workspace_id'])
SharedTracker.upsert_issue(t,w['path'])
SharedTracker.record_result(task_id=t['id'],status=status,job_ref={'job_id':'old-fixture-job','process_id':'old-fixture-process'},workdir=w['path'])
`, JSON.stringify(workspace), status], { cwd: projectRoot, env: instance.environment, stdio: 'pipe' });
    };
    const purge = async name => {
      await api(`/api/workdirs/${name}`, 'DELETE');
      await api(`/api/workdirs/${name}/purge`, 'POST', { confirm_workspace_id: name });
      for (let i = 0; i < 100; i++) {
        const state = await api(`/api/workdirs/${name}/purge`);
        if (['purged', 'purge_failed'].includes(state.status)) return state;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw Error('Purge did not finish');
    };
    const workspace = await create('finished-history');
    seed(workspace, 'completed'); seed(workspace, 'failed');
    assert.equal((await purge(workspace.name)).status, 'purged');
    assert(!fs.existsSync(workspace.path));
    assert.equal((await create(workspace.name)).name, workspace.name);
    const active = await create('pending-remote'); seed(active, 'running');
    const blocked = await purge(active.name);
    assert.equal(blocked.status, 'purge_failed');
    assert(blocked.error.includes('任务服务当前未连接'));
    assert(!blocked.error.includes('not found'));
    assert(fs.existsSync(active.path), 'Unconfirmed remote job must retain files');
    assert.equal((await create('new-after-failed-purge')).name, 'new-after-failed-purge');
    console.log(JSON.stringify({ passed: true, isolatedRoot: instance.root,
      checks: ['original login', 'gateway intentionally unavailable', 'finished history purged', 'same name recreated',
        'active remote job safely blocked with readable error', 'files retained', 'new workspace after failed purge'] }));
  } finally { await backend.stop(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
