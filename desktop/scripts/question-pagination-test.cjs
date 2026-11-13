// Real Solid components in a hidden, offline window; no backend or real profile.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const assert = require('node:assert/strict');
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 900, height: 700 });
  win.webContents.on('console-message', details => { if (details.level === 'error') console.error(details.message); });
  try {
    const web = path.resolve(__dirname, '../../web');
    const { build } = await import(pathToFileURL(path.join(web, 'node_modules/vite/dist/node/index.js')));
    const { default: solid } = await import(pathToFileURL(path.join(web, 'node_modules/vite-plugin-solid/dist/esm/index.mjs')));
    const entry = path.join(web, 'src/question-pagination-fixture.tsx');
    const code = `import {render} from 'solid-js/web';import {createSignal} from 'solid-js';import QuestionPager from './components/QuestionPager';import './studio.css';import './interface.css';
      const [requests,setRequests]=createSignal([{request_id:'single',session_id:'owner',question:{question:'问题一',type:'custom'}},{request_id:'batch',session_id:'owner',question:{question:'问题组',type:'batch',questions:[{id:'input',header:'序列',question:'输入序列',type:'custom',required:true},{id:'choices',header:'方法',question:'选择方法',type:'multiple',options:['方法甲','方法乙'],allow_custom:false,required:true},{id:'optional',header:'备注',question:'备注',type:'custom',required:false}]}}]);
      window.calls=[];window.fail=true;window.refreshRequests=()=>setRequests(items=>items.map(item=>({...item})));
      render(()=><QuestionPager requests={requests()} onAnswer={async(request,answer)=>{window.calls.push({id:request.request_id,session:request.session_id,answer});if(window.fail)throw new Error('离线提交失败');setRequests(items=>items.filter(item=>item.request_id!==request.request_id));}}/>,document.body);`;
    const result = await build({configFile:false,root:web,logLevel:'error',plugins:[solid(),{name:'fixture',resolveId(id){if(id===entry)return entry;if(id.endsWith('/api/client'))return '\0client';},load(id){if(id===entry)return code;if(id==='\0client')return 'export const respondQuestion=async()=>{throw new Error("Unexpected API call")};';}}],build:{write:false,minify:false,lib:{entry,name:'QuestionsFixture',formats:['iife']}}});
    const output = Array.isArray(result) ? result.flatMap(item => item.output) : result.output;
    const js = output.filter(item=>item.type==='chunk').map(item=>item.code).join('\n');
    const css = output.filter(item=>item.type==='asset').map(item=>item.source).join('\n');
    await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<style>${css}</style><body><script>for(const name of ['localStorage','sessionStorage'])Object.defineProperty(window,name,{value:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}});window.fetch=()=>{throw new Error('Network forbidden')};${js.replace(/<\/script/gi,'<\\/script')}</script></body>`)}`);
    const run = code => win.webContents.executeJavaScript(code);
    await run(`window.card=()=>[...document.querySelectorAll('[data-question-card]')].find(e=>e.checkVisibility());window.clickText=(text,root=window.card())=>[...root.querySelectorAll('button')].find(b=>b.textContent.trim()===text&&b.checkVisibility()).click();window.fill=(value)=>{const input=[...window.card().querySelectorAll('input')].find(e=>e.checkVisibility());input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}));};window.outer=(next)=>document.querySelector('nav.question-pagination').querySelectorAll('button')[next?1:0].click();undefined;`);
    assert.equal(await run(`document.querySelectorAll('[data-question-card]').length`), 2);
    await run(`window.fill('未提交的草稿');window.outer(true)`);
    assert(await run(`window.card().hasAttribute('data-question-batch')`));
    assert.equal(await run(`[...window.card().querySelectorAll('fieldset')].filter(e=>e.checkVisibility()).length`),1);
    assert(await run(`[...window.card().querySelectorAll('button')].find(b=>b.textContent.trim()==='提交全部回答').disabled`),'required questions must block premature submission');
    await run(`window.fill('SEQUENCE');window.clickText('下一题');window.clickText('方法甲');window.clickText('方法乙');window.clickText('下一题');window.refreshRequests()`);
    assert.equal(await run(`window.calls.length`),0,'paging must never submit');
    await run(`window.outer(false)`);
    assert.equal(await run(`window.card().querySelector('input').value`),'未提交的草稿');
    await run(`window.outer(true);window.clickText('提交全部回答')`);
    assert.equal(await run(`window.card().querySelector('[role="alert"]').textContent`),'离线提交失败');
    const call = await run(`window.calls[0]`);
    assert.equal(call.session,'owner');
    assert.deepEqual(JSON.parse(call.answer),{input:'SEQUENCE',choices:'1, 2',optional:''});
    await run(`window.clickText('上一题')`);
    assert.equal(await run(`[...window.card().querySelectorAll('button[aria-pressed="true"]')].length`),2);
    await run(`window.fail=false;window.clickText('提交全部回答')`);
    assert.equal(await run(`document.querySelectorAll('[data-question-card]').length`),1);
    assert.equal(await run(`window.card().querySelector('input').value`),'未提交的草稿');
    console.log('PASS: one visible request/item, draft retention, no auto-submit, batch JSON, optional field, failure retry and next pending request');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { win.destroy(); app.exit(process.exitCode || 0); }
});
