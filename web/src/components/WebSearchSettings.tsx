import { Show, createSignal, onMount } from 'solid-js';

type SearchSettings = {
  provider: string;
  searxng_url: string;
  brave_key_configured: boolean;
};

export default function WebSearchSettings() {
  const endpoint = '/api/config/websearch';
  const [saved, setSaved] = createSignal<SearchSettings>();
  const [provider, setProvider] = createSignal('');
  const [url, setUrl] = createSignal('');
  const [key, setKey] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const [status, setStatus] = createSignal('');
  const [testing, setTesting] = createSignal(false);
  const [testResult, setTestResult] = createSignal<{ ok: boolean; reason: string; tested_at?: string }>();
  let testRevision = 0;
  const dirty = () => provider() !== saved()?.provider ||
    (provider() === 'brave' ? Boolean(key().trim()) : url().trim() !== saved()?.searxng_url);

  function invalidateTest() {
    testRevision++;
    setTestResult(undefined);
  }

  async function testConnection() {
    if (!saved() || busy() || testing() || dirty()) return;
    const revision = testRevision;
    setTesting(true);
    setTestResult(undefined);
    try {
      const response = await fetch(`${endpoint}/test`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const data = await response.json();
      if (revision === testRevision) {
        setTestResult(response.ok ? data : { ok: false, reason: data.detail || '无法测试连接。' });
      }
    } catch {
      if (revision === testRevision) setTestResult({ ok: false, reason: '无法联系 TCode 后端，请重试。' });
    } finally {
      setTesting(false);
    }
  }

  function apply(settings: SearchSettings) {
    setSaved(settings);
    setProvider(settings.provider);
    setUrl(settings.searxng_url);
    setKey('');
  }

  onMount(async () => {
    try {
      const response = await fetch(endpoint, { cache: 'no-store' });
      if (!response.ok) {
        setError(response.status === 403 ? '仅实例管理员可以管理网页搜索。' : '无法读取网页搜索配置。');
        return;
      }
      apply(await response.json());
    } catch {
      setError('无法读取网页搜索配置。');
    }
  });

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!saved() || busy()) return;
    invalidateTest();
    setBusy(true);
    setError('');
    setStatus('');
    try {
      const response = await fetch(endpoint, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(provider() === 'brave'
          ? { provider: 'brave', brave_api_key: key() }
          : { provider: 'searxng', searxng_url: url() }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        setError(typeof data.detail === 'string' ? data.detail : '无法保存网页搜索配置。');
        return;
      }
      apply(await response.json());
      setStatus('已保存，对后续搜索生效。');
    } catch {
      setError('无法保存网页搜索配置。');
    } finally {
      setBusy(false);
    }
  }

  return <div class="settings-page">
    <h2>网页搜索</h2>
    <p>选择 Agent 使用的搜索服务。此设置影响当前实例，搜索失败时不会自动切换服务。</p>
    <Show when={saved()} fallback={<Show when={!error()}><p role="status">正在读取配置…</p></Show>}>
      <form class="settings-form" onSubmit={save}>
        <label>搜索服务
          <select value={provider()} disabled={busy()} onChange={event => {
            invalidateTest();
            setProvider(event.currentTarget.value);
            setKey('');
            setError('');
            setStatus('');
          }}>
            <Show when={!['searxng', 'brave'].includes(provider())}><option value="">请选择搜索服务</option></Show>
            <option value="searxng">SearXNG（默认）</option>
            <option value="brave">Brave Search</option>
          </select>
        </label>
        <Show when={provider() === 'searxng'}>
          <label>实例地址<input type="url" value={url()} disabled={busy()} maxLength={2048}
            placeholder="http://127.0.0.1:8888" onInput={event => { invalidateTest(); setUrl(event.currentTarget.value); }} /></label>
          <p class="settings-muted">实例需允许 JSON 搜索，且能从 TCode 后端访问。留空保留当前地址。</p>
        </Show>
        <Show when={provider() === 'brave'}>
          <label>Brave API Key<input type="password" autocomplete="new-password" value={key()}
            disabled={busy()} maxLength={512} required={!saved()?.brave_key_configured}
            onInput={event => { invalidateTest(); setKey(event.currentTarget.value); }} /></label>
          <p class="settings-muted">{saved()?.brave_key_configured ? '已配置 · 留空保留原 Key。' : '未配置 · 请填写自己的 Key。'}搜索词会发送给 Brave。</p>
        </Show>
        <div><button type="submit" class="settings-save" disabled={busy() || !['searxng', 'brave'].includes(provider())}>{busy() ? '正在保存…' : '保存更改'}</button></div>
        <div><button type="button" class="btn" disabled={busy() || testing() || dirty() || !['searxng', 'brave'].includes(provider())}
          onClick={() => void testConnection()}>{testing() ? '正在测试…' : '测试连接'}</button></div>
        <p class="settings-muted">{dirty() ? '请先保存更改，再测试连接。' : '仅点击时发送一条测试搜索；Brave 可能消耗额度。'}</p>
        <p role="status">
          <span aria-hidden="true" style={{ color: testResult() ? testResult()?.ok ? 'var(--tcode-success)' : 'var(--tcode-danger)' : 'var(--tcode-text-muted)' }}>●</span>
          {' '}{testResult()?.ok ? '搜索可用' : testResult() ? '测试失败' : testing() && !dirty() ? '测试中' : '未验证'}
          <Show when={testResult()?.tested_at}>{timestamp => <> · <time dateTime={timestamp()}>{new Date(timestamp()).toLocaleString()}</time></>}</Show>
          <Show when={testResult()?.reason}>{reason => <> · {reason()}</>}</Show>
        </p>
      </form>
    </Show>
    <Show when={error()}><p role="alert">{error()}</p></Show>
    <Show when={status()}><p role="status">{status()}</p></Show>
  </div>;
}
