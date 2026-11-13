/* Real desktop component, isolated fixture APIs: no models or computations run. */
const {app,BrowserWindow}=require('electron');
const path=require('node:path');
const assert=require('node:assert/strict');
app.whenReady().then(async()=>{
  let server,window,goal=null,failStop=true,failRead=true;const writes=[];
  try{
    const root=path.resolve(__dirname,'../../web');
    const {createServer}=await import(path.join(root,'node_modules/vite/dist/node/index.js'));
    server=await createServer({root,configFile:path.join(root,'vite.config.ts'),server:{host:'127.0.0.1',port:0},plugins:[{
      name:'goal-mode-fixture',enforce:'pre',
      resolveId(id){if(id==='/__goal_fixture.tsx')return path.join(root,'__goal_fixture.tsx');},
      load(id){if(id===path.join(root,'__goal_fixture.tsx'))return `import {render} from 'solid-js/web';import {createSignal} from 'solid-js';import GoalModePanel from '/src/components/GoalModePanel';import '/src/index.css';render(()=>{const [context,setContext]=createSignal({sid:'fixture',workspace:'workspace'});const [mount,setMount]=createSignal();const [text,setText]=createSignal('');let submit;return <><form id="composer" onSubmit={e=>{e.preventDefault();void submit?.(text());}}><button type="button" onClick={()=>setContext({...context()})}>刷新会话</button><div ref={setMount}/><textarea aria-label="聊天输入" value={text()} onInput={e=>setText(e.currentTarget.value)}/><button>发送</button></form><GoalModePanel controlMount={mount()} sessionId={context().sid} workspaceId={context().workspace} native={true} onSubmitReady={handler=>submit=handler}/></>},document.getElementById('root'));`;},
      configureServer(dev){dev.middlewares.use((req,res,next)=>{
        const url=new URL(req.url,'http://local');
        const send=(data,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
        if(url.pathname==='/__goal_fixture'){res.setHeader('Content-Type','text/html');res.end('<div id="root"></div><script type="module" src="/__goal_fixture.tsx"></script>');return;}
        if(url.pathname==='/api/fixture/exhaust'){goal.status='budget_exhausted';goal.rounds_used=goal.max_rounds;send({});return;}
        if(url.pathname==='/api/fixture/replace'){goal={...goal,id:'goal-replacement-fixture',status:'paused'};send({});return;}
        if(!url.pathname.startsWith('/api/sessions/fixture/goal'))return next();
        if(req.method==='GET'){if(failRead){failRead=false;console.log('Fixture: first goal read failed intentionally');send({detail:'测试状态暂时不可用'},503);return;}send({goal});return;}
        let text='';req.on('data',v=>text+=v);req.on('end',()=>{
          const body=JSON.parse(text);writes.push(body);
          if(!url.pathname.endsWith('/action'))goal={...body,id:'goal-internal-fixture',status:'ready',rounds_used:0,reason:'',progress:'',next_step:'',deadline:Date.now()/1000+1800};
          else if(body.action==='pause')goal.status='paused';
          else if(body.action==='guide'){goal.next_step=body.note;goal.status='ready';}
          else if(body.action==='stop'){if(failStop){failStop=false;send({detail:'停止失败，请重试'},503);return;}goal.status='stopped';}
          else if(goal.status==='budget_exhausted'&&!body.extra_rounds){send({detail:'预算已耗尽，请明确增加预算'},409);return;}
          else{goal.status='ready';goal.max_rounds+=body.extra_rounds||0;}
          send({goal});
        });
      });}
    }]});await server.listen();
    window=new BrowserWindow({show:process.env.TCODE_GOAL_FIXTURE_MANUAL==='1',title:'Goal mode regression fixture',webPreferences:{backgroundThrottling:false}});
    await window.loadURL(`http://127.0.0.1:${server.httpServer.address().port}/__goal_fixture`);
    if(process.env.TCODE_GOAL_FIXTURE_MANUAL==='1'){
      console.log('Manual fixture ready: first read fails once; use native UI to verify retry and draft cancellation.');
      await new Promise(resolve=>window.once('closed',resolve));return;
    }
    await window.webContents.executeJavaScript(`(async()=>{
      const wait=ms=>new Promise(r=>setTimeout(r,ms));
      const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text);
      const toggle=()=>document.querySelector('[role=switch]');
      const detail=()=>document.querySelector('[aria-label="查看目标状态"]');
      for(let i=0;i<100&&(!toggle()||toggle().disabled);i++)await wait(50);
      if(toggle().checked||document.querySelector('dialog'))throw Error('ordinary mode should not occupy chat space');
      const help=document.querySelector('[aria-label="目标模式说明"]');
      help.dispatchEvent(new MouseEvent('mouseenter'));await wait(20);
      if(!document.querySelector('[role=tooltip]')?.textContent.includes('限定预算'))throw Error('hover help missing');
      help.dispatchEvent(new MouseEvent('mouseleave'));await wait(20);
      if(document.querySelector('[role=tooltip]'))throw Error('hover help did not dismiss');
      help.focus();help.dispatchEvent(new FocusEvent('focus'));await wait(20);
      if(!document.querySelector('[role=tooltip]'))throw Error('keyboard help missing');
      window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));await wait(20);
      if(document.querySelector('[role=tooltip]'))throw Error('Escape did not dismiss help');
      help.click();await wait(20);
      if(!document.querySelector('[role=tooltip]')||document.querySelector('dialog')||toggle().checked)throw Error('help click changed goal mode');
      window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));help.blur();
      toggle().click();await wait(50);
      if(document.querySelector('dialog')||!toggle().checked)throw Error('mode selection must not open a dialog');
      document.querySelector('[aria-label="目标模式设置"]').click();await wait(50);
      const draftRounds=document.querySelector('dialog input[type=number]');draftRounds.value='1';draftRounds.dispatchEvent(new Event('input',{bubbles:true}));
      button('取消').click();await wait(50);
      document.querySelector('[aria-label="目标模式设置"]').click();await wait(50);
      if(document.querySelector('dialog input[type=number]').value!=='8')throw Error('cancel retained unsaved budget');
      const firstDialog=document.querySelector('dialog');
      if(!firstDialog?.open||firstDialog.closest('#composer'))throw Error('configuration must open outside composer form');
      // Equivalent to a fresh session-list object with unchanged scope IDs.
      button('刷新会话').click();await wait(50);
      if(document.querySelector('dialog')!==firstDialog)throw Error('session refresh closed configuration');
      const criteria=document.querySelector('dialog textarea');criteria.value='报告包含三个术语及解释';criteria.dispatchEvent(new Event('input',{bubbles:true}));
      document.querySelector('dialog form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await wait(100);
      const chat=document.querySelector('[aria-label="聊天输入"]');chat.value='整理蛋白术语报告';chat.dispatchEvent(new Event('input',{bubbles:true}));button('发送').click();await wait(100);
      if(document.querySelector('dialog')||!toggle().checked)throw Error('start must collapse details while keeping goal enabled');
      chat.value='面向初学者';chat.dispatchEvent(new Event('input',{bubbles:true}));button('发送').click();await wait(100);
      detail().click();await wait(50);
      if(!button('暂停目标'))throw Error('goal was not started');
      if(document.body.textContent.includes('goal-internal-fixture'))throw Error('internal ID exposed');
      button('暂停目标').click();await wait(100);if(!document.body.textContent.includes('已暂停'))throw Error('pause failed');
      const note=document.querySelector('dialog input[type=text], dialog input:not([type])');note.value='旧目标说明';note.dispatchEvent(new Event('input',{bubbles:true}));
      button('刷新会话').click();await wait(50);
      if(note.value!=='旧目标说明')throw Error('same goal refresh discarded draft');
      await fetch('/api/fixture/replace');await wait(2200);
      if(document.querySelector('dialog input[type=text], dialog input:not([type])').value!=='')throw Error('new goal inherited old guidance draft');
      button('继续目标').click();await wait(100);
      if(document.querySelector('dialog'))throw Error('resume should return space to chat');
      await fetch('/api/fixture/exhaust');await wait(2200);
      if(!document.body.textContent.includes('预算已耗尽'))throw Error('budget state not shown');
      if(document.querySelector('dialog'))throw Error('polling must not open an overlay');
      detail().click();await wait(50);
      button('继续目标').click();await wait(100);if(!document.querySelector('[role=alert]'))throw Error('budget denial hidden');
      button('加 4 轮及 15 分钟并继续').click();await wait(100);
      toggle().click();await wait(100);
      if(!toggle().checked||!document.querySelector('[role=alert]')?.textContent.includes('停止失败'))throw Error('failed stop must retain enabled switch and show error');
      button('返回').click();await wait(50);
      toggle().click();await wait(100);
      if(toggle().checked||document.querySelector('dialog'))throw Error('ordinary mode not restored');
      toggle().click();await wait(50);
      document.querySelector('[aria-label="目标模式设置"]').click();await wait(50);
      document.querySelector('dialog').dispatchEvent(new Event('cancel',{cancelable:true}));await wait(50);
      if(!toggle().checked||document.querySelector('dialog'))throw Error('cancel settings must not alter selected mode');
      toggle().click();await wait(50);
    })()`);
    assert.equal(writes[0].workspace_id,'workspace');assert.equal(writes[0].criteria,'报告包含三个术语及解释');
    assert.equal(writes[0].from_chat,true);assert.equal(writes[1].note,'面向初学者');
    assert.deepEqual(writes.slice(1).map(v=>v.action),['guide','pause','resume','resume','resume','stop','stop']);
    assert.equal(writes[5].extra_rounds,4);assert.equal(writes[5].extra_minutes,15);
    console.log('PASS: desktop goal opt-in, pause/resume, budget confirmation, stop and hidden IDs');
  }catch(error){console.error(error);process.exitCode=1;}finally{if(window)window.destroy();if(server)await server.close();app.exit(process.exitCode || 0);}
});
