import {groupResearchKnowledgeIssues} from '../utils/researchKnowledgeDisplay';
import { For, Show, createMemo, createSignal, onMount } from 'solid-js';

import ResearchDialog from './ResearchDialog';
import {
  buildResearchPack,
  buildResearchPackAssetOverlay,
  buildWorkspaceResearchPack,
  previewResearchPack,
  previewResearchPackAssetOverlay,
  previewWorkspaceResearchPack,
  researchPackDownloadUrl,
  type ResearchPackExport,
  type ResearchPackPreview,
} from '../api/research';
import {
  localDateTimeToIso,
  localDateTimeValue,
  researchPackPeriodBounds,
  researchPackPreviewMetrics,
  type ResearchPackPeriodPreset,
} from '../utils/researchPackUi';

export type ResearchPackShareTarget =
  | { mode: 'project'; researchId: string; title: string }
  | { mode: 'workspace_period'; title: string }
  | { mode: 'asset'; assetId: string; title: string };

type Props = {
  target: ResearchPackShareTarget;
  workspaceId?: string;
  onClose: () => void;
  onBuilt?: (item: ResearchPackExport) => void;
};

const presetOptions: Array<{ key: Exclude<ResearchPackPeriodPreset, 'custom'>; label: string }> = [
  { key: '7', label: '最近 7 天' },
  { key: '30', label: '最近 30 天' },
  { key: '90', label: '最近 90 天' },
];

const formatBytes = (value?: number) => {
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

export default function ResearchPackShareDialog(props: Props) {
  const initialBounds = researchPackPeriodBounds('30');
  const [preset, setPreset] = createSignal<ResearchPackPeriodPreset>('30');
  const [customStartedAt, setCustomStartedAt] = createSignal(localDateTimeValue(initialBounds.startedAt));
  const [customEndedAt, setCustomEndedAt] = createSignal(localDateTimeValue(initialBounds.endedAt));
  const [privacyProfile, setPrivacyProfile] = createSignal('standard');
  const [preview, setPreview] = createSignal<ResearchPackPreview | null>(null);
  const [previewRange, setPreviewRange] = createSignal(initialBounds);
  const [exported, setExported] = createSignal<ResearchPackExport | null>(null);
  const [busy, setBusy] = createSignal<'preview' | 'build' | ''>('');
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  let previewSequence = 0;

  const metrics = createMemo(() => {
    const value = preview();
    return value ? researchPackPreviewMetrics(value) : null;
  });

  const rangeFor = (selected: ResearchPackPeriodPreset = preset()) => {
    if (selected === 'custom') {
      return {
        startedAt: localDateTimeToIso(customStartedAt()),
        endedAt: localDateTimeToIso(customEndedAt()),
      };
    }
    return researchPackPeriodBounds(selected);
  };

  const refreshPreview = async (
    selected: ResearchPackPeriodPreset = preset(),
    profile = privacyProfile(),
  ) => {
    const sequence = ++previewSequence;
    const range = rangeFor(selected);
    if (props.target.mode === 'workspace_period' && (!range.startedAt || !range.endedAt)) {
      setError('请选择有效的开始和结束时间');
      return;
    }
    if (
      props.target.mode === 'workspace_period'
      && Date.parse(range.startedAt) >= Date.parse(range.endedAt)
    ) {
      setError('结束时间必须晚于开始时间');
      return;
    }
    setBusy('preview');
    setError('');
    setNotice('');
    setExported(null);
    try {
      const result = props.target.mode === 'project'
        ? await previewResearchPack(props.target.researchId, profile, props.workspaceId)
        : props.target.mode === 'asset'
          ? await previewResearchPackAssetOverlay(props.target.assetId, profile, props.workspaceId)
          : await previewWorkspaceResearchPack(range.startedAt, range.endedAt, profile, props.workspaceId);
      if (sequence !== previewSequence) return;
      setPreview(result.preview);
      setPreviewRange(range);
    } catch (reason) {
      if (sequence !== previewSequence) return;
      setPreview(null);
      setError(reason instanceof Error ? reason.message : '无法生成分享预览');
    } finally {
      if (sequence === previewSequence) setBusy('');
    }
  };

  const choosePreset = (value: ResearchPackPeriodPreset) => {
    setPreset(value);
    if (value !== 'custom') void refreshPreview(value);
  };

  const choosePrivacy = (value: string) => {
    setPrivacyProfile(value);
    void refreshPreview(preset(), value);
  };

  const build = async () => {
    const current = preview();
    if (!current || !current.export_ready || busy()) return;
    // The digest includes the time scope. Reuse the approved preview's exact bounds.
    const range = previewRange();
    setBusy('build');
    setError('');
    setNotice('');
    try {
      const result = props.target.mode === 'project'
        ? await buildResearchPack(
          props.target.researchId,
          {
            privacy_profile: privacyProfile(),
            expected_source_digest: current.source_digest,
            acknowledge_privacy: true,
          },
          props.workspaceId,
        )
        : props.target.mode === 'asset'
          ? await buildResearchPackAssetOverlay(
            props.target.assetId,
            {
              privacy_profile: privacyProfile(),
              expected_source_digest: current.source_digest,
              acknowledge_privacy: true,
            },
            props.workspaceId,
          )
          : await buildWorkspaceResearchPack(
            {
              started_at: range.startedAt,
              ended_at: range.endedAt,
              privacy_profile: privacyProfile(),
              expected_source_digest: current.source_digest,
              acknowledge_privacy: true,
            },
            props.workspaceId,
          );
      setExported(result.export);
      setNotice('研究资料包已生成。下载的是安全、可校验的一次性副本。');
      props.onBuilt?.(result.export);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '生成研究资料包失败';
      if (/changed|变化|source digest/i.test(message)) {
        await refreshPreview();
        setNotice('研究记录在确认期间发生了变化，预览已刷新，请核对后再次确认。');
      } else {
        setError(message);
      }
    } finally {
      setBusy('');
    }
  };

  onMount(() => {
    void refreshPreview();
  });

  return (
    <ResearchDialog title="分享研究资料" busy={Boolean(busy())} onClose={props.onClose}>
      <section
        class="mx-auto w-full max-w-3xl rounded-2xl border border-[#34404a] bg-[#11171b] shadow-2xl"
      >
        <header class="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-[#293238] bg-[#11171b]/95 px-5 py-4 backdrop-blur">
          <div>
            <div class="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#38bdf8]">研究资料包 · 第二版格式</div>
            <h2 id="research-pack-share-title" class="mt-1 text-lg font-semibold text-[#edf2f7]">
              {props.target.mode === 'asset' ? '再次导出研究资料' : props.target.mode === 'project' ? '分享这项研究' : '分享一段研究经历'}
            </h2>
            <p class="mt-1 text-xs text-[#8b99a4]">{props.target.title}</p>
          </div>
        </header>

        <div class="space-y-4 p-5">
          <Show when={props.target.mode === 'workspace_period'}>
            <section class="rounded-xl border border-[#293238] bg-[#0d1216] p-4">
              <div class="text-xs font-semibold text-[#bdc8d1]">时间范围</div>
              <div class="mt-3 flex flex-wrap gap-2">
                <For each={presetOptions}>
                  {(item) => (
                    <button
                      class={`rounded-full border px-3 py-1.5 text-xs ${preset() === item.key ? 'border-[#38bdf8]/45 bg-[#38bdf8]/12 text-[#8ed5ff]' : 'border-[#30383e] text-[#8b99a4]'}`}
                      aria-pressed={preset() === item.key}
                      disabled={Boolean(busy())}
                      onClick={() => choosePreset(item.key)}
                    >
                      {item.label}
                    </button>
                  )}
                </For>
                <button
                  class={`rounded-full border px-3 py-1.5 text-xs ${preset() === 'custom' ? 'border-[#38bdf8]/45 bg-[#38bdf8]/12 text-[#8ed5ff]' : 'border-[#30383e] text-[#8b99a4]'}`}
                  aria-pressed={preset() === 'custom'}
                  disabled={Boolean(busy())}
                  onClick={() => choosePreset('custom')}
                >
                  自定义
                </button>
              </div>
              <Show when={preset() === 'custom'}>
                <div class="mt-3 grid gap-3 sm:grid-cols-2">
                  <label class="text-xs text-[#8b99a4]">
                    开始时间
                    <input
                      class="mt-1 h-9 w-full rounded-lg border border-[#30383e] bg-[#11171b] px-3 text-[#d7dee4]"
                      type="datetime-local"
                      value={customStartedAt()}
                      onInput={(event) => setCustomStartedAt(event.currentTarget.value)}
                    />
                  </label>
                  <label class="text-xs text-[#8b99a4]">
                    结束时间
                    <input
                      class="mt-1 h-9 w-full rounded-lg border border-[#30383e] bg-[#11171b] px-3 text-[#d7dee4]"
                      type="datetime-local"
                      value={customEndedAt()}
                      onInput={(event) => setCustomEndedAt(event.currentTarget.value)}
                    />
                  </label>
                </div>
                <button
                  class="mt-3 rounded-lg border border-[#38bdf8]/30 px-3 py-1.5 text-xs text-[#8ed5ff] disabled:opacity-40"
                  disabled={Boolean(busy())}
                  onClick={() => void refreshPreview('custom')}
                >
                  更新预览
                </button>
              </Show>
            </section>
          </Show>

          <details class="rounded-xl border border-[#293238] bg-[#0d1216] px-4 py-3">
            <summary class="cursor-pointer text-xs font-semibold text-[#9faeb7]">高级隐私设置</summary>
            <div class="mt-3 grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label="隐私级别">
              <For each={[
                { key: 'strict', label: '严格', detail: '不包含原始研究活动正文' },
                { key: 'standard', label: '标准（默认）', detail: '保留必要研究上下文' },
                { key: 'full-research', label: '完整研究', detail: '仍隐藏账号密钥和本机文件位置' },
              ]}>
                {(item) => (
                  <button
                    class={`rounded-lg border p-3 text-left ${privacyProfile() === item.key ? 'border-[#c4a7ff]/45 bg-[#c4a7ff]/10' : 'border-[#30383e]'}`}
                    role="radio"
                    aria-checked={privacyProfile() === item.key}
                    disabled={Boolean(busy())}
                    onClick={() => choosePrivacy(item.key)}
                  >
                    <div class="text-xs font-semibold text-[#d7dee4]">{item.label}</div>
                    <div class="mt-1 text-[10px] leading-4 text-[#71808a]">{item.detail}</div>
                  </button>
                )}
              </For>
            </div>
          </details>

          <Show when={busy() === 'preview'}>
            <div class="rounded-xl border border-[#38bdf8]/25 bg-[#38bdf8]/8 px-4 py-5 text-center text-sm text-[#8ed5ff]" aria-live="polite">
              正在核对可分享内容与隐私边界…
            </div>
          </Show>

          <Show when={error()}>
            <div class="rounded-xl border border-[#ef6b73]/35 bg-[#ef6b73]/10 px-4 py-3 text-sm text-[#fca5a5]" role="alert">{error()}</div>
          </Show>
          <Show when={notice()}>
            <div class="rounded-xl border border-[#38b46a]/30 bg-[#38b46a]/10 px-4 py-3 text-sm text-[#9ee6bb]" aria-live="polite">{notice()}</div>
          </Show>

          <Show when={busy() !== 'preview' ? preview() : null} keyed>
            {(item) => (
              <section class="rounded-xl border border-[#2f3940] bg-[#0d1216] p-4">
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div class="text-xs font-semibold text-[#d7dee4]">将分享</div>
                    <div class="mt-1 text-[11px] text-[#71808a]">预计 {formatBytes(item.estimated_bytes)} · 不会发布到外部服务</div>
                  </div>
                  <span class={`rounded-full border px-2.5 py-1 text-[10px] ${item.export_ready ? 'border-[#38b46a]/30 text-[#9ee6bb]' : 'border-[#ef6b73]/35 text-[#fca5a5]'}`}>
                    {item.export_ready ? '可以生成' : '当前被阻断'}
                  </span>
                </div>
                <Show when={metrics()} keyed>
                  {(value) => (
                    <div class="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <For each={[
                        ['来源项目', value.researches],
                        ['已保存研究经历', value.experiences],
                        ['待归属研究经历', value.unassigned],
                        ['产物', value.artifacts],
                        ['证据', value.evidence],
                        ['辅助研究记录', value.supporting],
                        ['排除临时记录', value.transientExcluded],
                        ['隐私排除项', value.privacyExcluded],
                      ] as Array<[string, number]>}>
                        {([label, count]) => (
                          <div class="rounded-lg border border-[#293238] bg-[#11171b] px-3 py-2">
                            <div class="text-[10px] text-[#71808a]">{label}</div>
                            <div class="mt-1 text-base font-semibold text-[#d7dee4]">{count}</div>
                          </div>
                        )}
                      </For>
                    </div>
                  )}
                </Show>
                <Show when={(item.issues || []).length > 0}>
                  <div class="mt-4 space-y-2">
                    <For each={groupResearchKnowledgeIssues(item.issues)}>
                      {(issue) => (
                        <div class={`rounded-lg border px-3 py-2 text-xs ${issue.severity === 'error' ? 'border-[#ef6b73]/30 bg-[#ef6b73]/8 text-[#fca5a5]' : 'border-[#f1a02b]/25 bg-[#f1a02b]/8 text-[#ffc176]'}`}>
                          <strong>{issue.title}{issue.count>1?`（${issue.count} 项）`:""}</strong><p class="mt-2 leading-5">{issue.description}</p><p class="mt-2 leading-5">{issue.action}</p>
                        </div>
                      )}
                    </For>
                  </div>
                </Show>
              </section>
            )}
          </Show>

          <Show when={exported()} keyed>
            {(item) => (
              <section class="rounded-xl border border-[#38b46a]/35 bg-[#38b46a]/8 p-4">
                <div class="text-sm font-semibold text-[#b8efca]">分享副本已准备好</div>
                <div class="mt-1 text-xs text-[#78a98a]">研究资料包可下载</div>
                <a
                  class="mt-3 inline-flex rounded-lg border border-[#38b46a]/40 bg-[#38b46a]/12 px-3 py-2 text-xs font-semibold text-[#b8efca]"
                  href={researchPackDownloadUrl(item.pack_id, props.workspaceId)}
                  download={item.archive_name}
                >
                  下载研究资料包
                </a>
              </section>
            )}
          </Show>
        </div>

        <footer class="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-[#293238] bg-[#11171b]/95 px-5 py-4 backdrop-blur">
          <p class="max-w-xl text-[11px] leading-5 text-[#71808a]">
            生成操作只创建一次性分享副本；原始研究记录、收到的研究包和本地接力记录都不会被修改。
          </p>
          <Show when={!exported()}>
            <button
              class="rounded-lg border border-[#38b46a]/40 bg-[#38b46a]/12 px-4 py-2 text-sm font-semibold text-[#b8efca] disabled:cursor-not-allowed disabled:opacity-40"
              disabled={!preview()?.export_ready || Boolean(busy())}
              onClick={() => void build()}
            >
              {busy() === 'build' ? '正在生成…' : '确认生成研究资料包'}
            </button>
          </Show>
        </footer>
      </section>
    </ResearchDialog>
  );
}
