const fs = require('node:fs');
const path = require('node:path');

// Only the main process can decrypt this profile-local vault. Never use the
// Linux basic_text fallback, or write credentials into tcode.json/localStorage.
function modelSecrets(root, safeStorage) {
  const file = path.join(root, 'model-secrets.bin');
  function available() {
    return safeStorage.isEncryptionAvailable() && safeStorage.getSelectedStorageBackend?.() !== 'basic_text';
  }
  function read() {
    if (!fs.existsSync(file)) return {};
    if (!available()) throw new Error('系统密钥存储不可用，请解锁系统钥匙串后重试');
    try { return JSON.parse(safeStorage.decryptString(fs.readFileSync(file))); }
    catch { throw new Error('无法解锁模型密钥，请检查系统钥匙串'); }
  }
  function write(values) {
    if (!available()) throw new Error('系统密钥存储不可用，未保存密钥');
    fs.mkdirSync(root, { recursive: true });
    const temp = `${file}.tmp`;
    fs.writeFileSync(temp, safeStorage.encryptString(JSON.stringify(values)), { mode: 0o600 });
    fs.chmodSync(temp, 0o600);
    fs.renameSync(temp, file);
  }
  return { read, write, available };
}
module.exports = { modelSecrets };
