import { For, Show, createSignal, onMount } from 'solid-js';
import './WebModelSettings.css';

type Result = { ok: boolean; reason: string; tested_at: string };
type Capability = 'text' | 'image';
type Service = {
  id?: string; name: string; protocol: string; base_url: string; models: string[];
  key_saved?: boolean; active?: boolean; results?: Record<string, Partial<Record<Capability, Result>>>;
};
type Settings = {
  services: Service[]; models: { ref: string; label: string; supports_images: boolean }[];
  default_chat: string; default_vision: string; overrides: { model: string; vision_model: string };
};
const endpoint = '/api/config/web-models';
const initialDraft = (): Service => ({ name: '', protocol: 'openai_compatible', base_url: '', models: [] });

export default function WebModelSettings(props: { onChanged: () => void }) {
  const [data, setData] = createSignal<Settings>();
  const [draft, setDraft] = createSignal<Service>();
  const [modelList, setModelList] = createSignal('');
  const [key, setKey] = createSignal('');
  const [clear, setClear] = createSignal(false);
  const [busy, setBusy] = createSignal('');
  const [feedback, setFeedback] = createSignal('');
  const [failure, setFailure] = createSignal(false);
  const [restricted, setRestricted] = createSignal(false);
  const [fileModel, setFileModel] = createSignal('');
  const [fileResults, setFileResults] = createSignal<Record<string, Partial<Record<Capability, Result>>>>({});
  const managedRefs = () => new Set(data()?.services.filter(s => s.active).flatMap(s => s.models.map(m => `web-model-${s.id}/${m}`)) || []);
  const fileModels = () => data()?.models.filter(m => !managedRefs().has(m.ref)) || [];

  async function request<T>(suffix = '', method = 'GET', body?: object): Promise<T> {
    const response = await fetch(endpoint + suffix, {
      method, cache: 'no-store',
      ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) setRestricted(true);
      throw new Error(typeof value.detail === 'string' ? value.detail : '无法读取模型设置，请检查后端连接');
    }
    return value;
  }

  async function run(label: string, action: () => Promise<void>) {
    if (busy()) return;
    setBusy(label);
    setFailure(false);
    setFeedback('');
    try { await action(); }
    catch (error) { setFailure(true); setFeedback(error instanceof Error ? error.message : '操作失败，请重试'); }
    finally { setBusy(''); }
  }
  const load = async () => {
    setData(await request<Settings>());
    if (!fileModel() || !fileModels().some(m => m.ref === fileModel())) setFileModel(fileModels()[0]?.ref || '');
  };
  onMount(() => void run('正在读取模型…', load));

  function edit(service?: Service) {
    setFeedback('');
    setDraft(service || initialDraft());
    setModelList(service?.models.join('\n') || '');
    setKey('');
    setClear(false);
  }
  const patchDraft = (patch: Partial<Service>) => setDraft(value => value && { ...value, ...patch });
  const save = (event: SubmitEvent) => {
    event.preventDefault();
    const value = draft();
    if (!value) return;
    void run('正在保存服务…', async () => {
      setData(await request<Settings>('/services', 'POST', {
        id: value.id, name: value.name, base_url: value.base_url, protocol: value.protocol,
        models: modelList().split(/[\n,，]/).map(m => m.trim()).filter(Boolean),
        ...(key().trim() ? { api_key: key().trim() } : clear() ? { api_key: '' } : {}),
      }));
      setDraft(undefined);
      setKey('');
      setFeedback('服务已保存，可以开始测试。');
      props.onChanged();
    });
  };

  function test(ref: string, purpose: Capability, isFile = false) {
    const label = purpose === 'text' ? '文本' : '图片';
    void run(`正在测试${label}…`, async () => {
      const result = await request<Result>('/test', 'POST', { ref, purpose });
      if (isFile) setFileResults(previous => ({ ...previous, [ref]: { ...previous[ref], [purpose]: result } }));
      else await load();
      setFailure(!result.ok);
      setFeedback(result.ok ? `${label}测试通过。` : `${label}测试失败：${result.reason}`);
      if (result.ok && purpose === 'image') props.onChanged();
    });
  }

  function setDefault(purpose: 'model' | 'vision_model', ref: string) {
    void run('正在保存默认模型…', async () => {
      setData(await request<Settings>('/defaults', 'PUT', { purpose, ref }));
      setFeedback('已保存，后续新会话使用新默认值；已有会话不强制切换。');
      props.onChanged();
    });
  }

  const resultLabel = (result?: Result) => <span class="web-model-test-status" data-result={result ? (result.ok ? 'pass' : 'fail') : 'untested'}>
    {result ? (result.ok ? '通过' : '失败') : '未测试'}
    <Show when={result}><time dateTime={result!.tested_at}>{new Date(result!.tested_at).toLocaleString()}</time></Show>
    <Show when={result && !result.ok}><span>{result!.reason}</span></Show>
  </span>;
  const checks = (ref: string, results: Partial<Record<Capability, Result>> = {}, isFile = false) =>
    <div class="web-model-checks"><For each={['text', 'image'] as const}>{purpose => <div>
      <button type="button" class="btn" disabled={!!busy()} onClick={() => test(ref, purpose, isFile)}>{purpose === 'text' ? '文本测试' : '图片测试'}</button>
      {resultLabel(results[purpose])}
    </div>}</For></div>;

  return <section class="settings-form web-model-settings" aria-labelledby="web-model-heading">
    <div class="settings-title-row">
      <div><h3 id="web-model-heading">Web 模型服务</h3><p>配置实例默认模型。工作区和已有会话的选择仍优先。</p></div>
      <Show when={data() && !restricted()}><button type="button" class="btn" disabled={!!busy() || !!draft()} onClick={() => edit()}>添加聊天服务</button></Show>
    </div>
    <div class="web-model-feedback" data-error={failure()} role="status" aria-live="polite">{busy() || feedback()}</div>
    <Show when={!data() && !restricted() && !busy()}><button type="button" class="btn" onClick={() => void run('正在读取模型…', load)}>重新加载</button></Show>
    <Show when={data() && !restricted()}>
      <div class="web-model-defaults">
        <label>实例默认对话模型
          <select disabled={!!busy()} value={data()?.models.some(m => m.ref === data()?.overrides.model) ? data()?.overrides.model : ''} onChange={e => setDefault('model', e.currentTarget.value)}>
            <option value="">沿用文件配置</option>
            <For each={data()?.models}>{model => <option value={model.ref}>{model.label}</option>}</For>
          </select>
          <small>当前：{data()?.models.find(m => m.ref === data()?.default_chat)?.label || '未设置'}</small>
        </label>
        <label>实例默认图片理解模型
          <select disabled={!!busy()} value={data()?.models.some(m => m.ref === data()?.overrides.vision_model && m.supports_images) ? data()?.overrides.vision_model : ''} onChange={e => setDefault('vision_model', e.currentTarget.value)}>
            <option value="">沿用文件配置或自动选择</option>
            <For each={data()?.models.filter(m => m.supports_images)}>{model => <option value={model.ref}>{model.label}</option>}</For>
          </select>
          <small>当前：{data()?.models.find(m => m.ref === data()?.default_vision)?.label || '未指定'}</small>
        </label>
      </div>
      <p class="settings-muted">图片测试只发送内置测试图；两项测试都可能消耗少量额度，不验证工具调用。</p>
      <Show when={draft()}>{value => <form class="web-model-editor" onSubmit={save}>
        <h4>{value().id ? '编辑聊天服务' : '添加聊天服务'}</h4>
        <fieldset disabled={!!busy()}>
          <label>服务名称<input required maxlength={100} value={value().name} onInput={e => patchDraft({ name: e.currentTarget.value })}/></label>
          <label>接口协议<select value={value().protocol} onChange={e => patchDraft({ protocol: e.currentTarget.value })}>
            <option value="openai_compatible">OpenAI 兼容</option><option value="ollama">Ollama</option><option value="anthropic">Anthropic</option>
          </select></label>
          <label>接口地址<input type="url" required maxlength={1024} value={value().base_url} placeholder="https://provider.example/v1" onInput={e => patchDraft({ base_url: e.currentTarget.value })}/></label>
          <label>模型 ID（每行一个）<textarea required rows={3} maxlength={10000} value={modelList()} onInput={e => setModelList(e.currentTarget.value)}/></label>
          <label>API Key<input type="password" autocomplete="new-password" maxlength={4096} value={key()} onInput={e => setKey(e.currentTarget.value)} placeholder={value().key_saved ? '已保存，留空保留' : '无密钥服务可留空'}/></label>
          <Show when={value().key_saved}><label class="web-model-clear"><input type="checkbox" checked={clear()} onChange={e => setClear(e.currentTarget.checked)}/>清除原密钥</label></Show>
          <p class="settings-muted">密钥仅保存在服务端，不会回显。远程地址须用 HTTPS；回环地址仅在本机模式可用。</p>
          <div class="web-model-actions"><button type="submit" class="btn">保存服务</button><button type="button" class="btn" onClick={() => { setDraft(undefined); setKey(''); }}>取消</button></div>
        </fieldset>
      </form>}</Show>
      <Show when={!data()?.services.length && !draft()}><p class="settings-muted">还没有在网页添加服务。可先添加一个聊天服务，也可以继续使用文件配置。</p></Show>
      <For each={data()?.services}>{service => <div class="web-model-service">
        <div class="web-model-service-heading"><h4>{service.name}</h4><button type="button" class="btn" disabled={!!busy() || !!draft()} onClick={() => edit(service)}>编辑</button></div>
        <p class="settings-muted">{service.base_url} · {service.key_saved ? '密钥已保存' : '无密钥'}</p>
        <Show when={service.active} fallback={<p>同名服务已由文件配置接管。</p>}>
          <For each={service.models}>{model => <div class="web-model-row"><strong>{model}</strong>{checks(`web-model-${service.id}/${model}`, service.results?.[model])}</div>}</For>
        </Show>
      </div>}</For>
      <Show when={fileModels().length}><details class="web-model-service">
        <summary>测试文件中配置的模型</summary>
        <label>文件模型<select value={fileModel()} disabled={!!busy()} onChange={e => setFileModel(e.currentTarget.value)}>
          <For each={fileModels()}>{m => <option value={m.ref}>{m.label}</option>}</For>
        </select></label>
        <Show when={fileModel()}>{checks(fileModel(), fileResults()[fileModel()], true)}</Show>
      </details></Show>
    </Show>
  </section>;
}
