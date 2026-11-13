// Run with: desktop/node_modules/.bin/electron desktop/scripts/smoke-question-ui.cjs
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '../../web');
const name = `__question_ui_smoke_${process.pid}`;
const files = [path.join(root, `${name}.html`), path.join(root, `${name}.tsx`)];
let server;
let window;

app.whenReady().then(async () => {
  try {
    fs.writeFileSync(files[0], `<div id="root"></div><script type="module" src="/${name}.tsx"></script>`);
    fs.writeFileSync(files[1], `
      import { render } from 'solid-js/web';
      import QuestionCard from './src/components/QuestionCard';
      import './src/interface.css';
      window.answer = null;
      render(() => <QuestionCard request={{request_id:'smoke',session_id:'smoke',question:{
        question:'交互验证',type:'batch',questions:[
          {id:'platform',question:'目标平台',type:'single',options:['macOS','Linux'],allow_custom:true},
          {id:'features',question:'所需功能',type:'multiple',options:['预览','远程 MCP'],allow_custom:true},
          {id:'note',question:'备注',required:false},
        ]}}} onAnswer={async answer => {window.answer = JSON.parse(answer);}}/>, document.getElementById('root'));
    `);
    const { createServer } = await import(pathToFileURL(path.join(root, 'node_modules/vite/dist/node/index.js')).href);
    server = await createServer({root, server:{host:'127.0.0.1',port:0}});
    await server.listen();
    window = new BrowserWindow({show:false, width:1000, height:800, webPreferences:{contextIsolation:true, nodeIntegration:false}});
    await window.loadURL(`${server.resolvedUrls.local[0]}${name}.html`);
    for (let attempt = 0; attempt < 100; attempt++) {
      if (await window.webContents.executeJavaScript("Boolean(document.querySelector('[data-question-batch]'))")) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const answer = await window.webContents.executeJavaScript(`(async () => {
      const check = (condition, message) => { if (!condition) throw new Error(message); };
      const current = () => [...document.querySelectorAll('fieldset')].find(item => !item.hidden);
      const button = (text, scope = document) => [...scope.querySelectorAll('button')].find(item => item.textContent.trim().includes(text));
      const fill = text => { const input = current().querySelector('input'); input.focus(); input.value = text; input.dispatchEvent(new Event('input',{bubbles:true})); };
      const selected = text => button(text,current()).getAttribute('aria-pressed') === 'true';
      fill('保留的自定义草稿');
      check(selected('自定义输入'),'Typing must select custom mode');
      button('macOS',current()).click();
      check(selected('macOS') && !selected('自定义输入'),'An option must override the custom draft');
      check(current().querySelector('[role=status]').textContent.includes('macOS'),'Selected answer must be visible');
      button('下一题').click();
      button('预览',current()).click(); button('远程 MCP',current()).click();
      button('上一题').click();
      check(selected('macOS'),'Previous-page selection must survive');
      check(current().querySelector('input').value === '保留的自定义草稿','Custom draft must survive switching and paging');
      button('自定义输入',current()).click();
      check(selected('自定义输入') && !selected('macOS'),'Custom mode must deselect options visually');
      button('Linux',current()).click();
      button('下一题').click();
      check(selected('预览') && selected('远程 MCP'),'Multiple-choice draft must survive');
      button('远程 MCP',current()).click();
      button('下一题').click();
      check(window.answer === null,'Paging must never submit');
      button('提交全部回答').click();
      await new Promise(resolve => setTimeout(resolve, 0));
      return window.answer;
    })()`);
    assert.deepEqual(answer, {platform:'Linux',features:'1',note:''});
    console.log(JSON.stringify({smoke:'passed',answer}));
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    window?.destroy();
    await server?.close();
    for (const file of files) if (fs.existsSync(file)) fs.unlinkSync(file);
    app.exit(process.exitCode || 0);
  }
});
