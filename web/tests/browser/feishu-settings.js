// Run with tabbit-cli nodejs. All requests are fixtures; no real credentials or messages.
let state = {configured:false, app_id:'', secret_configured:false, workspace_id:'', chats:[], bound:false,
  sender_id:'', enabled:false, status:'stopped', last_event_at:0, error:'', permissions:[
    {scope:'im:message:readonly',purpose:'读取历史消息'}]};
let rejectSave = true;
const actions = [];
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname;
  if (!path.startsWith('/api/')) {await route.continue();return;}
  const method = route.request().method();
  if (path === '/api/feishu') {
    if (method === 'PUT') {
      actions.push({method, body: route.request().postDataJSON()});
      if (rejectSave) {rejectSave=false; await route.fulfill({status:400,json:{detail:'测试：鉴权未通过，原配置未修改'}});return;}
      const data=route.request().postDataJSON();
      state={...state,configured:true,app_id:data.app_id,secret_configured:true,workspace_id:data.workspace_id,chats:data.chats};
    }
    if (method === 'DELETE') {actions.push({method});state={...state,configured:false,app_id:'',secret_configured:false,bound:false,enabled:false,status:'stopped'};}
    await route.fulfill({json:state});return;
  }
  if(path==='/api/feishu/start'){state={...state,enabled:true,status:'listening'};await route.fulfill({json:state});return;}
  if(path==='/api/feishu/binding-code'){await route.fulfill({json:{command:'/bind fixture-code',expires_at:Date.now()/1000+600}});return;}
  if(path==='/api/workdirs'){await route.fulfill({json:{workdirs:[{name:'研究空间一',workspace_id:'ws1'},{name:'研究空间二',workspace_id:'ws2'}]}});return;}
  if(path==='/api/mcp/config'){await route.fulfill({json:{servers:[],can_edit:false}});return;}
  await route.fulfill({json:{runtimes:[],servers:[],models:[],agents:[],style:'default',default_style:'clay'}});
});
await page.goto('http://127.0.0.1:5178/tests/acceptance/feishu-settings.html',{waitUntil:'domcontentloaded'});
await expect(page.getByRole('heading',{name:'飞书接入',exact:true})).toBeVisible();
const start=page.getByRole('button',{name:'启动／重新连接'});
await expect(start).toBeDisabled();
await page.getByLabel('App ID',{exact:true}).fill('cli_fixture');
await page.getByLabel('App Secret',{exact:true}).fill('fixture-secret');
await page.getByRole('combobox',{name:'任务工作区',exact:true}).selectOption('ws1');
await page.getByRole('button',{name:'保存并验证鉴权'}).click();
await expect(page.getByRole('alert')).toContainText('鉴权未通过');
await expect(page.getByLabel('App Secret',{exact:true})).toHaveValue('fixture-secret');
await page.getByRole('button',{name:'保存并验证鉴权'}).click();
await expect(page.getByLabel('App Secret',{exact:true})).toHaveValue('');
await expect(start).toBeEnabled();
await page.getByRole('combobox',{name:'任务工作区',exact:true}).selectOption('ws2');
await expect(start).toBeDisabled();
await page.getByRole('button',{name:'放弃未保存修改'}).click();
await expect(page.getByRole('combobox',{name:'任务工作区',exact:true})).toHaveValue('ws1');
await start.click();
await expect(page.getByText('监听已启动，等待消息验证',{exact:true})).toBeVisible();
await page.getByRole('button',{name:'生成／刷新绑定码'}).click();
await expect(page.getByLabel('复制以下完整命令，私聊发送给机器人')).toHaveValue('/bind fixture-code');
state={...state,bound:true,sender_id:'ou_fixture',status:'receiving',last_event_at:Date.now()/1000};
await expect(page.locator('.feishu-bound-identity')).toContainText('ou_fixture',{timeout:10000});
await expect(page.getByLabel('复制以下完整命令，私聊发送给机器人')).toHaveCount(0);
await page.getByRole('button',{name:'删除配置',exact:true}).click();
await expect(page.getByRole('heading',{name:'删除飞书接入配置？'})).toBeVisible();
await page.getByRole('button',{name:'取消',exact:true}).click();
assert(!actions.some(a=>a.method==='DELETE'));
await page.getByRole('button',{name:'删除配置',exact:true}).click();
await page.getByRole('dialog').getByRole('button',{name:'删除配置',exact:true}).click();
await expect(page.getByLabel('App ID',{exact:true})).toHaveValue('');
assert(actions.filter(a=>a.method==='DELETE').length===1);
await page.emulateMedia({reducedMotion:'reduce'});
for(const size of [{width:375,height:812},{width:812,height:375},{width:1280,height:900}]){
  await page.setViewportSize(size);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
}
return {saveFailureRetry:true,secretNotEchoed:true,workspaceEdit:true,bind:true,deleteCancelAndConfirm:true,responsive:true};
