// 独立验收页：真实组件，API 由测试浏览器内存夹具拦截，不使用用户研究。
import { render } from 'solid-js/web';
import { createSignal } from 'solid-js';
import ResearchChangesPanel from '../../src/components/ResearchChangesPanel';
import type { ResearchProject } from '../../src/api/research';
import '../../src/index.css';
import '../../src/interface.css';
import type { ResearchChangeFocus } from '../../src/api/researchChanges';

const project={research_id:'visual-check',title:'双视图验收：稳定性研究（示例）',description:'仅用于界面验收，不代表科研结论。',status:'active'} as ResearchProject;
document.documentElement.classList.add('studio-theme');
document.documentElement.dataset.desktop='true';
render(()=>{
  const [focus,setFocus]=createSignal<ResearchChangeFocus>();
  return <main style="max-width:1200px;margin:32px auto;padding:0 24px">
    <p style="font-size:12px;color:var(--tcode-text-muted)">隔离界面验收 · 内存示例，不写入用户研究</p>
    <h1 style="font-size:24px;margin:12px 0">{project.title}</h1>
    <button style="font-size:13px" onClick={()=>setFocus({baseId:'before',headId:'after',nodeId:'experiment:route-a',side:'after',key:Date.now()})}>模拟智能交互定位</button>
    <ResearchChangesPanel project={project} workspaceId="visual-check" focus={focus()}/>
    <footer style="padding:80px 0;color:var(--tcode-text-muted)">验收页底部：用于检查地图滚动到边界后能否继续滚动页面。</footer>
  </main>;
},document.getElementById('root')!);
