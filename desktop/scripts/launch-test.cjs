const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');

let executable = require('electron');
if (process.platform === 'darwin') {
  // A separate bundle identity makes simultaneous development windows unambiguous.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-test-app-'));
  const bundle = path.join(root, 'TCode Desktop Test.app');
  fs.cpSync(path.resolve(executable, '../../..'), bundle, { recursive: true, verbatimSymlinks: true, mode: fs.constants.COPYFILE_FICLONE });
  const plist = path.join(bundle, 'Contents/Info.plist');
  execFileSync('/usr/bin/plutil', ['-replace', 'CFBundleIdentifier', '-string', 'com.tcode.desktop.test', plist]);
  execFileSync('/usr/bin/plutil', ['-replace', 'CFBundleName', '-string', 'TCode Desktop Test', plist]);
  execFileSync('/usr/bin/plutil', ['-replace', 'CFBundleDisplayName', '-string', 'TCode Desktop Test', plist]);
  const entry = path.join(bundle, 'Contents/Resources/app');
  fs.mkdirSync(entry);
  fs.copyFileSync(path.join(__dirname, 'test-bootstrap.cjs'), path.join(entry, 'main.cjs'));
  fs.writeFileSync(path.join(entry, 'package.json'), JSON.stringify({ name: 'tcode-desktop-test', version: '0.1.0', main: 'main.cjs', tcodeDesktopRoot: path.resolve(__dirname, '..') }));
  executable = path.join(bundle, 'Contents/MacOS/Electron');
}
const child = spawn(executable, [path.resolve(__dirname, '..'), '--test-instance'], { stdio: 'inherit' });
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code || 0; });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
