import { For, Show, createEffect, createMemo, createSignal, onCleanup } from 'solid-js';
import { Portal } from 'solid-js/web';
import MarkdownContent from './MarkdownContent';
import UserMessageText from './UserMessageText';
import { confirmModelSwitch } from '../utils/modelSwitchNotice';
import WorkflowReply from './WorkflowReply';
import SwarmRunSummary from './SwarmRunSummary';
import StructuredContentBlocks from './StructuredContentBlocks';
import {
  confirmOutcomeResult,
  createWorkflowDraft,
  createWorkflowRun,
  forkSession,
  getSession,
  getWorkflowRun,
  hasActiveChatSocket,
  markAsyncCompletionContinued,
  sendMessage,
  rewindUserMessage,
} from '../api/client';
import { sessionStore } from '../stores/session';
import type { Message, ProcessNote, ToolCall } from '../stores/session';
import { desktop } from '../desktop';
import { formatResponseTime } from '../utils/responseTime';
import { getToolTextDisplayStats } from '../utils/toolTextDisplay';
import { skillDisplayName, toolDisplayLabel } from '../utils/userFacingLabels';
import { getToolRuntimeSummary, getToolVerificationLabel } from '../utils/toolRuntime';
import { mergeAndStabilizeVisibleMessages } from '../utils/messageSnapshots';
import { compactProcessNotes, type CompactProcessNote } from '../utils/processNotes';
import { researchActionDisplayIndexes, researchActionFromTool } from '../utils/researchAction';
import ResearchActionCard from './ResearchActionCard';
import { wikiActionFromTool } from '../utils/wikiAction';
import WikiActionCard from './WikiActionCard';
import {
  canReviewOutcome,
  outcomeVerificationNarrative,
  outcomeVerificationSummary,
  shouldAttemptOutcomeAcceptance,
} from '../utils/runStatus';
import { formatAsyncResultSummary } from '../utils/asyncResultSummary';
import { latestDeliveredAssistant, verificationFeedback } from '../utils/verificationFeedback';
import {
  completedWorkflowRunId,
  needsWorkflowReportRecovery,
  recoverWorkflowReport,
} from '../utils/workflowChatReport';

interface MessageItemProps {
  message: Message;
  latestAssistantMessageId?: string;
  displayMode?: 'default' | 'process' | 'commentary';
}

type HumanReviewAlert = {
  identifier: string;
  reason: string;
  trigger: string;
};

type AsyncJobCompletionAlert = {
  identifier: string;
  status: 'completed' | 'failed' | 'cancelled' | 'unknown';
  title: string;
  state: string;
  outputPath: string;
  outputAbsPath: string;
  summary: string;
};

type WorkflowProposalPayload = {
  decision?: string;
  reason?: string;
  template_key?: string;
  display_name?: string;
  confidence?: number;
  input_payload?: Record<string, any>;
  missing_required?: string[];
  recommended_workflow_run_args?: Record<string, any>;
  matched_templates?: Array<Record<string, any>>;
  request_capabilities?: string[];
  safety?: Record<string, any>;
};

const WORKFLOW_RUN_CONTROL_ARG_KEYS = new Set([
  'template_key',
  'continue_existing',
  'auto_continue',
  'max_auto_stages',
  'run_id',
]);

const parseHumanReviewAlert = (content: string): HumanReviewAlert | null => {
  const text = String(content || '').trim();
  if (!text) return null;

  const match = text.match(/Human review required:\s*([A-Z]+-\d+|[A-Z]+-[A-Za-z0-9_-]+)\s*-\s*([^\n(]+?)(?:\s*\(trigger:\s*([^)]+)\))?/i);
  if (!match) return null;

  return {
    identifier: match[1],
    reason: match[2].trim() || '当前操作需要人工审核后才能继续',
    trigger: (match[3] || '').trim(),
  };
};

const stripInlineCode = (value: string) =>
  String(value || '').trim().replace(/^`+|`+$/g, '').trim();

const isAsyncJobAlreadyContinued = (identifier: string) => {
  const sessionId = sessionStore.currentSessionId();
  if (!sessionId) return false;
  return (
    sessionStore.isAsyncJobContinued(sessionId, identifier)
    || (sessionStore.currentSession()?.metadata?.async_completion_events || []).some(
      (event: any) => event.identifier === identifier && event.continuation_status === 'continued',
    )
  );
};

const isJsonText = (value: string) => {
  const text = String(value || '').trim();
  if (!text || !text.startsWith('{')) return false;
  try {
    JSON.parse(text);
    return true;
  } catch {
    return false;
  }
};

const workflowProposalFromTool = (tool: ToolCall): WorkflowProposalPayload | null => {
  if (String(tool.tool_name || '').trim() !== 'workflow_proposal') return null;
  const raw = tool.metadata?.workflow_proposal;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  return raw as WorkflowProposalPayload;
};

const workflowProposalKey = (
  sessionId: string,
  messageId: string,
  tool: ToolCall,
  index: number,
  proposal: WorkflowProposalPayload,
) => {
  const callId = String(tool.call_id || '').trim();
  const templateKey = String(
    proposal.recommended_workflow_run_args?.template_key || proposal.template_key || ''
  ).trim();
  const source = callId || `${messageId}:${index}`;
  return [sessionId, source, templateKey].filter(Boolean).join(':');
};

const workflowProposalInputPayload = (proposal: WorkflowProposalPayload) => {
  const inputPayload = {
    ...(proposal.input_payload && typeof proposal.input_payload === 'object'
      ? proposal.input_payload
      : {}),
  };
  const args = proposal.recommended_workflow_run_args || {};
  for (const [key, value] of Object.entries(args)) {
    if (WORKFLOW_RUN_CONTROL_ARG_KEYS.has(key)) continue;
    if (value === undefined || value === null || value === '') continue;
    inputPayload[key] = value;
  }
  return inputPayload;
};

const compactWorkflowProposalSnapshot = (
  proposal: WorkflowProposalPayload,
  messageId: string,
  tool: ToolCall,
) => ({
  decision: proposal.decision || '',
  template_key: proposal.template_key || '',
  display_name: proposal.display_name || '',
  confidence: Number(proposal.confidence || 0),
  reason: proposal.reason || '',
  missing_required: Array.isArray(proposal.missing_required)
    ? proposal.missing_required
    : [],
  request_capabilities: Array.isArray(proposal.request_capabilities)
    ? proposal.request_capabilities
    : [],
  recommended_workflow_run_args: proposal.recommended_workflow_run_args || {},
  source_message_id: messageId,
  source_tool_call_id: tool.call_id || '',
});

const pickMessageLineValue = (text: string, label: string) => {
  const prefix = `${label}:`;
  const line = text
    .split(/\r?\n/)
    .find((item) => item.trim().startsWith(prefix));
  if (!line) return '';
  return stripInlineCode(line.trim().slice(prefix.length));
};

const pickMessageBlockValue = (text: string, label: string) => {
  const lines = String(text || '').split(/\r?\n/);
  const prefix = `${label}:`;
  const start = lines.findIndex((item) => item.trim().startsWith(prefix));
  if (start < 0) return '';
  const result = [lines[start].trim().slice(prefix.length).trim()];
  for (const line of lines.slice(start + 1)) {
    if (/^(?:下一步|结果目录|结果目录\(绝对路径\)|相关产物|失败原因):/.test(line.trim())) break;
    result.push(line);
  }
  return stripInlineCode(result.join('\n').trim());
};

const parseAsyncJobCompletionAlert = (content: string): AsyncJobCompletionAlert | null => {
  const text = String(content || '').trim();
  if (!text) return null;

  const idMatch = text.match(/\[Symphony:([A-Za-z0-9_-]+)\]/);
  if (!idMatch || !/异步任务已(?:完成|失败|取消)/.test(text)) return null;

  const titleMatch = text.match(/^(.+?异步任务已(?:完成|失败|取消)。?)$/m);
  const title = titleMatch?.[1]?.trim() || '异步任务状态更新';
  const status: AsyncJobCompletionAlert['status'] =
    title.includes('已失败') ? 'failed' :
    title.includes('已取消') ? 'cancelled' :
    title.includes('已完成') ? 'completed' :
    'unknown';

  return {
    identifier: idMatch[1],
    status,
    title,
    state: pickMessageLineValue(text, '任务状态'),
    outputPath: pickMessageLineValue(text, '结果目录'),
    outputAbsPath: pickMessageLineValue(text, '结果目录(绝对路径)'),
    summary: pickMessageBlockValue(text, status === 'failed' ? '失败原因' : '结果摘要'),
  };
};

function HumanReviewRequiredCard(props: { alert: HumanReviewAlert }) {
  const openReviewPanel = () => {
    window.dispatchEvent(new CustomEvent('tcode-reveal-human-review-panel', {
      detail: {
        reviewIdentifier: props.alert.identifier,
      },
    }));
  };

  return (
    <div class="cursor-pointer overflow-hidden rounded-2xl border border-[#f1a02b]/55 bg-[#241a0d] shadow-[0_0_0_1px_rgba(241,160,43,0.16),0_18px_42px_rgba(0,0,0,0.32)]"
      onClick={(event) => { if (!(event.target as HTMLElement).closest('button')) openReviewPanel(); }}>
      <div class="flex flex-col gap-4 p-4 sm:flex-row sm:items-start sm:justify-between">
        <div class="min-w-0 flex-1">
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <span class="inline-flex h-8 w-8 items-center justify-center rounded-xl border border-[#f1a02b]/45 bg-[#f1a02b]/14 text-[#ffc176]">
              <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4" />
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 17h.01" />
                <path stroke-linecap="round" stroke-linejoin="round" d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
              </svg>
            </span>
            <span class="rounded-full border border-[#f1a02b]/40 bg-[#f1a02b]/12 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-[#ffc176]">
              等待人工审核
            </span>
            <span class="text-xs font-semibold text-[#ffe1b3]">待审核任务</span>
          </div>

          <div class="text-base font-semibold text-[#fff4dd]">当前任务已暂停，需要人工确认后继续</div>
          <div class="mt-2 text-sm text-[#ffd7a1]">{props.alert.reason}</div>

          <div class="mt-3 flex flex-wrap gap-2 text-xs">
            <span class="rounded-lg border border-[#f1a02b]/25 bg-[#0f1418]/45 px-2.5 py-1 text-[#ffc176]">
              请确认本次操作及其影响范围
            </span>
            <Show when={props.alert.trigger}>
              <span class="rounded-lg border border-[#f1a02b]/25 bg-[#0f1418]/45 px-2.5 py-1 text-[#ffc176]">
                触发: <span class="font-mono">{props.alert.trigger}</span>
              </span>
            </Show>
          </div>
        </div>

        <button
          class="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-xl border border-[#ffc176]/55 bg-[#ffc176]/14 px-3 text-sm font-semibold text-[#ffe1b3] transition-colors hover:bg-[#ffc176]/20"
          onClick={openReviewPanel}
        >
          打开审核面板
          <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M5 12h14" />
            <path stroke-linecap="round" stroke-linejoin="round" d="m12 5 7 7-7 7" />
          </svg>
        </button>
      </div>
    </div>
  );
}

function AsyncJobCompletionCard(props: { alert: AsyncJobCompletionAlert }) {
  const [submitting, setSubmitting] = createSignal(false);
  const [notice, setNotice] = createSignal('');
  const continuationAvailable = createMemo(() => props.alert.status === 'completed');
  const alreadyContinued = createMemo(() =>
    isAsyncJobAlreadyContinued(props.alert.identifier)
  );
  const foregroundBusy = createMemo(() => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId) return false;
    const runtime = sessionStore.getSessionRuntimeState(sessionId);
    return Boolean(sessionStore.isHarnessSwitchPending(sessionId) || runtime?.isRunning || sessionStore.isRunning() || hasActiveChatSocket(sessionId));
  });

  const tone = createMemo(() => {
    switch (props.alert.status) {
      case 'failed':
        return {
          frame: 'border-[#ef6767]/55 bg-[#251315] shadow-[0_0_0_1px_rgba(239,103,103,0.14),0_18px_42px_rgba(0,0,0,0.32)]',
          icon: 'border-[#ef6767]/45 bg-[#ef6767]/14 text-[#ffaaa6]',
          badge: 'border-[#ef6767]/40 bg-[#ef6767]/12 text-[#ffaaa6]',
          text: 'text-[#ffd1cd]',
          soft: 'text-[#ffb8b2]',
          chip: 'border-[#ef6767]/25 bg-[#0f1418]/45 text-[#ffaaa6]',
          button: 'border-[#ffaaa6]/55 bg-[#ffaaa6]/14 text-[#ffd1cd] hover:bg-[#ffaaa6]/20',
          label: '基于失败继续',
        };
      case 'cancelled':
        return {
          frame: 'border-[#8aa0ac]/45 bg-[#14191d] shadow-[0_0_0_1px_rgba(138,160,172,0.12),0_18px_42px_rgba(0,0,0,0.28)]',
          icon: 'border-[#8aa0ac]/40 bg-[#8aa0ac]/12 text-[#c5d3da]',
          badge: 'border-[#8aa0ac]/35 bg-[#8aa0ac]/10 text-[#c5d3da]',
          text: 'text-[#e2ebef]',
          soft: 'text-[#c5d3da]',
          chip: 'border-[#8aa0ac]/22 bg-[#0f1418]/45 text-[#c5d3da]',
          button: 'border-[#c5d3da]/45 bg-[#c5d3da]/10 text-[#e2ebef] hover:bg-[#c5d3da]/16',
          label: '继续处理',
        };
      default:
        return {
          frame: 'border-[#36c48f]/50 bg-[#0f201b] shadow-[0_0_0_1px_rgba(54,196,143,0.14),0_18px_42px_rgba(0,0,0,0.32)]',
          icon: 'border-[#36c48f]/45 bg-[#36c48f]/14 text-[#8ff0c8]',
          badge: 'border-[#36c48f]/38 bg-[#36c48f]/12 text-[#8ff0c8]',
          text: 'text-[#e0fff3]',
          soft: 'text-[#b9f4db]',
          chip: 'border-[#36c48f]/24 bg-[#0f1418]/45 text-[#9ee7ca]',
          button: 'border-[#8ff0c8]/55 bg-[#8ff0c8]/14 text-[#e0fff3] hover:bg-[#8ff0c8]/20',
          label: '继续此任务',
        };
    }
  });

  const buildContinueMessage = () => {
    return `继续异步任务 ${props.alert.identifier}。`;
  };

  const displaySummary = createMemo(() =>
    formatAsyncResultSummary(props.alert.summary).text
  );

  const continueAsyncJob = async () => {
    const sessionId = sessionStore.currentSessionId();
    if (!continuationAvailable() || !sessionId || submitting() || alreadyContinued()) return;

    setSubmitting(true);
    setNotice('');
    try {
      const message = buildContinueMessage();
      if (foregroundBusy()) {
        const queuedText = '当前问题仍在回答中，请等待本轮结束后再继续异步任务。';
        setNotice(queuedText);
        sessionStore.setStatus(queuedText);
        return;
      }

      await sendMessage(message, { async_continuation_id: props.alert.identifier });
      // This endpoint only acknowledges a server-committed continuation. A
      // blocked/partial turn or failed persistence must leave the action usable.
      await markAsyncCompletionContinued(sessionId, props.alert.identifier);
      sessionStore.markAsyncJobContinued(sessionId, props.alert.identifier);
    } catch (error) {
      sessionStore.unmarkAsyncJobContinued(sessionId, props.alert.identifier);
      const detail = error instanceof Error ? error.message : String(error || '继续任务失败');
      setNotice(detail);
      sessionStore.setStatus(detail);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div class={`overflow-hidden rounded-2xl border ${tone().frame}`}>
      <div class="flex flex-col gap-4 p-4 sm:flex-row sm:items-start sm:justify-between">
        <div class="min-w-0 flex-1">
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <span class={`inline-flex h-8 w-8 items-center justify-center rounded-xl border ${tone().icon}`}>
              <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                {props.alert.status === 'failed' ? (
                  <>
                    <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="M12 17h.01" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
                  </>
                ) : props.alert.status === 'cancelled' ? (
                  <>
                    <path stroke-linecap="round" stroke-linejoin="round" d="M18 6 6 18" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="m6 6 12 12" />
                  </>
                ) : (
                  <path stroke-linecap="round" stroke-linejoin="round" d="M20 6 9 17l-5-5" />
                )}
              </svg>
            </span>
            <span class={`rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.12em] ${tone().badge}`}>
              异步任务{props.alert.status === 'failed' ? '失败' : props.alert.status === 'cancelled' ? '取消' : '完成'}
            </span>
            <span class={`text-xs font-semibold ${tone().soft}`}>计算任务</span>
          </div>

          <div class={`text-base font-semibold ${tone().text}`}>{props.alert.title}</div>
          <Show when={displaySummary()}>
            <div class={`mt-2 whitespace-pre-line text-sm ${tone().soft}`}>{displaySummary()}</div>
          </Show>

          <div class="mt-3 flex flex-wrap gap-2 text-xs">
            <Show when={props.alert.state}>
              <span class={`rounded-lg border px-2.5 py-1 ${tone().chip}`}>
                状态: <span class="font-mono">{props.alert.state}</span>
              </span>
            </Show>
            <Show when={props.alert.outputPath || props.alert.outputAbsPath}>
              <span class={`min-w-0 rounded-lg border px-2.5 py-1 ${tone().chip}`}>
                结果目录: <span class="break-all font-mono">{props.alert.outputPath || props.alert.outputAbsPath}</span>
              </span>
            </Show>
          </div>

          <Show when={continuationAvailable() && notice()}>
            <div class={`mt-3 text-xs ${tone().soft}`}>{notice()}</div>
          </Show>
          <Show when={continuationAvailable() && alreadyContinued() && !notice()}>
            <div class={`mt-3 text-xs ${tone().soft}`}>这个异步任务已经发起过继续。</div>
          </Show>
        </div>

        <Show when={continuationAvailable()}>
          <button
            class={`inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-xl border px-3 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${tone().button}`}
            disabled={!sessionStore.currentSessionId() || submitting() || alreadyContinued() || foregroundBusy()}
            title={foregroundBusy() ? '当前问题回答结束后才能创建新的异步结果处理轮次' : ''}
            onClick={continueAsyncJob}
          >
            {submitting() ? '处理中...' : alreadyContinued() ? '已继续' : foregroundBusy() ? '等待当前回答' : tone().label}
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M5 12h14" />
              <path stroke-linecap="round" stroke-linejoin="round" d="m12 5 7 7-7 7" />
            </svg>
          </button>
        </Show>
      </div>
    </div>
  );
}

function WorkflowProposalConfirmationCard(props: {
  messageId: string;
  tool: ToolCall;
  index: number;
  proposal: WorkflowProposalPayload;
}) {
  const [submitting, setSubmitting] = createSignal(false);
  const [notice, setNotice] = createSignal('');
  const [createdDraftId, setCreatedDraftId] = createSignal('');
  const sessionId = createMemo(() => String(sessionStore.currentSessionId() || '').trim());
  const templateKey = createMemo(() =>
    String(
      props.proposal.recommended_workflow_run_args?.template_key
      || props.proposal.template_key
      || ''
    ).trim()
  );
  const proposalKey = createMemo(() =>
    workflowProposalKey(sessionId(), props.messageId, props.tool, props.index, props.proposal)
  );
  const createdRecord = createMemo(() =>
    sessionStore.workflowProposalRun(sessionId(), proposalKey())
  );
  const busy = createMemo(() => {
    const sid = sessionId();
    if (!sid) return false;
    const runtime = sessionStore.getSessionRuntimeState(sid);
    return Boolean(runtime?.isRunning || sessionStore.isRunning() || hasActiveChatSocket(sid));
  });
  const missingRequired = createMemo(() =>
    (Array.isArray(props.proposal.missing_required) ? props.proposal.missing_required : [])
      .map((item) => String(item || '').trim())
      .filter(Boolean)
  );
  const inputPayload = createMemo(() => workflowProposalInputPayload(props.proposal));
  const inputSummary = createMemo(() => {
    const payload = inputPayload();
    const keys = Object.keys(payload);
    if (keys.length === 0) return '暂无输入';
    return keys.slice(0, 5).join('、') + (keys.length > 5 ? ` 等 ${keys.length} 项` : '');
  });

  const refreshSessionFromServer = async () => {
    const sid = sessionId();
    if (!sid) return;
    const detail = await getSession(sid, { bustCache: true });
    if (!detail) return;
    const currentMessages = sessionStore.messages();
    const stableMessages = mergeAndStabilizeVisibleMessages(
      currentMessages,
      detail.messages || [],
    );
    sessionStore.upsertSession({
      session_id: detail.session_id,
      title: detail.title,
      agent_name: detail.agent_name,
      status: detail.status,
      workspace_id: detail.workspace_id,
      workdir: detail.workdir,
      workdir_name: detail.workdir_name,
      is_archived: detail.is_archived,
      archived_at: detail.archived_at ?? undefined,
      created_at: Number(detail.created_at || Date.now()),
      updated_at: Number(detail.updated_at || Date.now()),
      messages: [],
    });
    if (sessionStore.currentSessionId() === sid && stableMessages !== currentMessages) {
      sessionStore.setMessages(stableMessages);
    }
  };

  const confirmCreateRun = async () => {
    const sid = sessionId();
    const key = proposalKey();
    const selectedTemplateKey = templateKey();
    const isTemporary = props.proposal.decision === 'temporary_workflow_proposal';
    if (!sid || !key || (!selectedTemplateKey && !isTemporary) || submitting() || createdRecord() || createdDraftId() || busy()) return;

    setSubmitting(true);
    setNotice('');
    try {
      const currentSession = sessionStore.currentSession();
      if (isTemporary) {
        const goal = String(props.proposal.recommended_workflow_run_args?.goal || props.proposal.input_payload?.target_description || '').trim();
        const created = await createWorkflowDraft({
          session_id: sid,
          workdir_name: String(currentSession?.workdir_name || '').trim() || undefined,
          workdir_path: String(currentSession?.workdir || '').trim() || undefined,
          model: currentSession?.model || 'local-qwen/qwen3.8:latest',
          goal,
        });
        setCreatedDraftId(String(created.draft.draft_id || 'created'));
        setNotice(`已生成临时 DAG：${created.draft.name}`);
        window.dispatchEvent(new CustomEvent('tcode-workflow-run-created', { detail: { sessionId: sid, draftId: created.draft.draft_id } }));
        return;
      }
      const created = await createWorkflowRun({
        session_id: sid,
        workdir_name: String(currentSession?.workdir_name || '').trim() || undefined,
        workdir_path: String(currentSession?.workdir || '').trim() || undefined,
        template_key: selectedTemplateKey,
        input_payload: inputPayload(),
        metadata: {
          created_from: 'workflow_proposal_confirmation',
          auto_continue: false,
          source_message_id: props.messageId,
          source_tool_call_id: String(props.tool.call_id || ''),
          workflow_proposal_key: key,
          workflow_proposal: compactWorkflowProposalSnapshot(
            props.proposal,
            props.messageId,
            props.tool,
          ),
        },
      });
      const runId = String(created.run?.run_id || '').trim();
      sessionStore.markWorkflowProposalRunCreated(sid, key, runId, selectedTemplateKey);
      sessionStore.setStatus(`已创建 workflow run：${runId || selectedTemplateKey}`);
      window.dispatchEvent(new CustomEvent('tcode-workflow-run-created', {
        detail: {
          sessionId: sid,
          runId,
          templateKey: selectedTemplateKey,
        },
      }));
      setNotice(runId ? `已创建 run ${runId}` : '已创建 workflow run');
      window.setTimeout(() => {
        void refreshSessionFromServer().catch((error) => {
          console.warn('Workflow proposal session refresh failed:', error);
        });
      }, 0);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error || '创建 workflow run 失败');
      setNotice(detail);
      sessionStore.setStatus(detail);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Show when={(props.proposal.decision === 'workflow_proposal' && templateKey()) || props.proposal.decision === 'temporary_workflow_proposal'}>
      <div class="border-t border-[#2a3136] px-4 py-3">
        <div class="rounded-xl border border-[#38bdf8]/30 bg-[#0e1a20] p-3 shadow-[0_10px_24px_rgba(0,0,0,0.18)]">
          <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div class="min-w-0 flex-1">
              <div class="mb-2 flex flex-wrap items-center gap-2">
                <span class="inline-flex h-7 w-7 items-center justify-center rounded-lg border border-[#38bdf8]/35 bg-[#38bdf8]/12 text-[#8ed5ff]">
                  <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="M4 12h10" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="M4 18h7" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="m17 15 3 3 3-3" />
                  </svg>
                </span>
                <span class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-[#8ed5ff]">
                  {props.proposal.decision === 'temporary_workflow_proposal' ? 'Temporary Plan' : 'Level 3 Proposal'}
                </span>
                <span class="font-mono text-[11px] text-[#9faeb7]">{templateKey()}</span>
              </div>

              <div class="text-sm font-semibold text-[#f5f7fa]">
                {props.proposal.display_name || templateKey() || '生成临时 Workflow'}
              </div>
              <div class="mt-1 text-xs leading-5 text-[#9faeb7]">
                {props.proposal.reason || 'Composite skill plan 与该 Workflow 定义匹配。'}
              </div>

              <div class="mt-3 grid gap-2 text-[11px] sm:grid-cols-3">
                <div class="rounded-lg border border-[#2a3136] bg-[#0f1418] px-2.5 py-2">
                  <div class="text-[#6f7d87]">置信度</div>
                  <div class="mt-1 font-mono text-[#d6e3ea]">{Number(props.proposal.confidence || 0).toFixed(2)}</div>
                </div>
                <div class="rounded-lg border border-[#2a3136] bg-[#0f1418] px-2.5 py-2">
                  <div class="text-[#6f7d87]">输入</div>
                  <div class="mt-1 truncate text-[#d6e3ea]" title={inputSummary()}>{inputSummary()}</div>
                </div>
                <div class="rounded-lg border border-[#2a3136] bg-[#0f1418] px-2.5 py-2">
                  <div class="text-[#6f7d87]">缺失项</div>
                  <div class={missingRequired().length > 0 ? 'mt-1 truncate text-[#ffc176]' : 'mt-1 text-[#9ee6bb]'}>
                    {missingRequired().length > 0 ? missingRequired().join('、') : '无'}
                  </div>
                </div>
              </div>

              <Show when={notice()}>
                <div class="mt-3 text-xs text-[#8ed5ff]">{notice()}</div>
              </Show>
              <Show when={createdRecord()}>
                {(_record) => (
                  <div class="mt-3 text-xs text-[#9ee6bb]">
                    工作流运行已创建
                  </div>
                )}
              </Show>
              <Show when={busy() && !createdRecord()}>
                <div class="mt-3 text-xs text-[#ffc176]">当前会话仍在运行，结束后再确认创建。</div>
              </Show>
            </div>

            <button
              type="button"
              class="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-lg border border-[#8ed5ff]/45 bg-[#38bdf8]/12 px-3 text-xs font-semibold text-[#bdeaff] transition-colors hover:bg-[#38bdf8]/18 disabled:cursor-not-allowed disabled:opacity-55"
              disabled={!sessionId() || (!templateKey() && props.proposal.decision !== 'temporary_workflow_proposal') || submitting() || Boolean(createdRecord()) || Boolean(createdDraftId()) || busy()}
              onClick={() => void confirmCreateRun()}
              title="确认后只创建 workflow run，不自动执行后续阶段"
            >
              {submitting() ? '创建中...' : createdRecord() || createdDraftId() ? '已创建' : props.proposal.decision === 'temporary_workflow_proposal' ? '生成临时 DAG' : '确认创建 Run'}
              <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M5 12h14" />
                <path stroke-linecap="round" stroke-linejoin="round" d="m12 5 7 7-7 7" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </Show>
  );
}

const formatToolState = (state?: string) => {
  switch (String(state || '').trim().toLowerCase()) {
    case 'running':
      return '运行中';
    case 'completed':
      return '已完成';
    case 'error':
      return '错误';
    case 'skipped':
      return '已跳过';
    case 'interrupted':
      return '已中断';
    case 'pending':
      return '等待中';
    default:
      return state || '未知状态';
  }
};

const formatToolArguments = (argumentsValue?: Record<string, any>) => {
  try {
    return JSON.stringify(argumentsValue || {}, null, 2);
  } catch (error) {
    console.error('Tool arguments stringify failed:', error);
    return String(argumentsValue ?? '');
  }
};

const getToolDetailKey = (tool: ToolCall, index: number) => {
  if (tool.call_id) return tool.call_id;
  return `${tool.tool_name}:${index}`;
};

const processNoteLabel = (note: ProcessNote | CompactProcessNote) => {
  const phase = String(note.phase || '').trim().toLowerCase();
  if (phase === 'turn_start') return '开始';
  if (phase === 'preparing') return '准备';
  if (phase === 'context_check') return '上下文';
  if (phase === 'tool_preparation') return '工具';
  if (phase === 'model_wait') return '模型';
  if (phase === 'tool_selection') return '决策';
  if (phase === 'tool_start') return '运行';
  if (phase === 'tool_progress') return '运行中';
  if (phase === 'tool_result') return '结果';
  if (phase === 'final_response') return '整理';
  return '过程';
};

type ProcessNoteMessageSegment = {
  text: string;
  kind: 'normal' | 'elapsed-number';
};

const processNoteMessageSegments = (
  note: ProcessNote | CompactProcessNote,
): ProcessNoteMessageSegment[] => {
  const message = String(note.message || '');
  const match = message.match(/(已(?:等待|运行)\s*)((?:\d+\s*(?:秒|分钟|分|小时)\s*)+)/);
  if (!match || match.index === undefined) return [{ text: message, kind: 'normal' }];

  const before = message.slice(0, match.index);
  const prefix = match[1] || '';
  const elapsedText = match[2] || '';
  const after = message.slice(match.index + match[0].length);
  const segments: ProcessNoteMessageSegment[] = [];
  if (before) segments.push({ text: before, kind: 'normal' });
  if (prefix) segments.push({ text: prefix, kind: 'normal' });
  for (const part of elapsedText.split(/(\d+)/).filter(Boolean)) {
    segments.push({
      text: part,
      kind: /^\d+$/.test(part) ? 'elapsed-number' : 'normal',
    });
  }
  if (after) segments.push({ text: after, kind: 'normal' });
  return segments;
};

const processNoteDotClass = (note: ProcessNote | CompactProcessNote) => {
  const status = String(note.status || '').trim().toLowerCase();
  if (status === 'error' || status === 'failed') return 'border-[#ff9c96] bg-[#ff9c96]';
  if (status === 'completed') return 'border-[#9ee6bb] bg-[#9ee6bb]';
  const phase = String(note.phase || '').trim().toLowerCase();
  if (phase === 'tool_selection') return 'border-[#ffc176] bg-[#ffc176]';
  if (phase === 'tool_progress') return 'border-[#facc15] bg-[#facc15]';
  if (phase === 'model_wait' || phase === 'final_response') return 'border-[#8ed5ff] bg-[#8ed5ff]';
  return 'border-[#7f8b93] bg-[#7f8b93]';
};

const processNoteProgressPercent = (note: ProcessNote | CompactProcessNote) => {
  const current = Number(note.current);
  const total = Number(note.total);
  if (!Number.isFinite(current) || !Number.isFinite(total) || total <= 0) return undefined;
  return Math.max(0, Math.min(100, Math.round((current / total) * 100)));
};

type ProcessPreviewState = {
  key: string;
  notes: ProcessNote[];
};

const [processPreviewState, setProcessPreviewState] = createSignal<ProcessPreviewState | null>(null);

const visibleProcessNotes = (notes: ProcessNote[]) =>
  (Array.isArray(notes) ? notes : []).filter((note) => String(note?.message || '').trim());

export const isProcessSummaryPreviewOpen = () => Boolean(processPreviewState());

const openProcessSummaryPreview = (key: string, notes: ProcessNote[]) => {
  setProcessPreviewState({
    key,
    notes: visibleProcessNotes(notes),
  });
};

const updateProcessSummaryPreview = (key: string, notes: ProcessNote[]) => {
  setProcessPreviewState((current) => {
    if (!current || current.key !== key) return current;
    return {
      ...current,
      notes: visibleProcessNotes(notes),
    };
  });
};

const closeProcessSummaryPreview = () => setProcessPreviewState(null);

function ProcessNoteRow(props: { note: CompactProcessNote; compact?: boolean }) {
  const progressPercent = () => processNoteProgressPercent(props.note);
  return (
    <div
      class={
        props.compact
          ? 'flex min-w-0 items-center gap-2 text-xs text-[#d6e3ea]'
          : 'flex min-w-0 items-start gap-2 rounded-lg border border-[#26313a] bg-[#0f1418] px-2.5 py-2 text-xs text-[#d6e3ea]'
      }
    >
      <span class={`mt-[0.32rem] h-1.5 w-1.5 flex-shrink-0 rounded-full border ${processNoteDotClass(props.note)} ${props.compact ? 'mt-0' : ''}`} />
      <span class={`flex-shrink-0 text-[#8b949e] ${props.compact ? '' : 'w-12'}`}>{processNoteLabel(props.note)}</span>
      <div class={props.compact ? 'min-w-0 flex-1 truncate' : 'min-w-0 flex-1'}>
        <div class={props.compact ? 'truncate' : 'whitespace-pre-wrap break-words leading-5'}>
          <For each={processNoteMessageSegments(props.note)}>
            {(segment) => (
              <span class={segment.kind === 'elapsed-number' ? 'font-semibold tabular-nums text-[#facc15]' : undefined}>
                {segment.text}
              </span>
            )}
          </For>
        </div>
        <Show when={!props.compact && progressPercent() !== undefined}>
          <div class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[#253039]">
            <div
              class="h-full rounded-full bg-[#facc15] transition-[width] duration-300"
              style={{ width: `${progressPercent() || 0}%` }}
            />
          </div>
        </Show>
      </div>
      <Show when={progressPercent() !== undefined}>
        <span class="flex-shrink-0 font-semibold tabular-nums text-[#facc15]">
          {progressPercent()}%
        </span>
      </Show>
      <Show when={props.note.repeat_count && props.note.repeat_count > 1}>
        <span class="flex-shrink-0 rounded-full border border-[#3e484f] bg-[#171c20] px-1.5 py-0.5 text-[10px] tabular-nums text-[#8b949e]">
          x{props.note.repeat_count}
        </span>
      </Show>
    </div>
  );
}

export function ProcessSummaryPreviewPortal() {
  let previewScrollRef: HTMLDivElement | undefined;
  let previewStickToBottom = true;
  const previewNotes = createMemo(() => processPreviewState()?.notes || []);

  const isPreviewNearBottom = () => {
    if (!previewScrollRef) return true;
    const distance = previewScrollRef.scrollHeight - previewScrollRef.scrollTop - previewScrollRef.clientHeight;
    return distance < 16;
  };

  const scrollPreviewToLatest = () => {
    if (!previewScrollRef) return;
    previewScrollRef.scrollTop = previewScrollRef.scrollHeight;
  };

  const handlePreviewScroll = () => {
    previewStickToBottom = isPreviewNearBottom();
  };

  createEffect(() => {
    const current = processPreviewState();
    const noteCount = current?.notes.length || 0;
    if (!current || !previewStickToBottom) return;
    void noteCount;
    window.requestAnimationFrame(scrollPreviewToLatest);
  });

  createEffect(() => {
    if (!processPreviewState()) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeProcessSummaryPreview();
    };
    window.addEventListener('keydown', handleKeyDown);
    onCleanup(() => window.removeEventListener('keydown', handleKeyDown));
  });

  return (
    <Show when={processPreviewState()}>
      <Portal>
        <div
          class="fixed inset-0 z-[150] flex items-end justify-center bg-black/60 p-3 backdrop-blur-sm sm:items-center sm:p-6"
          onClick={closeProcessSummaryPreview}
        >
          <div
            class="flex h-[82vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0d1216] shadow-[0_24px_80px_rgba(0,0,0,0.48)] sm:h-[78vh]"
            onClick={(event) => event.stopPropagation()}
          >
            <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#26313a] px-4 py-3">
              <div class="min-w-0">
                <div class="text-xs font-semibold uppercase tracking-[0.16em] text-[#8ed5ff]">过程预览</div>
                <div class="mt-1 text-xs text-[#7f8b93]">共 {previewNotes().length} 条</div>
              </div>
              <button
                type="button"
                class="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[#3e484f] bg-[#171c20] text-[#b7c7d0] transition-colors hover:border-[#4f6571] hover:bg-[#1b2328]"
                onClick={closeProcessSummaryPreview}
                title="关闭"
                aria-label="关闭过程预览"
              >
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M18 6 6 18" />
                  <path stroke-linecap="round" stroke-linejoin="round" d="m6 6 12 12" />
                </svg>
              </button>
            </div>
            <div
              ref={previewScrollRef}
              class="tcode-scrollbar min-h-0 flex-1 space-y-2 overflow-y-auto overscroll-contain p-4"
              onScroll={handlePreviewScroll}
              onWheel={(event) => event.stopPropagation()}
              onTouchMove={(event) => event.stopPropagation()}
            >
              <For each={previewNotes()}>
                {(note) => <ProcessNoteRow note={note} />}
              </For>
            </div>
          </div>
        </div>
      </Portal>
    </Show>
  );
}

export function ProcessSummary(props: { notes: ProcessNote[]; maxItems?: number; previewKey?: string }) {
  let scrollRef: HTMLDivElement | undefined;
  let stickToBottom = true;
  const summaryNotes = createMemo(() => compactProcessNotes(props.notes, props.maxItems));
  const processPreviewKey = createMemo(() =>
    String(props.previewKey || `process-summary:${props.notes[0]?.id || 'default'}`)
  );
  const previewNotes = createMemo(() => visibleProcessNotes(props.notes));
  const representedNoteCount = createMemo(() =>
    summaryNotes().reduce((total, note) => total + (note.source_count || 1), 0)
  );
  const omittedNoteCount = createMemo(() =>
    Math.max(0, props.notes.length - representedNoteCount())
  );
  const hasMergedNotes = createMemo(() =>
    summaryNotes().some((note) => (note.source_count || 1) > 1)
  );
  const summaryCountText = createMemo(() => {
    if (omittedNoteCount() > 0) {
      return `显示最近 ${summaryNotes().length} 条 · 共 ${props.notes.length} 条`;
    }
    if (hasMergedNotes()) {
      return `共 ${props.notes.length} 条 · 合并为 ${summaryNotes().length} 条`;
    }
    return `共 ${props.notes.length} 条`;
  });

  const isNearBottom = () => {
    if (!scrollRef) return true;
    const distance = scrollRef.scrollHeight - scrollRef.scrollTop - scrollRef.clientHeight;
    return distance < 8;
  };

  const scrollToLatest = () => {
    if (!scrollRef) return;
    scrollRef.scrollTop = scrollRef.scrollHeight;
  };

  const handleSummaryScroll = () => {
    stickToBottom = isNearBottom();
  };

  const openPreview = () => {
    openProcessSummaryPreview(processPreviewKey(), previewNotes());
  };

  createEffect(() => {
    const noteCount = props.notes.length + summaryNotes().length;
    void noteCount;
    if (!stickToBottom) return;
    queueMicrotask(scrollToLatest);
  });

  createEffect(() => {
    updateProcessSummaryPreview(processPreviewKey(), previewNotes());
  });

  return (
    <div class="tcode-process-summary sticky top-2 z-20 rounded-xl border border-[#26313a] bg-[#10161b]/95 px-3 py-2 shadow-[0_10px_24px_rgba(0,0,0,0.24)] backdrop-blur">
      <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div class="text-[10px] font-semibold uppercase tracking-[0.16em] text-[#8ed5ff]">过程摘要</div>
        <div class="flex items-center gap-2">
          <div class="text-[10px] text-[#6f7d87]">
            {summaryCountText()}
          </div>
          <button
            type="button"
            class="inline-flex h-6 items-center gap-1 rounded-lg border border-[#3e484f] bg-[#171c20] px-2 text-[10px] font-semibold text-[#b7c7d0] transition-colors hover:border-[#4f6571] hover:bg-[#1b2328]"
            onClick={openPreview}
            title="打开过程预览"
            aria-label="打开过程预览"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16" />
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 12h16" />
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 18h10" />
            </svg>
            预览
          </button>
        </div>
      </div>
      <div
        ref={scrollRef}
        class="tcode-scrollbar h-24 space-y-1 overflow-y-auto overscroll-x-contain overscroll-y-auto pr-1"
        onScroll={handleSummaryScroll}
      >
        <For each={summaryNotes()}>
          {(note) => <ProcessNoteRow note={note} compact />}
        </For>
      </div>
    </div>
  );
}

export function ToolCallItem(props: {
  messageId: string;
  tool: ToolCall;
  index: number;
  showResearchAction?: boolean;
}) {
  const detailKey = createMemo(() => getToolDetailKey(props.tool, props.index));
  const isCollapsed = createMemo(() =>
    sessionStore.isMessageToolDetailsCollapsed(props.messageId, detailKey())
  );
  const hasArguments = createMemo(() => Object.keys(props.tool.arguments || {}).length > 0);
  const hasResult = createMemo(
    () => Boolean(props.tool.output || props.tool.error || ['completed', 'error', 'skipped', 'interrupted'].includes(String(props.tool.state || '')))
  );
  const runtimeSummary = createMemo(() => getToolRuntimeSummary(props.tool));
  const isSkill = () => props.tool.tool_name === 'skill';
  const argumentsText = createMemo(() => isSkill()
    ? `技能：${skillDisplayName(props.tool.arguments)}` : formatToolArguments(props.tool.arguments));
  const argumentStats = createMemo(() => getToolTextDisplayStats(argumentsText()));
  const resultText = createMemo(() => isSkill()
    ? (props.tool.error || props.tool.state === 'error' ? '技能加载失败，请检查技能配置或授权。'
      : props.tool.state === 'completed' ? `已加载${skillDisplayName(props.tool.arguments)}。`
      : '请根据调用状态确认技能是否完成加载。')
    : getToolVerificationLabel(props.tool)
    ? `操作已发送；此条记录不代表任务完成，实际效果请查看后续观察。\n${props.tool.output || ''}`.trim()
    : props.tool.error || props.tool.output || '（无返回内容）');
  const resultStats = createMemo(() => getToolTextDisplayStats(resultText()));
  const workflowProposal = createMemo(() => workflowProposalFromTool(props.tool));
  const researchAction = createMemo(() =>
    props.showResearchAction === false ? null : researchActionFromTool(props.tool)
  );
  const detailLabel = createMemo(() => {
    if (getToolVerificationLabel(props.tool)) return '动作回执';
    if (props.tool.error) return '错误返回';
    if (props.tool.output) return '返回结果';
    if (props.tool.state === 'completed') return '返回结果';
    if (props.tool.state === 'error') return '错误返回';
    if (props.tool.state === 'skipped') return '未执行（已被新指导替代）';
    if (props.tool.state === 'interrupted') return '中断结果';
    return '调用详情';
  });

  return (
    <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d] shadow-[0_10px_24px_rgba(0,0,0,0.18)]">
      <button
        class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-[#182027]"
        onClick={() => sessionStore.setMessageToolDetailsCollapsed(props.messageId, detailKey(), !isCollapsed())}
      >
        <span class="min-w-0 flex flex-1 items-start gap-2">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            class={`h-4 w-4 flex-shrink-0 text-[#9faeb7] transition-transform ${isCollapsed() ? '' : 'rotate-90'}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>
          <span class="min-w-0">
            <span class="flex min-w-0 flex-wrap items-center gap-2">
              <span class="break-all text-xs font-semibold text-[#8ed5ff]">{toolDisplayLabel(props.tool.tool_name, props.tool.arguments)}</span>
              <Show when={runtimeSummary()}>
                <span class={`flex-shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${runtimeSummary()!.badgeClass}`}>
                  {runtimeSummary()!.badge}
                </span>
              </Show>
            </span>
          </span>
        </span>
        <span class="flex-shrink-0 rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#9faeb7]">{getToolVerificationLabel(props.tool) || formatToolState(props.tool.state)}</span>
      </button>

      <Show when={props.tool.tool_name === 'task'}>
        <SwarmRunSummary metadata={props.tool.metadata} />
      </Show>

      <Show when={workflowProposal()}>
        {(proposal) => (
          <WorkflowProposalConfirmationCard
            messageId={props.messageId}
            tool={props.tool}
            index={props.index}
            proposal={proposal()}
          />
        )}
      </Show>

      <Show when={researchAction()}>
        {(action) => <ResearchActionCard action={action()} />}
      </Show>

      <Show when={!isCollapsed()}>
        <div class="space-y-3 px-4 pb-4 text-xs">
          <Show when={runtimeSummary()}>
            <div class={`rounded-xl border p-3 ${runtimeSummary()!.panelClass}`}>
              <div class="mb-2 text-[11px] uppercase tracking-[0.18em] text-[#7f8b93]">运行环境</div>
              <div class="grid gap-2 sm:grid-cols-2">
                <For each={runtimeSummary()!.rows}>
                  {(row) => (
                    <div class="min-w-0">
                      <div class="text-[10px] uppercase tracking-[0.12em] text-[#6f7d87]">{row.label}</div>
                      <div class="break-all text-[#d6e3ea]">{row.value}</div>
                    </div>
                  )}
                </For>
              </div>
            </div>
          </Show>

          <Show when={hasArguments()}>
            <div>
              <div class="mb-2 flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.18em] text-[#7f8b93]">
                <span>调用参数</span>
                <span class="normal-case tracking-normal text-[#6f7d87]">{argumentStats().label}</span>
              </div>
              <pre class="tcode-scrollbar max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#2a3136] bg-[#0d1216] p-3 text-[#cfe1e9]">{argumentsText()}</pre>
            </div>
          </Show>

          <Show when={hasResult()}>
            <div>
              <div class="mb-2 flex items-center justify-between gap-3">
                <div class={`text-[11px] uppercase tracking-[0.18em] ${props.tool.error && !getToolVerificationLabel(props.tool) ? 'text-[#ff9c96]' : 'text-[#7f8b93]'}`}>
                  {detailLabel()}
                </div>
                <div class="flex flex-shrink-0 items-center gap-2 text-[11px] text-[#6f7d87]">
                  <Show when={resultStats().isLong}>
                    <span>限制高度</span>
                  </Show>
                  <span>{resultStats().label}</span>
                </div>
              </div>
              <pre class={`tcode-scrollbar max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-xl border p-3 ${props.tool.error && !getToolVerificationLabel(props.tool) ? 'border-[#5a1e23] bg-[#2d1418] text-[#ffc3be]' : 'border-[#2a3136] bg-[#0d1216] text-[#cfe1e9]'}`}>
                {resultText()}
              </pre>
            </div>
          </Show>
        </div>
      </Show>
    </div>
  );
}

function MessageItem(props: MessageItemProps) {
  const [recoveredReport, setRecoveredReport] = createSignal('');
  const [fullReportOpen, setFullReportOpen] = createSignal(false);
  const [copied, setCopied] = createSignal(false);
  const [editing, setEditing] = createSignal(false);
  const [editText, setEditText] = createSignal('');
  const [editPending, setEditPending] = createSignal(false);
  const [editError, setEditError] = createSignal('');
  const submitEdit = async () => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId || editPending() || sessionStore.isRunning() || !editText().trim()) return;
    setEditPending(true);
    setEditError('');
    try {
      const text = editText().trim();
      if (!await confirmModelSwitch(sessionId, sessionStore.currentSession()?.model)) return;
      if (sessionStore.currentSessionId() !== sessionId || sessionStore.isRunning()) return;
      await rewindUserMessage(sessionId, props.message.id);
      if (sessionStore.currentSessionId() !== sessionId) return;
      const messages = sessionStore.messages();
      const cutoff = messages.findIndex(message => message.id === props.message.id);
      if (cutoff < 0) throw new Error('消息已更新，请刷新会话后重试');
      sessionStore.setMessages(messages.slice(0, cutoff));
      await sendMessage(text, {
        model: sessionStore.currentSession()?.model,
        ...(desktop ? { desktop_permission_mode: localStorage.getItem('tcode.desktop.permission-mode') === 'auto' ? 'auto' as const : 'ask' as const } : {}),
      });
    } catch (error) {
      setEditError(error instanceof Error ? error.message : '发送失败，请重试');
    } finally {
      setEditPending(false);
    }
  };
  const [forking, setForking] = createSignal(false);
  const [forkError, setForkError] = createSignal('');
  const isWorkflowFinalReport = createMemo(() => Boolean(completedWorkflowRunId(props.message)));
  const displayContent = createMemo(() => props.message.content);
  createEffect(() => {
    const message = props.message;
    const sessionId = sessionStore.currentSessionId();
    setRecoveredReport('');
    if (!sessionId || !needsWorkflowReportRecovery(message)) return;
    let cancelled = false;
    void getWorkflowRun(completedWorkflowRunId(message), { session_id: sessionId })
      .then(({ run }) => {
        if (!cancelled) setRecoveredReport(recoverWorkflowReport(message, run));
      })
      .catch(() => {
        // Keep the persisted message visible if the run is no longer available.
      });
    onCleanup(() => { cancelled = true; });
  });
  const [verificationExpanded, setVerificationExpanded] = createSignal(false);
  const verificationProblems = createMemo(() => verificationFeedback(props.message.verification));
  const [outcomeActionPending, setOutcomeActionPending] = createSignal(false);
  const [outcomeActionError, setOutcomeActionError] = createSignal('');
  const [outcomeFeedback, setOutcomeFeedback] = createSignal('');
  const [outcomeFeedbackOpen, setOutcomeFeedbackOpen] = createSignal(false);
  const [outcomeRepairLimit, setOutcomeRepairLimit] = createSignal(
    Math.min(5, Math.max(3, Number(props.message.verification?.repair_attempt || 0) + 1)),
  );
  const isWorkflowNarration = createMemo(
    () => props.message.presentation === 'workflow_narration'
  );
  const isLatestAssistantMessage = createMemo(() => {
    if (props.latestAssistantMessageId !== undefined) {
      return props.latestAssistantMessageId === props.message.id;
    }
    const assistantMessages = sessionStore.messages().filter((message) => message.role === 'assistant');
    return assistantMessages[assistantMessages.length - 1]?.id === props.message.id;
  });
  const isAssistantToolProgress = createMemo(
    () => props.message.role === 'assistant' && (
      Boolean(props.message.tool_calls && props.message.tool_calls.length > 0) ||
      Boolean(props.message.process_notes && props.message.process_notes.length > 0)
    )
  );
  const isLiveProcess = createMemo(() =>
    (isAssistantToolProgress() || props.displayMode === 'commentary') &&
    isLatestAssistantMessage() &&
    sessionStore.isRunning()
  );
  const showResponseActions = createMemo(() =>
    props.message.role === 'assistant'
    && props.displayMode !== 'commentary'
    && props.displayMode !== 'process'
    && !isLiveProcess()
    && Boolean(String(props.message.content || '').trim())
  );
  const responseTime = createMemo(() => {
    const timestamp = Number(props.message.metadata?.completed_at || props.message.created_at || 0);
    return formatResponseTime(timestamp);
  });

  const copyResponse = async () => {
    const content = String(props.message.content || '');
    if (!content) return;
    setForkError('');
    try {
      if (desktop?.copyText) {
        await desktop.copyText(content);
      } else {
        let copied = false;
        if (navigator.clipboard?.writeText) {
          copied = await navigator.clipboard.writeText(content).then(() => true, () => false);
        }
        if (!copied) {
          const textarea = document.createElement('textarea');
          textarea.value = content;
          textarea.style.cssText = 'position:fixed;left:-9999px;top:0';
          document.body.append(textarea);
          textarea.select();
          copied = document.execCommand('copy');
          textarea.remove();
        }
        if (!copied) throw new Error('copy failed');
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setForkError('复制失败，请检查剪贴板权限');
    }
  };

  const forkFromResponse = async () => {
    const sourceSessionId = String(sessionStore.currentSessionId() || '').trim();
    if (!sourceSessionId || forking() || sessionStore.isRunning()) return;
    setForking(true);
    setForkError('');
    try {
      const detail = await forkSession(sourceSessionId, props.message.id);
      const timestampMs = (value: unknown) => {
        const parsed = Number(value || Date.now());
        return parsed < 1_000_000_000_000 ? parsed * 1000 : parsed;
      };
      sessionStore.upsertSession({
        session_id: detail.session_id,
        title: detail.title,
        status: detail.status,
        agent_name: detail.agent_name,
        workspace_id: String(detail.workspace_id || ''),
        workdir: String(detail.workdir || ''),
        workdir_name: String(detail.workdir_name || ''),
        mode: detail.mode === 'research' ? 'research' : 'general',
        research_id: String(detail.research_id || ''),
        mode_source: String(detail.mode_source || 'default'),
        metadata: detail.metadata || {},
        messages: detail.messages || [],
        created_at: timestampMs(detail.created_at),
        updated_at: timestampMs(detail.updated_at),
      });
      sessionStore.switchSession(detail.session_id);
      sessionStore.setMessages(detail.messages || []);
      sessionStore.setStatus('已创建分支会话');
    } catch (error) {
      const message = error instanceof Error ? error.message : '分支会话失败';
      setForkError(message);
      sessionStore.setStatus(message);
    } finally {
      setForking(false);
    }
  };
  const isCollapsed = createMemo(() => {
    if (isLiveProcess()) return false;
    const preference = sessionStore.messageToolsCollapsePreference(props.message.id);
    if (preference !== undefined) return preference;
    if (props.displayMode === 'commentary') return false;
    if (props.message.process_notes && props.message.process_notes.length > 0) return false;
    return true;
  });
  const humanReviewAlert = createMemo(() =>
    props.message.role === 'assistant'
      ? parseHumanReviewAlert(String(props.message.content || ''))
      : null
  );
  const asyncCompletionAlert = createMemo(() =>
    props.message.role === 'assistant'
      ? parseAsyncJobCompletionAlert(String(props.message.content || ''))
      : null
  );
  const researchSources = createMemo(() => {
    const raw = props.message.research_grounding?.sources;
    return Array.isArray(raw)
      ? raw.filter((item: any) => item && typeof item === 'object').slice(0, 24)
      : [];
  });
  const wikiActions = createMemo(() => (
    (props.message.tool_calls || [])
      .map((tool) => wikiActionFromTool(tool))
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
  ));
  const visibleResearchActionIndexes = createMemo(() =>
    researchActionDisplayIndexes(props.message.tool_calls || [])
  );
  const verificationSummary = createMemo(() =>
    props.message.role === 'assistant'
      ? outcomeVerificationSummary(props.message.verification)
      : ''
  );
  const verificationStatus = createMemo(() =>
    String(props.message.verification?.verification_status || '').toUpperCase()
  );
  const verificationScore = createMemo(() =>
    Math.round((props.message.verification?.confidence || 0) * 100)
  );
  const verificationStatusLabel = createMemo(() => {
    if (props.message.verification?.superseded) return '已失效';
    if (verificationStatus() === 'VERIFIED') return '已验证';
    if (verificationStatus() === 'PARTIAL') return '部分验证';
    return '未验证';
  });
  const verificationBrief = createMemo(() => {
    const verification = props.message.verification;
    if (!verification) return '';
    if (verification.advisory && verification.delivery_state === 'accepted') return '已接受当前结果并完成，检查记录与置信度保持不变';
    if (verification.advisory && verification.delivery_state === 'modification_requested') return '已提出修改，原有问题记录保留';
    if (verification.superseded) return '该结果已被新的目标或验收标准替代';
    if (verification.advisory && verification.validator === 'semantic_unavailable') return '这次检查未完成，部分内容暂时无法判断';
    const total = verification.criteria_total;
    const passed = verification.criteria_passed;
    let summary = '';
    if (typeof total === 'number' && total > 0 && typeof passed === 'number') {
      if (verificationStatus() === 'VERIFIED') {
        summary = `已通过 ${passed}/${total} 项验收`;
      } else if (verificationStatus() === 'PARTIAL') {
        summary = verification.validator === 'semantic_unavailable'
          ? `${passed}/${total} 项验收通过，独立验收暂不可用，其余条件尚未验证`
          : verification.criteria_failed
            ? `${passed}项通过，${verification.criteria_failed}项需要修改${verification.criteria_unverified ? `，${verification.criteria_unverified}项尚未核实` : ''}`
            : verification.criteria_unverified
              ? `${passed}项通过，${verification.criteria_unverified}项尚未核实`
              : `${passed}项检查通过，仍有内容需要核对`;
      } else {
        summary = `${passed}/${total} 项验收通过，证据暂不充分`;
      }
    } else if (verificationStatus() === 'VERIFIED') {
      summary = '验收条件与证据要求均已满足';
    } else if (verificationStatus() === 'PARTIAL') {
      summary = '部分验收条件已满足，其余条件尚未验证';
    } else {
      summary = '当前结果尚未获得足够的验收证据';
    }
    if (verification.needs_user_confirmation) {
      summary += ' · 仍需用户确认';
    } else if (verification.user_confirmed) {
      summary += ' · 已结合用户确认完成裁决';
    }
    return summary;
  });
  const verificationNarrative = createMemo(() =>
    outcomeVerificationNarrative(props.message.verification)
  );
  const verificationValidatorLabel = createMemo(() => {
    const labels: Record<string, string> = {
      deterministic: '确定性验证',
      semantic: '语义验证',
      semantic_unavailable: '语义验证暂不可用',
      terminal_fallback: '终态兜底验证',
      disabled: '正式验证未启用',
    };
    return labels[String(props.message.verification?.validator || '')] || '';
  });
  const verificationComponents = createMemo(() => {
    const components = props.message.verification?.confidence_components;
    if (!components) return [];
    return [
      { label: '目标检查', value: components.objective_checks },
      { label: '证据覆盖', value: components.evidence_coverage },
      { label: '独立验证', value: components.independent_validator },
      { label: '结果一致性', value: components.agreement },
    ];
  });
  const outcomeConfirmationState = createMemo(() => (
    props.message.verification?.confirmation_state ||
    (props.message.verification?.needs_user_confirmation ? 'pending' : '')
  ));
  const canConfirmOutcome = createMemo(() => (
    props.message.role === 'assistant' &&
    (!props.message.verification?.advisory || (() => {
      const messages = sessionStore.messages();
      const latest = latestDeliveredAssistant(messages);
      if (!latest) return isLatestAssistantMessage();
      const index = messages.indexOf(latest);
      return latest.id === props.message.id && !messages.slice(index + 1).some(message =>
        message.role === 'user' && message.presentation !== 'outcome_confirmation');
    })()) &&
    canReviewOutcome(props.message.verification)
  ));
  const deliveryGatePending = createMemo(() => {
    if (!props.message.verification?.advisory) return false;
    const counts = sessionStore.currentSession()?.task_runtime?.active_counts || {};
    return ['waiting_user', 'waiting_review'].some(key => Number(counts[key] || 0) > 0);
  });
  const deliveryTurnRunning = createMemo(() => {
    if (!props.message.verification?.advisory) return sessionStore.isRunning();
    const sessionId = sessionStore.currentSessionId() || '';
    const runtime = sessionStore.getSessionRuntimeState?.(sessionId);
    return Boolean(hasActiveChatSocket?.(sessionId) ||
      (runtime?.deliveryTurnRunning ?? sessionStore.isRunning()));
  });
  const resolveOutcome = async (decision: 'accept' | 'modify') => {
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId || outcomeActionPending()) return;
    setOutcomeActionPending(true);
    setOutcomeActionError('');
    try {
      await confirmOutcomeResult({
        sessionId,
        messageId: props.message.id,
        decision,
        feedback: decision === 'modify' ? outcomeFeedback().trim() : '',
        maxRepairAttempts: outcomeRepairLimit(),
      });
      if (decision === 'modify') {
        setOutcomeFeedback('');
        setOutcomeFeedbackOpen(false);
      }
    } catch (error) {
      setOutcomeActionError(
        error instanceof Error ? error.message : '验收操作未能完成，请稍后重试。'
      );
    } finally {
      setOutcomeActionPending(false);
    }
  };
  const guidanceMetadata = createMemo(() => props.message.metadata?.guidance);

  return (
      <div
        data-message-id={props.message.id}
        class={`message-enter ${props.message.role === 'user' ? 'user-message-item' : ''} ${
          props.message.role === 'user'
            ? 'rounded-2xl border border-[#2f4552] bg-[#101920] shadow-[0_10px_24px_rgba(0,0,0,0.16)]'
            : props.displayMode === 'commentary' || isWorkflowNarration()
              ? 'rounded-2xl border border-[#31536a] bg-[#101a20] shadow-[0_10px_24px_rgba(0,0,0,0.18)]'
            : 'rounded-2xl border border-[#2a3136] bg-[#13191d] shadow-[0_10px_24px_rgba(0,0,0,0.18)]'
        }`}
      >
      <div class="message-body flex gap-4 p-4 md:p-5">
        {/* Avatar */}
        <div class={`message-avatar flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl border ${props.message.role === 'user' ? 'border-[#38bdf8]/35 bg-[#38bdf8]/12' : 'border-[#3e484f] bg-[#171c20]'}`}>
          <Show
            when={props.message.role === 'user'}
            fallback={
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5 text-[#8ed5ff]">
                <path d="M12 2 4 6v6c0 5.55 3.84 10.74 8 12 4.16-1.26 8-6.45 8-12V6l-8-4Zm0 2.18 5.82 2.91v4.77c0 4.24-2.79 8.59-5.82 9.82-3.03-1.23-5.82-5.58-5.82-9.82V7.09L12 4.18Z" />
              </svg>
            }
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5 text-[#8ed5ff]">
              <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/>
            </svg>
          </Show>
        </div>

        {/* Content */}
        <div class="flex-1 min-w-0">
          <div style={props.message.role === 'user' ? {display:'none'} : undefined} class="message-speaker mb-2 flex flex-wrap items-center gap-2">
            <span class="text-sm font-semibold text-[#f5f7fa]">
              {props.message.role === 'system'
                  ? '系统'
                  : '编排器'}
            </span>
            <span class={`message-role-badge rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] ${props.message.role === 'user' ? 'border-[#38bdf8]/35 bg-[#38bdf8]/10 text-[#8ed5ff]' : props.message.role === 'system' ? 'border-[#f1a02b]/30 bg-[#f1a02b]/10 text-[#ffc176]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`}>
              {props.displayMode === 'commentary' || isWorkflowNarration()
                ? '旁白'
                : props.message.role === 'user'
                ? '用户'
                : props.message.role === 'system'
                  ? '系统'
                  : props.message.role === 'tool'
                    ? '工具'
                    : '助手'}
            </span>
            <Show when={guidanceMetadata()}>
              <span
                data-guidance-message-status
                class="inline-flex items-center gap-1 text-[10px] font-medium text-[#8b949e]"
                title={guidanceMetadata()?.applied_safe_point
                  ? `追加指导已在 ${guidanceMetadata()?.applied_safe_point} 应用`
                  : guidanceMetadata()?.mode === 'queue'
                    ? '追加指导已排队'
                    : '追加指导正在接入当前任务'}
              >
                <span class={`h-1.5 w-1.5 rounded-full ${guidanceMetadata()?.applied_safe_point ? 'bg-[#6f7d87]' : 'animate-pulse bg-[#8ed5ff]'}`} />
                {guidanceMetadata()?.applied_safe_point
                  ? '已应用'
                  : guidanceMetadata()?.mode === 'queue'
                    ? '已排队'
                    : '指导中'}
              </span>
            </Show>
          </div>

          <Show when={props.message.verification?.validator === 'disabled' && verificationStatus() === 'NOT_REQUIRED'}>
            <div class="mb-3 text-xs text-[#89969e]">结果验证已关闭</div>
          </Show>
          <Show when={verificationSummary()}>
            <div
              class={`mb-3 max-w-xl overflow-hidden rounded-xl border bg-[#0d1317] ${
                verificationStatus() === 'VERIFIED'
                  ? 'border-[#285344]'
                  : verificationStatus() === 'PARTIAL'
                    ? 'border-[#594524]'
                    : 'border-[#3a444b]'
              }`}
            >
              <button
                type="button"
                data-verification-score-toggle
                aria-expanded={verificationExpanded()}
                aria-controls={`verification-details-${props.message.id}`}
                onClick={() => setVerificationExpanded((value) => !value)}
                class="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-[#141c21]"
              >
                <span
                  class={`h-2 w-2 flex-shrink-0 rounded-full ${
                    verificationStatus() === 'VERIFIED'
                      ? 'bg-[#52d6a0]'
                      : verificationStatus() === 'PARTIAL'
                        ? 'bg-[#f1a02b]'
                        : 'bg-[#8b98a1]'
                  }`}
                />
                <span class="min-w-0 flex-1">
                  <span class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span class="text-[11px] font-semibold text-[#dce5ea]">{props.message.verification?.advisory ? '参考置信度' : '综合评分'}</span>
                    <span class="text-[10px] text-[#89969e]">{verificationStatusLabel()}</span>
                  </span>
                  <span class="block truncate text-[11px] text-[#9eabb3]" title={verificationBrief()}>
                    {verificationBrief()}
                  </span>
                </span>
                <span class="flex flex-shrink-0 items-center gap-2">
                  <span class={`text-sm font-semibold tabular-nums ${
                    verificationStatus() === 'VERIFIED'
                      ? 'text-[#8ce8c2]'
                      : verificationStatus() === 'PARTIAL'
                        ? 'text-[#ffc176]'
                        : 'text-[#b8c2cc]'
                  }`}>
                    {props.message.verification?.advisory && props.message.verification?.validator === 'semantic_unavailable' && !verificationScore()
                      ? '暂无法评估' : `${verificationScore()}%`}
                  </span>
                  <svg
                    viewBox="0 0 20 20"
                    fill="none"
                    aria-hidden="true"
                    class={`h-3.5 w-3.5 text-[#74828b] transition-transform ${verificationExpanded() ? 'rotate-180' : ''}`}
                  >
                    <path d="m5 7.5 5 5 5-5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </span>
              </button>

              <Show when={props.message.verification?.advisory}>
                <div class="border-t border-[#273138] px-3 py-2 text-[11px] text-[#89969e]">
                  {props.message.verification?.delivery_state === 'accepted' ? '已接受并完成，原有问题仍保留。' : '结果已交付，可继续聊天。'} 点击上方查看需要核对的内容；分数不代表事实正确率。
                </div>
              </Show>
              <Show when={canConfirmOutcome()}>
                <div class="border-t border-[#273138] px-3 py-2.5">
                  <div class="space-y-2.5">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                    <span class="text-[11px] text-[#c8b382]">
                      {props.message.verification?.advisory ? '你可以接受当前结果，也可以提出修改。' : outcomeConfirmationState() === 'modification_requested'
                        ? '上次修改未通过，可以继续补充要求'
                        : outcomeConfirmationState() === 'repair_exhausted'
                          ? '已到重试上限，可以提高上限后继续'
                        : outcomeConfirmationState() === 'objective_failure'
                          ? '客观检查未通过，请要求修改'
                          : '确认是否接受当前结果'}
                    </span>
                    <Show when={!props.message.verification?.advisory}><label class="flex items-center gap-1.5 text-[10px] text-[#89969e]">
                      重试上限
                      <select
                        value={outcomeRepairLimit()}
                        disabled={outcomeActionPending() || sessionStore.isRunning()}
                        onChange={event => setOutcomeRepairLimit(Number(event.currentTarget.value))}
                        class="rounded-md border border-[#34414a] bg-[#0b1115] px-1.5 py-1 text-[#dce5ea]"
                      >
                        <For each={[1, 2, 3, 4, 5]}>{value => <option value={value}>{value} 次</option>}</For>
                      </select>
                    </label></Show>
                    </div>
                    <div class="flex flex-wrap justify-end gap-2">
                    <button
                      type="button"
                      disabled={outcomeActionPending() || deliveryTurnRunning() || deliveryGatePending() || !shouldAttemptOutcomeAcceptance(props.message.verification)}
                      onClick={() => void resolveOutcome('accept')}
                      title={shouldAttemptOutcomeAcceptance(props.message.verification) ? '接受当前结果' : '客观检查未通过，不能直接确认'}
                      class="rounded-full border border-[#38b46a]/30 bg-[#38b46a]/10 px-3 py-1.5 text-[11px] font-semibold text-[#9ee6bb] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {outcomeActionPending() ? '处理中…' : props.message.verification?.advisory ? '接受当前结果并完成' : '用户确认'}
                    </button>
                    <button
                      type="button"
                      disabled={outcomeActionPending() || deliveryTurnRunning() || deliveryGatePending()}
                      onClick={() => setOutcomeFeedbackOpen(true)}
                      class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-3 py-1.5 text-[11px] font-semibold text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      要求修改
                    </button>
                    </div>
                    <Show when={outcomeFeedbackOpen()}>
                      <div role="dialog" aria-label="填写修改要求" class="rounded-xl border border-[#34414a] bg-[#10181d] p-3 shadow-[0_12px_30px_rgba(0,0,0,0.24)]">
                        <div class="mb-2 flex items-center justify-between gap-3">
                          <span class="text-[11px] font-semibold text-[#dce5ea]">填写修改要求</span>
                          <span class="text-[10px] text-[#8ed5ff]">{props.message.verification?.advisory ? '提交后才会开始修改' : '置空则为自动修改'}</span>
                        </div>
                          <textarea aria-label="希望修改的内容"
                          value={outcomeFeedback()}
                          onInput={event => setOutcomeFeedback(event.currentTarget.value)}
                          disabled={outcomeActionPending()}
                          rows={3}
                          autofocus
                          class="w-full resize-y rounded-lg border border-[#34414a] bg-[#0b1115] px-3 py-2 text-[11px] leading-5 text-[#dce5ea] outline-none placeholder:text-[#65737c] focus:border-[#7bd0ff]/45 disabled:opacity-50"
                          placeholder="输入希望调整的内容…"
                        />
                        <div class="mt-2 flex justify-end gap-2">
                          <button type="button" disabled={outcomeActionPending()} onClick={() => setOutcomeFeedbackOpen(false)} class="rounded-full border border-[#3e484f] px-3 py-1.5 text-[11px] text-[#9faeb7] disabled:opacity-50">取消</button>
                          <button type="button" disabled={outcomeActionPending() || (props.message.verification?.advisory && !outcomeFeedback().trim())} onClick={() => void resolveOutcome('modify')} class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-3 py-1.5 text-[11px] font-semibold text-[#8ed5ff] disabled:opacity-50">{outcomeActionPending() ? '提交中…' : '提交修改'}</button>
                        </div>
                      </div>
                    </Show>
                  </div>
                  <Show when={deliveryTurnRunning()}>
                    <div class="mt-1.5 text-[10px] text-[#89969e]">当前任务结束后可处理这项验收</div>
                  </Show>
                  <Show when={deliveryGatePending()}><div class="mt-1.5 text-[11px] text-[#9eabb3]">还有需要回答的问题或单独的人工审核，请先处理；后台计算本身不影响接受当前回答。</div></Show>
                  <Show when={outcomeActionError()}>
                    <div role="alert" class="mt-2 text-[11px] leading-5 text-[#ff9c96]">{outcomeActionError()}</div>
                  </Show>
                </div>
              </Show>

              <Show when={outcomeConfirmationState() === 'repair_exhausted'}>
                <div class="border-t border-[#273138] px-3 py-2.5">
                  <div class="rounded-xl border border-[#ff8b7b]/25 bg-[#ff8b7b]/8 px-3 py-2 text-[11px] leading-5 text-[#ffb0a6]">
                    已达到当前验证重试上限。仍可保存或发布为实验性流程；提高上限可继续修复，验收通过后将升级为已验证流程并参与自动匹配。
                  </div>
                </div>
              </Show>

              <Show when={verificationExpanded()}>
                <div
                  id={`verification-details-${props.message.id}`}
                  data-verification-score-details
                  class="border-t border-[#273138] px-3 py-2.5 text-[11px] text-[#aebbc3]"
                >
                  <For each={verificationProblems().slice(0, 3)}>{problem => <div class="mb-2 leading-relaxed">
                    <span class="font-semibold text-[#ffc176]">{problem.status === 'failed' ? '需要修改' : '尚未核实'}：{problem.title}</span>
                    <div class="mt-1">{problem.reason}</div>
                  </div>}</For>
                  <Show when={verificationProblems().length > 3}><div class="text-[#9eabb3]">另有{verificationProblems().length - 3}项内容需要核对。</div></Show>
                  <Show when={!verificationProblems().length}><div>目前未发现明确的问题；仍建议核对重要结论。</div></Show>
                  <Show when={!props.message.verification?.advisory}>
                  <Show when={verificationNarrative()}>
                    <div class="mb-2 rounded-lg border border-[#2b3941] bg-[#111a1f] px-2.5 py-2 leading-relaxed text-[#cbd5db]">
                      {verificationNarrative()}
                    </div>
                  </Show>
                  <Show when={verificationComponents().length > 0}>
                    <div class="grid grid-cols-2 gap-x-5 gap-y-1.5">
                      <For each={verificationComponents()}>
                        {(item) => (
                          <div class="flex items-center justify-between gap-2">
                            <span>{item.label}</span>
                            <span class="tabular-nums text-[#d0d9de]">
                              {Math.round(item.value * 100)}%
                            </span>
                          </div>
                        )}
                      </For>
                    </div>
                  </Show>
                  <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 border-t border-[#273138] pt-2 text-[#89969e]">
                    <Show when={verificationValidatorLabel()}>
                      <span>验证方式：{verificationValidatorLabel()}</span>
                    </Show>
                    <Show when={props.message.verification?.required_proof_level}>
                      <span>
                        证明等级：{props.message.verification?.achieved_proof_level || 'P0'} / 要求 {props.message.verification?.required_proof_level}
                      </span>
                    </Show>
                  </div>
                  <Show when={props.message.verification?.needs_user_confirmation}>
                    <div class="mt-1.5 text-[#ffc176]">仍需用户确认后才能完成验证</div>
                  </Show>
                  <Show when={props.message.verification?.user_confirmed}>
                    <div class="mt-1.5 text-[#9fc7ae]">
                      确认依据：用户确认仅用于裁决语义验收项，客观验证结果保持不变
                    </div>
                  </Show>
                  </Show>
                </div>
              </Show>
            </div>
          </Show>
          
          <Show when={editing()}>
            <div class="user-message-editor">
              <textarea aria-label="编辑已发送的消息" value={editText()} disabled={editPending()} onInput={event => setEditText(event.currentTarget.value)} onKeyDown={event => {
                if (event.key === 'Escape' && !editPending()) setEditing(false);
                if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); void submitEdit(); }
              }} ref={element => queueMicrotask(() => { element.focus(); element.setSelectionRange(element.value.length, element.value.length); })}/>
              <div class="user-message-edit-footer"><small>从这里重新运行后续对话；不会撤销已执行的操作。</small><button type="button" disabled={editPending()} onClick={() => setEditing(false)}>取消</button><button type="button" disabled={editPending() || sessionStore.isRunning() || !editText().trim()} onClick={() => void submitEdit()}>{editPending() ? '发送中…' : '发送'}</button></div>
            </div>
          </Show>
          <Show when={!editing() && (displayContent() || (props.message.content_blocks?.length || 0) > 0)}>
            <div
              class={`rounded-2xl ${
                props.displayMode === 'commentary' || isWorkflowNarration()
                  ? 'border border-[#31536a] bg-[#0e1820] px-4 py-3.5'
                  : props.message.role === 'assistant'
                  ? 'border border-[#2a3136] bg-[#10161a] px-4 py-3.5'
                  : props.message.role === 'system'
                    ? 'border border-[#4f3f20] bg-[#19160f] px-4 py-3.5'
                    : 'bg-transparent'
              }`}
            >
              <Show when={props.message.role === 'assistant'}>
                <div class="mb-2 text-[11px] uppercase tracking-[0.18em] text-[#7f8b93]">
                  {props.displayMode === 'commentary'
                    ? isLiveProcess() ? '实时行动说明' : '行动说明'
                    : isWorkflowNarration()
                      ? '任务进展与结果'
                    : isLiveProcess()
                      ? '实时过程'
                      : props.displayMode === 'process'
                      ? '阶段结果'
                      : '最终结果'}
                </div>
              </Show>
              <Show when={displayContent()}>
                <UserMessageText enabled={props.message.role === 'user'} contentKey={`${props.message.id}:${displayContent()}`}>
                <div class={isWorkflowFinalReport() ? 'tcode-scrollbar max-h-[70vh] overflow-y-auto overscroll-x-contain overscroll-y-auto' : ''}>
                  <Show
                    when={humanReviewAlert()}
                    fallback={
                      <Show
                        when={asyncCompletionAlert()}
                        fallback={
                          <Show
                            when={isJsonText(String(displayContent() || ''))}
                            fallback={
                              <Show
                                when={isWorkflowNarration() && String(props.message.id || '').startsWith('workflow-run:')}
                                fallback={<MarkdownContent
                                content={String(displayContent() || '')}
                                role={props.message.role}
                                renderKey={String(props.message.id || 'message')}
                                class="text-sm text-[#dee3e8]"
                                />}
                              >
                                <WorkflowReply content={String(displayContent() || '')} messageId={String(props.message.id || 'message')} />
                              </Show>
                            }
                          >
                            <pre class="max-h-[70vh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[#080d10] p-3 text-xs leading-relaxed text-[#dee3e8]">
                              {String(displayContent() || '')}
                            </pre>
                          </Show>
                        }
                      >
                        {(alert) => <AsyncJobCompletionCard alert={alert()} />}
                      </Show>
                    }
                  >
                    {(alert) => <HumanReviewRequiredCard alert={alert()} />}
                  </Show>
                </div>
                </UserMessageText>
              </Show>
              <StructuredContentBlocks
                blocks={props.message.content_blocks}
                class={props.message.content ? 'mt-3' : ''}
              />
              <Show when={needsWorkflowReportRecovery(props.message) && recoveredReport()}>
                <button
                  type="button"
                  class="mt-3 rounded-full border border-[#38b46a]/35 bg-[#38b46a]/12 px-3 py-1.5 text-xs font-semibold text-[#9ee6bb] transition-colors hover:border-[#38b46a]/55 hover:bg-[#38b46a]/18"
                  onClick={() => setFullReportOpen(true)}
                >查看完整报告</button>
              </Show>
            </div>
          </Show>

          <Show when={fullReportOpen() && recoveredReport()}>
            <Portal>
              <div class="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4" onClick={() => setFullReportOpen(false)}>
                <section class="flex max-h-[88vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-[#34414a] bg-[#10161a] shadow-2xl" onClick={event => event.stopPropagation()}>
                  <header class="flex items-center justify-between border-b border-[#2a3136] px-4 py-3">
                    <div><div class="text-xs font-semibold text-[#f5f7fa]">Workflow 完整报告</div><div class="mt-1 font-mono text-[10px] text-[#71808a]">{completedWorkflowRunId(props.message)}</div></div>
                    <button type="button" class="rounded-full border border-[#3e484f] px-3 py-1 text-xs text-[#b9c7cf]" onClick={() => setFullReportOpen(false)}>关闭</button>
                  </header>
                  <div class="tcode-scrollbar overflow-y-auto p-5"><MarkdownContent content={recoveredReport()} role="assistant" renderKey={`workflow-report-${completedWorkflowRunId(props.message)}`} class="text-sm text-[#dee3e8]" /></div>
                </section>
              </div>
            </Portal>
          </Show>

          <Show when={props.message.role === 'assistant' && researchSources().length > 0}>
            <details class="mt-3 rounded-xl border border-[#31536a] bg-[#10202a] px-3 py-2 text-xs text-[#b9d9e8]">
              <summary class="cursor-pointer select-none font-semibold text-[#8ed5ff]">
                本回答使用了 {researchSources().length} 条 ResearchPack 片段
              </summary>
              <div class="mt-2 space-y-1.5">
                <For each={researchSources()}>
                  {(source: any) => (
                    <div class="break-words rounded-lg border border-[#2a4658] bg-[#0d1920] px-2.5 py-2">
                      <div class="font-medium text-[#d5effb]">
                        {String(source.asset_title || source.asset_id || 'ResearchPack')}
                        {source.object_type ? ` · ${String(source.object_type)}` : ''}
                      </div>
                      <div class="mt-0.5 font-mono text-[10px] text-[#7896a7]">
                        {String(source.logical_path || source.object_id || source.chunk_id || '')}
                        {Number(source.line_start || 0) > 0
                          ? `:${Number(source.line_start)}${Number(source.line_end || 0) > Number(source.line_start || 0) ? `-${Number(source.line_end)}` : ''}`
                          : ''}
                      </div>
                    </div>
                  )}
                </For>
              </div>
            </details>
          </Show>

          <Show when={props.message.role === 'assistant' && wikiActions().length > 0}>
            <div class="space-y-3">
              <For each={wikiActions()}>
                {(action) => <WikiActionCard action={action} />}
              </For>
            </div>
          </Show>

          {/* Tool calls are execution records; public narration is a separate commentary message. */}
          <Show when={props.message.tool_calls && props.message.tool_calls.length > 0}>
            <div class="mt-4 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3">
              <button
                class="flex w-full items-center justify-between gap-2 text-xs text-[#9faeb7] hover:text-[#e6edf3]"
                onClick={() => sessionStore.setMessageToolsCollapsed(props.message.id, !isCollapsed())}
              >
                <span class="flex items-center gap-1">
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    class={`h-4 w-4 transition-transform ${isCollapsed() ? '' : 'rotate-90'}`}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                  </svg>
                  工具调用 ({props.message.tool_calls?.length || 0})
                </span>
                <span class="text-[11px] text-[#6f7d87]">
                  {isLiveProcess() && !isCollapsed() ? '实时展开' : isCollapsed() ? '已折叠' : '展开中'}
                </span>
              </button>
              
              <Show when={!isCollapsed()}>
                <div class="mt-2 space-y-2">
                  <Show when={props.message.tool_calls && props.message.tool_calls.length > 0}>
                    <For each={props.message.tool_calls}>
                      {(tool, index) => (
                        <ToolCallItem
                          messageId={props.message.id}
                          tool={tool}
                          index={index()}
                          showResearchAction={visibleResearchActionIndexes().has(index())}
                        />
                      )}
                    </For>
                  </Show>
                </div>
              </Show>
            </div>
          </Show>

          <Show when={props.message.role === 'user' && !editing()}>
            <div class="user-message-actions">
              <Show when={copied()}><span role="status" class="text-xs">已复制</span></Show>
              <time>{responseTime()}</time>
              <button type="button" aria-label={copied() ? '已复制消息' : '复制消息'} title="复制" onClick={() => void copyResponse()}><svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="8" y="8" width="12" height="12" rx="3"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg></button>
              <button type="button" aria-label="编辑消息" title={sessionStore.isRunning() ? '任务结束后可编辑' : '再次编辑'} disabled={sessionStore.isRunning()} onClick={() => { setEditText(props.message.content); setEditError(''); setEditing(true); }}><svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m15 4 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14z"/></svg></button>
            </div>
          </Show>
          <Show when={editError()}><p role="alert" class="user-message-edit-error">{editError()}</p></Show>
          <Show when={props.message.role === 'user' && forkError()}><p role="alert" class="user-message-edit-error">{forkError()}</p></Show>
          {/* Usage info */}
          <Show when={props.message.usage}>
            <div class="mt-3 inline-flex items-center gap-2 rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7]">
              令牌 {props.message.usage!.prompt_tokens} / {props.message.usage!.completion_tokens} / {props.message.usage!.total_tokens}
              <Show when={responseTime()}>{(time) => <span>· {time()}</span>}</Show>
            </div>
          </Show>

          <Show when={showResponseActions()}>
            <div class="group/response-actions mt-2 flex items-center gap-1 text-[#7f8b93]">
              <span class="group relative inline-flex">
                <button
                  type="button"
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-[#20282d] hover:text-[#dce5ea]"
                  aria-label={copied() ? '已复制回答' : '复制回答'}
                  onClick={() => void copyResponse()}
                >
                  <Show
                    when={copied()}
                    fallback={
                      <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                        <rect x="9" y="9" width="11" height="11" rx="2" />
                        <path stroke-linecap="round" stroke-linejoin="round" d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3" />
                      </svg>
                    }
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-[#9ee6bb]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path stroke-linecap="round" stroke-linejoin="round" d="m5 12 4 4L19 6" />
                    </svg>
                  </Show>
                </button>
                <span role="tooltip" class="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-md border border-[#3e484f] bg-[#11171b] px-2 py-1 text-[11px] text-[#dce5ea] opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                  {copied() ? '已复制' : '复制回答'}
                </span>
              </span>
              <span class="group relative inline-flex">
                <button
                  type="button"
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-[#20282d] hover:text-[#dce5ea] disabled:cursor-not-allowed disabled:opacity-45"
                  aria-label="从此回答分支到新聊天"
                  disabled={forking() || sessionStore.isRunning()}
                  onClick={() => void forkFromResponse()}
                >
                  <Show
                    when={forking()}
                    fallback={
                      <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                        <circle cx="6" cy="5" r="2" />
                        <circle cx="18" cy="7" r="2" />
                        <circle cx="6" cy="19" r="2" />
                        <path stroke-linecap="round" stroke-linejoin="round" d="M6 7v10M8 11h4a6 6 0 0 0 6-2" />
                      </svg>
                    }
                  >
                    <span class="h-4 w-4 animate-spin rounded-full border-2 border-[#65737c] border-t-[#dce5ea]" />
                  </Show>
                </button>
                <span role="tooltip" class="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-md border border-[#3e484f] bg-[#11171b] px-2 py-1 text-[11px] text-[#dce5ea] opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                  {sessionStore.isRunning() ? '当前回答结束后可分支' : '从此回答分支到新聊天'}
                </span>
              </span>
              <button type="button" class="side-chat-response-trigger" aria-label="针对这条回复打开侧聊"
                onClick={()=>window.dispatchEvent(new CustomEvent('tcode-open-side-chat',{detail:{parentSessionId:sessionStore.currentSessionId(),sourceMessageId:props.message.id}}))}>侧聊</button>
              <Show when={responseTime()}>
                <span class="ml-1 text-[11px] opacity-0 transition-opacity group-hover/response-actions:opacity-100 group-focus-within/response-actions:opacity-100">
                  {responseTime()}
                </span>
              </Show>
              <Show when={forkError()}>
                <span class="ml-1 text-[11px] text-[#ff9c96]">{forkError()}</span>
              </Show>
            </div>
          </Show>
        </div>
      </div>
      </div>
  );
}

export default MessageItem;
