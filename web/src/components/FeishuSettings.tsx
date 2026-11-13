import { For, Show, createSignal, onCleanup, onMount } from 'solid-js';
import './FeishuSettings.css';
import { confirmAction } from './FeedbackDialog';

type State = {
  configured: boolean; app_id: string; secret_configured: boolean; workspace_id: string;
  chats: string[]; bound: boolean; sender_id: string; enabled: boolean; status: string;
  last_event_at: number; error: string; permissions: { scope: string; purpose: string }[];
};
const statuses: Record<string, string> = {
  stopped: '未启动', listening: '监听已启动，等待消息验证', receiving: '已收到事件', retrying: '连接中断，正在重试',
};

export default function FeishuSettings(props: { workspaceId?: string }) {
  const [state, setState] = createSignal<State>();
  const [appId, setAppId] = createSignal('');
  const [secret, setSecret] = createSignal('');
  const [workspace, setWorkspace] = createSignal(props.workspaceId || '');
  const [chats, setChats] = createSignal('');
  const [workspaces, setWorkspaces] = createSignal<{ name: string; workspace_id: string }[]>([]);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [binding, setBinding] = createSignal<{ command: string; expires_at: number }>();
  const [now, setNow] = createSignal(Date.now() / 1000);
  let alive = true;
  let alert: HTMLParagraphElement | undefined;
  const dirty = () => !!state() && (appId() !== state()!.app_id || !!secret()
    || workspace() !== state()!.workspace_id || chats() !== state()!.chats.join(', '));
  const consoleUrl = () => state()?.app_id ? `https://open.feishu.cn/app/${state()!.app_id}` : 'https://open.feishu.cn/app';
  async function request(path = '', method = 'GET', body?: unknown) {
    const response = await fetch(`/api/feishu${path}`, {
      method, cache: 'no-store', headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '配置请求失败，请检查输入后重试');
    return result;
  }
  async function action(operation: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await operation(); }
    catch (e) { if (alive) { setError(e instanceof Error ? e.message : '操作失败'); queueMicrotask(() => alert?.focus()); } }
    finally { if (alive) setBusy(false); }
  }
  async function refresh() {
    const data: State = await request();
    if (!alive) return;
    setState(data);
    if (data.bound) setBinding(undefined);
  }
  onMount(() => {
    void action(async () => {
      const data: State = await request();
      const response = await fetch('/api/workdirs');
      if (!response.ok) throw new Error('工作区列表加载失败');
      const list = await response.json();
      if (!alive) return;
      setWorkspaces(list.workdirs || []);
      setState(data); setAppId(data.app_id); setWorkspace(data.workspace_id || props.workspaceId || '');
      setChats(data.chats.join(', '));
    });
    const timer = window.setInterval(() => {
      setNow(Date.now() / 1000);
      if (!busy()) void refresh().catch(() => { if (alive) setError('状态更新失败，请检查网络并刷新'); });
    }, 4000);
    onCleanup(() => { alive = false; window.clearInterval(timer); setSecret(''); });
  });
  return <div class="settings-page feishu-settings">
    <header class="feishu-page-header">
      <div class="feishu-page-title"><span class="feishu-connection-icon" aria-hidden="true"><svg width="25" height="25" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="m9 15 6-6M7 14l-2 2a3 3 0 0 0 4 4l3-3M17 10l2-2a3 3 0 0 0-4-4l-3 3"/></svg></span><div><h2>飞书接入</h2><p>让任务与结果，在飞书和 TCode 之间流转。</p></div></div>
      <a class="feishu-console-link" href={consoleUrl()} target="_blank" rel="noopener noreferrer">开发者后台 <span aria-hidden="true">↗</span></a>
    </header>
    <div class="feishu-overview" aria-label="飞书接入状态" aria-busy={!state()}>
      <div><span>应用配置</span><strong data-ready={Boolean(state()?.configured)}>{!state() ? '读取中…' : state()?.configured ? '已保存' : '待配置'}</strong></div>
      <div><span>消息监听</span><strong data-ready={Boolean(state()?.enabled)}>{!state() ? '读取中…' : state()?.enabled ? '已启动' : '未启动'}</strong></div>
      <div><span>身份绑定</span><strong data-ready={Boolean(state()?.bound)}>{!state() ? '读取中…' : state()?.bound ? '已绑定' : '待绑定'}</strong></div>
    </div>
    <p class="feishu-intro">使用自己的飞书应用，在私聊或指定群中发任务、连续追问并接收结果。</p>
    <Show when={error()}><p role="alert" tabIndex={-1} ref={alert} class="feishu-error">{error()}</p></Show>
    <Show when={notice()}><p role="status" class="feishu-notice">{notice()}</p></Show>
    <section class="settings-section" aria-labelledby="feishu-credentials">
      <div class="feishu-section-heading"><span class="feishu-step" aria-hidden="true">01</span><div><h3 id="feishu-credentials">应用与工作区</h3><p>填写应用凭据，选择接收任务的工作区。</p></div></div>
      <form class="settings-form" onSubmit={event => { event.preventDefault(); void action(async () => {
        const data = await request('', 'PUT', { app_id: appId().trim(), app_secret: secret() || null,
          workspace_id: workspace(), chats: chats().split(/[,，\s]+/).filter(Boolean) });
        setSecret(''); setState(data); setAppId(data.app_id); setChats(data.chats.join(', ')); setBinding(undefined);
        setNotice(data.bound ? '配置已保存，身份绑定已保留。' : '鉴权已通过，配置已保存。接下来启动连接并绑定飞书身份。');
      }); }}>
        <fieldset disabled={busy() || !state()}>
          <div class="feishu-credential-grid"><label>App ID<input required pattern="cli_(?:[A-Za-z0-9_]|-)+" value={appId()} onInput={e => setAppId(e.currentTarget.value)} autocomplete="off" placeholder="cli_…" /></label>
          <label>App Secret<input type="password" required={!state()?.secret_configured || appId() !== state()?.app_id}
            value={secret()} onInput={e => setSecret(e.currentTarget.value)} autocomplete="new-password"
            placeholder={state()?.secret_configured ? '已配置，留空保留' : '填写飞书应用密钥'} /></label></div>
          <p class="settings-muted">密钥只提交给 TCode 后端，保存后不回显。</p>
          <label>任务工作区<select required value={workspace()} onChange={e => setWorkspace(e.currentTarget.value)}>
            <option value="">选择工作区</option>
            <For each={workspaces()}>{w => <option value={w.workspace_id || w.name}>{w.name}</option>}</For>
          </select></label>
          <details><summary>可选：允许的飞书群</summary>
            <label>群 ID<input value={chats()} onInput={e => setChats(e.currentTarget.value)} placeholder="oc_…，多个用逗号分隔" /></label>
            <p class="settings-muted">留空只使用私聊。群内每次提问或命令都需 @ 机器人；一个话题固定对应一个会话，仅发起者可操作。</p>
          </details>
          <Show when={state()?.configured}><p class="settings-muted">{appId().trim() !== state()?.app_id ? '更换应用会停止旧连接并解除绑定，保存后需绑定新机器人。' : '修改群列表、密钥或工作区会保留身份绑定；更新密钥时自动重连。切换工作区不迁移旧会话，已有任务不会自动取消。'}</p></Show>
          <div class="feishu-form-actions"><button class="settings-save feishu-primary" type="submit">{busy() ? '处理中…' : '保存并验证鉴权'}</button>
          <Show when={state()?.configured}><button class="btn" type="button" onClick={() => {
            const data = state()!; setAppId(data.app_id); setSecret(''); setWorkspace(data.workspace_id); setChats(data.chats.join(', '));
          }}>放弃未保存修改</button></Show></div>
        </fieldset>
      </form>
    </section>
    <section class="settings-section" aria-labelledby="feishu-permissions">
      <div class="feishu-section-heading"><span class="feishu-step" aria-hidden="true">02</span><div><h3 id="feishu-permissions">开通应用权限</h3><p>权限在飞书管理，收发能力由实际测试验证。</p></div><span class="feishu-manual-badge">需手动核对</span></div>
      <p>在 <a href={consoleUrl()} target="_blank" rel="noopener noreferrer">飞书开发者后台</a> 的「权限管理」中开通以下权限。</p>
      <details class="feishu-permission-details"><summary>查看需要开通的权限<Show when={state()}><span class="feishu-count">{state()?.permissions.length} 项</span></Show></summary><ul class="feishu-permissions"><For each={state()?.permissions}>{item => <li><code>{item.scope}</code><span>{item.purpose}</span></li>}</For></ul></details>
      <p class="settings-muted">权限状态需在飞书后台核对。鉴权通过不代表这些权限已开通；收发能力将在绑定和测试任务时验证。</p>
    </section>
    <section class="settings-section" aria-labelledby="feishu-events">
      <div class="feishu-section-heading"><span class="feishu-step" aria-hidden="true">03</span><div><h3 id="feishu-events">启动连接</h3><p>连接 TCode 后，在飞书后台完成事件订阅与发布。</p></div></div>
      <div class="feishu-connection-status" data-enabled={Boolean(state()?.enabled)}><p role="status"><span class="feishu-status-dot" aria-hidden="true"/>{statuses[state()?.status || 'stopped']}<Show when={state()?.last_event_at}><span class="feishu-last-event">最近事件 {new Date(state()!.last_event_at * 1000).toLocaleTimeString()}</span></Show></p></div>
      <Show when={state()?.error}><p role="alert" class="feishu-error">{state()!.error}</p></Show>
      <div class="feishu-actions">
        <button class="btn feishu-primary" disabled={busy() || !state()?.configured || dirty()} onClick={() => void action(async () => {
          setState(await request('/start', 'POST')); setNotice('监听已启动，请继续配置飞书事件订阅。');
        })}>启动／重新连接</button>
        <button class="btn" disabled={busy() || !state()?.enabled} onClick={() => void action(async () => {
          setState(await request('/stop', 'POST')); setNotice('已停止接收和通知；已启动的 TCode 任务仍可在任务页查看。');
        })}>停止连接</button>
      </div>
      <Show when={dirty()}><p class="settings-muted">有未保存的修改，请先保存并验证。</p></Show>
      <details class="feishu-event-guide"><summary>飞书后台配置步骤</summary><ol>
        <li>点击「启动／重新连接」，保持 TCode 服务在线。</li>
        <li>飞书后台「事件与回调」→「事件配置」，选择「使用长连接接收事件」并保存。</li>
        <li>添加「接收消息」事件 <code>im.message.receive_v1</code>。在「回调配置」订阅「卡片回传交互」<code>card.action.trigger</code>，使用长连接，以接收审批按钮操作。</li>
        <li>「版本管理与发布」→ 创建版本，可用范围先选择自己或测试成员，提交发布；若需要审核，请等待管理员通过。</li>
      </ol></details>
      <p class="settings-muted">本地版需要电脑和 TCode 持续运行；云端部署由服务器保持连接。无需配置 OAuth 重定向地址。</p>
    </section>
    <section class="settings-section" aria-labelledby="feishu-binding">
      <div class="feishu-section-heading"><span class="feishu-step" aria-hidden="true">04</span><div><h3 id="feishu-binding">绑定身份并测试</h3><p>只允许已绑定身份发起任务，连接范围由你掌控。</p></div></div>
      <Show when={state()?.bound} fallback={<>
        <ol class="feishu-binding-guide" aria-label="飞书身份绑定步骤">
          <li><strong>切换飞书组织。</strong>打开飞书客户端，在头像或组织切换入口中，选择创建此应用的企业／组织。可在上方「开发者后台」查看应用所属组织；有多个账号或组织时，请先确认当前身份。</li>
          <li><strong>搜索机器人。</strong>在飞书搜索中输入应用的名称（例如你将应用命名为 tcode，就搜索 tcode），打开带有「机器人」标识的私聊会话。</li>
          <li><strong>发送绑定码。</strong>点击下方「生成／刷新绑定码」，复制完整的 <code>/bind …</code> 命令，粘贴到机器人私聊中并发送，不要发到群里。</li>
          <li><strong>确认绑定成功。</strong>保持 TCode 在线，发送后回到本页，等待状态自动变为「身份已绑定」，再发送测试任务。</li>
        </ol>
        <details class="feishu-binding-help"><summary>找不到机器人，或发送后没有绑定成功？</summary>
          <p class="settings-muted">找不到机器人：确认飞书当前组织与应用所属组织一致、应用已启用机器人能力并发布，且「版本管理与发布」中的可用范围包含你。请搜索应用名称，而不是 App ID。</p>
          <p class="settings-muted">未绑定成功：确认第 3 步已启动连接、已订阅接收消息事件，并在正确机器人的私聊中发送完整绑定命令，不要发到群里。绑定码过期后重新生成并发送，以本页「身份已绑定」状态为准。</p>
        </details>
        <p class="settings-muted">绑定码有效期为 10 分钟，请勿转发给他人。刷新绑定码后，请使用最新命令。</p>
        <button class="btn feishu-primary" disabled={busy() || !state()?.enabled || dirty()} onClick={() => void action(async () => {
          setBinding(await request('/binding-code', 'POST'));
        })}>生成／刷新绑定码</button>
        <Show when={binding()}>{value => <div class="feishu-binding-code">
          <Show when={value().expires_at > now()} fallback={<p>绑定码已过期，请重新生成。</p>}>
            <label>复制以下完整命令，私聊发送给机器人<input readOnly value={value().command} onFocus={e => e.currentTarget.select()} /></label>
            <p>有效至 {new Date(value().expires_at * 1000).toLocaleTimeString()}，等待绑定…</p>
          </Show>
        </div>}</Show>
      </>}>
        <p role="status" class="feishu-bound-identity"><span>身份已绑定</span><code>{state()?.sender_id}</code></p>
        <button class="btn" disabled={busy()} onClick={() => void action(async () => {
          setState(await request('/binding', 'DELETE')); setBinding(undefined); setNotice('绑定已解除，连接已停止。');
        })}>解除绑定并停止连接</button>
        <p>给机器人发送「只回复：连接成功」，收到结果后再发送「把刚才的回复翻译成英文」。测试中发送 <code>/stop</code> 可停止当前会话并清空待处理追问。</p>
        <p>私聊发送 <code>/new</code> 新建、<code>/sessions</code> 查看、<code>/use 编号</code> 切换。群主窗口 @机器人 <code>/new 任务描述</code> 新开话题；<code>/sessions</code> 查看自己的话题入口，<code>/use 编号</code> 只返回入口，不改变话题绑定。进入原话题并 @机器人 即可追问。</p>
        <p>发送 <code>/delete 编号</code> 删除会话，机器人会返回有效期 5 分钟的确认命令。运行或排队中的会话需先切换并 <code>/stop</code>。私聊删除当前会话后自动切换，无剩余会话时新建。群话题内可直接 <code>/delete</code>；确认删除后原话题关闭，不自动切换或新建。删除 TCode 会话记录不可撤销，不删除飞书聊天消息及共享工作区文件。</p>
        <p>支持发送图片、PDF、UTF-8 文本和常用代码文件，单文件不超过 10 MB，单条消息最多 8 个附件。附件保存到当前会话工作区；可继续发文字说明处理要求。看图需要模型支持视觉。</p>
        <p class="settings-muted">新任务的异步结果收集后，会自动在原会话解读并通知，无需点击桌面端「确认继续」。会话忙时等待，已续接的结果不重复处理，不自动批准新的计算。/stop 暂停本会话的自动续接；下一次发送任务时恢复。旧任务不会因升级自动重新解读。</p>
        <p class="settings-muted">最终答案中展示的图片或链接的工作区文件会作为附件发回飞书；需要开通文件/图片上传权限并发布应用。群话题中可先发送图片或文件，再 @机器人说明要求；自动关联同话题、本人最近 10 分钟内尚未处理的附件（最近 50 条消息，最多 8 个）。需开通群消息读取权限 im:message.group_msg 并发布应用。</p>
        <p class="settings-muted">每条任务用同一条消息显示提交、排队和执行状态，完成后替换成最终答案；失败或停止也更新这条消息。私聊显示在主窗口，命令保留操作确认。切换不会停止旧任务，旧会话结果会简短注明来源。<code>/stop</code> 只停止发送命令时选中的会话。远程任务使用真实任务记录，在原话题更新同一条状态消息；@机器人 /status 查询，等待审批时点击卡片「批准本次／拒绝」，仅任务发起人可操作，30 分钟有效；按钮不可用时可发送卡片内的完整确认命令。审批状态与 TCode 同步，参数过长或审批失效时回到 TCode 处理。群聊的答案和附件回复在原话题中；话题内 <code>/stop</code> 只停止该话题的任务。新任务请回到群主窗口发起。</p>
        <p class="settings-muted">需要操作授权或回答问题时，请回到 TCode 会话处理。已提交的远程计算不保证随 /stop 取消。</p>
      </Show>
    </section>
    <Show when={state()?.configured}><section class="settings-section feishu-danger-zone" aria-labelledby="feishu-delete">
      <h3 id="feishu-delete">删除接入配置</h3>
      <p>停止连接、删除已保存凭据并解除身份绑定。已有 TCode 会话保留，已经开始的任务不会自动取消。</p>
      <button class="btn feishu-danger" disabled={busy()} onClick={() => void action(async () => {
        if (!await confirmAction('删除后将停止飞书消息接收与结果通知，并清除本接入的凭据和身份绑定。已有 TCode 会话和飞书应用会保留。',
          { title: '删除飞书接入配置？', confirmLabel: '删除配置' })) return;
        setState(await request('', 'DELETE')); setAppId(''); setSecret(''); setChats(''); setBinding(undefined);
        setNotice('飞书接入配置已删除。');
      })}>删除配置</button>
    </section></Show>
  </div>;
}
