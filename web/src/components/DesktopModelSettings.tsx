import { For, Show, createSignal, onMount } from 'solid-js';
import { desktop, type DesktopModelService } from '../desktop';
import { confirmAction } from './FeedbackDialog';

const presets = [
  {name:'OpenAI 兼容',protocol:'openai_compatible',url:'https://api.openai.com/v1'},
  {name:'Ollama 本地',protocol:'ollama',url:'http://127.0.0.1:11434'},
  {name:'Anthropic',protocol:'anthropic',url:'https://api.anthropic.com'},
  {name:'自定义第三方',protocol:'openai_compatible',url:''},
] as const;

export default function DesktopModelSettings(props:{onChanged:()=>void}) {
  const [services,setServices]=createSignal<DesktopModelService[]>([]);
  const [defaultChat,setDefaultChat]=createSignal('');
  const [defaultEmbedding,setDefaultEmbedding]=createSignal('');
  const [draft,setDraft]=createSignal<DesktopModelService>();
  const [modelText,setModelText]=createSignal('');
  const [key,setKey]=createSignal('');
  const [clearKey,setClearKey]=createSignal(false);
  const [showKey,setShowKey]=createSignal(false);
  const [busy,setBusy]=createSignal(false);
  const [message,setMessage]=createSignal('');
  const [success,setSuccess]=createSignal(false);
  const [ready,setReady]=createSignal(false);
  const [secure,setSecure]=createSignal(false);
  const [localOnly,setLocalOnly]=createSignal(false);
  const call=async(payload:Record<string,unknown>)=>{
    if(!desktop?.modelSettings)throw new Error('请重启桌面端以加载模型配置功能');
    const result=await desktop.modelSettings(payload);
    if(result.ok===false)throw new Error(result.message || '操作失败');
    return result;
  };
  const apply=(result:Awaited<ReturnType<typeof call>>)=>{
    if(result.services)setServices(result.services);
    if(result.default_chat!==undefined)setDefaultChat(result.default_chat);
    if(result.default_embedding!==undefined)setDefaultEmbedding(result.default_embedding);
    if(result.secure_storage!==undefined)setSecure(result.secure_storage);
    if(result.embedding_local_only!==undefined)setLocalOnly(result.embedding_local_only);
  };
  const load=()=>{setMessage('');void call({action:'list'}).then(result=>{apply(result);setReady(true);}).catch(error=>setMessage(error.message));};
  onMount(load);
  const edit=(service?:DesktopModelService,kind:'chat'|'embedding'='chat')=>{
    setDraft(service?{...service,models:[...service.models]}:{id:crypto.randomUUID(),name:'',kind,protocol:'openai_compatible',base_url:'',models:[]});
    setModelText(service?.models.join('\n') || '');setKey('');setClearKey(false);setShowKey(false);setMessage('');
  };
  const update=(field:Partial<DesktopModelService>)=>setDraft(value=>value?{...value,...field}:value);
  const credential=()=>key()?{api_key:key()}:clearKey()||!draft()?.key_saved?{api_key:''}:{};
  const run=async(action:'save'|'test')=>{
    const value=draft();if(!value)return;
    setBusy(true);setSuccess(false);setMessage(action==='test'?'正在测试连接…':'正在保存…');
    try{
      const result=await call({...value,models:modelText().split(/[\n,，]/).map(m=>m.trim()).filter(Boolean),...credential(),action});
      setSuccess(true);setMessage(result.message || '服务已保存');
      if(action==='save'){apply(result);setDraft(undefined);setKey('');props.onChanged();}
    }catch(error){setMessage(error instanceof Error?error.message:'操作失败，请重试');}
    finally{setBusy(false);}
  };
  const makeDefault=async(service:DesktopModelService,model:string)=>{
    if(service.kind==='embedding'&&!await confirmAction('更换嵌入模型后，旧向量索引可能不兼容，需要手动重建相关知识索引。本次不会自动重建或删除数据。',{title:'设置默认嵌入模型',confirmLabel:'确认设置'}))return;
    setBusy(true);setSuccess(false);
    try{const result=await call({...service,model_id:model,action:'default'});apply(result);setSuccess(true);setMessage(result.message || '默认模型已设置');props.onChanged();}
    catch(error){setMessage(error instanceof Error?error.message:'设置失败');}
    finally{setBusy(false);}
  };
  return <div class="desktop-model-settings">
    <p class="model-settings-security">密钥通过系统加密保存，不写入配置文件或浏览器存储。{ready()&&!secure()?'当前系统密钥存储不可用，请先解锁。':''}</p>
    <Show when={localOnly()}><p class="model-settings-feedback">启动环境启用了仅本地向量策略。嵌入服务可保存和测试，但检索不会向外部模型发送内容；需调整启动环境策略后启用。</p></Show>
    <Show when={message()&&!draft()}><p role="status" class="model-settings-feedback" data-success={success()}>{message()}</p></Show>
    <Show when={!ready()&&message()}><button type="button" onClick={load}>重新加载模型配置</button></Show>
    <Show when={!ready() && !message()}><p>正在加载模型服务…</p></Show>
    <For each={['chat','embedding'] as const}>{kind=><section class="settings-section">
      <div class="model-service-heading"><h3>{kind==='chat'?'聊天模型服务':'嵌入模型服务'}</h3><button type="button" disabled={busy()||!ready()} onClick={()=>edit(undefined,kind)}>添加{kind==='chat'?'聊天':'嵌入'}服务</button></div>
      <p class="model-service-description">{kind==='chat'?'用于 Agent 对话和工具调用；默认值用于后续新任务。工作区单独配置仍优先。':'用于知识检索。测试只发送固定测试文本，不发送工作区内容。更换后需重建相关索引。'}</p>
      <Show when={!services().some(s=>s.kind===kind)}><p class="model-service-empty">尚未添加服务。可选预设或填写第三方兼容接口。</p></Show>
      <For each={services().filter(s=>s.kind===kind)}>{service=>{
        const [model,setModel]=createSignal(service.models[0]);
        const current=()=>kind==='chat'?defaultChat():defaultEmbedding();
        const isDefault=()=>current()===(kind==='chat'?'desktop-':'')+service.id+'/'+model();
        return <article class="model-service-card"><div class="model-service-heading"><strong>{service.name}</strong><span>{service.key_saved?'密钥已保存':'无密钥'}</span></div><p title={service.base_url}>{service.base_url}</p><div class="model-service-actions"><select aria-label={`${service.name} 的模型`} value={model()} disabled={busy()} onChange={e=>setModel(e.currentTarget.value)}><For each={service.models}>{id=><option>{id}</option>}</For></select><button type="button" disabled={busy()} onClick={()=>edit(service)}>编辑服务</button><button type="button" disabled={busy()||isDefault()} onClick={()=>void makeDefault(service,model())}>{isDefault()?'当前默认':'设为默认'}</button></div></article>;
      }}</For>
    </section>}</For>
    <Show when={draft()} keyed>{value=><form class="model-service-form" onSubmit={e=>{e.preventDefault();void run('save');}}>
      <h3>{value.kind==='chat'?'聊天':'嵌入'}服务配置</h3>
      <fieldset disabled={busy()}>
      <label>服务预设<select aria-label="服务预设" value="custom" onChange={e=>{const p=presets[Number(e.currentTarget.value)];if(p)update({name:p.name,protocol:p.protocol,base_url:p.url});}}><option value="custom">选择预设或自行填写</option><For each={presets.filter(p=>value.kind==='chat'||p.protocol!=='anthropic')}>{p=><option value={presets.indexOf(p)}>{p.name}</option>}</For></select></label>
      <label>服务名称<input aria-label="服务名称" required maxLength={100} value={draft()?.name || ''} onInput={e=>update({name:e.currentTarget.value})}/></label>
      <label>接口协议<select aria-label="接口协议" value={draft()?.protocol} onChange={e=>update({protocol:e.currentTarget.value as DesktopModelService['protocol']})}><option value="openai_compatible">OpenAI 兼容</option><option value="ollama">Ollama</option><Show when={value.kind==='chat'}><option value="anthropic">Anthropic</option></Show></select></label>
      <label>接口地址<input aria-label="接口地址" required type="url" value={draft()?.base_url || ''} placeholder="https://服务商地址/v1" onInput={e=>update({base_url:e.currentTarget.value})}/></label>
      <label>模型 ID（每行一个）<textarea aria-label="模型 ID" required rows={3} value={modelText()} placeholder="填写服务商提供的模型 ID" onInput={e=>setModelText(e.currentTarget.value)}/></label>
      <label>API Key<input aria-label="模型服务 API Key" type={showKey()?'text':'password'} autocomplete="off" value={key()} placeholder={value.key_saved?'已安全保存；留空保留原密钥':'本地无密钥服务可留空'} onInput={e=>setKey(e.currentTarget.value)}/></label>
      <div class="model-key-options"><label><input type="checkbox" checked={showKey()} onChange={e=>setShowKey(e.currentTarget.checked)}/>显示本次输入</label><Show when={value.key_saved}><label><input type="checkbox" checked={clearKey()} onChange={e=>setClearKey(e.currentTarget.checked)}/>清除原密钥</label></Show></div>
      <p>测试会向所填地址发送固定测试请求，可能产生少量费用。请确认地址属于你的服务商。</p>
      <Show when={message()}><p role="status" class="model-settings-feedback" data-success={success()}>{message()}</p></Show>
      <div class="model-service-actions"><button type="button" onClick={()=>void run('test')}>测试连接</button><button type="button" onClick={()=>{setDraft(undefined);setKey('');}}>取消</button><button type="submit">保存服务</button></div>
      </fieldset>
    </form>}</Show>
  </div>;
}
