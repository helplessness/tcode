const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createTestInstance, readTestLogin } = require('../test-instance.cjs');

test('test profiles have independent storage, credentials and no inherited service configuration', () => {
  const credentials = { email: 'test@example.com', password: 'test-only-strong-password', userId: 'test' };
  const first = createTestInstance(os.tmpdir(), { PATH: '/bin', HOME: '/real', TCODE_CONFIG_ROOT: '/real/config', TCODE_WORKSPACE_STATE_DB: '/real/db', TCODE_ENV_FILE: '/real/env', OPENAI_API_KEY: 'secret', TMCP_JOB_PROTOCOL_URL: 'http://real', HTTPS_PROXY: 'http://real' }, credentials);
  const second = createTestInstance(os.tmpdir(), {}, credentials);
  try {
    assert.notEqual(first.root, second.root);
    assert.equal(first.environment.TCODE_BOOTSTRAP_PASSWORD, credentials.password);
    assert.equal(first.environment.TCODE_BOOTSTRAP_USER_ID, credentials.userId);
    assert.equal(first.environment.HOME, path.join(first.root, 'home'));
    assert.equal(first.environment.TCODE_CONFIG_ROOT, path.join(first.root, 'runtime/config'));
    assert.equal(first.environment.TCODE_AUTH_REQUIRED, '1');
    for (const key of ['TCODE_WORKSPACE_STATE_DB', 'TCODE_ENV_FILE', 'OPENAI_API_KEY', 'TMCP_JOB_PROTOCOL_URL', 'HTTPS_PROXY']) assert.equal(first.environment[key], undefined);
    assert.equal(fs.existsSync(path.join(first.root, 'test-login.json')), false);
    assert.equal(fs.statSync(path.join(first.root, 'test-instance.json')).mode & 0o777, 0o600);
  } finally {
    fs.rmSync(first.root, { recursive: true });
    fs.rmSync(second.root, { recursive: true });
  }
});

test('only bootstrap identity is extracted; shell code and service settings are not executed or inherited', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-login-test-'));
  const file = path.join(root, 'login.env');
  try {
    fs.writeFileSync(file, 'export TCODE_BOOTSTRAP_EMAIL="test@example.com"\nTCODE_BOOTSTRAP_PASSWORD="test-strong-password"\nTCODE_BOOTSTRAP_USER_ID=test\nTCODE_MCP_CONFIG=/real/service.json\nUNUSED=$(touch should-not-exist)\n');
    assert.deepEqual(readTestLogin(file), { email: 'test@example.com', password: 'test-strong-password', userId: 'test' });
    fs.writeFileSync(file, 'TCODE_BOOTSTRAP_EMAIL=test@example.com\nTCODE_BOOTSTRAP_PASSWORD=${TCODE_BOOTSTRAP_PASSWORD:-test-default-password}\n');
    assert.equal(readTestLogin(file, {}).password, 'test-default-password');
    assert.equal(readTestLogin(file, { TCODE_BOOTSTRAP_PASSWORD: 'test-override-password' }).password, 'test-override-password');
    fs.writeFileSync(file, 'TCODE_BOOTSTRAP_EMAIL=test@example.com\nTCODE_BOOTSTRAP_PASSWORD=$(touch should-not-exist)\n');
    assert.throws(() => readTestLogin(file, {}), /不支持的表达式/);
    assert.throws(() => readTestLogin(path.join(root, 'missing')), /未找到/);
    fs.writeFileSync(file, 'TCODE_BOOTSTRAP_EMAIL=test@example.com\nTCODE_BOOTSTRAP_PASSWORD=short\n');
    assert.throws(() => readTestLogin(file), /至少 12 位/);
  } finally { fs.rmSync(root, { recursive: true }); }
});
