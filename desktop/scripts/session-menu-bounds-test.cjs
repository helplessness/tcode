// Offline regression for the real positioning helper; no visible test window.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show:false, width:800, height:700 });
  try {
    const web = path.resolve(__dirname,'../../web');
    const ts = require(path.join(web,'node_modules/typescript'));
    const js = ts.transpileModule(fs.readFileSync(path.join(web,'src/utils/workspaceMenu.ts'),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS}}).outputText;
    const css = ['studio.css','interface.css'].map(file=>fs.readFileSync(path.join(web,'src',file),'utf8')).join('\n');
    await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<html class="studio-theme" data-desktop="true"><style>${css}#host{position:fixed;left:12px;top:70px;bottom:80px;width:300px;overflow:hidden;transform:translateZ(0)}</style><body><div id="host"></div><div class="session-context-menu" role="menu">${['上移','下移','重命名','关联到课题','查看研究课题与候选','归档','删除'].map(text=>`<button>${text}</button>`).join('')}</div><script>const exports={};${js}window.anchor={x:270,y:innerHeight-90};window.dispose=exports.watchSessionMenu(document.querySelector('.session-context-menu'),document.querySelector('#host'),window.anchor);</script></body></html>`)}`);
    const run = code => win.webContents.executeJavaScript(code);
    const settle = () => run(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))`);
    const fits = () => run(`(()=>{const p=document.querySelector('.session-context-menu').getBoundingClientRect(),h=document.querySelector('#host').getBoundingClientRect();return p.top>=h.top+7&&p.bottom<=h.bottom-7&&p.left>=h.left+7&&p.right<=h.right-7})()`);
    await settle();assert(await fits(),'bottom menu must fit sidebar');
    assert(await run(`document.querySelector('.session-context-menu').getBoundingClientRect().bottom<window.anchor.y`),'bottom menu should open upwards');
    await run(`document.querySelector('.session-context-menu').insertAdjacentHTML('afterbegin','<button>读取课题后的新增选项</button><button>另一个异步新增选项</button>')`);await settle();
    assert(await fits(),'async height changes must stay in bounds');
    win.setSize(800,300);await settle();
    assert(await fits(),'short window must constrain menu');
    assert(await run(`(()=>{const p=document.querySelector('.session-context-menu');return p.scrollHeight>p.clientHeight})()`),'short menu needs internal scrolling');
    await run(`document.querySelector('.session-context-menu button:last-child').focus()`);await settle();
    assert(await run(`(()=>{const p=document.querySelector('.session-context-menu').getBoundingClientRect(),b=document.querySelector('.session-context-menu button:last-child').getBoundingClientRect();return b.top>=p.top&&b.bottom<=p.bottom})()`),'delete must remain keyboard accessible');
    await run(`window.dispose();undefined`);
    console.log('PASS: upward opening, sidebar boundaries, async options, short-window scrolling and focus on delete');
  } catch(error) { console.error(error);process.exitCode=1; }
  finally { win.destroy();app.exit(process.exitCode||0); }
});
