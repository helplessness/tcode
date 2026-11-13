import { For, Show, createSignal, onCleanup, onMount } from 'solid-js';
import { desktop } from '../desktop';

type ServiceState = { state: string; url: string };
type Services = { remote: string; job_protocol?: ServiceState; artifact?: ServiceState; artifacts?: (ServiceState & { service_id: string; name: string; default: boolean })[]; config_file: string; artifact_configured: boolean; dev_local_services: boolean; local_execution: string };

export default function DesktopServices(props: { onOpenSettings: (section: 'mcp' | 'desktop') => void }) {
  const [status, setStatus] = createSignal<Services>();
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  let details: HTMLDetailsElement | undefined;
  const jobState = () => status()?.job_protocol?.state || status()?.remote || 'checking';
  const artifactState = () => status()?.artifact?.state || (status()?.artifact_configured ? 'checking' : 'unconfigured');
  const artifacts = () => status()?.artifacts?.length ? status()!.artifacts! : [{ service_id: '', name: 'Artifact 文件服务', default: true, state: artifactState(), url: status()?.artifact?.url || '' }];
  const stateLabel = (state: string) => ({ connected: '已连接', disconnected: '连接失败', invalid: '配置无效', unconfigured: '未配置', checking: '检查中' } as Record<string, string>)[state] || '状态未知';
  const stateTone = (state: string) => state === 'connected' ? 'connected' : ['disconnected', 'invalid'].includes(state) ? 'failed' : state === 'unconfigured' ? 'unconfigured' : 'checking';
  const indicator = () => {
    if (busy() || !status()) return 'checking';
    const states = [...(desktop || status()?.job_protocol ? [jobState()] : []), ...artifacts().map(item => item.state)];
    if (error() || states.some(item => ['disconnected', 'invalid'].includes(item))) return 'failed';
    if (states.every(item => item === 'connected')) return 'connected';
    if (states.includes('checking')) return 'checking';
    return 'unconfigured';
  };
  const summaryLabel = () => indicator() === 'connected' ? '远程服务已连接' : indicator() === 'failed' ? '远程服务连接异常' : indicator() === 'unconfigured' ? '远程服务待配置' : '正在检查远程服务';
  const openSettings = (section: 'mcp' | 'desktop') => {
    if (details) details.open = false;
    props.onOpenSettings(section);
  };
  async function refresh(retry = false) {
    if (busy()) return;
    setBusy(true); setError('');
    try {
      const response = await fetch(desktop ? `/api/desktop/services?retry=${retry}` : '/api/artifact-services', { signal: AbortSignal.timeout(60000) });
      if (!response.ok) throw new Error();
      setStatus(await response.json());
    } catch { setError('无法读取服务状态，请稍后重试。'); }
    finally { setBusy(false); }
  }
  onMount(() => {
    void refresh();
    const closeOutside = (event: PointerEvent) => {
      if (details?.open && !details.contains(event.target as Node)) details.open = false;
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && details?.open) {
        details.open = false;
        details.querySelector<HTMLElement>('summary')?.focus();
      }
    };
    document.addEventListener('pointerdown', closeOutside, true);
    document.addEventListener('keydown', closeOnEscape);
    onCleanup(() => {
      document.removeEventListener('pointerdown', closeOutside, true);
      document.removeEventListener('keydown', closeOnEscape);
    });
  });
  return <details ref={details} class="studio-layout-menu desktop-services">
    <summary><span class={`desktop-service-light is-${indicator()}`} aria-hidden="true" />{summaryLabel()}</summary>
    <div>
      <b>远端服务</b>
      <p role="status" class="desktop-service-overview">{error() || (busy() ? '正在检查远程服务…' : `${desktop ? 'Job Protocol · ' : ''}${status()?.artifacts?.length ?? (status()?.artifact_configured ? 1 : 0)} 个 Artifact 服务，任务使用其绑定的文件服务。`)}</p>
      <div class="desktop-service-items" role="region" aria-label="远程服务列表" tabIndex={0}>
        <Show when={desktop || status()?.job_protocol}><div><span class={`desktop-service-light is-${stateTone(jobState())}`} aria-hidden="true"/><span><b>Job Protocol</b><small>提交、查询和取消远程计算任务</small><code title={status()?.job_protocol?.url || ''}>{status()?.job_protocol?.url || '尚未配置地址'}</code></span><span class="desktop-service-item-actions"><strong>{stateLabel(jobState())}</strong><button onClick={() => openSettings('mcp')}>设置</button></span></div></Show>
        <For each={artifacts()}>{item => <div><span class={`desktop-service-light is-${stateTone(item.state)}`} aria-hidden="true"/><span><b>{item.name || item.service_id}{item.default ? '（默认）' : ''}</b><small>Artifact{item.service_id ? ` · ${item.service_id}` : ''}</small><code title={item.url}>{item.url || '尚未配置地址'}</code></span><span class="desktop-service-item-actions"><strong>{stateLabel(item.state)}</strong><button aria-label={`配置 ${item.name || item.service_id || 'Artifact'}`} onClick={() => openSettings('desktop')}>设置</button></span></div>}</For>
      </div>
      {/* <p>{status()?.dev_local_services ? '开发联调模式：连接这台电脑上已运行的 Job Protocol；桌面不会代为启动计算服务。正式部署需改用服务器地址。' : '远程 MCP 在服务器运行，无需在此电脑启动 Job Protocol 或 Sandbox Runner。'}</p>
      <p>配置位置：Job Protocol 在“设置 → MCP”，Artifact 地址在“设置 → 桌面端”。保存 Artifact 地址后需重启本地服务。</p>
      <Show when={status()?.config_file}><p>配置文件：<code style={{ 'overflow-wrap': 'anywhere' }}>{status()!.config_file}</code></p></Show>
      <p>填写远程地址并启用后，通过应用菜单“重启本地服务”生效。有未结束任务时请勿更换服务器。</p>
      <p>{status()?.local_execution === 'offline-single-process' ? '本地文件工具、单进程命令、离线技能动作及隔离型插件工具已启用原生隔离。插件的宿主导入、本地 MCP 和外部 CLI 暂未开放。' : '本地原生隔离自检未通过，执行已阻断；本地历史及文件界面仍可使用。'}</p> */}
      <button disabled={busy()} onClick={() => void refresh(true)}>重新检查连接</button>
    </div>
  </details>;
}
