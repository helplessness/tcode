const fs = require('node:fs');
const path = require('node:path');
const { parseEnv } = require('node:util');

function readTestLogin(file, inherited = process.env) {
  if (!fs.existsSync(file)) throw new Error('未找到测试登录配置，请通过 TCODE_DESKTOP_TEST_LOGIN_CONFIG 指定原账号配置文件。');
  const values = parseEnv(fs.readFileSync(file, 'utf8'));
  for (const key of ['TCODE_BOOTSTRAP_EMAIL', 'TCODE_BOOTSTRAP_PASSWORD', 'TCODE_BOOTSTRAP_USER_ID']) {
    const value = values[key] || '';
    // Support the existing shell-style self/default assignment without sourcing
    // the service file or executing command substitutions.
    const fallback = value.match(new RegExp(`^\\$\\{${key}(?::-([^{}]*))?\\}$`));
    if (fallback) values[key] = inherited[key] || fallback[1] || '';
    else if (value.includes('${') || value.includes('$(')) throw new Error(`测试登录配置中的 ${key} 使用了不支持的表达式，请使用字面值或该字段的默认值表达式。`);
  }
  const credentials = { email: values.TCODE_BOOTSTRAP_EMAIL, password: values.TCODE_BOOTSTRAP_PASSWORD, userId: values.TCODE_BOOTSTRAP_USER_ID };
  if (!credentials.email || !credentials.password || credentials.password.length < 12) throw new Error('测试登录配置需要有效的 TCODE_BOOTSTRAP_EMAIL 和至少 12 位的 TCODE_BOOTSTRAP_PASSWORD。');
  return credentials;
}

// A fresh profile, not a copy of the normal account or developer configuration.
function createTestInstance(temp, inherited, credentials) {
  const root = fs.mkdtempSync(path.join(temp, 'tcode-isolated-'));
  fs.chmodSync(root, 0o700);
  const environment = {};
  for (const key of ['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'SystemRoot', 'WINDIR']) {
    if (inherited[key] !== undefined) environment[key] = inherited[key];
  }
  // Libraries using home/XDG defaults must not read or write the real profile.
  const home = path.join(root, 'home');
  fs.mkdirSync(home, { mode: 0o700 });
  fs.writeFileSync(path.join(root, 'test-instance.json'), JSON.stringify({ isolated: true, createdAt: new Date().toISOString() }), { mode: 0o600 });
  Object.assign(environment, {
    HOME: home, XDG_CONFIG_HOME: path.join(home, '.config'), XDG_CACHE_HOME: path.join(home, '.cache'),
    TCODE_DESKTOP_TEST_INSTANCE: '1', TCODE_CONFIG_ROOT: path.join(root, 'runtime/config'),
    TCODE_AUTH_REQUIRED: '1', TCODE_BOOTSTRAP_EMAIL: credentials.email,
    TCODE_BOOTSTRAP_PASSWORD: credentials.password, TCODE_BOOTSTRAP_USER_ID: credentials.userId || 'desktop-test',
  });
  return { root, environment };
}

module.exports = { createTestInstance, readTestLogin };
