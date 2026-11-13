// Isolated real renderer + mocked desktop bridge. No backend, Driver or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  if (!process.versions.electron) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tcode-p2-offline-'));
    const web = path.resolve(__dirname, '../../web');
    const {build} = await import(path.join(web, 'node_modules/vite/dist/node/index.js'));
    const {default:solid} = await import(path.join(web, 'node_modules/vite-plugin-solid/dist/esm/index.mjs'));
    await build({root:web,base:'./',configFile:false,logLevel:'warn',plugins:[solid()],resolve:{dedupe:['solid-js']},
      build:{outDir:path.join(root,'page'),emptyOutDir:true,target:'esnext',
        rollupOptions:{input:path.resolve(__dirname,'../test/fixture/computer-canvas-offline.tsx'),output:{entryFileNames:'entry.js'}}}});
    const assets = path.join(root,'page/assets');
    const styles = fs.readdirSync(assets).filter(name=>name.endsWith('.css')).map(name=>`<link rel="stylesheet" href="assets/${name}">`).join('');
    fs.writeFileSync(path.join(root,'page/index.html'), `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'none'"><meta name="viewport" content="width=device-width,initial-scale=1">${styles}<style>body{margin:16px;font-family:system-ui;background:#f7f5fa}main{max-width:1080px;margin:auto}</style></head><body><div id="root"></div><script type="module" src="entry.js"></script></body></html>`);
    const env = {...process.env,TCODE_OFFLINE_RENDER_ROOT:root};
    delete env.ELECTRON_RUN_AS_NODE;
    const child = require('node:child_process').spawnSync(require('../node_modules/electron'),[__filename],{env,stdio:'inherit',timeout:60000});
    assert.equal(child.status,0,child.error?.message || 'Offline renderer failed');
    console.log(`Offline screenshots: ${root}/desktop.png, ${root}/narrow.png`);
    return;
  }
  const {app,BrowserWindow,session} = require('electron');
  const root = process.env.TCODE_OFFLINE_RENDER_ROOT;
  assert.ok(root);
  app.setPath('userData',path.join(root,'profile'));
  app.commandLine.appendSwitch('disable-background-networking');
  await app.whenReady();
  let blocked = 0;
  session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']},(_details,done)=>{blocked++;done({cancel:true});});
  const win = new BrowserWindow({show:false,width:1100,height:1000,webPreferences:{nodeIntegration:false,contextIsolation:true,backgroundThrottling:false}});
  win.webContents.on('console-message', event => console.error('Renderer:',event.message));
  const evaluate = script => win.webContents.executeJavaScript(script);
  const wait = script => evaluate(`new Promise((resolve,reject)=>{const start=Date.now();const poll=()=>{if(${script})resolve(true);else if(Date.now()-start>5000)reject(new Error('Offline condition timed out: '+${JSON.stringify(script)}));else setTimeout(poll,20)};poll()})`);
  try {
    await win.loadFile(path.join(root,'page/index.html'));
    await wait('window.offline && document.querySelector(".intelligent-canvas-stage iframe")');
    const ready = {available:true,enabled:true,status:'waiting_for_approval',control_epoch:1,driver_generation:1,
      permissions:{status:'known',identity_verified:true,accessibility:true,screen_recording:true},
      pending_approval:{approval_id:'a',revision:1,task_id:'t',operation:'observe',delivery_mode:'background',parameter_digest:'d',
        action_summary:{approval_kind:'task',navigation_proposal:true,exact_action:'仅操作离线样本'}}};
    await evaluate(`window.offline.push(${JSON.stringify(ready)});window.offline.resolveStatus({available:true,enabled:false,status:'paused'})`);
    await wait('document.querySelector(".computer-approval")');
    assert.match(await evaluate('document.querySelector(".computer-use-trigger").textContent'),/等待授权/);
    await evaluate('document.querySelector(".computer-approval input").click()');
    assert.equal(await evaluate('document.querySelector(".computer-approval input").checked'),false);
    await evaluate(`window.offline.push(${JSON.stringify(ready)})`);
    assert.equal(await evaluate('document.querySelector(".computer-approval input").checked'),false,'Same revision preserves explicit choice');
    ready.pending_approval.revision = 2;
    await evaluate(`window.offline.push(${JSON.stringify(ready)})`);
    assert.equal(await evaluate('document.querySelector(".computer-approval input").checked'),true,'New revision resets consent options');
    ready.control_epoch = 2;
    await evaluate(`window.offline.push(${JSON.stringify(ready)});document.querySelector('.computer-app-preview').dispatchEvent(new MouseEvent('mouseenter'));window.offline.preview(0,{image:'data:image/png;base64,old',app:'OLD',observed_at:1})`);
    assert.equal(await evaluate('document.querySelector(".computer-app-frame")'),null,'Old preview must not return across control epochs');
    const image = 'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="#cbd5ee"/></svg>');
    await evaluate(`window.offline.preview(window.offline.previewCount()-1,${JSON.stringify({image,app:'离线应用',observed_at:Date.now()-60000})});document.querySelector('.computer-app-preview').dispatchEvent(new MouseEvent('mouseenter'))`);
    await wait('document.querySelector(".computer-app-frame figcaption")');
    assert.match(await evaluate('document.querySelector("figcaption").textContent'),/较早的观察.*不是实时画面/);
    ready.enabled = false; ready.pending_approval = null;
    await evaluate(`window.offline.push(${JSON.stringify(ready)})`);
    assert.equal(await evaluate('document.querySelector(".computer-app-frame")'),null);
    await evaluate('document.querySelector(".computer-use-trigger").click()');

    const frame = () => win.webContents.mainFrame.frames.find(item=>item !== win.webContents.mainFrame);
    await wait('document.querySelector(".intelligent-canvas-parts button")');
    for (let attempt=0; attempt<100 && frame()?.url !== 'about:srcdoc'; attempt++) await new Promise(resolve=>setTimeout(resolve,20));
    assert.ok(frame(),JSON.stringify(win.webContents.mainFrame.frames.map(item=>item.url)));
    assert.equal(frame().url,'about:srcdoc');
    assert.equal(await frame().executeJavaScript('document.querySelector("script").textContent.includes("parent.__unsafeCanvasScript=true")'),false,'Source script is sanitized');
    assert.equal(await evaluate('window.__unsafeCanvasScript'),undefined);
    const width = await frame().executeJavaScript('document.querySelector("svg").getBoundingClientRect().width');
    await evaluate('document.querySelector("[aria-label=放大图解]").click();document.querySelector("[aria-label=放大图解]").click()');
    await wait('document.querySelector(".intelligent-canvas-zoom span").textContent==="150%"');
    const enlarged = await frame().executeJavaScript('new Promise(r=>requestAnimationFrame(()=>r(document.querySelector("svg").getBoundingClientRect().width)))');
    assert.ok(Math.abs(enlarged/width-1.5)<.03,'Zoom changes the rendered SVG, not only its label');
    await evaluate('document.querySelector(".intelligent-canvas-quality summary").click();document.querySelector(".intelligent-canvas-quality-issue button").click()');
    await wait('document.querySelector(".intelligent-canvas-detail h4").textContent==="反证与错误"');
    assert.equal(await frame().executeJavaScript('document.getElementById("counter").getAttribute("aria-pressed")'),'true');
    win.webContents.focus();
    await evaluate('document.querySelector("iframe").focus()');
    await frame().executeJavaScript('document.getElementById("candidate").focus()');
    win.webContents.debugger.attach('1.3');
    await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await wait('document.querySelector(".intelligent-canvas-detail h4").textContent==="关键候选"');
    assert.equal(await frame().executeJavaScript('document.activeElement.getAttribute("aria-label")'),'关键候选');
    await frame().executeJavaScript("parent.postMessage({type:'tcode-canvas-select',nonce:'wrong',element_id:'counter'},'*')");
    await evaluate('new Promise(r=>setTimeout(r,20))');
    assert.equal(await evaluate('document.querySelector(".intelligent-canvas-detail h4").textContent'),'关键候选','Wrong bridge nonce cannot change selection');
    assert.equal(await evaluate('window.offline.requests.every(item=>item.startsWith("GET "))'),true,'Reading and zooming never save edits');
    await evaluate('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="适应窗口").click()');
    await frame().executeJavaScript('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
    await evaluate('new Promise(r=>setTimeout(r,150))');
    fs.writeFileSync(path.join(root,'desktop.png'),(await win.webContents.capturePage()).toPNG());
    win.setSize(375,1000);
    await evaluate('new Promise(r=>setTimeout(r,150))');
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true,'Narrow canvas does not overflow the host page');
    fs.writeFileSync(path.join(root,'narrow.png'),(await win.webContents.capturePage()).toPNG());
    assert.equal(blocked,0,'No external requests attempted');
    console.log('PASS: stale status/preview isolation, revised consent, rendered zoom, quality location, keyboard selection, narrow layout, SVG sanitization; no network or Driver');
  } catch (error) {console.error(error);console.error(await evaluate('document.body.innerText'));process.exitCode=1;}
  finally {win.destroy();app.exit(process.exitCode || 0);}
}
main().catch(error=>{console.error(error);if(process.versions.electron)require('electron').app.exit(1);else process.exitCode=1;});
