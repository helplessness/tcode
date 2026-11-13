// Hidden, offline component regression; never reads or writes real chat data.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const assert = require('node:assert/strict');
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 1000, height: 800 });
  win.webContents.on('console-message', details => { if (details.level === 'error') console.error(details.message); });
  try {
    const web = path.resolve(__dirname, '../../web');
    const { build } = await import(pathToFileURL(path.join(web, 'node_modules/vite/dist/node/index.js')));
    const { default: solid } = await import(pathToFileURL(path.join(web, 'node_modules/vite-plugin-solid/dist/esm/index.mjs')));
    const entry = path.join(web, 'src/user-message-text-fixture.tsx');
    const code = `import {render} from 'solid-js/web';import {createSignal} from 'solid-js';import UserMessageText from './components/UserMessageText';import './interface.css';
      const [text,setText]=createSignal('简短消息');window.setText=setText;
      render(()=><><main style="width:600px;font-size:16px;line-height:24px"><UserMessageText enabled contentKey={text()}><p style="white-space:pre-wrap;margin:0;overflow-wrap:anywhere">{text()}</p></UserMessageText></main><aside><UserMessageText enabled={false} contentKey="assistant"><p>助手回复不改变</p></UserMessageText></aside></>,document.body);`;
    const result = await build({configFile:false,root:web,logLevel:'error',plugins:[solid(),{name:'fixture',resolveId(id){if(id===entry)return entry;},load(id){if(id===entry)return code;}}],build:{write:false,minify:false,lib:{entry,name:'UserMessageFixture',formats:['iife']}}});
    const output = Array.isArray(result) ? result.flatMap(item=>item.output) : result.output;
    const js = output.filter(item=>item.type==='chunk').map(item=>item.code).join('\n');
    const css = output.filter(item=>item.type==='asset').map(item=>item.source).join('\n');
    await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<style>${css}</style><body><script>${js.replace(/<\/script/gi,'<\\/script')}</script></body>`)}`);
    const run = async code => { try { return await win.webContents.executeJavaScript(code); } catch(error) { throw new Error(code,{cause:error}); } };
    const settle = () => run(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))`);
    await settle();
    assert.equal(await run(`document.querySelector('.user-message-expand')`),null);
    assert.equal(await run(`document.querySelector('aside .user-message-bounded')`),null);
    await run(`window.setText(Array(100).fill('长消息内容').join(String.fromCharCode(10))+String.fromCharCode(10)+'最后一行');undefined`);await settle();
    assert(await run(`document.querySelector('.user-message-expand').getAttribute('aria-expanded')==='false'`));
    assert(await run(`document.querySelector('.user-message-text').clientHeight<=192`));
    assert(await run(`document.querySelector('.user-message-text').textContent.endsWith('最后一行')`));
    await run(`document.querySelector('.user-message-expand').click()`);await settle();
    assert(await run(`(()=>{const e=document.querySelector('.user-message-text');return e.clientHeight>192&&e.clientHeight<=480&&e.scrollHeight>e.clientHeight&&e.tabIndex===0})()`));
    await run(`document.querySelector('.user-message-text').scrollTop=9999;document.querySelector('.user-message-expand').click()`);await settle();
    assert.equal(await run(`document.querySelector('.user-message-text').scrollTop`),0);
    await run(`window.setText('短消息');undefined`);await settle();
    assert.equal(await run(`document.querySelector('.user-message-expand')`),null);
    await run(`window.setText('连续长文本'.repeat(90));document.querySelector('main').style.width='600px';undefined`);await settle();
    await run(`document.querySelector('main').style.width='180px'`);await settle();
    assert(await run(`!!document.querySelector('.user-message-expand')`));
    console.log('PASS: short text, eight-line clipping, full text retained, bounded scrolling, collapse reset, resize and assistant unaffected');
  } catch (error) { console.error(error); process.exitCode=1; }
  finally { win.destroy();app.exit(process.exitCode||0); }
});
