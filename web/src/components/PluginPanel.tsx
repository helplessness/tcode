import { For, Show, createEffect, createMemo, createSignal, onCleanup } from 'solid-js';
import PluginAsyncTargetsSettings from './PluginAsyncTargetsSettings';
import './PluginPackageImport.css';
import FeedbackDialog, { confirmAction } from './FeedbackDialog';

type PluginInfo = {
  name: string;
  version: string;
  description: string;
  author?: string;
  source: string;
  plugin_type: string[];
  tools: string[];
  hooks: Record<string, string[]>;
  commands?: string[];
  skills?: string[];
  agents?: string[];
  mcp_servers?: Array<{
    name?: string;
    status?: string;
    connected?: boolean;
    error?: string;
    tools?: number;
    tool_names?: string[];
    url?: string;
  }>;
  manifest?: Record<string, any> | null;
  component_errors?: Array<{ component: string; error: string }>;
  permissions?: Array<{
    scope: string;
    patterns: string[];
    description?: string;
    required: boolean;
  }>;
  granted_permissions?: Array<{
    scope: string;
    patterns: string[];
    description?: string;
    required: boolean;
  }>;
};

type AvailablePlugin = {
  name: string;
  path: string;
  description?: string;
  source?: string;
  enabled?: boolean;
  loaded: boolean;
  loadable?: boolean;
  desktop_delivery?: 'local' | 'remote';
  availability_message?: string;
};

type PluginTool = {
  id: string;
  description: string;
  plugin: string;
  parameters: string;
};

type PluginCommand = {
  name: string;
  plugin: string;
  description: string;
  argument_hint?: string;
  agent?: string;
  source: string;
};

type HookEventCatalogItem = {
  event: string;
  category: string;
  description: string;
  required_input: string[];
  registered_handlers: number;
};

type PluginDoctorReport = {
  name: string;
  configured: boolean;
  loaded: boolean;
  path: string;
  manifest: boolean;
  commands: number;
  skills: number;
  agents: number;
  tools: number;
  hooks: number;
  mcp_servers: number;
  mcp_details?: Array<{ name: string; status: string; connected: boolean; error?: string; tools: number; tool_names?: string[]; url?: string }>;
  load_error?: string;
  dependencies?: Array<{ name: string; installed: boolean; path?: string | null }>;
};

type PluginLoadDiagnostic = {
  category?: string;
  code?: string;
  message?: string;
  path?: string;
  state?: string;
};

interface PluginPanelProps {
  width: number;
  embedded?: boolean;
  workdir?: string;
  onWidthChange?: (width: number) => void;
  onHide?: () => void;
}

export default function PluginPanel(props: PluginPanelProps) {
  const [activeTab, setActiveTab] = createSignal<'plugins' | 'tools' | 'commands' | 'hooks' | 'doctor'>('plugins');
  const [loadedPlugins, setLoadedPlugins] = createSignal<PluginInfo[]>([]);
  const [availablePlugins, setAvailablePlugins] = createSignal<AvailablePlugin[]>([]);
  const [pluginTools, setPluginTools] = createSignal<PluginTool[]>([]);
  const [pluginCommands, setPluginCommands] = createSignal<PluginCommand[]>([]);
  const [pluginHooks, setPluginHooks] = createSignal<string[]>([]);
  const [hookCatalog, setHookCatalog] = createSignal<HookEventCatalogItem[]>([]);
  const [doctorReports, setDoctorReports] = createSignal<PluginDoctorReport[]>([]);
  const [pluginErrors, setPluginErrors] = createSignal<Record<string, string>>({});
  const [pluginDiagnostics, setPluginDiagnostics] = createSignal<Record<string, PluginLoadDiagnostic>>({});
  const [loading, setLoading] = createSignal(false);
  const [message, setMessage] = createSignal<{ type: 'success' | 'error'; text: string } | null>(null);
  const [advanced, setAdvanced] = createSignal(false);
  const [installOpen, setInstallOpen] = createSignal(false);
  const [packageFile, setPackageFile] = createSignal<File>();
  const [installing, setInstalling] = createSignal(false);
  const [installError, setInstallError] = createSignal('');
  const [installation, setInstallation] = createSignal<{ name: string; version: string; path: string; async_targets: boolean }>();
  const normalizedWorkdir = createMemo(() => String(props.workdir || '').trim());
  let successMessageTimer: number | undefined;
  let pluginListRequestId = 0;
  let packageInput: HTMLInputElement | undefined;

  const closeInstaller = () => {
    if (installing()) return;
    setInstallOpen(false);
    setPackageFile(undefined);
    setInstallError('');
  };

  /** 显示操作结果；错误保持到用户关闭，成功提示短暂显示。 */
  const showMessage = (type: 'success' | 'error', text: string) => {
    if (successMessageTimer !== undefined) {
      window.clearTimeout(successMessageTimer);
      successMessageTimer = undefined;
    }
    setMessage({ type, text });
    if (type === 'success') {
      successMessageTimer = window.setTimeout(() => {
        setMessage(null);
        successMessageTimer = undefined;
      }, 3000);
    }
  };

  /** 构造绑定到稳定工作目录字符串的插件 API 地址。 */
  const pluginApiUrl = (path: string) => {
    const search = new URLSearchParams();
    const workdir = normalizedWorkdir();
    if (workdir) search.set('workdir', workdir);
    const query = search.toString();
    return query ? `/api/plugins/${path}?${query}` : `/api/plugins/${path}`;
  };

  /** 只安装包，不执行入口；加载和远端输入授权保持分步确认。 */
  const installPackage = async () => {
    const file = packageFile();
    const workdir = normalizedWorkdir();
    if (!file || installing()) return;
    setInstalling(true); setInstallError(''); setInstallation(undefined);
    try {
      if (!file.name.toLowerCase().endsWith('.tcode-plugin')) throw new Error('请选择 .tcode-plugin 文件');
      if (file.size > 32 * 1024 * 1024) throw new Error('插件包不能超过 32 MiB');
      const url = new URL(pluginApiUrl('install'), window.location.origin);
      url.searchParams.set('filename', file.name);
      const response = await fetch(url.pathname + url.search, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
        signal: AbortSignal.timeout(60_000),
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.detail || '插件安装失败');
      if (workdir !== normalizedWorkdir()) return;
      setInstallation(data.installation); setPackageFile(undefined);
      if (packageInput) packageInput.value = '';
      await loadPlugins();
    } catch (error) {
      if (workdir === normalizedWorkdir()) setInstallError(error instanceof Error ? error.message : '插件安装失败');
    } finally { setInstalling(false); }
  };

  createEffect(() => {
    normalizedWorkdir();
    setPackageFile(undefined); setInstallation(undefined); setInstallError('');
    setInstallOpen(false);
    if (packageInput) packageInput.value = '';
  });

  /** 并行读取插件列表与可用目录，并忽略已经过期的并发响应。 */
  const loadPlugins = async () => {
    const requestId = ++pluginListRequestId;
    setLoading(true);
    try {
      const [loadedResp, availableResp] = await Promise.all([
        fetch(pluginApiUrl('list')),
        fetch(pluginApiUrl('available')),
      ]);
      if (!loadedResp.ok || !availableResp.ok) {
        throw new Error(`插件目录请求失败 (HTTP ${loadedResp.status}/${availableResp.status})`);
      }
      const loadedData = await loadedResp.json();
      const availableData = await availableResp.json();
      if (requestId !== pluginListRequestId) return;
      setLoadedPlugins(loadedData.plugins || []);
      setAvailablePlugins(availableData.plugins || []);
      setPluginErrors(loadedData.errors || {});
      setPluginDiagnostics(loadedData.diagnostics || {});
    } catch (error) {
      console.error(error);
      showMessage('error', '加载插件失败');
    } finally {
      if (requestId === pluginListRequestId) setLoading(false);
    }
  };

  const loadTools = async () => {
    const response = await fetch(pluginApiUrl('tools'));
    const data = await response.json();
    setPluginTools(data.tools || []);
  };

  const loadHooks = async () => {
    const response = await fetch(pluginApiUrl('hooks'));
    const data = await response.json();
    setPluginHooks(data.hooks || []);
    setHookCatalog(data.catalog || []);
  };

  const loadCommands = async () => {
    const response = await fetch(pluginApiUrl('commands'));
    const data = await response.json();
    setPluginCommands(data.commands || []);
  };

  const loadDoctor = async () => {
    const response = await fetch(pluginApiUrl('doctor'));
    const data = await response.json();
    setDoctorReports(data.reports || []);
  };

  const loadPlugin = async (name: string, path: string) => {
    setLoading(true);
    try {
      const response = await fetch('/api/plugins/load', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, path, enabled: true, workdir: props.workdir }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        showMessage('error', data.error || data.detail || `加载插件失败 (HTTP ${response.status})`);
        await loadPlugins();
        return;
      }
      showMessage('success', `插件 ${name} 已加载`);
      await loadPlugins();
    } catch (error) {
      console.error(error);
      showMessage('error', error instanceof Error ? error.message : '加载插件失败');
    } finally {
      setLoading(false);
    }
  };

  const unloadPlugin = async (name: string) => {
    setLoading(true);
    try {
      const response = await fetch('/api/plugins/unload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, workdir: props.workdir }),
      });
      const data = await response.json();
      if (!data.success) {
        showMessage('error', data.error || '卸载插件失败');
        return;
      }
      showMessage('success', `插件 ${name} 已卸载`);
      await loadPlugins();
    } finally {
      setLoading(false);
    }
  };

  const deletePlugin = async (name: string) => {
    const workdir = props.workdir;
    if (!await confirmAction(`删除插件“${name}”？将永久删除当前工作区的插件安装文件、登记和异步连接配置。已有任务记录与结果文件不会删除。此操作不能撤销；需要时可重新导入插件包。`, { title: '删除插件', tone: 'danger', confirmLabel: '删除插件' })) return;
    if (workdir !== props.workdir) return;
    setLoading(true);
    try {
      const response = await fetch('/api/plugins/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, workdir, confirmed: true }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.error || data.detail || '删除插件失败');
      if (installation()?.name === name) setInstallation(undefined);
      showMessage('success', `插件 ${name} 已删除，已有任务结果已保留`);
      await loadPlugins();
    } catch (error) {
      showMessage('error', error instanceof Error ? error.message : '删除插件失败');
    } finally { setLoading(false); }
  };

  const reloadPlugins = async () => {
    setLoading(true);
    try {
      const response = await fetch(pluginApiUrl('reload-config'), { method: 'POST' });
      const data = await response.json();
      if (!data.success) {
        showMessage('error', data.error || '重新加载插件失败');
        return;
      }
      const failedCount = Object.keys(data.errors || {}).length;
      if (failedCount > 0) {
        showMessage('error', `已加载 ${data.loaded_count} 个插件，另有 ${failedCount} 个插件失败；详情保留在诊断区。`);
      } else {
        showMessage('success', `已重新加载 ${data.loaded_count} 个插件`);
      }
      await loadPlugins();
    } finally {
      setLoading(false);
    }
  };

  createEffect(() => {
    normalizedWorkdir();
    if (activeTab() === 'plugins') {
      loadPlugins();
    } else if (activeTab() === 'tools') {
      loadTools();
    } else if (activeTab() === 'commands') {
      loadCommands();
    } else if (activeTab() === 'doctor') {
      loadDoctor();
    } else {
      loadHooks();
    }
  });

  onCleanup(() => {
    if (successMessageTimer !== undefined) window.clearTimeout(successMessageTimer);
    pluginListRequestId += 1;
  });

  const tabClass = (tab: 'plugins' | 'tools' | 'commands' | 'hooks' | 'doctor') =>
    `flex-1 rounded-xl px-2 py-2 text-xs font-semibold transition-colors ${
      activeTab() === tab
        ? 'bg-[#38bdf8]/12 text-[#8ed5ff]'
        : 'text-[#8b949e] hover:text-[#e6edf3]'
    }`;

  return (
    <div class={`plugin-management ${props.embedded ? 'relative h-full w-full' : 'fixed bottom-0 right-0 top-14'} flex min-h-0 flex-col overflow-hidden`} style={{ width: props.embedded ? '100%' : `${props.width}px` }}>
      <div class="plugin-management-header">
        <div class="flex items-center justify-between gap-3">
          <div>
            <h2 class="plugin-management-title">插件</h2>
            <p class="plugin-management-subtitle">管理能力扩展 · 已加载 {loadedPlugins().length} 个</p>
          </div>
          <div class="flex items-center gap-2">
            <button class="plugin-action plugin-action-primary" onClick={() => setInstallOpen(true)}>安装插件</button>
            <button class="plugin-action" onClick={() => props.onHide?.()} title="隐藏面板">
              关闭
            </button>
          </div>
        </div>

        <div class="plugin-management-toolbar">
          <button class="plugin-action" disabled={loading()} onClick={reloadPlugins}>重新加载</button>
          <button class="plugin-action" aria-expanded={advanced()} onClick={() => { setAdvanced(!advanced()); setActiveTab(advanced() ? 'doctor' : 'plugins'); }}>{advanced() ? '返回插件列表' : '高级信息'}</button>
        </div>
        <Show when={advanced()}><nav aria-label="插件高级信息" class="mt-3 grid grid-cols-5 gap-1">
          <button class={tabClass('plugins')} aria-pressed={activeTab() === 'plugins'} onClick={() => setActiveTab('plugins')}>插件</button>
          <button class={tabClass('tools')} aria-pressed={activeTab() === 'tools'} onClick={() => setActiveTab('tools')}>工具</button>
          <button class={tabClass('commands')} aria-pressed={activeTab() === 'commands'} onClick={() => setActiveTab('commands')}>命令</button>
          <button class={tabClass('hooks')} aria-pressed={activeTab() === 'hooks'} onClick={() => setActiveTab('hooks')}>钩子</button>
          <button class={tabClass('doctor')} aria-pressed={activeTab() === 'doctor'} onClick={() => setActiveTab('doctor')}>诊断</button>
        </nav></Show>
      </div>
      <Show when={installOpen()}>
        <FeedbackDialog title="安装插件" tone="info" onClose={closeInstaller}>
        <section class="plugin-package-import plugin-install-body" aria-label="导入插件包" aria-busy={installing()}>
          <label class="block font-semibold" for="plugin-package-file">导入 .tcode-plugin 插件包</label>
          <p class="my-2 leading-6">安装到当前工作区并自动登记，无需编辑 plugins.json。导入不会运行代码；加载后仍需单独授权异步 MCP。</p>
          <p class="mb-3 leading-6">仅导入可信来源。此入口校验包结构与摘要，不验证发布者签名；插件始终按隔离模式安装。</p>
          <input ref={packageInput} id="plugin-package-file" type="file" accept=".tcode-plugin" disabled={installing()} class="block w-full rounded-lg border border-[#65717c] p-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-400" onChange={e => { setPackageFile(e.currentTarget.files?.[0]); setInstallError(''); }} />
          <button type="button" disabled={!packageFile() || installing()} class="mt-3 min-h-10 rounded-lg border border-[#65717c] px-4 py-2 font-semibold hover:bg-[#29353e] focus-visible:outline-2 focus-visible:outline-sky-400 disabled:opacity-50" onClick={() => void installPackage()}>{installing() ? '正在校验并安装…' : '安装插件包'}</button>
          <Show when={installError()}><p role="alert" class="mt-3 leading-6">{installError()}</p></Show>
          <Show when={installation()}>{installed => <div class="mt-3 space-y-2" role="status">
            <p>已安装 {installed().name} v{installed().version}，当前{loadedPlugins().some(p => p.name === installed().name) ? '已加载' : '未加载'}。</p>
            <p class="break-all">位置：{installed().path}</p>
            <Show when={!loadedPlugins().some(p => p.name === installed().name)}>
              <button type="button" disabled={loading()} class="min-h-10 rounded-lg border border-[#65717c] px-4 py-2 font-semibold hover:bg-[#29353e] focus-visible:outline-2 focus-visible:outline-sky-400 disabled:opacity-50" onClick={() => loadPlugin(installed().name, installed().path)}>加载已安装插件</button>
            </Show>
            <Show when={installed().async_targets && loadedPlugins().some(p => p.name === installed().name)}>
              <p>插件已加载，可在下面检查并授权异步目标；也可进入“设置 → MCP 服务 → 插件异步 MCP”。</p>
              <PluginAsyncTargetsSettings workspaceId={normalizedWorkdir().split('/').filter(Boolean).pop()} />
            </Show>
          </div>}</Show>
        </section>
        <footer class="feedback-dialog-actions"><button type="button" disabled={installing()} onClick={closeInstaller}>返回插件列表</button></footer>
        </FeedbackDialog>
      </Show>

      <Show when={message()}>
        <div class="plugin-message mx-4 mt-3 flex items-start justify-between gap-3" data-tone={message()?.type} role={message()?.type === 'error' ? 'alert' : 'status'}>
          <span>{message()?.text}</span>
          <button class="shrink-0 text-current opacity-70 hover:opacity-100" onClick={() => setMessage(null)} title="关闭提示">✕</button>
        </div>
      </Show>

      <div class="plugin-management-list tcode-scrollbar min-h-0 flex-1 overflow-y-auto p-4 space-y-4" aria-label="插件列表">
        <Show when={loading()}>
          <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] py-6 text-center text-[#8b949e]">加载中...</div>
        </Show>

        <Show when={activeTab() === 'plugins' && !loading()}>
          <div class="space-y-4">
            <Show when={Object.keys(pluginErrors()).length > 0}>
              <details class="plugin-diagnostics">
                <summary>{Object.keys(pluginErrors()).length} 个插件未能加载 · 查看原因</summary>
                <div class="space-y-3">
                  <For each={Object.entries(pluginErrors())}>
                    {([name, error]) => {
                      const diagnostic = () => pluginDiagnostics()[name] || {};
                      return (
                        <div class="plugin-diagnostic-item">
                          <div class="font-semibold">{name}</div>
                          <div class="mt-2 break-words leading-5">{diagnostic().message || error}</div>
                          <Show when={diagnostic().code}><div class="mt-2 font-mono text-xs">{diagnostic().category || 'runtime'} · {diagnostic().code}</div></Show>
                        </div>
                      );
                    }}
                  </For>
                </div>
              </details>
            </Show>
            <section aria-label="已安装与可用插件">
              <Show when={availablePlugins().length > 0} fallback={<div class="plugin-empty">还没有插件，点击“安装插件”导入插件包。</div>}>
                <For each={availablePlugins()}>
                  {(plugin) => (
                    <article class="plugin-card" aria-label={plugin.name}>
                      <div class="plugin-card-heading">
                        <h3>{plugin.name}</h3>
                        <span class="plugin-state" data-active={plugin.loaded}>{plugin.loaded ? '已加载' : plugin.loadable === false ? '需远程服务' : '未加载'}</span>
                      </div>
                      <p class="plugin-card-description">{plugin.description || '暂无描述。'}</p>
                      <Show when={plugin.availability_message}><p class="plugin-card-note">{plugin.availability_message}</p></Show>
                      <div class="plugin-card-actions">
                        <Show when={plugin.loaded} fallback={<Show when={plugin.loadable !== false}><button class="plugin-action plugin-action-primary" disabled={loading()} onClick={() => loadPlugin(plugin.name, plugin.path)}>加载</button></Show>}>
                          <button class="plugin-action" disabled={loading()} onClick={() => unloadPlugin(plugin.name)} title="停止运行并保留安装文件">卸载</button>
                        </Show>
                        <Show when={plugin.path === `${normalizedWorkdir().replace(/\/$/, '')}/.tcode/installed-plugins/${plugin.name}`}>
                          <button type="button" class="plugin-action plugin-action-danger" disabled={plugin.loaded || loading()} title={plugin.loaded ? '请先卸载，再删除安装文件' : '删除安装文件与配置，保留任务结果'} onClick={() => void deletePlugin(plugin.name)}>删除插件</button>
                        </Show>
                      </div>
                      <details class="plugin-card-details"><summary>插件详情</summary>
                        <p>安装位置：{plugin.path}</p>
                        <Show when={loadedPlugins().find(p => p.name === plugin.name)}>{loaded => <>
                          <p>版本：{loaded().version}</p>
                          <Show when={loaded().author}><p>作者：{loaded().author}</p></Show>
                          <p>技能：{(loaded().skills || []).join('、') || '无'}</p>
                          <p>工具：{loaded().tools.join('、') || '无'}</p>
                          <p>命令：{(loaded().commands || []).join('、') || '无'}</p>
                          <p>代理：{(loaded().agents || []).join('、') || '无'}</p>
                          <p>插件 MCP：{(loaded().mcp_servers || []).map(server => server.name || '未知').join('、') || '无'}</p>
                        </>}</Show>
                        <Show when={plugin.loaded && plugin.path.includes('/.tcode/installed-plugins/')}><p>删除安装文件前，请先卸载。已有任务结果会保留。</p></Show>
                      </details>
                    </article>
                  )}
                </For>
              </Show>
            </section>
          </div>
        </Show>

        <Show when={activeTab() === 'tools' && !loading()}>
          <Show when={pluginTools().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] py-6 text-center text-sm text-[#8b949e]">暂无插件工具</div>}>
            <div class="space-y-2">
              <For each={pluginTools()}>
                {(tool) => (
                  <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
                    <div class="text-sm font-semibold text-[#8ed5ff]">{tool.id}</div>
                    <div class="mt-2 text-xs leading-6 text-[#9faeb7]">{tool.description}</div>
                    <div class="mt-3 text-xs text-[#bdc8d1]">插件: {tool.plugin}</div>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </Show>

        <Show when={activeTab() === 'commands' && !loading()}>
          <Show when={pluginCommands().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] py-6 text-center text-sm text-[#8b949e]">暂无插件命令</div>}>
            <div class="space-y-2">
              <For each={pluginCommands()}>
                {(command) => (
                  <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
                    <div class="text-sm font-semibold text-[#8ed5ff]">/{command.name}</div>
                    <div class="mt-2 text-xs leading-6 text-[#9faeb7]">{command.description || '暂无描述'}</div>
                    <div class="mt-3 text-xs text-[#bdc8d1]">插件: {command.plugin}</div>
                    <Show when={command.argument_hint}><div class="mt-1 text-xs text-[#9faeb7]">参数: {command.argument_hint}</div></Show>
                    <Show when={command.agent}><div class="mt-1 text-xs text-[#9faeb7]">代理: {command.agent}</div></Show>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </Show>

        <Show when={activeTab() === 'hooks' && !loading()}>
          <div class="mb-3 flex items-center justify-between border-b border-[#2a3136] pb-3 text-xs text-[#9faeb7]">
            <span>系统事件 {hookCatalog().length}</span>
            <span>已注册处理器 {pluginHooks().length}</span>
          </div>
          <Show when={hookCatalog().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] py-6 text-center text-sm text-[#8b949e]">暂无钩子事件目录</div>}>
            <div class="space-y-2">
              <For each={hookCatalog()}>
                {(hook) => (
                  <div class="border-b border-[#252c31] px-1 py-3 last:border-0">
                    <div class="flex items-center justify-between gap-3">
                      <div class="min-w-0 truncate font-mono text-xs text-[#8ed5ff]" title={hook.event}>{hook.event}</div>
                      <span class={`shrink-0 text-[11px] ${hook.registered_handlers ? 'text-[#9ee6bb]' : 'text-[#6f7d87]'}`}>
                        {hook.registered_handlers} 个处理器
                      </span>
                    </div>
                    <div class="mt-1 text-[11px] text-[#9faeb7]">{hook.category} · {hook.description}</div>
                    <Show when={hook.required_input.length > 0}>
                      <div class="mt-1 truncate font-mono text-[10px] text-[#6f7d87]" title={hook.required_input.join(', ')}>
                        input: {hook.required_input.join(', ')}
                      </div>
                    </Show>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </Show>

        <Show when={activeTab() === 'doctor' && !loading()}>
          <Show when={doctorReports().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#13191d] py-6 text-center text-sm text-[#8b949e]">暂无诊断数据</div>}>
            <div class="space-y-2">
              <For each={doctorReports()}>
                {(report) => (
                  <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
                    <div class="flex items-center justify-between gap-3">
                      <div class="text-sm font-semibold text-[#e6edf3]">{report.name}</div>
                      <span class={`rounded-full border px-3 py-1 text-[11px] ${report.loaded ? 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`}>{report.loaded ? '已加载' : '未加载'}</span>
                    </div>
                    <div class="mt-3 grid grid-cols-2 gap-2 text-xs text-[#9faeb7]">
                      <div>命令 {report.commands}</div>
                      <div>技能 {report.skills}</div>
                      <div>代理 {report.agents}</div>
                      <div>工具 {report.tools}</div>
                      <div>钩子 {report.hooks}</div>
                      <div>MCP {report.mcp_servers}</div>
                    </div>
                    <Show when={report.load_error}><div class="mt-3 text-xs text-[#ffb6b0]">{report.load_error}</div></Show>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </Show>
      </div>

      <Show when={!props.embedded}><div
        class="absolute bottom-0 left-0 top-0 z-50 w-1 cursor-col-resize transition-colors hover:bg-[#38bdf8]"
        onMouseDown={(e) => {
          e.preventDefault();
          const handleMouseMove = (moveEvent: MouseEvent) => {
            const newWidth = Math.max(250, Math.min(600, window.innerWidth - moveEvent.clientX));
            props.onWidthChange?.(newWidth);
          };
          const handleMouseUp = () => {
            document.removeEventListener('mousemove', handleMouseMove);
            document.removeEventListener('mouseup', handleMouseUp);
          };
          document.addEventListener('mousemove', handleMouseMove);
          document.addEventListener('mouseup', handleMouseUp);
        }}
        title="拖拽调整宽度"
      /></Show>
    </div>
  );
}
