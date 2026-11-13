// Isolated native DOM regression using the production disclosure setup.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const ts = require('../../web/node_modules/typescript');
app.setPath('userData', fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'tcode-goal-disclosure-')));
app.whenReady().then(async () => {
  const window = new BrowserWindow({ show: false });
  try {
    await window.loadURL('data:text/html,<div class="composer-input-card" style="width:500px"><details><summary>Goal</summary><button>Inside</button></details></div><button id="outside">Outside</button>');
    const source = fs.readFileSync(path.resolve(__dirname, '../../web/src/components/MessageInput.tsx'), 'utf8');
    const start = source.indexOf("const pane = element.closest('.composer-input-card')!");
    const end = source.indexOf('\n            })}', start);
    assert(start >= 0 && end > start);
    const setup = ts.transpileModule(source.slice(start, end), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
    await window.webContents.executeJavaScript(`{
      const element = document.querySelector('details');
      const onCleanup = callback => window.cleanupDisclosure = callback;
      ${setup}
      void 0;
    }`);
    const run = code => window.webContents.executeJavaScript(code);
    const wait = () => new Promise(resolve => setTimeout(resolve, 100));
    await wait();
    assert.equal(await run("document.querySelector('details').open"), false);
    await run("document.querySelector('details').open=true;document.querySelector('details button').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))");
    assert.equal(await run("document.querySelector('details').open"), true);
    await run("document.querySelector('#outside').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))");
    assert.equal(await run("document.querySelector('details').open"), false);
    await run("document.querySelector('details').open=true;document.querySelector('details button').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))");
    assert.equal(await run("document.querySelector('details').open"), false);
    assert.equal(await run("document.activeElement.tagName"), 'SUMMARY');
    await run("document.querySelector('.composer-input-card').style.width='800px'");
    await wait();
    await run("document.querySelector('#outside').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))");
    assert.equal(await run("document.querySelector('details').open"), true);
    await run('window.cleanupDisclosure()');
    console.log('PASS: internal click, outside dismissal, Escape focus, wide layout preserved');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { window.destroy(); app.exit(process.exitCode || 0); }
});
