// Production panels and real local HTTP routes. No fetch interception or fake responses.
import { render } from 'solid-js/web';
import { createSignal, Show } from 'solid-js';
import GoalModePanel from '../../src/components/GoalModePanel';
import ResearchSessionPanel from '../../src/components/ResearchSessionPanel';
import UsageDetailModal from '../../src/components/UsageDetailModal';
import '../../src/index.css';
import '../../src/interface.css';
import '../../src/studio.css';

const context = await fetch('./context.json').then(r=>r.json());
document.documentElement.classList.add('studio-theme');
render(()=>{
  const [session,setSession]=createSignal(context.sessions[0].id);
  const [research,setResearch]=createSignal(context.research[0].id);
  const [usage,setUsage]=createSignal(false);
  const [message,setMessage]=createSignal('');
  const [handler,setHandler]=createSignal<((text:string)=>Promise<boolean>)>();
  return <main style="max-width:1050px;margin:24px auto;padding:20px">
    <h1>预算与运行状态本地验收</h1>
    <p>真实生产组件、真实本地 API 与独立数据库。作业计数为受控本地快照样本，不代表实时远端状态。</p>
    <section style="margin:24px 0;padding:20px;border:1px solid #64748b">
      <h2>目标控制</h2>
      <label>验收会话<select aria-label="验收会话" value={session()} onChange={e=>setSession(e.currentTarget.value)}>
        {context.sessions.map((item:any)=><option value={item.id}>{item.title}</option>)}
      </select></label>
      <GoalModePanel sessionId={session()} workspaceId={context.workspace_id} native={true} onSubmitReady={next=>setHandler(()=>next)}/>
      <label style="display:block;margin-top:16px">目标内容<textarea aria-label="目标内容" style="width:100%;height:100px" value={message()} onInput={e=>setMessage(e.currentTarget.value)}/></label>
      <button onClick={()=>void handler()?.(message())}>提交目标</button>
    </section>
    <section style="margin:24px 0;padding:20px;border:1px solid #64748b">
      <h2>研究续跑控制</h2>
      <label>验收课题<select aria-label="验收课题" value={research()} onChange={e=>setResearch(e.currentTarget.value)}>
        {context.research.map((item:any,index:number)=><option value={item.id}>{index===0?'本地未计价流程／可暂停':'本地轮次耗尽／保留已有流程'}</option>)}
      </select></label>
      <ResearchSessionPanel tab="continuation" researchId={research()} workspaceId={context.research.find((item:any)=>item.id===research()).workspace_id}/>
    </section>
    <button onClick={()=>setUsage(true)}>查看真实用量面板</button>
    <Show when={usage()}><UsageDetailModal onClose={()=>setUsage(false)}/></Show>
  </main>;
},document.getElementById('root')!);
