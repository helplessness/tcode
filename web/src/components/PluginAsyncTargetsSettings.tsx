import { For, Show, createEffect, createSignal } from 'solid-js';
import './PluginAsyncTargetsSettings.css';
import FeedbackDialog from './FeedbackDialog';
import DynamicHeadersEditor from './DynamicHeadersEditor';
import type { DynamicHeaders } from '../api/capabilities';

type Target = { plugin: string; target: string; server: string; active: boolean; version: string;
  execution: { mode: 'blocking' | 'remote-job'; cancelTool?: string }; tools: string[]; url: string; token_env: string;
  error?: string; declaration_files: string[]; dynamic_headers?: DynamicHeaders; has_draft?: boolean };
type Catalog = { targets: Target[]; gateway_url: string; config_file: string; management_configured: boolean };

export default function PluginAsyncTargetsSettings(props: { workspaceId?: string }) {
  const [catalog, setCatalog] = createSignal<Catalog>();
  const [error, setError] = createSignal('');
  const [loading, setLoading] = createSignal(false);
  const query = () => props.workspaceId ? `?workdir_name=${encodeURIComponent(props.workspaceId)}` : '';
  async function request(path = '', body?: Record<string, unknown>) {
    const response = await fetch(`/api/plugins${path}${query()}`, body ? {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(60_000),
    } : { signal: AbortSignal.timeout(30_000) });
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail || '插件异步配置请求失败');
    return value;
  }
  async function load() {
    setLoading(true); setError('');
    try { setCatalog(await request('/async-targets')); }
    catch (e) { setError(e instanceof Error ? e.message : '加载失败'); }
    finally { setLoading(false); }
  }
  createEffect(() => { props.workspaceId; void load(); });
  function Card(card: { target: Target }) {
    const target = card.target;
    const [url, setUrl] = createSignal(target.url);
    const [tokenEnv, setTokenEnv] = createSignal(target.token_env);
    const [dynamicHeaders, setDynamicHeaders] = createSignal<DynamicHeaders>(target.dynamic_headers || {});
    const [approved, setApproved] = createSignal(false);
    const [busy, setBusy] = createSignal(false);
    const [editorOpen, setEditorOpen] = createSignal(false);
    const [status, setStatus] = createSignal('');
    const [failed, setFailed] = createSignal(false);
    async function act(action: 'save' | 'check' | 'activate' | 'deactivate') {
      if (busy()) return;
      setBusy(true); setFailed(false); setStatus(action === 'check' ? '正在检查网关和远端工具…' : '正在保存…');
      try {
        await request(`/${encodeURIComponent(target.plugin)}/async-targets/${encodeURIComponent(target.target)}/${action}`,
          { url: url().trim(), token_env: tokenEnv().trim(), dynamic_headers: dynamicHeaders(), approved: approved() });
        if (action === 'check') setStatus('连接与工具检查通过。文件服务可达性仍需通过一次实际文件任务验证。');
        else await load();
      } catch (e) { setFailed(true); setStatus(e instanceof Error ? e.message : '操作失败，请重试'); }
      finally { setBusy(false); }
    }
    return <><article class="settings-form plugin-target-card" aria-label={`${target.plugin}/${target.target}`}>
      <h4>{target.plugin} / {target.target} · {target.active ? '已启用' : '未启用'}</h4>
      <Show when={target.has_draft}><p role="status">本地草稿已保存，尚未应用；现有已启用配置与任务不变。</p></Show>
      <p>{target.execution.mode === 'remote-job' ? `远端持久任务：可按原任务 ID 恢复${target.execution.cancelTool ? '，支持远端取消' : ''}` : '普通长调用：仅后台等待，断连后结果可能未知'}。工具：{target.tools.join('、')}</p>
      <button type="button" onClick={() => setEditorOpen(true)}>配置连接</button>
      <Show when={target.error}><p role="alert">{target.error}</p></Show>
    </article>
    <Show when={editorOpen()}><FeedbackDialog title={`配置 ${target.plugin} / ${target.target}`} onClose={() => { if (!busy()) setEditorOpen(false); }}>
      <details><summary>高级：声明与目标标识</summary><p class="settings-muted">声明文件：{target.declaration_files.join('、')}<br/>目标标识：<code>{target.server}</code></p></details>
      <label>计算 MCP 地址<input type="url" value={url()} onInput={e => { setUrl(e.currentTarget.value); setApproved(false); }} placeholder="https://compute.example.com/mcp" required /></label>
      <details><summary>可选：服务认证</summary><label>Bearer Token 环境变量名（可选，不是令牌值）<input value={tokenEnv()} onInput={e => { setTokenEnv(e.currentTarget.value); setApproved(false); }} placeholder="TCODE_PLUGIN_TOKEN_MY_COMPUTE" /></label>
      <p class="settings-muted">令牌在 TCode 后端的运行环境中设置，变量名以 TCODE_PLUGIN_TOKEN_ 开头，修改后需重启。这里不会保存或回显令牌明文。</p></details>
      <label class="plugin-target-consent"><input type="checkbox" checked={approved()} onChange={e => setApproved(e.currentTarget.checked)} />我允许向此服务器发送本插件任务的输入文件和参数</label>
      <DynamicHeadersEditor value={dynamicHeaders()} onChange={value => { setDynamicHeaders(value); setApproved(false); }} disabled={busy()} />
      <div class="mcp-editor-actions">
        <button type="button" disabled={busy() || !url().trim()} onClick={() => void act('save')}>保存草稿（不连接）</button>
        <button type="button" disabled={busy() || !url().trim()} onClick={() => void act('check')}>检查连接</button>
        <button type="button" class="btn" disabled={busy() || !approved() || !url().trim()} onClick={() => void act('activate')}>启用／更新异步目标</button>
        <button type="button" disabled={busy() || !target.active} onClick={() => void act('deactivate')}>停用新任务</button>
      </div>
      <Show when={status()}><p role={failed() ? 'alert' : 'status'}>{status()}</p></Show>
      <Show when={target.error}><p role="alert">{target.error}</p></Show>
      <div class="feedback-dialog-actions"><button type="button" disabled={busy()} onClick={() => setEditorOpen(false)}>返回服务列表</button></div>
    </FeedbackDialog></Show></>;
  }
  return <section class="plugin-async-settings" aria-label="插件异步 MCP 配置">
    <h3>插件异步 MCP</h3>
    <details><summary>高级：接入协议与部署说明</summary><p>运行链路：TCode → 用户本地 Job Protocol → 第三方计算 MCP。不要只在上面的常规 MCP 表单中选择“异步”，那不会自动注册第三方计算目标。</p>
    <p>先安装声明 <code>asyncTargets</code> 的插件，再在这里配置并授权。已有任务结果不会因停用而删除。</p></details>
    <button type="button" onClick={() => void load()} disabled={loading()}>刷新插件目标</button>
    <Show when={error()}><p role="alert">{error()}</p></Show>
    <Show when={catalog()}>{data => <>
      <details><summary>高级：网关与配置存储</summary><p>Job Protocol：<code>{data().gateway_url}</code>（默认在用户本地运行）</p><p class="settings-config-path">{data().config_file}</p></details>
      <Show when={!data().management_configured}><p role="alert">注册令牌未配置：在 TCode 和 Job Protocol 中设置相同的 TMCP_TARGET_REGISTRY_TOKEN。桌面端托管启动会自动配置。</p></Show>
      <Show when={data().targets.length} fallback={<p>当前工作区没有声明异步计算目标的插件。安装插件后点击刷新。</p>}>
        <For each={data().targets}>{target => <Card target={target} />}</For>
      </Show>
      <p class="settings-muted">文件传输：使用部署方配置的 Artifact 服务；第三方服务器必须能访问该地址，不能使用用户本地的 127.0.0.1。</p>
    </>}</Show>
  </section>;
}
