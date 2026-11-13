// 使用真实 Workflow 详情组件；失败案例由通用后端状态机生成，不调用模型。
import { render } from 'solid-js/web';
import { createSignal, Show } from 'solid-js';
import WorkflowWorkspacePanel from '../../src/components/WorkflowWorkspacePanel';
import { sessionStore } from '../../src/stores/session';
import '../../src/index.css';
import '../../src/interface.css';

sessionStore.setSessions([{ id: 'diagnostic-acceptance', session_id: 'diagnostic-acceptance', title: '中文诊断验收' }] as any);
sessionStore.setCurrentSessionId('diagnostic-acceptance');
render(() => {
  const [mode, setMode] = createSignal<'templates'|'temporary'>('templates');
  return <main style="padding:20px;max-width:1200px;margin:auto">
    <p>隔离验收：故障由测试主动注入，真实组件读取真实后端状态机产物，不调用模型。</p>
    <button onClick={() => setMode('templates')}>运行案例</button>
    <button onClick={() => setMode('temporary')}>配置导入案例</button>
    <Show when={mode()} keyed>{value => <WorkflowWorkspacePanel active mode={value} />}</Show>
  </main>;
}, document.getElementById('root')!);
