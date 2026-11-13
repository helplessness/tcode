import { For, Show, createEffect, createSignal, onCleanup } from 'solid-js';
import { installSkill, resolveSkillSource, type SkillSourcePreview } from '../api/capabilities';

export default function SkillInstallForm(props: {
  workspaceId?: string; onClose: () => void; onInstalled: (name: string) => Promise<void>;
}) {
  const [url, setUrl] = createSignal('');
  const [preview, setPreview] = createSignal<SkillSourcePreview | null>(null);
  const [selected, setSelected] = createSignal('');
  const [busy, setBusy] = createSignal<'resolve' | 'install' | ''>('');
  const [error, setError] = createSignal('');
  const [success, setSuccess] = createSignal('');
  const [direct, setDirect] = createSignal(false);
  let revision = 0;
  let controller: AbortController | undefined;
  createEffect(() => {
    props.workspaceId;
    revision += 1;
    controller?.abort();
    setPreview(null); setSelected(''); setBusy(''); setError(''); setSuccess(''); setDirect(false);
  });
  onCleanup(() => { revision += 1; controller?.abort(); });

  const resolve = async (event: SubmitEvent) => {
    event.preventDefault();
    if (busy() || !url().trim()) return;
    const current = ++revision;
    const workspace = props.workspaceId;
    controller = new AbortController();
    setBusy('resolve'); setError(''); setSuccess(''); setPreview(null);
    try {
      const supplied = new URL(url().trim());
      const parts = supplied.pathname.split('/').filter(Boolean);
      if (supplied.protocol === 'https:' && supplied.host === 'github.com' && parts.length >= 5 && parts[2] === 'tree') {
        // Preserve existing GitHub/private-repository installs without requiring public API access.
        const source = url().trim();
        setDirect(true);
        setPreview({ page_url: source, warnings: [], notice: '这是你提供的 GitHub 目录。确认后将由现有安装器下载并校验，不覆盖已有技能，也不执行包内脚本。私有仓库沿用已有 Git 凭据。',
          candidates: [{ name: decodeURIComponent(parts[parts.length - 1]), description: '安装器将在安装时校验技能清单。',
            repository: parts.slice(0, 2).join('/'), path: parts.slice(4).join('/'), source_url: source, install_path: '', ref: '' }] });
        setSelected(source);
        return;
      }
      setDirect(false);
      const result = await resolveSkillSource(url().trim(), workspace, controller.signal);
      if (current !== revision) return;
      setPreview(result);
      setSelected(result.candidates.length === 1 ? result.candidates[0].source_url : '');
    } catch (err) {
      if (current === revision) setError(err instanceof Error ? err.message : '无法解析技能来源');
    } finally { if (current === revision) setBusy(''); }
  };
  const confirmInstall = async () => {
    const result = preview();
    const candidate = result?.candidates.find(item => item.source_url === selected());
    if (!result || !candidate || busy()) return;
    const current = ++revision;
    const workspace = props.workspaceId;
    setBusy('install'); setError('');
    try {
      const installed = await installSkill(candidate.source_url, workspace, direct() ? undefined : {
        introduction_url: result.page_url, path: candidate.install_path, ref: candidate.ref, confirmed: true,
      });
      if (current !== revision) return;
      setPreview(null); setSuccess(`技能“${installed.name}”已安装到当前工作区。`);
      await props.onInstalled(installed.name);
    } catch (err) {
      if (current === revision) setError(err instanceof Error ? err.message : '技能安装失败');
    } finally { if (current === revision) setBusy(''); }
  };
  const back = () => {
    revision += 1; controller?.abort(); setBusy(''); setPreview(null); setSelected(''); setError('');
  };

  return <section class="max-h-[55vh] shrink-0 overflow-y-auto border-b border-[#2a3136] bg-[#11171b] px-5 py-4" aria-label="从网址安装技能" aria-busy={Boolean(busy())}>
    <Show when={!preview()}>
      <form onSubmit={resolve} class="flex flex-wrap items-end gap-3">
        <div class="min-w-0 flex-1">
          <label class="mb-2 block text-sm text-[#dee3e8]" for="skill-install-url">技能介绍网址或 GitHub 技能目录地址</label>
          <input id="skill-install-url" type="url" required disabled={Boolean(busy())} value={url()}
            onInput={event => setUrl(event.currentTarget.value)} aria-describedby="skill-install-help"
            placeholder="https://…" class="w-full rounded-lg border border-[#344149] bg-[#0c1115] px-3 py-2 text-sm text-[#e6edf3] outline-none focus:border-[#7bd0ff]" />
          <p id="skill-install-help" class="mt-2 text-xs text-[#aab6be]">先查找并验证来源，再由你确认安装。不会执行网页中的命令。</p>
        </div>
        <button type="submit" disabled={Boolean(busy()) || !url().trim()} class="rounded-lg bg-[#486442] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy() === 'resolve' ? '解析来源中…' : '查找来源'}</button>
        <button type="button" disabled={busy() === 'install'} onClick={() => { back(); props.onClose(); }} class="px-3 py-2 text-sm text-[#dee3e8]">取消</button>
      </form>
    </Show>
    <Show when={preview()}>{result => <div>
      <h2 class="text-sm font-semibold text-[#e6edf3]">确认技能来源 · 安装到当前工作区</h2>
      <p class="mt-2 text-xs text-[#bdc8d1]">{result().notice}</p>
      <fieldset disabled={Boolean(busy())} class="mt-3 max-h-64 space-y-2 overflow-y-auto">
        <legend class="mb-2 text-xs text-[#dee3e8]">{result().candidates.length > 1 ? '找到多个候选，请选择要安装的技能' : '已找到技能来源'}</legend>
        <For each={result().candidates}>{candidate => <label class="flex cursor-pointer items-start gap-3 rounded-lg border border-[#344149] p-3">
          <input type="radio" name="skill-source" value={candidate.source_url} checked={selected() === candidate.source_url} onChange={() => setSelected(candidate.source_url)} />
          <span class="min-w-0 text-sm text-[#e6edf3]">
            <span class="block font-semibold">{candidate.name}</span>
            <span class="mt-1 block text-xs text-[#bdc8d1]">{candidate.description}</span>
            <a class="mt-2 block break-words text-xs text-[#8ed5ff] underline" href={candidate.source_url} target="_blank" rel="noopener noreferrer">GitHub 来源：{candidate.repository} / {candidate.path}</a>
            <span class="mt-1 block text-xs text-[#aab6be]">{direct() ? '使用所提供目录的版本' : '已固定到检查时的版本'}</span>
          </span>
        </label>}</For>
      </fieldset>
      <For each={result().warnings}>{warning => <p class="mt-2 text-xs text-[#ffc176]">{warning}</p>}</For>
      <div class="mt-3 flex gap-2">
        <button type="button" disabled={Boolean(busy()) || !selected()} onClick={() => void confirmInstall()} class="rounded-lg bg-[#486442] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy() === 'install' ? '安装中…' : '确认安装'}</button>
        <button type="button" disabled={Boolean(busy())} onClick={back} class="px-3 py-2 text-sm text-[#dee3e8]">返回修改网址</button>
        <button type="button" disabled={Boolean(busy())} onClick={props.onClose} class="px-3 py-2 text-sm text-[#dee3e8]">取消</button>
      </div>
    </div>}</Show>
    <Show when={error()}><p role="alert" class="mt-3 text-sm text-[#ffb3ad]">{error()}</p></Show>
    <Show when={success()}><p role="status" class="mt-3 text-sm text-[#9ee6bb]">{success()}</p></Show>
  </section>;
}
