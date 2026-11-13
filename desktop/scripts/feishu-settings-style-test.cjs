// Hidden offline UI fixture. Uses no real credentials, connections or chat data.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const assert = require('node:assert/strict');
app.whenReady().then(async () => {
  const win = new BrowserWindow({show:false,width:1100,height:1000});
  win.webContents.on('console-message',d=>{if(d.level==='error')console.error(d.message);});
  try {
    const web=path.resolve(__dirname,'../../web');
    const {build}=await import(pathToFileURL(path.join(web,'node_modules/vite/dist/node/index.js')));
    const {default:solid}=await import(pathToFileURL(path.join(web,'node_modules/vite-plugin-solid/dist/esm/index.mjs')));
    const entry=path.join(web,'src/feishu-style-fixture.tsx');
    const code=`import {render} from 'solid-js/web';import FeishuSettings from './components/FeishuSettings';import './index.css';import './paper-theme.css';import './studio.css';import './interface.css';document.documentElement.className='studio-theme';render(()=><FeishuSettings workspaceId="ws1"/>,document.body);`;
    const result=await build({configFile:false,root:web,logLevel:'error',plugins:[solid(),{name:'fixture',resolveId(id){if(id===entry)return entry;},load(id){if(id===entry)return code;}}],build:{write:false,minify:false,lib:{entry,name:'FeishuFixture',formats:['iife']}}});
    const output=Array.isArray(result)?result.flatMap(item=>item.output):result.output;
    const js=output.filter(item=>item.type==='chunk').map(item=>item.code).join('\n');
    const css=output.filter(item=>item.type==='asset'&&item.fileName.endsWith('.css')).map(item=>item.source).join('\n');
    const mock=`for(const name of ['localStorage','sessionStorage'])Object.defineProperty(window,name,{value:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}});
      window.fixture={configured:false,app_id:'',secret_configured:false,workspace_id:'ws1',chats:[],bound:false,sender_id:'',enabled:false,status:'stopped',last_event_at:0,error:'',permissions:[{scope:'im:message:readonly',purpose:'读取消息'},{scope:'im:message:send_as_bot',purpose:'以应用身份发送消息'}]};window.rejectSave=true;window.actions=[];window.setInterval=fn=>{window.poll=fn;return 1};window.clearInterval=()=>{};
      window.fetch=async(url,options={})=>{const method=options.method||'GET';window.actions.push(method+' '+url);let data=window.fixture,ok=true;
      if(url==='/api/workdirs')data={workdirs:[{name:'研究空间',workspace_id:'ws1'}]};
      else if(url==='/api/feishu'&&method==='PUT'){if(window.rejectSave){window.rejectSave=false;ok=false;data={detail:'测试鉴权失败'};}else{const input=JSON.parse(options.body);window.fixture={...window.fixture,configured:true,secret_configured:true,app_id:input.app_id,workspace_id:input.workspace_id,chats:input.chats};data=window.fixture;}}
      else if(url==='/api/feishu/start'){window.fixture={...window.fixture,enabled:true,status:'listening'};data=window.fixture;}
      else if(url==='/api/feishu/binding-code')data={command:'/bind fixture-only',expires_at:Date.now()/1000+600};
      else if(url!=='/api/feishu')throw new Error('Unexpected fixture endpoint '+url);
      return {ok,json:async()=>data};};`;
    await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<style>${css}</style><body><script>${mock}${js.replace(/<\/script/gi,'<\\/script')}</script></body>`)}`);
    const run=code=>win.webContents.executeJavaScript(code);
    const settle=()=>run(`new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(true))))`);
    await settle();
    await run(`window.button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===text);window.fill=(label,value)=>{const e=[...document.querySelectorAll('label')].find(l=>l.textContent.trim()===label).querySelector('input');e.value=value;e.dispatchEvent(new Event('input',{bubbles:true}));};undefined`);
    assert(await run(`window.button('启动／重新连接').disabled`));
    await run(`window.fill('App ID','cli_fixture');window.fill('App Secret','fixture-only');document.querySelector('select').value='ws1';document.querySelector('select').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('form').requestSubmit()`);await settle();
    assert(await run(`document.querySelector('[role="alert"]').textContent.includes('测试鉴权失败')`));
    assert.equal(await run(`document.querySelector('input[type=password]').value`),'fixture-only');
    await run(`document.querySelector('form').requestSubmit()`);await settle();
    assert.equal(await run(`document.querySelector('input[type=password]').value`),'');
    await run(`document.querySelector('.feishu-permission-details summary').click();document.querySelector('.feishu-event-guide summary').click();window.button('启动／重新连接').click()`);await settle();
    assert(await run(`document.querySelector('.feishu-permission-details').open&&document.querySelector('.feishu-event-guide').open`));
    await run(`document.querySelector('.feishu-permission-details summary').click()`);
    assert(await run(`!document.querySelector('.feishu-permission-details').open`));
    await run(`document.querySelector('.feishu-permission-details summary').click()`);
    assert(await run(`document.querySelector('.feishu-connection-status').textContent.includes('监听已启动，等待消息验证')`));
    await run(`window.button('生成／刷新绑定码').click()`);await settle();
    assert.equal(await run(`document.querySelector('.feishu-binding-code input').value`),'/bind fixture-only');
    for(const width of [1100,600,375]) { win.setSize(width,1000);await settle();assert(await run(`document.documentElement.scrollWidth<=innerWidth`),`${width}px overflow`); }
    fs.writeFileSync('/tmp/tcode-feishu-mobile.png',(await win.webContents.capturePage()).toPNG());
    win.setSize(1100,1000);await settle();
    fs.writeFileSync('/tmp/tcode-feishu-desktop.png',(await win.webContents.capturePage()).toPNG());
    await run(`window.fixture={...window.fixture,bound:true,sender_id:'ou_fixture',status:'receiving'};window.poll();undefined`);await settle();
    assert(await run(`document.querySelector('.feishu-bound-identity').textContent.includes('ou_fixture')`));
    assert.equal(await run(`document.querySelector('.feishu-binding-code')`),null);
    await run(`window.button('删除配置').click()`);await settle();
    assert(await run(`document.querySelector('dialog[open]').textContent.includes('删除飞书接入配置？')`));
    await run(`window.button('取消').click()`);await settle();
    await run(`window.scrollTo(0,document.body.scrollHeight)`);await settle();
    fs.writeFileSync('/tmp/tcode-feishu-details.png',(await win.webContents.capturePage()).toPNG());
    assert(await run(`!window.actions.some(item=>item.startsWith('DELETE'))`));
    console.log('PASS: credentials failure/retry, secret cleared, disclosures, listening/binding status, no destructive calls, 375/600/1100px fit');
  } catch(error) {console.error(error);process.exitCode=1;} finally {win.destroy();app.exit(process.exitCode||0);}
});
