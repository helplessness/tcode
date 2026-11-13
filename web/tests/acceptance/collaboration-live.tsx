// Only the host is test-specific: panels, requests and state are production code.
import { render } from 'solid-js/web';
import { createSignal, Show } from 'solid-js';
import GoalModePanel from '../../src/components/GoalModePanel';
import SubagentsWorkspace from '../../src/components/SubagentsWorkspace';
import UsageDetailModal from '../../src/components/UsageDetailModal';
import { sessionStore } from '../../src/stores/session';
import { sendMessage } from '../../src/api/chat';
import '../../src/index.css';
import '../../src/interface.css';
import '../../src/studio.css';

const context=await fetch('./context.json').then(r=>r.json());
sessionStore.setCurrentSessionId(context.sessions[0].id);
sessionStore.setSelectedAgentName('local-team');
document.documentElement.classList.add('studio-theme');
render(()=>{
  const [message,setMessage]=createSignal('');
  const [handler,setHandler]=createSignal<((text:string)=>Promise<boolean>)>();
  const [usage,setUsage]=createSignal(false);
  return <main style="max-width:1100px;margin:24px auto;padding:20px">
    <h1>协作预算本地验收</h1>
    <p>真实 DeepSeek、本地 Worker、生产面板与生产控制接口。未连接远程作业服务。</p>
    <GoalModePanel sessionId={context.sessions[0].id} workspaceId={context.workspace_id} native={true} onSubmitReady={next=>setHandler(()=>next)}/>
    <label style="display:block">任务输入<textarea aria-label="任务输入" style="width:100%;height:150px" value={message()} onInput={e=>setMessage(e.currentTarget.value)}/></label>
    <button onClick={()=>void handler()?.(message())}>提交多 Agent 目标</button>
    <button onClick={()=>void sendMessage(message(),{agent_name:'local-team',workdir:context.workspace_path}).catch(e=>sessionStore.setStatus(String(e)))}>普通聊天提交任务</button>
    <p role="status">{sessionStore.status()}</p>
    <button onClick={()=>setUsage(true)}>查看真实用量面板</button>
    <section style="height:900px;margin-top:20px"><SubagentsWorkspace active={true}/></section>
    <Show when={usage()}><UsageDetailModal onClose={()=>setUsage(false)}/></Show>
  </main>;
},document.getElementById('root')!);
