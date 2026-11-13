import { For, Show, createMemo, createSignal, onMount } from 'solid-js';

import {
  approveWikiProposal,
  getWikiProposal,
} from '../api/paperWiki';
import { sessionStore } from '../stores/session';
import {
  wikiActionOperationLabel,
  type WikiActionPayload,
} from '../utils/wikiAction';


const proposalStatusLabel = (status: string) => ({
  pending_review: '等待确认',
  applied: '已应用',
  approved: '已批准',
  rejected: '已驳回',
  superseded: '已被替代',
}[String(status || '').trim()] || status || '未知');

export default function WikiActionCard(props: { action: WikiActionPayload }) {
  const [busy, setBusy] = createSignal(false);
  const [status, setStatus] = createSignal(String(props.action.proposal_status || 'pending_review'));
  const [notice, setNotice] = createSignal('');
  const [error, setError] = createSignal('');
  const workspaceId = createMemo(() => (
    String(props.action.workspace_id || sessionStore.currentSession()?.workspace_id || '').trim()
  ));
  const directConfirm = createMemo(() => (
    props.action.direct_confirm_allowed
    && props.action.operations.every((item) => ['archive_page', 'restore_page'].includes(item.type))
  ));
  const pending = createMemo(() => status() === 'pending_review');

  const refreshProposal = async () => {
    try {
      const current = await getWikiProposal(props.action.proposal_id, workspaceId());
      setStatus(String(current.status || ''));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取 Wiki Proposal');
    }
  };

  onMount(() => {
    void refreshProposal();
  });

  const openReview = () => {
    window.dispatchEvent(new CustomEvent('tcode-open-paper-wiki', {
      detail: {
        workspaceId: workspaceId(),
        proposalId: props.action.proposal_id,
      },
    }));
  };

  const confirmApply = async () => {
    if (!directConfirm() || !pending() || busy()) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const current = await getWikiProposal(props.action.proposal_id, workspaceId());
      if (current.status !== 'pending_review') {
        setStatus(current.status);
        setNotice(`Proposal 当前状态为“${proposalStatusLabel(current.status)}”，没有重复执行。`);
        return;
      }
      if (current.proposal_hash !== props.action.proposal_hash) {
        setStatus(current.status);
        setError('Proposal 在展示后发生了变化。请打开 Wiki 审核页重新核对，当前卡片不会应用旧内容。');
        return;
      }
      const applied = await approveWikiProposal(
        current,
        workspaceId(),
        '用户在自然语言 Wiki 操作卡片中确认',
      );
      setStatus(applied.status);
      setNotice('Wiki 变更已应用；服务端已重新校验 Proposal Hash 和页面 Revision。');
      sessionStore.setStatus('Wiki 变更已应用');
      window.dispatchEvent(new CustomEvent('tcode-wiki-changed', {
        detail: {
          workspaceId: workspaceId(),
          proposalId: props.action.proposal_id,
          pageIds: props.action.operations.map((item) => item.page_id).filter(Boolean),
        },
      }));
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '应用 Wiki Proposal 失败';
      setError(/changed|stale|revision|hash|409/i.test(message)
        ? '页面或 Proposal 已被其他操作更新。请打开 Wiki 审核页刷新后重新确认。'
        : message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="mt-3 overflow-hidden rounded-2xl border border-[#a78bfa]/35 bg-[#171326] shadow-[0_14px_34px_rgba(0,0,0,0.26)]">
      <div class="p-4">
        <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded-full border border-[#a78bfa]/35 bg-[#a78bfa]/12 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-[#c4b5fd]">Wiki Proposal</span>
              <span class={`rounded-full border px-2.5 py-1 text-[10px] ${pending() ? 'border-[#f1a02b]/35 bg-[#f1a02b]/10 text-[#ffc176]' : status() === 'applied' ? 'border-[#38b46a]/35 bg-[#38b46a]/10 text-[#9ee6bb]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`}>
                {proposalStatusLabel(status())}
              </span>
            </div>
            <div class="mt-2 text-sm font-semibold text-[#f4efff]">{props.action.title}</div>
            <div class="mt-1 text-xs leading-5 text-[#b7accf]">{props.action.message || '请核对操作后确认。'}</div>
          </div>
          <span class="shrink-0 font-mono text-[9px] text-[#776b91]">{props.action.proposal_id}</span>
        </div>

        <div class="mt-3 space-y-2">
          <For each={props.action.operations}>
            {(operation) => (
              <div class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[#332b45] bg-[#100d18]/70 px-3 py-2 text-xs">
                <div class="min-w-0">
                  <div class="truncate font-semibold text-[#e8e0fa]">{operation.title}</div>
                  <div class="mt-0.5 text-[10px] text-[#857a9c]">{operation.page_type || 'Wiki'} · r{Number(operation.revision || 0)} · {operation.current_status || '当前状态未知'} → {operation.target_status || '待审核'}</div>
                </div>
                <span class="rounded-full border border-[#a78bfa]/28 bg-[#a78bfa]/10 px-2 py-1 text-[10px] text-[#c4b5fd]">{wikiActionOperationLabel(operation.type)}</span>
              </div>
            )}
          </For>
        </div>

        <Show when={!directConfirm()}>
          <div class="mt-3 rounded-lg border border-[#f1a02b]/25 bg-[#f1a02b]/8 px-3 py-2 text-xs leading-5 text-[#ffc176]">该 Proposal 包含内容或 Claim 变更，必须进入完整 Wiki 审核页逐项核对，聊天卡片不会直接应用。</div>
        </Show>
        <Show when={notice()}><div class="mt-3 text-xs text-[#9ee6bb]">{notice()}</div></Show>
        <Show when={error()}><div class="mt-3 text-xs text-[#fca5a5]">{error()}</div></Show>

        <div class="mt-4 flex flex-wrap gap-2">
          <Show when={directConfirm() && pending()}>
            <button
              type="button"
              class="rounded-lg border border-[#38b46a]/40 bg-[#38b46a]/14 px-3 py-2 text-xs font-semibold text-[#b8efca] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={busy()}
              onClick={() => void confirmApply()}
            >
              {busy() ? '正在校验并应用…' : '确认并应用'}
            </button>
          </Show>
          <button
            type="button"
            class="rounded-lg border border-[#a78bfa]/32 bg-[#a78bfa]/10 px-3 py-2 text-xs font-semibold text-[#d7ccf5]"
            onClick={openReview}
          >
            打开 Wiki 审核页
          </button>
        </div>
      </div>
    </div>
  );
}
