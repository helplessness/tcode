import ResearchRecordDetails from './ResearchRecordDetails';
import { For, Show, createMemo, createSignal } from 'solid-js';

import {
  buildResearchPack,
  confirmResearchCandidate,
  previewResearchPack,
  researchPackDownloadUrl,
  type ResearchPackExport,
} from '../api/research';
import { sendMessage } from '../api/client';
import { sessionStore } from '../stores/session';
import {
  researchSourceCategoryLabel,
  type ResearchActionPayload,
} from '../utils/researchAction';


const formatBytes = (value?: number) => {
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

export default function ResearchActionCard(props: { action: ResearchActionPayload }) {
  const [busy, setBusy] = createSignal(false);
  const [cancelled, setCancelled] = createSignal(false);
  const [notice, setNotice] = createSignal('');
  const [error, setError] = createSignal('');
  const [confirmedResearchId, setConfirmedResearchId] = createSignal(
    String(props.action.confirmed_research_id || '').trim(),
  );
  const [exported, setExported] = createSignal<ResearchPackExport | null>(null);
  const [preview, setPreview] = createSignal(props.action.preview || null);
  const workspaceId = createMemo(() => String(sessionStore.currentSession()?.workspace_id || '').trim());
  const supportRows = createMemo(() => Object.entries(props.action.support_counts || {})
    .filter(([, count]) => Number(count || 0) > 0));
  const packCountRows = createMemo(() => {
    const counts = preview()?.counts || {};
    const labels: Record<string, string> = {
      researches: '课题',
      activities: '研究活动',
      experience_items: '研究经验',
      artifacts: '研究产物',
      evidence_records: 'Evidence',
      papers: '论文',
      wiki_pages: 'Wiki 页面',
      findings: '研究结论',
      episodes: '研究阶段',
    };
    return Object.entries(labels)
      .map(([key, label]) => ({ label, count: Number(counts[key] || 0) }))
      .filter((item) => item.count > 0)
      .slice(0, 8);
  });
  const privacyExcludedCount = createMemo(() => Object.values(preview()?.exclusions || {})
    .reduce((total, value) => total + Math.max(0, Number(value || 0)), 0));
  const candidateAction = createMemo(() => [
    'candidate_confirmation',
    'candidate_rename',
    'candidate_dismissal',
  ].includes(props.action.kind));
  const actionTypeLabel = createMemo(() => ({
    candidate_confirmation: '正式课题确认',
    candidate_rename: '候选课题重命名',
    candidate_dismissal: '忽略候选课题',
    pack_build: 'ResearchPack 生成确认',
    pack_download: 'ResearchPack 下载',
    research_step: '研究路线操作确认',
  }[props.action.kind] || '研究操作'));
  const statusTone = createMemo(() => (
    props.action.status === 'blocked'
      ? 'border-[#ef6b73]/35 bg-[#ef6b73]/8 text-[#fca5a5]'
      : props.action.status === 'cancelled' || cancelled()
        ? 'border-[#49545b] bg-[#171d21] text-[#aebbc3]'
      : props.action.status === 'completed' || confirmedResearchId() || exported()
        ? 'border-[#38b46a]/35 bg-[#38b46a]/8 text-[#b8efca]'
        : 'border-[#38bdf8]/30 bg-[#38bdf8]/8 text-[#8ed5ff]'
  ));

  const submitNaturalDecision = async (decision: 'confirm' | 'cancel') => {
    const actionId = String(props.action.action_id || '').trim();
    if (!actionId || busy()) return false;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const title = String(props.action.title || '刚才').trim();
      const visible = decision === 'confirm'
        ? `确认执行“${title}”这项研究操作`
        : `取消“${title}”这项研究操作`;
      await sendMessage(`${visible}\n<!-- tcode-research-action:${actionId} -->`);
      setNotice(decision === 'confirm' ? '已发送确认，服务端正在复核后执行。' : '已发送取消。');
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发送研究操作决定失败');
      return true;
    } finally {
      setBusy(false);
    }
  };

  const confirmCandidate = async () => {
    const candidateId = String(props.action.candidate_id || '').trim();
    if (!candidateId || busy() || confirmedResearchId()) return;
    if (await submitNaturalDecision('confirm')) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await confirmResearchCandidate(
        candidateId,
        workspaceId(),
        String(props.action.candidate_digest || ''),
      );
      setConfirmedResearchId(result.project.research_id);
      setNotice('候选已确认为正式课题，来源记录和审计历史均已保留。');
      sessionStore.setStatus(`已确认正式课题：${result.project.title}`);
      window.dispatchEvent(new CustomEvent('tcode-research-changed', {
        detail: { workspaceId: workspaceId(), researchId: result.project.research_id },
      }));
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '确认正式课题失败';
      setError(/changed since preview/i.test(message)
        ? '候选课题在确认前发生了变化，请重新询问 Agent 后再确认。'
        : message);
    } finally {
      setBusy(false);
    }
  };

  const buildPack = async () => {
    const researchId = String(props.action.research_id || '').trim();
    const current = preview();
    if (!researchId || !current?.export_ready || !current.source_digest || busy()) return;
    if (await submitNaturalDecision('confirm')) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await buildResearchPack(
        researchId,
        {
          privacy_profile: String(props.action.privacy_profile || 'strict'),
          expected_source_digest: current.source_digest,
          acknowledge_privacy: true,
        },
        workspaceId(),
      );
      setExported(result.export);
      setNotice('ResearchPack 已生成，可以下载一次性副本。');
      sessionStore.setStatus(`ResearchPack 已生成：${result.export.archive_name}`);
      window.dispatchEvent(new CustomEvent('tcode-research-changed', {
        detail: { workspaceId: workspaceId(), researchId, packId: result.export.pack_id },
      }));
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '生成 ResearchPack 失败';
      if (/changed|变化|source digest/i.test(message)) {
        try {
          const refreshed = await previewResearchPack(
            researchId,
            String(props.action.privacy_profile || 'strict'),
            workspaceId(),
          );
          setPreview(refreshed.preview);
          setNotice('研究内容在确认期间发生了变化，预览已刷新，请核对后再次确认。');
        } catch (refreshReason) {
          setError(refreshReason instanceof Error ? refreshReason.message : message);
        }
      } else {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  };

  const downloadPack = createMemo(() => {
    const generated = exported();
    if (generated) {
      return {
        packId: generated.pack_id,
        archiveName: generated.archive_name,
        archiveSize: generated.archive_size,
      };
    }
    if (props.action.kind !== 'pack_download') return null;
    return {
      packId: String(props.action.pack_id || ''),
      archiveName: String(props.action.archive_name || 'ResearchPack.researchpack.zip'),
      archiveSize: Number(props.action.archive_size || 0),
    };
  });

  return (
    <div class="border-t border-[#2a3136] px-4 py-3">
      <div class={`rounded-xl border p-4 ${statusTone()}`}>
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="text-[10px] font-semibold uppercase tracking-[0.16em] opacity-75">
              {actionTypeLabel()}
            </div>
            <div class="mt-1 text-sm font-semibold text-[#eef6fa]">{props.action.title}</div>
            <Show when={props.action.message}>
              <div class="mt-1 text-xs leading-5 opacity-90">{props.action.message}</div>
            </Show>
          </div>
          <span class="rounded-full border border-current/25 px-2.5 py-1 text-[10px] font-semibold">
            {props.action.status === 'blocked'
              ? '当前被阻断'
              : props.action.status === 'cancelled' || cancelled()
                ? '已取消'
              : props.action.kind === 'pack_download'
                ? '可以下载'
              : props.action.status === 'completed' || confirmedResearchId() || exported()
                ? '已完成'
                : '等待确认'}
          </span>
        </div>

        <Show when={props.action.kind === 'research_step'}>
          <ResearchRecordDetails value={props.action.details||{}}/>
          <Show when={props.action.result}><p class="mt-3 text-xs">操作结果已保存。本次未启动计算。</p></Show>
        </Show>

        <Show when={candidateAction()}>
          <Show when={props.action.kind === 'candidate_rename'}>
            <div class="mt-3 grid gap-2 text-xs sm:grid-cols-[1fr_auto_1fr] sm:items-center">
              <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
                <div class="text-[10px] text-[#71808a]">当前名称</div>
                <div class="mt-1 text-[#dce8ed]">{props.action.current_title || props.action.candidate?.title || '未命名候选'}</div>
              </div>
              <div class="text-center text-[#71808a]">→</div>
              <div class="rounded-lg border border-[#38bdf8]/30 bg-[#0d1a20]/70 px-3 py-2">
                <div class="text-[10px] text-[#71808a]">新名称</div>
                <div class="mt-1 font-semibold text-[#8ed5ff]">{props.action.proposed_title}</div>
              </div>
            </div>
          </Show>
          <div class="mt-3 grid gap-2 text-[11px] sm:grid-cols-3">
            <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
              <div class="text-[#71808a]">可读依据</div>
              <div class="mt-1 text-sm font-semibold text-[#dce8ed]">{Number(props.action.readable_support_count || 0)}</div>
            </div>
            <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
              <div class="text-[#71808a]">稳定度</div>
              <div class="mt-1 text-sm font-semibold text-[#dce8ed]">
                {Math.round(Number(props.action.candidate?.stability_score || 0) * 100)}%
              </div>
            </div>
            <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
              <div class="text-[#71808a]">技术记录</div>
              <div class="mt-1 text-sm font-semibold text-[#dce8ed]">已折叠 {Number(props.action.technical_record_count || 0)}</div>
            </div>
          </div>
          <Show when={supportRows().length > 0}>
            <div class="mt-3 flex flex-wrap gap-2">
              <For each={supportRows()}>
                {([category, count]) => (
                  <span class="rounded-full border border-[#3b474e] bg-[#11181c] px-2.5 py-1 text-[10px] text-[#b7c7d0]">
                    {researchSourceCategoryLabel(category)} {Number(count)}
                  </span>
                )}
              </For>
            </div>
          </Show>
          <Show when={(props.action.representative_sources || []).length > 0}>
            <div class="mt-3 space-y-2">
              <For each={(props.action.representative_sources || []).slice(0, 3)}>
                {(source) => (
                  <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2 text-xs text-[#b7c7d0]">
                    <div class="font-semibold text-[#dce8ed]">{source.title || researchSourceCategoryLabel(source.category || '')}</div>
                    <div class="mt-1 line-clamp-2 leading-5">{source.summary || '已纳入候选支持记录'}</div>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </Show>

        <Show when={props.action.kind === 'pack_build' && preview()}>
          {(current) => (
            <>
              <div class="mt-3 grid gap-2 text-[11px] sm:grid-cols-3">
                <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
                  <div class="text-[#71808a]">预计大小</div>
                  <div class="mt-1 text-sm font-semibold text-[#dce8ed]">{formatBytes(current().estimated_bytes)}</div>
                </div>
                <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
                  <div class="text-[#71808a]">隐私模式</div>
                  <div class="mt-1 text-sm font-semibold text-[#dce8ed]">{props.action.privacy_profile || 'strict'}</div>
                </div>
                <div class="rounded-lg border border-[#334047] bg-[#10171b]/70 px-3 py-2">
                  <div class="text-[#71808a]">状态</div>
                  <div class="mt-1 text-sm font-semibold text-[#dce8ed]">{current().export_ready ? '可以生成' : '存在阻断项'}</div>
                </div>
              </div>
              <Show when={packCountRows().length > 0 || privacyExcludedCount() > 0}>
                <div class="mt-3 flex flex-wrap gap-2">
                  <For each={packCountRows()}>
                    {(item) => (
                      <span class="rounded-full border border-[#3b474e] bg-[#11181c] px-2.5 py-1 text-[10px] text-[#b7c7d0]">
                        {item.label} {item.count}
                      </span>
                    )}
                  </For>
                  <Show when={privacyExcludedCount() > 0}>
                    <span class="rounded-full border border-[#f1a02b]/30 bg-[#f1a02b]/8 px-2.5 py-1 text-[10px] text-[#ffc176]">
                      隐私排除 {privacyExcludedCount()}
                    </span>
                  </Show>
                </div>
              </Show>
              <Show when={(current().issues || []).length > 0}>
                <div class="mt-3 space-y-2">
                  <For each={(current().issues || []).slice(0, 5)}>
                    {(issue) => (
                      <div class="rounded-lg border border-[#ef6b73]/25 bg-[#2a1618]/55 px-3 py-2 text-xs text-[#fca5a5]">
                        {issue.message || issue.code || '研究包预览未通过'}
                      </div>
                    )}
                  </For>
                </div>
              </Show>
            </>
          )}
        </Show>

        <Show when={notice()}><div class="mt-3 text-xs text-[#9ee6bb]">{notice()}</div></Show>
        <Show when={error()}><div class="mt-3 text-xs text-[#fca5a5]">{error()}</div></Show>
        <Show when={props.action.status === 'ready' && props.action.action_id && props.action.confirmation_hint}>
          <div class="mt-3 rounded-lg border border-[#38bdf8]/20 bg-[#0d1a20]/65 px-3 py-2 text-xs leading-5 text-[#9fc8dc]">
            {props.action.confirmation_hint}
          </div>
        </Show>

        <div class="mt-4 flex flex-wrap gap-2">
          <Show when={props.action.kind === 'research_step' && props.action.status === 'ready' && props.action.action_id}>
            <button type="button" class="rounded-lg border border-[#38b46a]/40 px-3 py-2 text-xs" disabled={busy()} onClick={() => void submitNaturalDecision('confirm')}>{busy() ? '正在提交…' : '确认此研究操作（不启动计算）'}</button>
          </Show>
          <Show when={props.action.kind === 'candidate_confirmation' && props.action.status === 'ready' && !confirmedResearchId() && !cancelled()}>
            <button
              type="button"
              class="rounded-lg border border-[#38b46a]/40 bg-[#38b46a]/14 px-3 py-2 text-xs font-semibold text-[#b8efca] disabled:opacity-45"
              disabled={busy()}
              onClick={() => void confirmCandidate()}
            >
              {busy() ? '正在确认…' : '确认设为正式课题'}
            </button>
          </Show>
          <Show when={props.action.kind === 'pack_build' && props.action.status === 'ready' && preview()?.export_ready && !exported() && !cancelled()}>
            <button
              type="button"
              class="rounded-lg border border-[#38b46a]/40 bg-[#38b46a]/14 px-3 py-2 text-xs font-semibold text-[#b8efca] disabled:opacity-45"
              disabled={busy()}
              onClick={() => void buildPack()}
            >
              {busy() ? '正在生成…' : '确认生成 ResearchPack'}
            </button>
          </Show>
          <Show when={['candidate_rename', 'candidate_dismissal'].includes(props.action.kind) && props.action.status === 'ready' && !cancelled()}>
            <button
              type="button"
              class={`rounded-lg border px-3 py-2 text-xs font-semibold disabled:opacity-45 ${props.action.kind === 'candidate_dismissal' ? 'border-[#ef6b73]/40 bg-[#ef6b73]/12 text-[#fca5a5]' : 'border-[#38b46a]/40 bg-[#38b46a]/14 text-[#b8efca]'}`}
              disabled={busy()}
              onClick={() => void submitNaturalDecision('confirm')}
            >
              {busy()
                ? '正在提交…'
                : props.action.kind === 'candidate_dismissal'
                  ? '确认忽略候选'
                  : '确认重命名'}
            </button>
          </Show>
          <Show when={props.action.status === 'ready' && props.action.kind !== 'pack_download' && !confirmedResearchId() && !exported() && !cancelled()}>
            <button
              type="button"
              class="rounded-lg border border-[#49545b] bg-[#171d21] px-3 py-2 text-xs font-semibold text-[#aebbc3] disabled:opacity-45"
              disabled={busy()}
              onClick={() => void (async () => {
                if (await submitNaturalDecision('cancel')) return;
                setCancelled(true);
                setError('');
                setNotice('已取消，本次没有修改研究数据。');
              })()}
            >
              取消
            </button>
          </Show>
          <Show when={downloadPack()} keyed>
            {(item) => (
              <a
                class="tcode-download-button px-3 py-2 text-xs"
                href={researchPackDownloadUrl(item.packId, workspaceId())}
                download={item.archiveName}
              >
                下载 ResearchPack{item.archiveSize ? ` · ${formatBytes(item.archiveSize)}` : ''}
              </a>
            )}
          </Show>
        </div>
      </div>
    </div>
  );
}
