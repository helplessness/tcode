import { confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, onMount } from 'solid-js';
import { Portal } from 'solid-js/web';
import {
  attachWorkflowCandidate,
  confirmWorkflowDraft,
  createWorkflowDraft,
  deleteWorkflowDraft,
  approveWorkflowProposal,
  createWorkflowRun,
  deleteWorkflowRun,
  deleteWorkflowTemplate,
  executeWorkflowStage,
  getSession,
  getSubagents,
  getWorkflowTemplates,
  getWorkflowRunEvidence,
  listWorkflowDrafts,
  listWorkflowRuns,
  rejectWorkflowProposal,
  restartWorkflowDraft,
  rerunWorkflowFromStage,
  saveWorkflowTemplate,
  saveWorkflowDraftAsTemplateDraft,
  publishWorkflowDraft,
  updateWorkflowDraft,
  terminateWorkflowRun,
  transitionWorkflowStage,
  type WorkflowScope,
  type WorkflowApplicabilityAssessment,
  type WorkflowProposal,
  type WorkflowRun,
  type WorkflowDraft,
  type WorkflowTemplate,
  type WorkflowStageEvidence,
  WorkflowApplicabilityConfirmationRequired,
} from '../api/client';
import type { Message } from '../stores/session';
import MarkdownContent from './MarkdownContent';
import { openCitation } from '../utils/citations';
import { readableSourceLabel } from '../utils/evidenceDisplay';
import {
  asyncRefLabel,
  buildCandidateFieldObject,
  createCandidateFieldRow,
  formatList,
  formatTimestamp,
  latestWorkflowEvents,
  normalizeOptionalNumber,
  pendingWorkflowProposals,
  proposalPreview,
  proposalTypeLabel,
  refreshOpenWorkflowRecord,
  resolveWorkflowRunSelection,
  stabilizeWorkflowRuns,
  stageAsyncRefs,
  stageInputGuardNotice,
  stageStatusLabel,
  statusClass,
  statusLabel,
  suggestCandidateId,
  workflowAsyncHistory,
  workflowDetailExpansionKey,
  workflowDetailIsExpanded,
  workflowFinalReport,
  workflowInputPayloadFromDraft,
  workflowRunAllowsHistoryMutation,
  workflowRunNeedsSync,
  workflowStageCanRerun,
  updateWorkflowDetailExpansion,
  type CandidateFieldRow,
  type WorkflowDetailExpansionState,
} from './WorkflowWorkspacePanel.helpers';
import {
  WorkflowBlockedValidation,
  WorkflowValidationReportPanel,
  workflowValidationFailures,
} from './WorkflowValidationReport';
import { sessionStore } from '../stores/session';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import WorkflowDag from './WorkflowDag';
import type { WorkflowDagStage } from '../utils/workflowDag';

type WorkflowWorkspacePanelProps = {
  active?: boolean;
  mode?: 'templates' | 'temporary';
  focusRequest?: {sessionId?: string; runId?: string; templateKey?: string; draftId?: string};
};

const WORKFLOW_STAGE_POLL_INTERVAL_MS = 5000;
const WORKFLOW_SELECTED_TEMPLATE_STORAGE_KEY = 'tcode.workflow.selected-template';
const WORKFLOW_CANDIDATE_PAGE_SIZE = 10;
const WORKFLOW_EVENT_PAGE_SIZE = 10;
type WorkflowRefreshOptions = {
  selectLatest?: boolean;
  silent?: boolean;
  runsOnly?: boolean;
  sync?: boolean;
};
type PendingWorkflowConfirmation = {
  assessment: WorkflowApplicabilityAssessment;
  template: Record<string, any>;
  templateKey: string;
  inputPayload: Record<string, any>;
  scope: WorkflowScope;
  planHash: string;
  revision: number;
};
type WorkflowExecutionRecord = {
  runId: string;
  sessionId: string;
  stageId: string;
  agentName: string;
  title: string;
  status: string;
  createdAt: number;
  messages: Message[];
};

export default function WorkflowWorkspacePanel(props: WorkflowWorkspacePanelProps = {}) {
  const [templates, setTemplates] = createSignal<WorkflowTemplate[]>([]);
  const [drafts, setDrafts] = createSignal<WorkflowDraft[]>([]);
  const [catalogMode, setCatalogMode] = createSignal<'templates' | 'temporary'>(props.mode || 'templates');
  const [selectedDraftId, setSelectedDraftId] = createSignal('');
  const [draftGoal, setDraftGoal] = createSignal('');
  const [draftJson, setDraftJson] = createSignal('');
  const [draftAction, setDraftAction] = createSignal('');
  const [publishName, setPublishName] = createSignal('');
  const [publishDescription, setPublishDescription] = createSignal('');
  const [publishTemplateKey, setPublishTemplateKey] = createSignal('');
  const [runs, setRuns] = createSignal<WorkflowRun[]>([]);
  const [selectedTemplateKey, setSelectedTemplateKey] = createSignal((() => {
    try { return localStorage.getItem(WORKFLOW_SELECTED_TEMPLATE_STORAGE_KEY) || ''; }
    catch { return ''; }
  })());
  const [selectedRunId, setSelectedRunId] = createSignal('');
  const [selectedStageId, setSelectedStageId] = createSignal('');
  const [transitionStatusValue, setTransitionStatusValue] = createSignal<'running' | 'committed' | 'failed'>('committed');
  const [transitionNote, setTransitionNote] = createSignal('');
  const [candidateId, setCandidateId] = createSignal('');
  const [candidateSourceStageId, setCandidateSourceStageId] = createSignal('');
  const [candidateStatus, setCandidateStatus] = createSignal('draft');
  const [candidateRank, setCandidateRank] = createSignal('');
  const [candidateSummary, setCandidateSummary] = createSignal('');
  const [candidateDockingScore, setCandidateDockingScore] = createSignal('');
  const [candidateInteractionCount, setCandidateInteractionCount] = createSignal('');
  const [candidateConfidenceScore, setCandidateConfidenceScore] = createSignal('');
  const [candidatePosePath, setCandidatePosePath] = createSignal('');
  const [candidateStructurePath, setCandidateStructurePath] = createSignal('');
  const [candidateReportPath, setCandidateReportPath] = createSignal('');
  const [candidateMetricRows, setCandidateMetricRows] = createSignal<CandidateFieldRow[]>([]);
  const [candidateArtifactRows, setCandidateArtifactRows] = createSignal<CandidateFieldRow[]>([]);
  const [loading, setLoading] = createSignal(false);
  const [creating, setCreating] = createSignal(false);
  const [actionLoading, setActionLoading] = createSignal('');
  const [actionFeedback, setActionFeedback] = createSignal('');
  const [error, setError] = createSignal('');
  const [lastUpdatedAt, setLastUpdatedAt] = createSignal('');
  const [advancedSectionExpanded, setAdvancedSectionExpanded] = createSignal(false);
  const [templateManagerExpanded, setTemplateManagerExpanded] = createSignal(false);
  const [stageSectionExpanded, setStageSectionExpanded] = createSignal(false);
  const [candidateSectionExpanded, setCandidateSectionExpanded] = createSignal(false);
  const [eventSectionExpanded, setEventSectionExpanded] = createSignal(false);
  const [executionSectionExpanded, setExecutionSectionExpanded] = createSignal(false);
  const [selectedExecutionRecord, setSelectedExecutionRecord] = createSignal<WorkflowExecutionRecord | null>(null);
  const [executionRecordMaximized, setExecutionRecordMaximized] = createSignal(false);
  const [executionRecordSize, setExecutionRecordSize] = createSignal<{ width: number; height: number } | null>(null);
  const [finalReportDetailOpen, setFinalReportDetailOpen] = createSignal(false);
  const [detailExpansion, setDetailExpansion] = createSignal<WorkflowDetailExpansionState>({});
  const [candidatePage, setCandidatePage] = createSignal(1);
  const [eventPage, setEventPage] = createSignal(1);
  const [executionRecords, setExecutionRecords] = createSignal<WorkflowExecutionRecord[]>([]);
  const [executionRecordsLoading, setExecutionRecordsLoading] = createSignal(false);
  const [stageEvidence, setStageEvidence] = createSignal<Record<string, WorkflowStageEvidence>>({});
  const [targetDrafts, setTargetDrafts] = createSignal<Record<string, string>>({});
  const targetDescription = () => targetDrafts()[selectedTemplateKey()] || '';
  const setTargetDescription = (value: string) => {
    const templateKey = selectedTemplateKey();
    if (templateKey) setTargetDrafts(previous => ({ ...previous, [templateKey]: value }));
  };
  const [templateJsonDraft, setTemplateJsonDraft] = createSignal('');
  const [templateOverwrite, setTemplateOverwrite] = createSignal(false);
  const [templateForceDelete, setTemplateForceDelete] = createSignal(false);
  const [templateActionLoading, setTemplateActionLoading] = createSignal<'save' | 'delete' | ''>('');
  const [pendingCreationConfirmation, setPendingCreationConfirmation] = createSignal<PendingWorkflowConfirmation | null>(null);
  const [dagHeight, setDagHeight] = createSignal(300);
  const pageVisible = createPageVisibleSignal();
  const isActive = () => (props.active ?? true) && pageVisible();
  let workflowRefreshInFlight: Promise<void> | null = null;
  let stopDagResize: (() => void) | null = null;
  let stopExecutionRecordResize: (() => void) | null = null;
  let templateSelectElement: HTMLSelectElement | undefined;

  const startDagResize = (event: PointerEvent) => {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = dagHeight();
    const move = (moveEvent: PointerEvent) => {
      setDagHeight(Math.min(window.innerHeight * .72, Math.max(210, startHeight + moveEvent.clientY - startY)));
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      stopDagResize = null;
    };
    stopDagResize?.();
    stopDagResize = stop;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop, { once: true });
  };

  onCleanup(() => stopDagResize?.());

  const startExecutionRecordResize = (event: PointerEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const dialog = (event.currentTarget as HTMLElement | null)?.closest('section');
    const rect = dialog?.getBoundingClientRect();
    const startWidth = rect?.width ?? Math.min(768, window.innerWidth - 24);
    const startHeight = rect?.height ?? window.innerHeight * 0.72;
    const startX = event.clientX;
    const startY = event.clientY;
    const move = (moveEvent: PointerEvent) => {
      const maxWidth = window.innerWidth - 24;
      const maxHeight = window.innerHeight - 24;
      const width = Math.min(maxWidth, Math.max(360, startWidth + (moveEvent.clientX - startX)));
      const height = Math.min(maxHeight, Math.max(320, startHeight + (moveEvent.clientY - startY)));
      setExecutionRecordSize({ width, height });
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
      stopExecutionRecordResize = null;
    };
    stopExecutionRecordResize?.();
    stopExecutionRecordResize = stop;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop, { once: true });
    window.addEventListener('pointercancel', stop, { once: true });
  };

  const executionRecordDialogStyle = () => {
    if (executionRecordMaximized()) return undefined;
    const size = executionRecordSize();
    if (!size) return undefined;
    return {
      width: `${Math.min(size.width, window.innerWidth - 24)}px`,
      height: `${Math.min(size.height, window.innerHeight - 24)}px`,
    };
  };

  onCleanup(() => stopExecutionRecordResize?.());

  const currentSession = createMemo(() => sessionStore.currentSession());
  const currentScope = createMemo(() => ({
    session_id: String(sessionStore.currentSessionId() || '').trim(),
    workdir_name: String(currentSession()?.workdir_name || '').trim() || undefined,
    workdir_path: String(currentSession()?.workdir || '').trim() || undefined,
  }));

  createEffect(on(
    () => [selectedRunId(), scopeKey(currentScope())] as const,
    ([runId]) => {
      setStageEvidence({});
      if (!runId) return;
      void getWorkflowRunEvidence(runId, currentScope())
        .then((result) => setStageEvidence(Object.fromEntries(
          result.stages.map((item) => [item.stage_id, item]),
        )))
        .catch((cause) => setError(cause instanceof Error ? cause.message : 'Workflow 证据加载失败'));
    },
  ));

  const scopeKey = (scope: WorkflowScope) => [
    String(scope.session_id || '').trim(),
    String(scope.workdir_name || '').trim(),
    String(scope.workdir_path || '').trim(),
  ].join('|');

  const selectedTemplate = createMemo(() => {
    const currentKey = selectedTemplateKey();
    const enabledTemplates = templates().filter((item) => item.enabled !== false);
    return templates().find((item) => item.template_key === currentKey) || enabledTemplates[0] || templates()[0] || null;
  });

  const selectedTemplateEnabled = createMemo(() => selectedTemplate()?.enabled !== false);
  const selectedDraft = createMemo(() => drafts().find(item => item.draft_id === selectedDraftId()) || drafts()[0] || null);
  const selectedDraftRun = createMemo(() => runs().find(item => item.run_id === selectedDraft()?.source_run_id) || null);
  const selectedDraftCheckpointStage = createMemo(() => {
    const run = selectedDraftRun();
    const stages = run?.stage_states || [];
    return stages.find(stage => stage.stage_id === run?.current_stage_id)
      || stages.find(stage => String(stage.status || '').trim().toLowerCase() !== 'committed')
      || stages[stages.length - 1]
      || null;
  });
  const selectedDraftRunDagStages = createMemo<WorkflowDagStage[]>(() => {
    const run = selectedDraftRun();
    const templateStages = selectedDraft()?.template.stages || [];
    if (!run) return templateStages;
    return run.stage_states.map(stage => {
      const template = templateStages.find(item => item.stage_id === stage.stage_id);
      return { ...template, ...stage, depends_on: template?.depends_on || [] };
    });
  });
  const selectedDraftStatus = createMemo(() => {
    const run = selectedDraftRun();
    const acceptance = String(run?.metadata?.delivery_record?.acceptance || '');
    if (acceptance === 'accepted') return 'accepted';
    if (run?.status === 'completed') return 'review_pending';
    return run?.status || selectedDraft()?.status || '';
  });

  // Solid's `value` initializes a native select, but replacing its option list
  // can make Chromium visually fall back to the first option. Keep the native
  // control synchronized with the state that already drives the DAG.
  createEffect(() => {
    const key = selectedTemplateKey();
    templates().length;
    if (templateSelectElement && templateSelectElement.value !== key) {
      templateSelectElement.value = key;
    }
    if (key) {
      try { localStorage.setItem(WORKFLOW_SELECTED_TEMPLATE_STORAGE_KEY, key); }
      catch { /* The selection still remains stable for this mounted panel. */ }
    }
  });

  const selectedRun = createMemo(() => {
    const runId = selectedRunId();
    return runs().find((item) => item.run_id === runId) || runs()[0] || null;
  });

  const activeRuns = createMemo(() => runs().filter(run => {
    const status = String(run.status || '').trim().toLowerCase();
    return ['queued', 'running', 'retrying', 'waiting_external_job', 'collecting', 'validating', 'review_pending'].includes(status);
  }));
  const selectedTemplateRun = createMemo(() => {
    const key = selectedTemplateKey();
    if (!key) return null;
    const selected = selectedRun();
    if (selected?.template_key === key) return selected;
    return activeRuns().find(run => run.template_key === key) || null;
  });
  const selectedRunDagStages = createMemo<WorkflowDagStage[]>(() => {
    const run = selectedTemplateRun();
    if (!run) return [];
    const templateStages = templates().find(template => template.template_key === run.template_key)?.stages || [];
    return run.stage_states.map(stage => {
      const template = templateStages.find(item => item.stage_id === stage.stage_id);
      return { ...template, ...stage, depends_on: template?.depends_on || [] };
    });
  });
  const selectedRunAllowsHistoryMutation = createMemo(() => (
    workflowRunAllowsHistoryMutation(selectedRun())
  ));

  const selectedRunCheckpointStage = createMemo(() => {
    const run = selectedRun();
    if (!run) return null;
    const stages = Array.isArray(run.stage_states) ? run.stage_states : [];
    return stages.find((stage) => stage.stage_id === run.current_stage_id)
      || stages.find((stage) => String(stage.status || '').trim().toLowerCase() !== 'committed')
      || stages[stages.length - 1]
      || null;
  });

  const stageStatusSummary = createMemo(() => {
    const stages = Array.isArray(selectedRun()?.stage_states) ? selectedRun()!.stage_states : [];
    const total = stages.length;
    const completed = stages.filter((stage) => {
      const normalized = String(stage.status || '').trim().toLowerCase();
      return normalized === 'committed';
    }).length;
    return { total, completed };
  });

  const candidateStats = createMemo(() => {
    const candidates = Array.isArray(selectedRun()?.candidates) ? selectedRun()!.candidates : [];
    return {
      total: candidates.length,
      ranked: candidates.filter((candidate) => candidate.rank !== null && candidate.rank !== undefined).length,
    };
  });

  const allCandidates = createMemo(() => {
    return Array.isArray(selectedRun()?.candidates) ? selectedRun()!.candidates : [];
  });

  const candidatePageCount = createMemo(() => {
    return Math.max(1, Math.ceil(allCandidates().length / WORKFLOW_CANDIDATE_PAGE_SIZE));
  });

  const pagedCandidates = createMemo(() => {
    const totalPages = candidatePageCount();
    const currentPage = Math.min(Math.max(1, candidatePage()), totalPages);
    const start = (currentPage - 1) * WORKFLOW_CANDIDATE_PAGE_SIZE;
    return allCandidates().slice(start, start + WORKFLOW_CANDIDATE_PAGE_SIZE);
  });

  const allWorkflowEvents = createMemo(() => latestWorkflowEvents(selectedRun()));

  const eventSummary = createMemo(() => {
    const recent = allWorkflowEvents();
    return {
      total: recent.length,
    };
  });

  const eventPageCount = createMemo(() => {
    return Math.max(1, Math.ceil(allWorkflowEvents().length / WORKFLOW_EVENT_PAGE_SIZE));
  });

  const pagedWorkflowEvents = createMemo(() => {
    const totalPages = eventPageCount();
    const currentPage = Math.min(Math.max(1, eventPage()), totalPages);
    const start = (currentPage - 1) * WORKFLOW_EVENT_PAGE_SIZE;
    return allWorkflowEvents().slice(start, start + WORKFLOW_EVENT_PAGE_SIZE);
  });

  const visibleExecutionRecords = createMemo(() => {
    const stageId = String(selectedStageId() || '').trim();
    return stageId ? executionRecords().filter(record => record.stageId === stageId) : executionRecords();
  });

  const loadExecutionRecords = async (run: WorkflowRun | null | undefined) => {
    if (!run?.session_id) {
      setExecutionRecords([]);
      return;
    }
    setExecutionRecordsLoading(true);
    try {
      const children = (await getSubagents(run.session_id)).filter(item =>
        item.session_id.startsWith(`workflow-agent-${run.run_id}-`),
      );
      const stageBySession = new Map(
        run.stage_states.map(stage => [String(stage.metadata?.agent_session_id || ''), stage.stage_id]),
      );
      const records = await Promise.all(children.map(async item => {
        const detail = await getSession(item.session_id, { includeToolMessages: true, bustCache: true });
        const stageId = stageBySession.get(item.session_id)
          || run.stage_states.find(stage => item.session_id.includes(`-${stage.stage_id}-`))?.stage_id
          || '';
        return {
          runId: run.run_id,
          sessionId: item.session_id,
          stageId,
          agentName: item.agent_name,
          title: item.display_name || item.title || item.agent_name,
          status: item.lifecycle_status || item.status,
          createdAt: Number(item.created_at || 0),
          messages: detail?.messages || [],
        } satisfies WorkflowExecutionRecord;
      }));
      if (selectedRun()?.run_id === run.run_id) {
        const nextRecords = records.sort((a, b) => a.createdAt - b.createdAt);
        setExecutionRecords(nextRecords);
        setSelectedExecutionRecord(opened => refreshOpenWorkflowRecord(opened, nextRecords, run.run_id));
      }
    } finally {
      if (selectedRun()?.run_id === run.run_id) setExecutionRecordsLoading(false);
    }
  };

  const selectedStageSupportsAutoRun = createMemo(() => {
    const normalized = String(selectedStageId() || '').trim();
    if (!normalized) return false;
    return Boolean(selectedRun()?.stage_states.some((stage) => stage.stage_id === normalized));
  });

  const selectedStageGuardNotice = createMemo(() => {
    const run = selectedRun();
    if (!run) return '';
    const stages = Array.isArray(run.stage_states) ? run.stage_states : [];
    const selectedStage = stages.find((stage) => stage.stage_id === String(selectedStageId() || '').trim());
    if (selectedStage) {
      return stageInputGuardNotice(selectedStage);
    }
    const currentStage = stages.find((stage) => stage.stage_id === String(run.current_stage_id || '').trim());
    return stageInputGuardNotice(currentStage || null);
  });

  const focusedRunStage = createMemo(() => {
    const run = selectedRun();
    if (!run) return null;
    const stages = Array.isArray(run.stage_states) ? run.stage_states : [];
    const selectedStage = stages.find((stage) => stage.stage_id === String(selectedStageId() || '').trim());
    if (selectedStage) return selectedStage;
    const currentStage = stages.find((stage) => stage.stage_id === String(run.current_stage_id || '').trim());
    if (currentStage) return currentStage;
    return stages.find((stage) => {
      const normalized = String(stage.status || '').trim().toLowerCase();
      return normalized === 'running'
        || normalized === 'blocked'
        || normalized === 'waiting_external_job'
        || normalized === 'collecting'
        || normalized === 'review_pending'
        || normalized === 'validating';
    }) || stages[0] || null;
  });

  const selectedRunBlockingReason = createMemo(() => {
    const run = selectedRun();
    if (!run) return '';
    const pendingFields = Array.isArray(run.metadata?.clarification_pending_fields)
      ? run.metadata.clarification_pending_fields
          .map((item: any) => String(item || '').trim())
          .filter(Boolean)
      : [];
    if (pendingFields.length > 0) {
      return `等待补充：${pendingFields.join('、')}`;
    }

    const stage = focusedRunStage();
    if (stage?.diagnostic) return `${stage.diagnostic.title}：${stage.diagnostic.message} 处理建议：${stage.diagnostic.suggestion}`;
    if (stage?.last_error) return stage.diagnostic
      ? `${stage.diagnostic.title}：${stage.diagnostic.message} 处理建议：${stage.diagnostic.suggestion}`
      : '当前步骤未完成，请展开阶段状态查看具体原因和保留结果。';
    if (stage?.result_summary) {
      const stageStatus = String(stage.status || '').trim().toLowerCase();
      if (stageStatus === 'waiting_external_job' || stageStatus === 'collecting' || stageStatus === 'review_pending') {
        return String(stage.result_summary || '').trim();
      }
    }
    return '';
  });

  const asyncHistory = createMemo(() => workflowAsyncHistory(selectedRun()));
  const finalReportText = createMemo(() => workflowFinalReport(selectedRun()));

  createEffect(on(
    () => selectedRunId(),
    (runId) => {
      if (!runId) return;
      setExecutionSectionExpanded(false);
      if (selectedExecutionRecord()?.runId !== runId) setSelectedExecutionRecord(null);
      setExecutionRecordMaximized(false);
      setFinalReportDetailOpen(false);
    },
    { defer: true },
  ));

  createEffect(() => {
    if (!finalReportDetailOpen() && !selectedExecutionRecord()) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setSelectedExecutionRecord(null);
      setExecutionRecordMaximized(false);
      setFinalReportDetailOpen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    onCleanup(() => window.removeEventListener('keydown', handleKeyDown));
  });

  const upsertRun = (run: WorkflowRun) => {
    setRuns((prev) => stabilizeWorkflowRuns(
      prev,
      [run, ...prev.filter((item) => item.run_id !== run.run_id)],
    ));
    setSelectedRunId(run.run_id);
    setLastUpdatedAt(new Date().toLocaleTimeString());
    return run;
  };

  const loadSelectedTemplateIntoDraft = () => {
    const template = selectedTemplate();
    if (!template) {
      setTemplateJsonDraft('');
      return;
    }
    setTemplateJsonDraft(JSON.stringify(template, null, 2));
  };

  let renderedScopeKey = '';
  const refreshData = async (options: WorkflowRefreshOptions = {}) => {
    if (workflowRefreshInFlight) {
      if (options.silent) {
        return workflowRefreshInFlight;
      }
      try {
        await workflowRefreshInFlight;
      } catch {
        // The next foreground refresh will surface its own error if it still fails.
      }
    }

    const task = (async () => {
      const scope = currentScope();
      const requestScopeKey = scopeKey(scope);
      if (renderedScopeKey !== requestScopeKey || !scope.session_id) {
        setRuns([]);
        const requested = props.focusRequest;
        setSelectedRunId(scope.session_id && requested && (!requested.sessionId || requested.sessionId === scope.session_id)
          ? requested.runId || '' : '');
        renderedScopeKey = requestScopeKey;
      }
      // Hidden studio tabs suspend refresh, not the user's historical run selection.
      if (!isActive() || !scope.session_id) return;
      const selectionAtRequestStart = selectedRunId();
      const showLoading = !options.silent;
      if (showLoading) {
        setLoading(true);
        setError('');
      }

      try {
        const runRequest = listWorkflowRuns({
          ...scope,
          session_id: props.mode === 'temporary' ? '' : scope.session_id,
          sync: Boolean(options.sync),
        });
        const draftRequest = listWorkflowDrafts(scope);
        const [templateData, runData, draftData] = options.runsOnly
          ? [null, await runRequest, await draftRequest]
          : await Promise.all([
              getWorkflowTemplates({ ...scope, includeDisabled: true }),
              runRequest,
              draftRequest,
            ]);
        if (requestScopeKey !== scopeKey(currentScope())) {
          return;
        }

        const nextTemplates = templateData
          ? (Array.isArray(templateData.templates) ? templateData.templates : [])
          : templates();
        const allRuns = Array.isArray(runData.runs) ? runData.runs : [];
        const isTemporaryRun = (run: WorkflowRun) => Boolean(run.metadata?.workflow_draft_id || run.metadata?.created_from === 'temporary_workflow_draft');
        const nextRuns = props.mode === 'temporary' ? allRuns.filter(isTemporaryRun) : allRuns.filter(run => !isTemporaryRun(run));
        const allDrafts = Array.isArray(draftData.drafts) ? draftData.drafts : [];
        const nextDrafts = props.mode === 'temporary' ? allDrafts.filter(draft => draft.status !== 'published') : [];
        const focusedDraft = nextDrafts.find(draft => draft.source_run_id === props.focusRequest?.runId);
        if (focusedDraft) setSelectedDraftId(focusedDraft.draft_id);
        const nextEnabledTemplates = nextTemplates.filter((item) => item.enabled !== false);

        if (templateData) {
          setTemplates(nextTemplates);
          if (!selectedTemplateKey() && nextTemplates.length > 0) {
            setSelectedTemplateKey((nextEnabledTemplates[0] || nextTemplates[0]).template_key);
          } else if (selectedTemplateKey() && !nextTemplates.some((item) => item.template_key === selectedTemplateKey())) {
            setSelectedTemplateKey((nextEnabledTemplates[0] || nextTemplates[0])?.template_key || '');
          }
        }

        setRuns((previousRuns) => stabilizeWorkflowRuns(previousRuns, nextRuns));
        setDrafts(nextDrafts);
        if (!selectedDraftId() && nextDrafts.length) setSelectedDraftId(nextDrafts[0].draft_id);
        else if (selectedDraftId() && !nextDrafts.some(item => item.draft_id === selectedDraftId())) setSelectedDraftId(nextDrafts[0]?.draft_id || '');
        const nextSelectedRunId = resolveWorkflowRunSelection(
          selectedRunId(),
          nextRuns,
          { selectLatest: options.selectLatest, selectionAtRequestStart },
        );
        if (nextSelectedRunId !== selectedRunId()) {
          setSelectedRunId(nextSelectedRunId);
        }

        setLastUpdatedAt(new Date().toLocaleTimeString());
      } catch (nextError) {
        if (!options.silent) {
          setError(nextError instanceof Error ? nextError.message : '加载流程失败');
        }
      } finally {
        if (showLoading) {
          setLoading(false);
        }
      }
    })();

    workflowRefreshInFlight = task;
    try {
      await task;
    } finally {
      if (workflowRefreshInFlight === task) {
        workflowRefreshInFlight = null;
      }
    }
  };

  createEffect(() => {
    const draft = selectedDraft();
    setDraftJson(draft ? JSON.stringify(draft.template, null, 2) : '');
    setPublishName(draft?.name || '');
    setPublishDescription(draft?.template.description || '');
    const asciiName = String(draft?.name || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
    setPublishTemplateKey(draft ? `private-${asciiName || draft.draft_id.slice(4)}` : '');
  });

  const handleCreateTemporaryWorkflow = async () => {
    const goal = draftGoal().trim();
    if (!goal) return setError('请先填写需要规划的复杂任务');
    setDraftAction('create'); setError('');
    try {
      const result = await createWorkflowDraft({ ...currentScope(), goal, model: currentSession()?.model || 'local-qwen/qwen3.8:latest' });
      setSelectedDraftId(result.draft.draft_id); setCatalogMode('temporary'); setDraftGoal('');
      await refreshData(); setActionFeedback('临时 DAG 已生成，请检查并编辑后确认');
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : '生成临时 Workflow 失败'); }
    finally { setDraftAction(''); }
  };

  const handleUpdateTemporaryWorkflow = async () => {
    const draft = selectedDraft(); if (!draft) return;
    setDraftAction('update'); setError('');
    try {
      const template = JSON.parse(draftJson());
      await updateWorkflowDraft(draft.draft_id, { ...currentScope(), name: String(template.display_name || draft.name), template });
      await refreshData(); setActionFeedback('临时 DAG 已保存并通过契约校验');
    } catch (nextError) { setError(nextError instanceof SyntaxError ? '流程 JSON 格式有误，尚未执行。请检查引号、逗号和括号后重新保存。' : nextError instanceof Error ? nextError.message : '保存临时 Workflow 失败'); }
    finally { setDraftAction(''); }
  };

  const handleConfirmTemporaryWorkflow = async () => {
    const draft = selectedDraft(); if (!draft) return;
    setDraftAction('confirm'); setError('');
    try {
      const result = await confirmWorkflowDraft(draft.draft_id, { ...currentScope(), input_payload: { target_description: draft.goal } });
      setSelectedRunId(result.run.run_id); await refreshData({ selectLatest: true });
      setActionFeedback('计划已锁定并开始执行，确认后不能修改');
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : '确认临时 Workflow 失败'); }
    finally { setDraftAction(''); }
  };

  const handleRestartTemporaryWorkflow = async () => {
    const draft = selectedDraft(); if (!draft) return;
    setDraftAction('restart'); setError('');
    try {
      const result = await restartWorkflowDraft(draft.draft_id, {
        ...currentScope(), input_payload: { target_description: draft.goal },
      });
      upsertRun(result.run); setSelectedRunId(result.run.run_id);
      await refreshData({ sync: true });
      setActionFeedback('已在当前会话创建新的运行实例并开始执行');
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : '重新运行临时 Workflow 失败'); }
    finally { setDraftAction(''); }
  };

  const handleSaveAcceptedDraft = async () => {
    const draft = selectedDraft(); if (!draft) return;
    setDraftAction('save'); setError('');
    try {
      await saveWorkflowDraftAsTemplateDraft(draft.draft_id, {
        ...currentScope(), template_key: publishTemplateKey().trim(),
        display_name: publishName().trim(), description: publishDescription().trim(),
      });
      await refreshData(); setActionFeedback('已保存为当前用户私有草稿');
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : '保存私有草稿失败'); }
    finally { setDraftAction(''); }
  };

  const handlePublishDraft = async (overwrite: boolean) => {
    const draft = selectedDraft(); if (!draft) return;
    setDraftAction('publish'); setError('');
    try {
      const result = await publishWorkflowDraft(draft.draft_id, { ...currentScope(), overwrite });
      await refreshData(); setSelectedTemplateKey(result.template.template_key);
      setActionFeedback(
        result.workflow_maturity === 'experimental'
          ? `已发布实验性 Workflow：${result.template.display_name}（可手动运行，不参与自动匹配）`
          : `已发布已验证 Workflow：${result.template.display_name}`,
      );
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : '发布 Workflow 失败'); }
    finally { setDraftAction(''); }
  };

  const handleDeleteTemporaryWorkflow = async () => {
    const draft = selectedDraft(); if (!draft) return;
    const run = selectedDraftRun();
    if (run && !['completed', 'failed', 'terminated', 'cancelled'].includes(String(run.status || '').toLowerCase())) {
      setError('该 Workflow 正在运行，请先终止运行再删除');
      return;
    }
    setDraftAction('delete'); setError('');
    try {
      await deleteWorkflowDraft(draft.draft_id, currentScope());
      setSelectedDraftId('');
      await refreshData();
      setActionFeedback('临时 Workflow 已删除');
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : '删除临时 Workflow 失败'); }
    finally { setDraftAction(''); }
  };

  const exportSelectedDraft = () => {
    const draft = selectedDraft(); if (!draft) return;
    const blob = new Blob([JSON.stringify(draft.saved_template || draft.template, null, 2)], { type: 'application/json' });
    const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = `${draft.template.template_key}.json`; link.click(); URL.revokeObjectURL(link.href);
  };

  const importTemporaryWorkflow = async (event: Event) => {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0]; if (!file) return;
    setDraftAction('import'); setError('');
    try {
      const template = JSON.parse(await file.text());
      const goal = String(template.description || template.display_name || '导入的临时 Workflow');
      const result = await createWorkflowDraft({ ...currentScope(), goal, model: currentSession()?.model || 'local-qwen/qwen3.8:latest', template });
      setSelectedDraftId(result.draft.draft_id); setCatalogMode('temporary'); await refreshData();
      setActionFeedback('临时 Workflow 已导入并通过契约校验');
    } catch (nextError) { setError(nextError instanceof SyntaxError ? '流程 JSON 格式有误，尚未执行。请检查引号、逗号和括号后重新导入。' : nextError instanceof Error ? nextError.message : '导入临时 Workflow 失败'); }
    finally { input.value = ''; setDraftAction(''); }
  };

  createEffect(on(()=>props.focusRequest,request=>{
    if(!request || (request.sessionId && request.sessionId!==currentScope().session_id))return;
    if(request.runId)setSelectedRunId(request.runId);
    if(request.templateKey)setSelectedTemplateKey(request.templateKey);
    if(request.draftId)setSelectedDraftId(request.draftId);
    void refreshData();
  }));
  onMount(() => {
    const handleWorkflowRunCreated = (event: Event) => {
      const detail = (event as CustomEvent<{
        sessionId?: string;
        runId?: string;
        templateKey?: string;
      }>).detail || {};
      const targetSessionId = String(detail.sessionId || '').trim();
      if (targetSessionId && targetSessionId !== currentScope().session_id) return;
      const runId = String(detail.runId || '').trim();
      if (runId) setSelectedRunId(runId);
      void refreshData();
    };
    window.addEventListener('tcode-workflow-run-created', handleWorkflowRunCreated);
    onCleanup(() => {
      window.removeEventListener('tcode-workflow-run-created', handleWorkflowRunCreated);
    });
  });

  createEffect(on(
    () => {
      const run = selectedRun();
      return run ? `${run.run_id}:${run.current_stage_id}:${run.candidates.length}` : '';
    },
    () => {
      const run = selectedRun();
      if (!run) return;
      const validStageIds = run.stage_states.map((stage) => stage.stage_id);
      let preferredStageId = run.current_stage_id || '';
      if (!preferredStageId) {
        const completedStages = run.stage_states.filter((stage) => {
          const normalized = String(stage.status || '').trim().toLowerCase();
          return normalized === 'committed';
        });
        preferredStageId = completedStages[completedStages.length - 1]?.stage_id || validStageIds[0] || '';
      }
      if (!selectedStageId() || !validStageIds.includes(selectedStageId())) {
        setSelectedStageId(preferredStageId);
      }
      if (!candidateSourceStageId() || !validStageIds.includes(candidateSourceStageId())) {
        setCandidateSourceStageId(preferredStageId || 'candidate_generation');
      }
      if (!candidateId()) {
        setCandidateId(suggestCandidateId(run));
      }
    },
  ));

  createEffect(on(
    () => [
      isActive(),
      currentScope().session_id,
      currentScope().workdir_name || '',
      currentScope().workdir_path || '',
    ],
    () => {
      void refreshData();
    },
  ));

  createEffect(on(
    () => selectedRun()?.run_id || '',
    () => {
      setCandidatePage(1);
      setEventPage(1);
    },
  ));

  createEffect(on(
    () => {
      const run = selectedRun();
      return run ? `${run.run_id}:${run.version}` : '';
    },
    () => void loadExecutionRecords(selectedRun()),
  ));

  createEffect(() => {
    const maxPage = candidatePageCount();
    if (candidatePage() > maxPage) {
      setCandidatePage(maxPage);
    }
  });

  const detailKey = (
    section: 'stage' | 'candidate' | 'event',
    itemId: string,
  ) => workflowDetailExpansionKey(selectedRun()?.run_id || '', section, itemId);

  const detailIsExpanded = (
    section: 'stage' | 'candidate' | 'event',
    itemId: string,
    defaultExpanded = false,
  ) => workflowDetailIsExpanded(detailExpansion(), detailKey(section, itemId), defaultExpanded);

  const rememberDetailExpansion = (
    section: 'stage' | 'candidate' | 'event',
    itemId: string,
    expanded: boolean,
  ) => {
    const key = detailKey(section, itemId);
    setDetailExpansion((previous) => updateWorkflowDetailExpansion(previous, key, expanded));
  };

  createEffect(() => {
    const maxPage = eventPageCount();
    if (eventPage() > maxPage) {
      setEventPage(maxPage);
    }
  });

  createEffect(() => {
    if (!isActive()) return;
    if (!currentScope().session_id) return;

    const timer = window.setInterval(() => {
      if (loading() || creating() || actionLoading() !== '') return;

      if (!runs().some(workflowRunNeedsSync)) return;

      void refreshData({ silent: true, runsOnly: true });
    }, WORKFLOW_STAGE_POLL_INTERVAL_MS);

    onCleanup(() => {
      window.clearInterval(timer);
    });
  });

  const handleCreateRun = async () => {
    const scope = currentScope();
    const templateKey = String(selectedTemplateKey() || '').trim();
    const description = String(targetDescription() || '').trim();
    if (!scope.session_id || !templateKey || !description || !selectedTemplateEnabled()) return;

    setCreating(true);
    setError('');
    setPendingCreationConfirmation(null);
    try {
      const create = (confirmation?: { fingerprint: string }) => createWorkflowRun({
        ...scope,
        template_key: templateKey,
        input_payload: workflowInputPayloadFromDraft(description),
        metadata: {
          created_from: 'workflow_workspace_panel',
          ...(confirmation ? {
            workflow_applicability_confirmation: {
              decision: 'proceed',
              fingerprint: confirmation.fingerprint,
              source: 'workflow_workspace_panel',
            },
          } : {}),
        },
      });
      let created: Awaited<ReturnType<typeof create>>;
      try {
        created = await create();
      } catch (createError) {
        if (!(createError instanceof WorkflowApplicabilityConfirmationRequired)) {
          throw createError;
        }
        setPendingCreationConfirmation({
          assessment: createError.assessment,
          template: createError.template,
          templateKey,
          inputPayload: workflowInputPayloadFromDraft(description),
          scope,
          planHash: createError.planHash,
          revision: createError.revision,
        });
        return;
      }
      setRuns((prev) => [created.run, ...prev.filter((item) => item.run_id !== created.run.run_id)]);
      setSelectedRunId(created.run.run_id);
      setLastUpdatedAt(new Date().toLocaleTimeString());
      await refreshData({ selectLatest: true });
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '创建流程运行实例失败');
    } finally {
      setCreating(false);
    }
  };

  const handleConfirmedCreateRun = async () => {
    const pending = pendingCreationConfirmation();
    if (!pending) return;
    setCreating(true);
    setError('');
    try {
      const created = await createWorkflowRun({
        ...pending.scope,
        template_key: pending.templateKey,
        input_payload: pending.inputPayload,
        metadata: {
          created_from: 'workflow_workspace_panel',
          workflow_applicability_confirmation: {
            decision: 'proceed',
            fingerprint: pending.assessment.fingerprint,
            plan_hash: pending.planHash,
            source: 'workflow_workspace_panel',
          },
        },
      });
      setPendingCreationConfirmation(null);
      setRuns(prev => [created.run, ...prev.filter(item => item.run_id !== created.run.run_id)]);
      setSelectedRunId(created.run.run_id);
      setLastUpdatedAt(new Date().toLocaleTimeString());
      await refreshData({ selectLatest: true });
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '确认并创建流程失败');
    } finally {
      setCreating(false);
    }
  };

  const handleSaveTemplate = async () => {
    const raw = String(templateJsonDraft() || '').trim();
    if (!raw) {
      setError('请先粘贴或载入一个 workflow JSON');
      return;
    }

    setTemplateActionLoading('save');
    setActionFeedback('');
    setError('');
    try {
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('workflow JSON 必须是对象');
      }
      const saved = await saveWorkflowTemplate(parsed, { overwrite: templateOverwrite() });
      await refreshData();
      setSelectedTemplateKey(saved.template.template_key);
      setTemplateJsonDraft(JSON.stringify(saved.template, null, 2));
      setActionFeedback(`Workflow ${saved.template.template_key} 已保存`);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '保存 Workflow 失败');
    } finally {
      setTemplateActionLoading('');
    }
  };

  const handleDeleteTemplate = async () => {
    const template = selectedTemplate();
    if (!template) return;

    const confirmed = await confirmAction(`确认删除 Workflow ${template.template_key} 吗？删除定义后不能创建新的运行实例，已有运行记录不会删除。`);
    if (!confirmed) return;

    setTemplateActionLoading('delete');
    setActionFeedback('');
    setError('');
    try {
      await deleteWorkflowTemplate(template.template_key, { force: templateForceDelete() });
      setTemplateJsonDraft('');
      setSelectedTemplateKey('');
      await refreshData();
      setActionFeedback(`Workflow ${template.template_key} 已删除`);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '删除 Workflow 失败');
    } finally {
      setTemplateActionLoading('');
    }
  };

  const resetCandidateForm = (run?: WorkflowRun | null) => {
    setCandidateId(suggestCandidateId(run || selectedRun()));
    setCandidateStatus('draft');
    setCandidateRank('');
    setCandidateSummary('');
    setCandidateDockingScore('');
    setCandidateInteractionCount('');
    setCandidateConfidenceScore('');
    setCandidatePosePath('');
    setCandidateStructurePath('');
    setCandidateReportPath('');
    setCandidateMetricRows([]);
    setCandidateArtifactRows([]);
  };

  const updateMetricRow = (rowId: string, field: 'key' | 'value', value: string) => {
    setCandidateMetricRows((prev) => prev.map((row) => (
      row.id === rowId ? { ...row, [field]: value } : row
    )));
  };

  const updateArtifactRow = (rowId: string, field: 'key' | 'value', value: string) => {
    setCandidateArtifactRows((prev) => prev.map((row) => (
      row.id === rowId ? { ...row, [field]: value } : row
    )));
  };

  const handleStageTransition = async () => {
    const run = selectedRun();
    const stageId = String(selectedStageId() || '').trim();
    if (!run || !stageId) return;

    setActionLoading('stage');
    setActionFeedback('');
    setError('');
    try {
      const status = transitionStatusValue();
      const note = String(transitionNote() || '').trim();
      const payload: WorkflowScope & {
        status: string;
        expected_version?: number;
        result_summary?: string;
        artifact_refs?: Record<string, any>;
        error?: string;
        metadata?: Record<string, any>;
      } = {
        ...currentScope(),
        expected_version: run.version,
        status,
        metadata: {
          updated_from: 'workflow_workspace_panel',
        },
      };
      if (status === 'committed' && note) {
        payload.result_summary = note;
      }
      if (status === 'failed') {
        payload.error = note || '已在流程工作区面板手动标记为失败';
      }
      if (status === 'running' && note) {
        if (!payload.metadata) payload.metadata = {};
        payload.metadata.note = note;
      }
      const updated = await transitionWorkflowStage(run.run_id, stageId, payload);
      upsertRun(updated.run);
      setActionFeedback(`阶段 ${stageId} 已更新为 ${statusLabel(status)}`);
      setTransitionNote('');
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '更新阶段失败');
    } finally {
      setActionLoading('');
    }
  };

  const handleExecuteStage = async () => {
    const run = selectedRun();
    const stageId = String(selectedStageId() || '').trim();
    if (!run || !stageId) return;

    setActionLoading('execute');
    setActionFeedback('');
    setError('');
    try {
      const updated = await executeWorkflowStage(run.run_id, stageId, {
        ...currentScope(),
        expected_version: run.version,
      });
      upsertRun(updated.run);
      setActionFeedback(`阶段 ${stageId} 已自动执行`);
      setTransitionNote('');
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '自动执行阶段失败');
    } finally {
      setActionLoading('');
    }
  };

  const handleAttachCandidate = async () => {
    const run = selectedRun();
    const nextCandidateId = String(candidateId() || '').trim();
    const sourceStageId = String(candidateSourceStageId() || '').trim();
    if (!run || !nextCandidateId || !sourceStageId) return;

    setActionLoading('candidate');
    setActionFeedback('');
    setError('');
    try {
      const scoreCard: Record<string, any> = {
        ...buildCandidateFieldObject(candidateMetricRows(), { coerceValues: true }),
      };
      const dockingScore = normalizeOptionalNumber(candidateDockingScore());
      const interactionCount = normalizeOptionalNumber(candidateInteractionCount());
      const confidenceScore = normalizeOptionalNumber(candidateConfidenceScore());
      if (dockingScore !== undefined) scoreCard.docking_score = dockingScore;
      if (interactionCount !== undefined) scoreCard.interaction_count = interactionCount;
      if (confidenceScore !== undefined) scoreCard.confidence_score = confidenceScore;

      const artifactRefs: Record<string, any> = {
        ...buildCandidateFieldObject(candidateArtifactRows()),
      };
      if (String(candidatePosePath() || '').trim()) artifactRefs.pose = String(candidatePosePath() || '').trim();
      if (String(candidateStructurePath() || '').trim()) artifactRefs.structure = String(candidateStructurePath() || '').trim();
      if (String(candidateReportPath() || '').trim()) artifactRefs.report = String(candidateReportPath() || '').trim();

      const updated = await attachWorkflowCandidate(run.run_id, {
        ...currentScope(),
        expected_version: run.version,
        candidate: {
          candidate_id: nextCandidateId,
          source_stage_id: sourceStageId,
          status: String(candidateStatus() || '').trim() || 'draft',
          rank: String(candidateRank() || '').trim() ? Number(candidateRank()) : null,
          score_card: scoreCard,
          artifact_refs: artifactRefs,
          summary: String(candidateSummary() || '').trim(),
          metadata: {
            created_from: 'workflow_workspace_panel',
          },
        },
      });
      const nextRun = upsertRun(updated.run);
      setActionFeedback(`候选 ${nextCandidateId} 已写入运行实例`);
      resetCandidateForm(nextRun);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '写入候选失败');
    } finally {
      setActionLoading('');
    }
  };

  const handleProposalAction = async (proposal: WorkflowProposal, action: 'approve' | 'reject') => {
    const run = selectedRun();
    if (!run || !proposal.proposal_id) return;

    setActionLoading('proposal');
    setActionFeedback('');
    setError('');
    try {
      const payload = {
        ...currentScope(),
        expected_version: run.version,
        actor: 'operator',
        reason: action === 'approve' ? 'approved from workflow workspace panel' : 'rejected from workflow workspace panel',
      };
      const updated = action === 'approve'
        ? await approveWorkflowProposal(run.run_id, proposal.proposal_id, payload)
        : await rejectWorkflowProposal(run.run_id, proposal.proposal_id, payload);
      upsertRun(updated.run);
      setActionFeedback(`${proposalTypeLabel(proposal.proposal_type)}已${action === 'approve' ? '批准' : '拒绝'}`);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '处理流程提案失败');
    } finally {
      setActionLoading('');
    }
  };

  const handleTerminateRun = async (runOverride?: WorkflowRun | null) => {
    const run = runOverride || selectedRun();
    if (!run) return;

    const normalizedStatus = String(run.status || '').trim().toLowerCase();
    if (normalizedStatus === 'terminated') return;

    const confirmed = await confirmAction(`确认完全终止当前运行吗？该操作会中断当前流程执行。`);
    if (!confirmed) return;

    setActionLoading('terminate');
    setActionFeedback('');
    setError('');
    try {
      const updated = await terminateWorkflowRun(run.run_id, {
        ...currentScope(),
        session_id: run.session_id || currentScope().session_id,
        expected_version: run.version,
        actor: 'operator',
        reason: 'terminated from workflow workspace panel',
      });
      upsertRun(updated.run);
      setActionFeedback(`当前运行已完全终止`);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '终止流程运行实例失败');
    } finally {
      setActionLoading('');
    }
  };

  const handleDeleteRun = async (runOverride?: WorkflowRun | null) => {
    const run = runOverride || selectedRun();
    if (!run || !workflowRunAllowsHistoryMutation(run)) return;

    const confirmed = await confirmAction(
      `确认删除当前运行吗？该运行的阶段结果和事件历史会一起删除，且无法恢复。`,
    );
    if (!confirmed) return;

    setActionLoading('delete-run');
    setActionFeedback('');
    setError('');
    try {
      await deleteWorkflowRun(run.run_id, {
        ...currentScope(),
        session_id: run.session_id || currentScope().session_id,
        expected_version: run.version,
      });
      const nextRuns = runs().filter((item) => item.run_id !== run.run_id);
      setRuns(nextRuns);
      setSelectedRunId(resolveWorkflowRunSelection('', nextRuns));
      setActionFeedback(`运行已删除`);
      setLastUpdatedAt(new Date().toLocaleTimeString());
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '删除流程运行实例失败');
    } finally {
      setActionLoading('');
    }
  };

  const handleSaveRunAsTemplate = async () => {
    const run = selectedRun();
    const snapshot = run?.metadata?.template_snapshot;
    if (!run || !snapshot || typeof snapshot !== 'object') return;
    setTemplateActionLoading('save');
    setError('');
    try {
      const suffix = new Date().toISOString().replace(/\D/g, '').slice(0, 14);
      const templateKey = `${run.template_key}_saved_${suffix}`;
      const saved = await saveWorkflowTemplate({
        ...snapshot,
        template_key: templateKey,
        display_name: `${String(snapshot.display_name || run.template_key)} · 已保存`,
        metadata: { ...snapshot.metadata, source_run_id: run.run_id, saved_from_successful_run: true },
      });
      await refreshData();
      setSelectedTemplateKey(saved.template.template_key);
      setActionFeedback(`成功流程已保存为 Workflow ${saved.template.template_key}`);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '保存成功流程失败');
    } finally {
      setTemplateActionLoading('');
    }
  };

  const handleRerunStage = async (stageId: string, runOverride?: WorkflowRun | null) => {
    const run = runOverride || selectedRun();
    const stage = run?.stage_states.find((item) => item.stage_id === stageId);
    if (!run || !stage || !workflowStageCanRerun(run, stage)) return;

    const confirmed = await confirmAction(
      `确认从“${stage.label || stage.stage_id}”重新运行吗？该阶段及所有下游阶段的现有结果会失效，上游已提交结果会保留。`,
    );
    if (!confirmed) return;

    setActionLoading(`rerun:${stage.stage_id}`);
    setActionFeedback('');
    setError('');
    try {
      const updated = await rerunWorkflowFromStage(run.run_id, stage.stage_id, {
        ...currentScope(),
        session_id: run.session_id || currentScope().session_id,
        expected_version: run.version,
        actor: 'operator',
        reason: 'rerun requested from workflow workspace panel',
        auto_continue: true,
      });
      upsertRun(updated.run);
      setSelectedStageId(stage.stage_id);
      setStageSectionExpanded(true);
      setActionFeedback(
        updated.auto_continue_scheduled
          ? `已从“${stage.label || stage.stage_id}”恢复，并开始继续执行`
          : `已从“${stage.label || stage.stage_id}”恢复；可点击“自动运行当前阶段”继续`,
      );
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '从指定阶段重新运行失败');
    } finally {
      setActionLoading('');
    }
  };

  return (
    <div class="overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#13191d] shadow-[0_18px_40px_rgba(0,0,0,0.18)]">
      <div class="border-b border-[#2a3136] bg-[#10161a] px-4 py-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#7bd0ff]">{props.mode === 'temporary' ? 'Runtime Workflow' : 'Workflow 定义'}</div>
            <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">{props.mode === 'temporary' ? '临时流程与实时进度' : '已发布流程与运行记录'}</div>
          </div>
          <div class="flex items-center gap-2">
            <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7]">{props.mode === 'temporary' ? drafts().length : runs().length}</span>
            <button
              class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
              onClick={() => {
                void refreshData({ sync: true });
              }}
              disabled={loading()}
            >
              {loading() ? '刷新中...' : '刷新'}
            </button>
          </div>
        </div>
        <div class="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-[#8b949e]">
          <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">会话: {currentScope().session_id || '-'}</span>
          <Show when={currentSession()?.workdir_name}>
            <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">工作目录: {currentSession()?.workdir_name}</span>
          </Show>
          <span>上次刷新: {lastUpdatedAt() || '-'}</span>
        </div>
      </div>

      <div class="space-y-4 p-4">
        <Show when={error()}>
          <div class="rounded-2xl border border-[#5a1e23] bg-[#201317] px-4 py-3 text-sm text-[#ffb6b0]">{error()}</div>
        </Show>

        <Show
          when={currentScope().session_id}
          fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#10161a] px-4 py-6 text-sm text-[#8b949e]">当前没有活动会话，先在左侧创建或切换到一个工作目录会话。</div>}
        >
          <Show when={catalogMode() === 'templates'} fallback={
            <section class="workflow-temporary-console">
              <div class="workflow-template-picker">
                <label for="workflow-draft-select">临时流程</label>
                <select id="workflow-draft-select" value={selectedDraftId()} onChange={event => setSelectedDraftId(event.currentTarget.value)}>
                  <Show when={!drafts().length}><option value="" selected={!selectedDraftId()}>还没有临时流程</option></Show>
                  <For each={drafts()}>{draft => (
                    <option value={draft.draft_id} selected={draft.draft_id === selectedDraftId()}>
                      {draft.name} · {statusLabel(String(runs().find(run => run.run_id === draft.source_run_id)?.status || draft.status))}
                    </option>
                  )}</For>
                </select>
                <span>{selectedDraftStatus() || 'new'}</span>
              </div>
              <details class="workflow-temporary-create-details">
                <summary>手动新建临时 DAG <small>也可以直接在主界面描述多步任务</small></summary>
                <div class="workflow-temporary-create">
                  <textarea value={draftGoal()} onInput={event => setDraftGoal(event.currentTarget.value)} placeholder="仅在需要绕过主聊天、直接规划时使用" />
                  <button disabled={Boolean(draftAction()) || !draftGoal().trim()} onClick={() => void handleCreateTemporaryWorkflow()}>{draftAction() === 'create' ? '规划中…' : '生成临时 DAG'}</button>
                </div>
              </details>
              <label class="workflow-temporary-import">导入 JSON<input type="file" accept="application/json,.json" disabled={Boolean(draftAction())} onChange={event => void importTemporaryWorkflow(event)} /></label>
              <Show when={selectedDraft()}>{draft => <div class="workflow-temporary-meta">
                <div><strong>{draft().name}</strong><span>修订 {draft().revision} · {draft().model || '当前会话模型'}</span></div>
                <div class="workflow-temporary-actions">
                  <Show when={draft().status === 'editing'}>
                    <button disabled={Boolean(draftAction())} onClick={() => void handleUpdateTemporaryWorkflow()}>保存编辑</button>
                    <button class="primary" disabled={Boolean(draftAction())} onClick={() => void handleConfirmTemporaryWorkflow()}>{draftAction() === 'confirm' ? '启动中…' : '确认并运行'}</button>
                  </Show>
                  <Show when={['review_pending', 'accepted'].includes(selectedDraftStatus()) && draft().status !== 'saved' && draft().status !== 'published'}>
                    <div class="workflow-publish-form">
                      <label>流程名称<input value={publishName()} onInput={event => setPublishName(event.currentTarget.value)} /></label>
                      <label>Workflow 标识<input value={publishTemplateKey()} onInput={event => setPublishTemplateKey(event.currentTarget.value)} /></label>
                      <label class="wide">流程描述<textarea value={publishDescription()} onInput={event => setPublishDescription(event.currentTarget.value)} /></label>
                      <button class="primary" disabled={Boolean(draftAction()) || !publishName().trim() || !publishTemplateKey().trim()} onClick={() => void handleSaveAcceptedDraft()}>保存为私有草稿</button>
                      <Show when={selectedDraftStatus() !== 'accepted'}><small class="wide">当前运行尚未通过验收，可以保存草稿并发布为实验性流程；实验性流程仅支持手动运行。</small></Show>
                    </div>
                  </Show>
                  <Show when={draft().status === 'saved'}>
                    <button class="primary" disabled={Boolean(draftAction())} onClick={() => void handlePublishDraft(false)}>发布为新 Workflow</button>
                    <button disabled={Boolean(draftAction())} onClick={() => void handlePublishDraft(true)}>覆盖同名 Workflow</button>
                    <Show when={selectedDraftStatus() !== 'accepted'}><span>将以实验性流程发布，可手动运行，暂不参与自动匹配。</span></Show>
                  </Show>
                  <button onClick={exportSelectedDraft}>导出 JSON</button>
                  <button class="danger" disabled={Boolean(draftAction()) || ['running', 'queued', 'retrying'].includes(selectedDraftStatus())} onClick={() => void handleDeleteTemporaryWorkflow()}>{draftAction() === 'delete' ? '删除中…' : '删除'}</button>
                </div>
                <Show when={draft().status === 'editing'}><details><summary>编辑完整节点契约</summary><textarea class="workflow-temporary-json" value={draftJson()} onInput={event => setDraftJson(event.currentTarget.value)} spellcheck={false} /></details></Show>
              </div>}</Show>
            </section>
          }>
            <div class="workflow-template-picker">
              <label for="workflow-template-select">Workflow</label>
              <select ref={templateSelectElement} id="workflow-template-select" value={selectedTemplateKey()} classList={{ 'workflow-template-picker__select--running': Boolean(selectedTemplateRun() && activeRuns().some(run => run.run_id === selectedTemplateRun()?.run_id)) }} onChange={(event) => setSelectedTemplateKey(event.currentTarget.value)}>
                <For each={templates()}>{template => <option value={template.template_key} disabled={template.enabled === false}>{template.display_name} · {template.stages.length} 节点{template.metadata?.workflow_maturity === 'experimental' ? ' · 实验性' : ''}{template.enabled === false ? ' · 已禁用' : ''}</option>}</For>
              </select>
              <span>{selectedTemplate()?.domain || 'workflow'}</span>
            </div>
          </Show>

          <Show when={activeRuns().length > 0}>
            <div class="workflow-running-notice">
              <i />
              <span>{activeRuns().map(run => templates().find(template => template.template_key === run.template_key)?.display_name || '工作流').join('、')} 正在运行</span>
              <small>{props.mode === 'temporary' ? '关闭界面后仍会后台执行，可随时返回查看' : '可以继续切换 Workflow 查看其它 DAG'}</small>
            </div>
          </Show>

          <section class="workflow-primary-dag" aria-label="Workflow 执行图" style={{ height: `${dagHeight()}px` }}>
            <WorkflowDag
              stages={catalogMode() === 'temporary'
                ? selectedDraftRunDagStages()
                : (selectedTemplateRun() ? selectedRunDagStages() : (selectedTemplate()?.stages || []))}
              label={catalogMode() === 'temporary'
                ? `${selectedDraft()?.name || '临时 Workflow'} · ${selectedDraftStatus() || '待生成'}`
                : (selectedTemplateRun() ? '工作流执行图' : `${selectedTemplate()?.display_name || '工作流'} 计划图`)}
              onStageSelect={stageId => setSelectedStageId(stageId)}
            />
            <div
              class="workflow-primary-dag__resize-handle"
              role="separator"
              aria-label="调整 DAG 区域高度"
              aria-orientation="horizontal"
              aria-valuemin="210"
              aria-valuemax={String(Math.round(window.innerHeight * .72))}
              aria-valuenow={String(Math.round(dagHeight()))}
              onPointerDown={startDagResize}
              onDblClick={() => setDagHeight(300)}
            ><span /></div>
          </section>

          <Show when={catalogMode() === 'temporary' && selectedDraftRun()}>{run =>
            <section class="workflow-temporary-run" aria-label="临时 Workflow 运行实例">
              <div class="workflow-temporary-run__heading">
                <div>
                  <span>运行实例</span>
                  <strong>当前工作流运行</strong>
                  <small>当前节点：{run().current_stage_id || '等待调度'}</small>
                </div>
                <b class={statusClass(run().status)}>{statusLabel(run().status)}</b>
              </div>
              <div class="workflow-temporary-run__actions">
                <Show when={['terminated', 'failed', 'cancelled', 'completed'].includes(String(run().status || '').toLowerCase())}>
                  <button
                    disabled={actionLoading() !== '' || draftAction() !== ''}
                    onClick={() => void handleRestartTemporaryWorkflow()}
                    title="保留旧运行记录，并在当前会话创建新的运行实例"
                  >{draftAction() === 'restart' ? '启动中…' : '重新运行'}</button>
                </Show>
                <button
                  disabled={actionLoading() !== '' || !workflowStageCanRerun(run(), selectedDraftCheckpointStage())}
                  onClick={() => {
                    const checkpoint = selectedDraftCheckpointStage();
                    if (checkpoint) void handleRerunStage(checkpoint.stage_id, run());
                  }}
                  title="保留已提交的上游结果，从当前断点继续执行"
                >{actionLoading().startsWith('rerun:') ? '恢复中…' : '从断点续跑'}</button>
                <button
                  class="danger"
                  disabled={actionLoading() !== '' || ['terminated', 'completed', 'failed', 'cancelled'].includes(String(run().status || '').toLowerCase())}
                  onClick={() => void handleTerminateRun(run())}
                >{actionLoading() === 'terminate' ? '终止中…' : '终止运行'}</button>
                <button
                  class="danger"
                  disabled={actionLoading() !== '' || !workflowRunAllowsHistoryMutation(run())}
                  onClick={() => void handleDeleteRun(run())}
                >{actionLoading() === 'delete-run' ? '删除中…' : '删除运行记录'}</button>
              </div>
              <div class="workflow-temporary-run__stages">
                <For each={run().stage_states}>{stage =>
                  <div>
                    <span>{stage.label || stage.stage_id}</span>
                    <small>{statusLabel(stage.status)}</small>
                    <button
                      disabled={actionLoading() !== '' || !workflowStageCanRerun(run(), stage)}
                      onClick={() => void handleRerunStage(stage.stage_id, run())}
                    >从此节点续跑</button>
                  </div>
                }</For>
              </div>
              <Show when={pendingWorkflowProposals(run()).length > 0}>
                <section class="mt-3 rounded-xl border border-[#f1a02b]/30 p-3">
                  <strong>待处理提案</strong><p class="text-xs">自动审核提出的修订建议在这里处理；查看或隐藏提醒不会批准提案。</p>
                  <For each={pendingWorkflowProposals(run())}>{proposal => <div class="mt-2">
                    <p>{proposal.summary || '请查看提案详情'}</p>
                    <button disabled={Boolean(actionLoading())} onClick={() => void handleProposalAction(proposal, 'approve')}>批准</button>
                    <button disabled={Boolean(actionLoading())} onClick={() => void handleProposalAction(proposal, 'reject')}>拒绝</button>
                  </div>}</For>
                </section>
              </Show>
              <For each={run().stage_states}>{stage => <section class="mt-3 rounded-xl border border-[#3e484f] p-3">
                <Show when={stage.diagnostic}>{diagnostic => <div class="text-xs text-[#ffe1b3]">
                  <strong>{diagnostic().title}</strong><p>{diagnostic().message}</p><p>处理建议：{diagnostic().suggestion}</p>
                </div>}</Show>
                <Show when={stage.result_summary || stage.draft_result?.response || stage.metadata?.previous_attempt_result?.draft_result?.response}>
                  <details data-testid="workflow-temporary-preserved-result">
                    <summary>查看{stage.label || stage.stage_id}的已有结果{stage.status !== 'committed' ? '（草稿）' : ''}</summary>
                    <MarkdownContent content={String(stage.committed_result?.response || stage.draft_result?.response || stage.result_summary || stage.metadata?.previous_attempt_result?.draft_result?.response)} role="assistant" renderKey={`temporary-result-${run().run_id}-${stage.stage_id}-${stage.attempt}`} class="mt-2 text-sm" />
                  </details>
                </Show>
                <Show when={stage.last_error}><details><summary>技术详情</summary><pre class="whitespace-pre-wrap text-xs">{stage.last_error}</pre></details></Show>
              </section>}</For>
            </section>
          }</Show>

          <Show when={catalogMode() === 'templates'}>
          <div class="grid gap-4 xl:grid-cols-[minmax(280px,340px)_minmax(0,1fr)]">
            <div class="workflow-sidebar-list">
              <div class="workflow-manual-create rounded-3xl border border-[#2a3136] bg-[#10161a] p-4">
                <div class="flex items-center justify-between gap-3">
                  <div>
                    <div class="text-xs font-semibold text-[#e6edf3]">新建流程运行实例</div>
                    <div class="mt-1 text-[11px] text-[#8b949e]">直接基于当前会话作用域创建运行实例。</div>
                  </div>
	                  <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#8b949e]">
	                    {templates().filter((template) => template.enabled !== false).length}/{templates().length} 启用
	                  </span>
                </div>

                <div class="mt-4 space-y-3">
                  <div class="workflow-selected-template">
                    <span>当前 Workflow</span>
                    <strong>{selectedTemplate()?.display_name || '请选择 Workflow'}</strong>
                  </div>

                  <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                    目标描述 / 输入 JSON
                    <textarea
                      class="mt-2 min-h-[120px] w-full rounded-2xl border border-[#30363d] bg-[#0d1216] px-3 py-3 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                      value={targetDescription()}
                      onInput={(event) => setTargetDescription(event.currentTarget.value)}
                      placeholder={`描述“${selectedTemplate()?.display_name || '当前 Workflow'}”本次要处理的具体目标，或输入 JSON`}
                    />
                    <span class="mt-1 block text-[10px] font-normal normal-case tracking-normal text-[#6f7d87]">
                      普通文本会作为 target_description；以 {'{'} 开头的有效 JSON 对象会原样作为 input_payload。
                    </span>
                  </label>

                  <button
                    class="inline-flex w-full items-center justify-center rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 py-2.5 text-sm font-semibold text-[#9ee6bb] transition-colors hover:border-[#38b46a]/45 hover:bg-[#38b46a]/16 disabled:cursor-not-allowed disabled:opacity-60"
                    onClick={() => {
                      void handleCreateRun();
                    }}
	                    disabled={creating() || !selectedTemplateKey() || !selectedTemplateEnabled() || !String(targetDescription() || '').trim()}
	                  >
                    {creating() ? '创建中...' : selectedTemplateEnabled() ? '创建运行实例' : 'Workflow 已禁用'}
	                  </button>
                </div>
              </div>

	              <Show when={selectedTemplate()}>
	                <details class="workflow-fold workflow-template-summary">
                    <summary><span>Workflow 摘要</span><small>{selectedTemplate()?.stages.length || 0} 个节点</small></summary>
                    <div class="workflow-fold__body">
                  <div class="mt-2 text-sm text-[#c9d1d9]">{selectedTemplate()?.description || '暂无描述'}</div>
	                  <div class="mt-4 space-y-2 text-[11px] text-[#8b949e]">
	                    <div>状态: {selectedTemplateEnabled() ? '启用' : '已禁用'}</div>
	                    <div>必填输入: {formatList((selectedTemplate()?.input_contract?.required || []) as string[])}</div>
                    <div>必填输出: {formatList((selectedTemplate()?.output_contract?.required || []) as string[])}</div>
                    <div>阶段数: {selectedTemplate()?.stages.length || 0}</div>
                  </div>
                  <div class="mt-4 flex flex-wrap gap-2">
                    <For each={selectedTemplate()?.stages || []}>
                      {(stage) => (
                        <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1 text-[11px] text-[#bdc8d1]">
                          {stage.label}
                        </span>
                      )}
                    </For>
                  </div>
	                  </div>
	                </details>
	              </Show>

                <Show when={pendingCreationConfirmation()}>{pending => {
                  const assessment = () => pending().assessment;
                  const template = () => pending().template;
                  const stages = () => Array.isArray(template().stages) ? template().stages as WorkflowDagStage[] : [];
                  return <div class="workflow-confirmation-card">
                    <div class="workflow-confirmation-heading">
                      <div><span>执行前确认 · 计划 v{pending().revision}</span><h3>{String(template().display_name || template().template_key || pending().templateKey)}</h3></div>
                      <strong>{assessment().verdict === 'incompatible' ? '适用性较低' : '需要你的确认'}</strong>
                    </div>
                    <p>{String(assessment().workflow_summary || template().description || '按图中的阶段执行任务。')}</p>
                    <dl><dt>判断原因</dt><dd>{assessment().reason || '匹配信息不足'}</dd></dl>
                    <dl><dt>计划标识</dt><dd>{pending().planHash ? pending().planHash.slice(0, 12) : '未提供'}</dd></dl>
                    <Show when={(assessment().gaps || []).length}><dl><dt>已知限制</dt><dd>{assessment().gaps!.slice(0, 4).join('；')}</dd></dl></Show>
                    <WorkflowDag stages={stages()} label="等待确认的 Workflow DAG" />
                    <div class="workflow-confirmation-actions">
                      <button onClick={() => void handleConfirmedCreateRun()} disabled={creating()}>{creating() ? '启动中…' : '确认并启动'}</button>
                      <button onClick={() => { setPendingCreationConfirmation(null); setActionFeedback('已取消创建，未产生 Workflow 运行记录'); }} disabled={creating()}>取消</button>
                    </div>
                  </div>;
                }}</Show>

	              <details class="workflow-fold workflow-template-manager">
                <summary><span>Workflow 运维</span><small>导入、编辑与删除</small></summary>
	                <div class="workflow-fold__body">
	                <div class="flex items-center justify-between gap-3">
	                  <div>
                    <div class="text-xs font-semibold text-[#e6edf3]">Workflow 管理</div>
	                    <div class="mt-1 text-[11px] text-[#8b949e]">保存或删除 workflow JSON。</div>
	                  </div>
	                  <button
	                    class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
	                    onClick={() => setTemplateManagerExpanded((value) => !value)}
	                  >
	                    {templateManagerExpanded() ? '收起' : '展开'}
	                  </button>
	                </div>

	                <Show when={templateManagerExpanded()}>
	                  <div class="mt-4 space-y-3">
	                    <div class="flex flex-wrap gap-2">
	                      <button
	                        class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-60"
	                        onClick={loadSelectedTemplateIntoDraft}
	                        disabled={!selectedTemplate()}
	                      >
                        载入当前 Workflow
	                      </button>
	                      <button
	                        class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
	                        onClick={() => setTemplateJsonDraft('')}
	                      >
	                        清空
	                      </button>
	                    </div>

	                    <textarea
	                      class="min-h-[220px] w-full rounded-2xl border border-[#30363d] bg-[#0d1216] px-3 py-3 font-mono text-[12px] leading-5 text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
	                      placeholder='{"template_key":"my_workflow","display_name":"My Workflow","domain":"protein","stages":[...]}'
	                      value={templateJsonDraft()}
	                      onInput={(event) => setTemplateJsonDraft(event.currentTarget.value)}
	                    />

	                    <label class="flex items-center gap-2 text-[11px] text-[#9faeb7]">
	                      <input
	                        type="checkbox"
	                        checked={templateOverwrite()}
	                        onInput={(event) => setTemplateOverwrite(event.currentTarget.checked)}
	                      />
                      覆盖同名 Workflow
	                    </label>

	                    <div class="flex flex-wrap gap-2">
	                      <button
	                        class="rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 py-2 text-xs font-semibold text-[#9ee6bb] transition-colors hover:border-[#38b46a]/45 disabled:cursor-not-allowed disabled:opacity-60"
	                        onClick={() => {
	                          void handleSaveTemplate();
	                        }}
	                        disabled={templateActionLoading() !== '' || !String(templateJsonDraft() || '').trim()}
	                      >
                        {templateActionLoading() === 'save' ? '保存中...' : '保存 Workflow'}
	                      </button>
	                      <button
	                        class="rounded-full border border-[#5a1e23] bg-[#201317] px-4 py-2 text-xs font-semibold text-[#ffb6b0] transition-colors hover:border-[#ff9c96]/40 disabled:cursor-not-allowed disabled:opacity-60"
	                        onClick={() => {
	                          void handleDeleteTemplate();
	                        }}
	                        disabled={templateActionLoading() !== '' || !selectedTemplate()}
	                      >
                        {templateActionLoading() === 'delete' ? '删除中...' : '删除 Workflow 定义'}
	                      </button>
	                    </div>

	                    <label class="flex items-center gap-2 text-[11px] text-[#9faeb7]">
	                      <input
	                        type="checkbox"
	                        checked={templateForceDelete()}
	                        onInput={(event) => setTemplateForceDelete(event.currentTarget.checked)}
	                      />
	                      强制删除
	                    </label>
	                  </div>
	                </Show>
	              </div>
	              </details>

	              <details class="workflow-fold workflow-run-list" open>
	                <summary><span>运行实例</span><small>{runs().length} 条记录</small></summary>
	                <div class="workflow-fold__body">
	              <div class="rounded-3xl border border-[#2a3136] bg-[#10161a] p-4">
                <div class="flex items-center justify-between gap-3">
                  <div>
                    <div class="text-xs font-semibold text-[#e6edf3]">当前会话的运行实例</div>
                    <div class="mt-1 text-[11px] text-[#8b949e]">按更新时间倒序展示。</div>
                  </div>
                  <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#8b949e]">{runs().length}</span>
                </div>

                <div class="mt-3 space-y-2">
                  <Show
                    when={runs().length > 0}
                    fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#0d1216] px-4 py-5 text-sm text-[#8b949e]">当前会话还没有流程运行实例。</div>}
                  >
                    <For each={runs()}>
                      {(run) => (
                        <button
                          class={`w-full rounded-2xl border px-3 py-3 text-left transition-colors ${selectedRun()?.run_id === run.run_id ? 'border-[#7bd0ff]/35 bg-[#10202a]' : 'border-[#2a3136] bg-[#0d1216] hover:border-[#3e484f] hover:bg-[#12191e]'}`}
                          onClick={() => setSelectedRunId(run.run_id)}
                          aria-pressed={selectedRun()?.run_id === run.run_id}
                        >
                          <div class="flex items-center justify-between gap-3">
                            <div class="min-w-0">
                              <div class="truncate text-sm font-semibold text-[#e6edf3]">{templates().find(item => item.template_key === run.template_key)?.display_name || '工作流运行'} · {formatTimestamp(run.created_at)}</div>
                              <div class="mt-1 truncate text-[11px] text-[#8b949e]">版本 {run.version || 1}</div>
                            </div>
                            <span class={`rounded-full border px-2.5 py-1 text-[10px] font-semibold ${statusClass(run.status)}`}>
                              {statusLabel(run.status)}
                            </span>
                          </div>
                        </button>
                      )}
                    </For>
                  </Show>
                </div>
                </div>
                </div>
              </details>
            </div>

            <div class="rounded-3xl border border-[#2a3136] bg-[#10161a] p-4">
              <Show
                when={selectedRun()}
                fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#0d1216] px-4 py-10 text-sm text-[#8b949e]">选择一个运行实例查看阶段和候选明细。</div>}
              >
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div class="text-xs font-semibold text-[#8b949e]">运行详情</div>
                    <div class="mt-1 text-lg font-semibold text-[#f5f7fa]">当前工作流运行</div>
                    <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-[#8b949e]">
                      <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">Workflow: {selectedRun()?.template_key}</span>
                      <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">当前阶段: {selectedRun()?.current_stage_id || '-'}</span>
                      <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">版本: v{selectedRun()?.version || 1}</span>
                      <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">候选数: {selectedRun()?.candidates.length || 0}</span>
                      <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1">待审查: {pendingWorkflowProposals(selectedRun()).length}</span>
                    </div>
                  </div>
                  <div class="flex flex-wrap items-center justify-end gap-2">
                    <button
                      class="rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-3 py-1.5 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50"
                      onClick={() => void handleSaveRunAsTemplate()}
                      disabled={actionLoading() !== '' || templateActionLoading() !== '' || selectedRun()?.status !== 'completed'}
                      title="把本次成功运行使用的计划保存为新的 Workflow 定义"
                    >
                      {templateActionLoading() === 'save' ? '保存中…' : '保存为 Workflow'}
                    </button>
                    <button
                      class="rounded-full border border-[#7bd0ff]/28 bg-[#7bd0ff]/10 px-3 py-1.5 text-xs font-semibold text-[#8ed5ff] transition-colors hover:border-[#7bd0ff]/45 hover:bg-[#7bd0ff]/14 disabled:cursor-not-allowed disabled:opacity-50"
                      onClick={() => {
                        const checkpoint = selectedRunCheckpointStage();
                        if (checkpoint) void handleRerunStage(checkpoint.stage_id);
                      }}
                      disabled={
                        actionLoading() !== ''
                        || !workflowStageCanRerun(selectedRun(), selectedRunCheckpointStage())
                      }
                      title="保留已提交的上游结果，从当前断点重新执行"
                    >
                      {actionLoading().startsWith('rerun:') ? '恢复中...' : '从当前断点重跑'}
                    </button>
                    <button
                      class="rounded-full border border-[#5a1e23] bg-[#2a1115] px-3 py-1.5 text-xs font-semibold text-[#ff9c96] transition-colors hover:border-[#ff7b72]/50 hover:bg-[#351419] disabled:cursor-not-allowed disabled:opacity-60"
                      onClick={() => {
                        void handleDeleteRun();
                      }}
                      disabled={actionLoading() !== '' || !selectedRunAllowsHistoryMutation()}
                      title={selectedRunAllowsHistoryMutation() ? '永久删除当前运行记录' : '请先终止正在执行的运行实例'}
                    >
                      {actionLoading() === 'delete-run' ? '删除中...' : '删除记录'}
                    </button>
                    <button
                      class="rounded-full border border-[#5a1e23] bg-[#201317] px-3 py-1.5 text-xs font-semibold text-[#ffb6b0] transition-colors hover:border-[#ff9c96]/40 disabled:cursor-not-allowed disabled:opacity-60"
                      onClick={() => {
                        void handleTerminateRun();
                      }}
                      disabled={
                        actionLoading() !== ''
                        || String(selectedRun()?.status || '').trim().toLowerCase() === 'terminated'
                      }
                      title={String(selectedRun()?.status || '').trim().toLowerCase() === 'terminated' ? '该运行已终止' : '完全终止当前运行'}
                    >
                      {actionLoading() === 'terminate' ? '终止中...' : '完全终止'}
                    </button>
                    <div class={`rounded-full border px-3 py-1.5 text-xs font-semibold ${statusClass(selectedRun()?.status)}`}>
                      {statusLabel(selectedRun()?.status)}
                    </div>
                  </div>
                </div>
                <Show when={selectedRun()?.metadata?.delivery_record}>{record => <div class="workflow-delivery-card">
                  <div class="workflow-delivery-heading"><div><span>交付记录</span><strong>{record().schema || 'tcode.workflow-delivery.v1'}</strong></div><b data-state={record().acceptance}>{record().acceptance === 'pending' ? '等待验收' : record().acceptance === 'accepted' ? '已验收' : '生成中'}</b></div>
                  <div class="workflow-delivery-stats">
                    <span>节点 <b>{record().stage_summary?.completed || 0}/{record().stage_summary?.total || 0}</b></span>
                    <span>验证 <b>{record().validation?.passed || 0}/{record().validation?.total || 0}</b></span>
                    <span>尝试 <b>{record().attempts || 0}</b></span>
                    <span>产物 <b>{record().artifacts?.length || 0}</b></span>
                  </div>
                  <Show when={(record().unresolved || []).length > 0}><div class="workflow-delivery-unresolved">仍有 {record().unresolved.length} 个未解决节点</div></Show>
                </div>}</Show>

                <Show when={selectedRunBlockingReason()}>
                  <div class="mt-4 rounded-2xl border border-[#f1a02b]/30 bg-[#2a2114] px-4 py-3">
                    <div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#ffe1b3]">当前阻塞原因</div>
                    <div class="mt-2 whitespace-pre-wrap text-sm text-[#ffe1b3]">{selectedRunBlockingReason()}</div>
                  </div>
                </Show>

                <WorkflowBlockedValidation run={selectedRun()} />

                <Show when={finalReportText()}>
                  <div class="mt-4 rounded-2xl border border-[#238636]/30 bg-[#0f1c13] px-4 py-4">
                    <div class="flex items-center justify-between gap-3">
                      <div><div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#9ee6bb]">{selectedRun()?.status === 'completed' ? '最终报告' : '已有阶段结果'}</div><div class="mt-1 text-xs text-[#79aa88]">{selectedRun()?.status === 'completed' ? '报告已生成，可按需打开查看。' : '以下内容尚未完成全部审核，可打开查看已有结果。'}</div></div>
                      <button type="button" class="rounded-full border border-[#38b46a]/35 bg-[#38b46a]/12 px-3 py-1.5 text-xs font-semibold text-[#9ee6bb] transition-colors hover:border-[#38b46a]/55 hover:bg-[#38b46a]/18" onClick={() => setFinalReportDetailOpen(true)}>查看完整报告</button>
                    </div>
                  </div>
                </Show>

                <Show when={asyncHistory().length > 0}>
                  <div class="mt-4 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-4">
                    <div class="flex items-center justify-between gap-3">
                      <div>
                        <div class="text-xs font-semibold text-[#e6edf3]">异步任务记录</div>
                        <div class="mt-1 text-[11px] text-[#8b949e]">已发起过的后台任务会在这里持续保留，不会因阶段切换而消失。</div>
                      </div>
                      <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1 text-[11px] text-[#8b949e]">{asyncHistory().length}</span>
                    </div>

                    <div class="mt-3 space-y-3">
                      <For each={asyncHistory()}>
                        {(item) => (
                          <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] px-3 py-3">
                            <div class="flex flex-wrap items-start justify-between gap-3">
                              <div>
                                <div class="text-sm font-semibold text-[#f5f7fa]">{item.stage.label}</div>
                                <div class="mt-1 text-[11px] text-[#8b949e]">{item.stage.stage_id} · {statusLabel(item.stage.status)}</div>
                              </div>
                              <span class={`rounded-full border px-2.5 py-1 text-[10px] font-semibold ${statusClass(item.stage.status)}`}>
                                {item.refs.length} 个异步任务
                              </span>
                            </div>

                            <div class="mt-3 flex flex-wrap gap-2">
                              <For each={item.refs}>
                                {(ref) => (
                                  <span
                                    class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/10 px-3 py-1 text-[11px] text-[#ffe1b3]"
                                    title={String(ref.output || ref.route_mode || '')}
                                  >
                                    {asyncRefLabel(ref)}
                                  </span>
                                )}
                              </For>
                            </div>

                            <Show when={item.stage.result_summary}>
                              <div class="mt-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-3 py-2">
                                <MarkdownContent
                                  content={String(item.stage.result_summary || '')}
                                  role="assistant"
                                  renderKey={`workflow-async-history-${selectedRun()?.run_id || 'none'}-${item.stage.stage_id}`}
                                  class="text-[11px] text-[#8b949e]"
                                />
                              </div>
                            </Show>
                          </div>
                        )}
                      </For>
                    </div>
                  </div>
                </Show>

                <Show when={actionFeedback()}>
                  <div class="mt-4 rounded-2xl border border-[#238636]/30 bg-[#238636]/12 px-4 py-3 text-sm text-[#9ee6bb]">{actionFeedback()}</div>
                </Show>

                <Show when={selectedStageGuardNotice()}>
                  <div class="mt-4 rounded-2xl border border-[#f1a02b]/30 bg-[#2a2114] px-4 py-3 text-sm text-[#ffe1b3]">
                    {selectedStageGuardNotice()}
                  </div>
                </Show>

                <Show when={pendingWorkflowProposals(selectedRun()).length > 0}>
                  <div class="mt-5 rounded-2xl border border-[#f1a02b]/24 bg-[#1c1710] p-4">
                    <div class="flex items-center justify-between gap-3">
                      <div>
                        <div class="text-xs font-semibold text-[#ffe1b3]">待审查提案</div>
                        <div class="mt-1 text-[11px] text-[#b99665]">Worker 用 workflow marker 提出的候选、返工或报告，需要在这里批准后才会写入运行结果。</div>
                      </div>
                      <span class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/10 px-3 py-1 text-[11px] text-[#ffe1b3]">
                        {pendingWorkflowProposals(selectedRun()).length}
                      </span>
                    </div>

                    <div class="mt-3 space-y-2">
                      <For each={pendingWorkflowProposals(selectedRun())}>
                        {(proposal) => (
                          <div class="rounded-2xl border border-[#3c3020] bg-[#13191d] px-3 py-3">
                            <div class="flex flex-wrap items-start justify-between gap-3">
                              <div class="min-w-0">
                                <div class="flex flex-wrap items-center gap-2">
                                  <span class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/10 px-2.5 py-1 text-[10px] font-semibold text-[#ffe1b3]">
                                    {proposalTypeLabel(proposal.proposal_type)}
                                  </span>
                                  <span class="text-[11px] text-[#8b949e]">{proposal.source_stage_id || '-'}</span>
                                </div>
                                <div class="mt-2 line-clamp-2 text-sm text-[#e6edf3]">{proposalPreview(proposal)}</div>
                                <div class="mt-1 text-[11px] text-[#8b949e]">{proposal.proposed_by || 'workflow'} · {formatTimestamp(proposal.created_at)}</div>
                              </div>
                              <div class="flex shrink-0 items-center gap-2">
                                <button
                                  class="rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-3 py-1.5 text-[11px] font-semibold text-[#9ee6bb] transition-colors hover:border-[#38b46a]/45 hover:bg-[#38b46a]/16 disabled:cursor-not-allowed disabled:opacity-60"
                                  onClick={() => {
                                    void handleProposalAction(proposal, 'approve');
                                  }}
                                  disabled={actionLoading() !== ''}
                                >
                                  批准
                                </button>
                                <button
                                  class="rounded-full border border-[#5a1e23] bg-[#201317] px-3 py-1.5 text-[11px] font-semibold text-[#ffb6b0] transition-colors hover:border-[#ff9c96]/40 disabled:cursor-not-allowed disabled:opacity-60"
                                  onClick={() => {
                                    void handleProposalAction(proposal, 'reject');
                                  }}
                                  disabled={actionLoading() !== ''}
                                >
                                  拒绝
                                </button>
                              </div>
                            </div>
                          </div>
                        )}
                      </For>
                    </div>
                  </div>
                </Show>

                <div class="mt-5">
                  <button
                    type="button"
                    class="flex w-full items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3 text-left transition-colors hover:border-[#3e484f] hover:bg-[#13191d]"
                    aria-expanded={executionSectionExpanded()}
                    onClick={() => setExecutionSectionExpanded(previous => !previous)}
                  >
                    <div>
                      <div class="text-xs font-semibold text-[#e6edf3]">执行记录</div>
                      <div class="mt-1 text-[11px] text-[#8b949e]">{selectedStageId() ? `当前节点 · ${selectedStageId()}` : '全部节点'} · {visibleExecutionRecords().length} 个 Agent 会话</div>
                    </div>
                    <span class="text-[11px] font-semibold text-[#8b949e]">{executionSectionExpanded() ? '收起' : '展开'}</span>
                  </button>
                  <Show when={executionSectionExpanded()}>
                    <div class="mt-3 space-y-2">
                      <Show when={selectedStageId()}>
                        <div class="flex justify-end">
                          <button class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#bdc8d1]" onClick={() => setSelectedStageId('')}>查看全部节点</button>
                        </div>
                      </Show>
                      <Show when={!executionRecordsLoading()} fallback={<div class="rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-5 text-sm text-[#8b949e]">正在加载执行记录…</div>}>
                        <Show when={visibleExecutionRecords().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#0d1216] px-4 py-5 text-sm text-[#8b949e]">当前范围暂无 Agent 讨论记录。</div>}>
                          <For each={visibleExecutionRecords()}>{record => (
                            <article class="flex items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3">
                              <div class="min-w-0">
                                <div class="flex min-w-0 items-center gap-2">
                                  <div class="truncate text-sm font-semibold text-[#e6edf3]">{record.title.replace(/^Workflow\s*·\s*/, '')}</div>
                                  <span class={`shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-semibold ${statusClass(record.status)}`}>{statusLabel(record.status)}</span>
                                </div>
                                <div class="mt-1 text-[11px] text-[#8b949e]">{record.stageId || '未关联节点'} · {record.agentName} · {record.messages.length} 条消息</div>
                              </div>
                              <button
                                type="button"
                                class="shrink-0 rounded-lg border border-[#365366] bg-[#15232b] px-3 py-1.5 text-[11px] font-semibold text-[#8ed5ff] transition-colors hover:border-[#4b7188] hover:bg-[#192b35]"
                                onClick={() => {
                                  setExecutionRecordMaximized(false);
                                  setSelectedExecutionRecord(record);
                                }}
                                aria-label={`展开查看 ${record.title.replace(/^Workflow\s*·\s*/, '')}`}
                              >
                                展开查看
                              </button>
                            </article>
                          )}</For>
                        </Show>
                      </Show>
                    </div>
                  </Show>
                </div>

                <div class="mt-5">
                  <button
                    class="flex w-full items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3 text-left transition-colors hover:border-[#3e484f] hover:bg-[#13191d]"
                    onClick={() => setAdvancedSectionExpanded((prev) => !prev)}
                  >
                    <div>
                      <div class="text-xs font-semibold text-[#e6edf3]">高级操作</div>
                      <div class="mt-1 text-[11px] text-[#8b949e]">需要人工接管时，再展开阶段控制、候选注入等联调能力。</div>
                    </div>
                    <div class="flex items-center gap-2 text-[11px] text-[#8b949e]">
                      <span>{advancedSectionExpanded() ? '收起' : '展开'}</span>
                      <svg
                        xmlns="http://www.w3.org/2000/svg"
                        class={`h-4 w-4 transition-transform ${advancedSectionExpanded() ? 'rotate-180' : ''}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                      </svg>
                    </div>
                  </button>

                  <Show when={advancedSectionExpanded()}>
                    <div class="mt-3 grid gap-4 xl:grid-cols-2">
                      <div class="rounded-2xl border border-[#2a3136] bg-[#0d1216] p-4">
                    <div class="text-xs font-semibold text-[#e6edf3]">手动推进阶段</div>
                    <div class="mt-1 text-[11px] text-[#8b949e]">用于先做联调，验证运行状态和阶段游标是否按预期变化。</div>

                    <div class="mt-4 space-y-3">
                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        阶段
                        <select
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={selectedStageId()}
                          onInput={(event) => setSelectedStageId(event.currentTarget.value)}
                        >
                          <For each={selectedRun()?.stage_states || []}>
                            {(stage) => (
                              <option value={stage.stage_id}>{stage.label}</option>
                            )}
                          </For>
                        </select>
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        状态
                        <select
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={transitionStatusValue()}
                          onInput={(event) => setTransitionStatusValue(event.currentTarget.value as 'running' | 'committed' | 'failed')}
                        >
                          <option value="running">进行中</option>
                          <option value="committed">校验并提交</option>
                          <option value="failed">失败</option>
                        </select>
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        备注
                        <textarea
                          class="mt-2 min-h-[110px] w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-3 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={transitionNote()}
                          onInput={(event) => setTransitionNote(event.currentTarget.value)}
                          placeholder="提交时写入待校验结果摘要；只有校验通过后才会进入下游"
                        />
                      </label>

                      <button
                        class="inline-flex w-full items-center justify-center rounded-full border border-[#7bd0ff]/28 bg-[#7bd0ff]/10 px-4 py-2.5 text-sm font-semibold text-[#8ed5ff] transition-colors hover:border-[#7bd0ff]/45 hover:bg-[#7bd0ff]/14 disabled:cursor-not-allowed disabled:opacity-60"
                        onClick={() => {
                          void handleStageTransition();
                        }}
                        disabled={actionLoading() !== '' || !selectedStageId()}
                      >
                        {actionLoading() === 'stage' ? '更新中...' : '更新阶段'}
                      </button>

                      <button
                        class="inline-flex w-full items-center justify-center rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-4 py-2.5 text-sm font-semibold text-[#9ee6bb] transition-colors hover:border-[#38b46a]/45 hover:bg-[#38b46a]/16 disabled:cursor-not-allowed disabled:opacity-60"
                        onClick={() => {
                          void handleExecuteStage();
                        }}
                        disabled={actionLoading() !== '' || !selectedStageId() || !selectedStageSupportsAutoRun()}
                      >
                        {actionLoading() === 'execute' ? '执行中...' : '自动运行当前阶段'}
                      </button>

                      <Show when={!selectedStageSupportsAutoRun()}>
                        <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] px-3 py-2 text-[11px] text-[#8b949e]">
                          请选择一个有效阶段后再执行。
                        </div>
                      </Show>
                    </div>
                  </div>

                      <div class="rounded-2xl border border-[#2a3136] bg-[#0d1216] p-4">
                    <div class="text-xs font-semibold text-[#e6edf3]">手动注入候选</div>
                    <div class="mt-1 text-[11px] text-[#8b949e]">在真实聚合逻辑接入前，用这个区域验证候选展示和排序字段。</div>

                    <div class="mt-4 grid gap-3 md:grid-cols-2">
                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        候选 ID
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateId()}
                          onInput={(event) => setCandidateId(event.currentTarget.value)}
                        />
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        来源阶段
                        <select
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateSourceStageId()}
                          onInput={(event) => setCandidateSourceStageId(event.currentTarget.value)}
                        >
                          <For each={selectedRun()?.stage_states || []}>
                            {(stage) => (
                              <option value={stage.stage_id}>{stage.label}</option>
                            )}
                          </For>
                        </select>
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        状态
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateStatus()}
                          onInput={(event) => setCandidateStatus(event.currentTarget.value)}
                        />
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        排名
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateRank()}
                          onInput={(event) => setCandidateRank(event.currentTarget.value)}
                          placeholder="可选，例如 1"
                        />
                      </label>
                    </div>

                    <label class="mt-3 block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                      摘要
                      <textarea
                        class="mt-2 min-h-[88px] w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-3 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                        value={candidateSummary()}
                        onInput={(event) => setCandidateSummary(event.currentTarget.value)}
                      />
                    </label>

                    <div class="mt-3 grid gap-3 md:grid-cols-3">
                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        对接分数
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateDockingScore()}
                          onInput={(event) => setCandidateDockingScore(event.currentTarget.value)}
                          placeholder="例如 -8.6"
                        />
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        相互作用数量
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateInteractionCount()}
                          onInput={(event) => setCandidateInteractionCount(event.currentTarget.value)}
                          placeholder="例如 14"
                        />
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        置信分数
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateConfidenceScore()}
                          onInput={(event) => setCandidateConfidenceScore(event.currentTarget.value)}
                          placeholder="例如 82.4"
                        />
                      </label>
                    </div>

                    <div class="mt-4 rounded-2xl border border-[#2a3136] bg-[#13191d] p-3">
                      <div class="flex items-center justify-between gap-3">
                        <div>
                          <div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">额外评分项</div>
                          <div class="mt-1 text-[11px] text-[#8b949e]">不够用时再按键值添加，不需要手写 JSON。</div>
                        </div>
                        <button
                          class="rounded-full border border-[#30363d] bg-[#0d1216] px-3 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                          onClick={() => setCandidateMetricRows((prev) => [...prev, createCandidateFieldRow()])}
                        >
                          添加指标
                        </button>
                      </div>

                      <div class="mt-3 space-y-2">
                        <Show when={candidateMetricRows().length > 0} fallback={<div class="text-[11px] text-[#8b949e]">当前没有额外评分项。</div>}>
                          <For each={candidateMetricRows()}>
                            {(row) => (
                              <div class="grid gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                                <input
                                  class="w-full rounded-2xl border border-[#30363d] bg-[#0d1216] px-3 py-2 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                                  value={row.key}
                                  onInput={(event) => updateMetricRow(row.id, 'key', event.currentTarget.value)}
                                  placeholder="指标键"
                                />
                                <input
                                  class="w-full rounded-2xl border border-[#30363d] bg-[#0d1216] px-3 py-2 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                                  value={row.value}
                                  onInput={(event) => updateMetricRow(row.id, 'value', event.currentTarget.value)}
                                  placeholder="值"
                                />
                                <button
                                  class="rounded-full border border-[#5a1e23] bg-[#201317] px-3 py-2 text-[11px] font-semibold text-[#ffb6b0] transition-colors hover:border-[#ff9c96]/40"
                                  onClick={() => setCandidateMetricRows((prev) => prev.filter((item) => item.id !== row.id))}
                                >
                                  删除
                                </button>
                              </div>
                            )}
                          </For>
                        </Show>
                      </div>
                    </div>

                    <div class="mt-4 grid gap-3 md:grid-cols-3">
                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        构象文件路径
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidatePosePath()}
                          onInput={(event) => setCandidatePosePath(event.currentTarget.value)}
                          placeholder="例如 outputs/cand-1.sdf"
                        />
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        结构文件路径
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateStructurePath()}
                          onInput={(event) => setCandidateStructurePath(event.currentTarget.value)}
                          placeholder="例如 outputs/cand-1.pdb"
                        />
                      </label>

                      <label class="block text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">
                        报告路径
                        <input
                          class="mt-2 w-full rounded-2xl border border-[#30363d] bg-[#13191d] px-3 py-2.5 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                          value={candidateReportPath()}
                          onInput={(event) => setCandidateReportPath(event.currentTarget.value)}
                          placeholder="例如 reports/cand-1.md"
                        />
                      </label>
                    </div>

                    <div class="mt-4 rounded-2xl border border-[#2a3136] bg-[#13191d] p-3">
                      <div class="flex items-center justify-between gap-3">
                        <div>
                          <div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8b949e]">额外产物引用</div>
                          <div class="mt-1 text-[11px] text-[#8b949e]">如果有额外文件，也用键值表单录入。</div>
                        </div>
                        <button
                          class="rounded-full border border-[#30363d] bg-[#0d1216] px-3 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                          onClick={() => setCandidateArtifactRows((prev) => [...prev, createCandidateFieldRow()])}
                        >
                          添加引用
                        </button>
                      </div>

                      <div class="mt-3 space-y-2">
                        <Show when={candidateArtifactRows().length > 0} fallback={<div class="text-[11px] text-[#8b949e]">当前没有额外产物引用。</div>}>
                          <For each={candidateArtifactRows()}>
                            {(row) => (
                              <div class="grid gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                                <input
                                  class="w-full rounded-2xl border border-[#30363d] bg-[#0d1216] px-3 py-2 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                                  value={row.key}
                                  onInput={(event) => updateArtifactRow(row.id, 'key', event.currentTarget.value)}
                                  placeholder="产物键"
                                />
                                <input
                                  class="w-full rounded-2xl border border-[#30363d] bg-[#0d1216] px-3 py-2 text-sm text-[#e6edf3] outline-none transition-colors focus:border-[#7bd0ff]/45"
                                  value={row.value}
                                  onInput={(event) => updateArtifactRow(row.id, 'value', event.currentTarget.value)}
                                  placeholder="路径或引用"
                                />
                                <button
                                  class="rounded-full border border-[#5a1e23] bg-[#201317] px-3 py-2 text-[11px] font-semibold text-[#ffb6b0] transition-colors hover:border-[#ff9c96]/40"
                                  onClick={() => setCandidateArtifactRows((prev) => prev.filter((item) => item.id !== row.id))}
                                >
                                  删除
                                </button>
                              </div>
                            )}
                          </For>
                        </Show>
                      </div>
                    </div>

                    <button
                      class="mt-3 inline-flex w-full items-center justify-center rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/12 px-4 py-2.5 text-sm font-semibold text-[#ffe1b3] transition-colors hover:border-[#f1a02b]/45 hover:bg-[#f1a02b]/16 disabled:cursor-not-allowed disabled:opacity-60"
                      onClick={() => {
                        void handleAttachCandidate();
                      }}
                      disabled={actionLoading() !== '' || !candidateId() || !candidateSourceStageId()}
                    >
                      {actionLoading() === 'candidate' ? '写入中...' : '写入候选'}
                    </button>
                      </div>
                    </div>
                  </Show>
                </div>

                <div class="mt-5">
                  <button
                    class="flex w-full items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3 text-left transition-colors hover:border-[#3e484f] hover:bg-[#13191d]"
                    onClick={() => setStageSectionExpanded((prev) => !prev)}
                  >
                    <div>
                      <div class="text-xs font-semibold text-[#e6edf3]">阶段状态</div>
                      <div class="mt-1 text-[11px] text-[#8b949e]">
                        已提交 {stageStatusSummary().completed} / {stageStatusSummary().total}
                      </div>
                    </div>
                    <div class="flex items-center gap-2 text-[11px] text-[#8b949e]">
                      <span>{stageSectionExpanded() ? '收起' : '展开'}</span>
                      <svg
                        xmlns="http://www.w3.org/2000/svg"
                        class={`h-4 w-4 transition-transform ${stageSectionExpanded() ? 'rotate-180' : ''}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                      </svg>
                    </div>
                  </button>

                  <Show when={stageSectionExpanded()}>
                    <div class="mt-3 space-y-3">
                      <For each={selectedRun()?.stage_states || []}>
                        {(stage) => (
                          <details
                            class="rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3"
                            open={detailIsExpanded(
                              'stage',
                              stage.stage_id,
                              workflowValidationFailures(stage.validation_report).length > 0,
                            )}
                            onToggle={(event) => rememberDetailExpansion(
                              'stage',
                              stage.stage_id,
                              event.currentTarget.open,
                            )}
                          >
                            <summary class="cursor-pointer list-none">
                              <div class="flex flex-wrap items-start justify-between gap-3">
                                <div>
                                  <div class="text-sm font-semibold text-[#f5f7fa]">{stage.label}</div>
                                  <div class="mt-1 text-[11px] text-[#8b949e]">{stage.stage_id} · {stage.owner} · {stage.call_type}</div>
                                </div>
                                <div class="flex flex-wrap items-center justify-end gap-2">
                                  <span class={`rounded-full border px-2.5 py-1 text-[10px] font-semibold ${statusClass(stage.status)}`}>
                                    {stageStatusLabel(stage)}
                                  </span>
                                  <Show when={workflowValidationFailures(stage.validation_report).length > 0}>
                                    <span class="rounded-full border border-[#da3633]/30 bg-[#da3633]/10 px-2.5 py-1 text-[10px] font-semibold text-[#ffb6b0]">
                                      校验失败 {workflowValidationFailures(stage.validation_report).length}
                                    </span>
                                  </Show>
                                </div>
                              </div>
                            </summary>
                            <div class="mt-3">
                              <Show when={stage.result_summary}>
                                <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] px-3 py-2">
                                  <MarkdownContent
                                    content={String(stage.result_summary || '')}
                                    role="assistant"
                                    renderKey={`workflow-stage-${selectedRun()?.run_id || 'none'}-${stage.stage_id}`}
                                    class="text-sm text-[#c9d1d9]"
                                  />
                                </div>
                              </Show>
                              <Show when={stageEvidence()[stage.stage_id]}>{(bundle) => (
                                <div class="mt-3 rounded-2xl border border-[#2a3136] bg-[#13191d] px-3 py-3">
                                  <div class="flex flex-wrap items-center justify-between gap-2">
                                    <div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8ed5ff]">Stage Evidence</div>
                                    <span class={`rounded-full border px-2 py-1 text-[10px] ${bundle().evidence_state === 'verified'
                                      ? 'border-[#38b46a]/30 text-[#9ee6bb]'
                                      : bundle().evidence_state === 'missing'
                                        ? 'border-[#f1a02b]/30 text-[#ffe1b3]'
                                        : 'border-[#3e484f] text-[#8b949e]'}`}>
                                      {bundle().evidence_state === 'verified' ? '已有可验证证据' : bundle().evidence_state === 'missing' ? '已完成但缺少可验证证据' : '等待阶段完成'}
                                    </span>
                                  </div>
                                  <div class="mt-2 flex flex-wrap gap-2 text-[10px] text-[#8b949e]">
                                    <span>证据 {bundle().counts.evidence}</span>
                                    <span>校验 {bundle().counts.validation_checks}</span>
                                    <span>产物 {bundle().counts.artifacts}</span>
                                  </div>
                                  <Show when={bundle().evidence.length > 0}>
                                    <div class="mt-3 space-y-2">
                                      <For each={bundle().evidence}>{(item) => (
                                        <button type="button" class="block w-full rounded-xl border border-[#2a3136] bg-[#0d1216] px-3 py-2 text-left hover:border-[#3a596c]" onClick={event => item.evidence_id && openCitation({kind:'evidence',id:String(item.evidence_id)},event.currentTarget)}>
                                          <div class="text-xs font-medium text-[#d7dee4]">{readableSourceLabel(item.title, '来源证据')}</div>
                                          <div class="mt-1 line-clamp-2 text-[11px] leading-5 text-[#8b949e]">{String(item.content || item.source_path || '')}</div>
                                        </button>
                                      )}</For>
                                    </div>
                                  </Show>
                                </div>
                              )}</Show>
                              <Show when={stageAsyncRefs(stage).length > 0}>
                                <div class="mt-3 rounded-2xl border border-[#3c3020] bg-[#17130d] px-3 py-3">
                                  <div class="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#ffe1b3]">异步引用</div>
                                  <div class="mt-2 flex flex-wrap gap-2">
                                    <For each={stageAsyncRefs(stage)}>
                                      {(ref) => (
                                        <span
                                          class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/10 px-3 py-1 text-[11px] text-[#ffe1b3]"
                                          title={String(ref.output || ref.route_mode || '')}
                                        >
                                          {asyncRefLabel(ref)}
                                        </span>
                                      )}
                                    </For>
                                  </div>
                                  <Show when={stage.metadata?.async_ref_count}>
                                    <div class="mt-2 text-[11px] text-[#b99665]">
                                      该阶段已落盘，刷新页面或重新进入当前会话后仍会保留。
                                    </div>
                                  </Show>
                                </div>
                              </Show>
                              <Show when={stage.last_error}>
                                <div class="mt-3 rounded-2xl border border-[#5a1e23] bg-[#201317] px-3 py-2 text-sm text-[#ffb6b0]">
                                  <Show when={stage.diagnostic} fallback={<p>当前步骤未完成，原因暂未分类。请查看技术详情定位问题。</p>}>
                                    {diagnostic => <><strong>{diagnostic().title}</strong><p>{diagnostic().message}</p><p>处理建议：{diagnostic().suggestion}</p></>}
                                  </Show>
                                  <details><summary>技术详情</summary><pre class="whitespace-pre-wrap text-xs">{stage.last_error}</pre></details>
                                </div>
                              </Show>
                              <Show when={stage.status !== 'committed' && (stage.draft_result?.response || stage.metadata?.previous_attempt_result?.draft_result?.response)}>
                                <details class="mt-3 rounded-xl border border-[#3e484f] p-3" data-testid="workflow-preserved-draft">
                                  <summary>查看保留的阶段草稿（未通过审核）</summary>
                                  <MarkdownContent content={String(stage.draft_result?.response || stage.metadata?.previous_attempt_result?.draft_result?.response)} role="assistant" renderKey={`draft-${stage.stage_id}-${stage.attempt}`} class="mt-2 text-sm" />
                                </details>
                              </Show>
                              <WorkflowValidationReportPanel
                                report={stage.validation_report}
                                variant="stage"
                              />
                              <Show when={Array.isArray(stage.metadata?.fallback_stage_ids) && stage.metadata.fallback_stage_ids.length > 0}>
                                <div class="mt-3 border-l-2 border-[#f1a02b]/40 pl-3 text-[11px] text-[#b99665]">
                                  失败回退: {stage.metadata.fallback_stage_ids.join(' → ')}
                                  <Show when={stage.metadata?.fallback_selected_stage_id}>
                                    <span class="ml-2 text-[#ffe1b3]">当前已回退到 {stage.metadata.fallback_selected_stage_id}</span>
                                  </Show>
                                  <Show when={stage.metadata?.fallback_exhausted}>
                                    <span class="ml-2 text-[#ff9c96]">回退路径已耗尽</span>
                                  </Show>
                                </div>
                              </Show>
                              <div class="mt-3 flex justify-end">
                                <button
                                  class="rounded-full border border-[#7bd0ff]/28 bg-[#7bd0ff]/10 px-3 py-1.5 text-xs font-semibold text-[#8ed5ff] transition-colors hover:border-[#7bd0ff]/45 hover:bg-[#7bd0ff]/14 disabled:cursor-not-allowed disabled:opacity-50"
                                  onClick={() => {
                                    void handleRerunStage(stage.stage_id);
                                  }}
                                  disabled={actionLoading() !== '' || !workflowStageCanRerun(selectedRun(), stage)}
                                  title={
                                    workflowStageCanRerun(selectedRun(), stage)
                                      ? '保留上游结果，从这个阶段重新执行'
                                      : '运行中实例需先终止；所选阶段的上游也必须已提交'
                                  }
                                >
                                  {actionLoading() === `rerun:${stage.stage_id}` ? '恢复中...' : '从此阶段重跑'}
                                </button>
                              </div>
                            </div>
                          </details>
                        )}
                      </For>
                    </div>
                  </Show>
                </div>

                <div class="mt-5">
                  <button
                    class="flex w-full items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3 text-left transition-colors hover:border-[#3e484f] hover:bg-[#13191d]"
                    onClick={() => setCandidateSectionExpanded((prev) => !prev)}
                  >
                    <div>
                      <div class="text-xs font-semibold text-[#e6edf3]">候选卡片</div>
                      <div class="mt-1 text-[11px] text-[#8b949e]">
                        共 {candidateStats().total} 个，已排名 {candidateStats().ranked} 个
                      </div>
                    </div>
                    <div class="flex items-center gap-2 text-[11px] text-[#8b949e]">
                      <span>{candidateSectionExpanded() ? '收起' : '展开'}</span>
                      <svg
                        xmlns="http://www.w3.org/2000/svg"
                        class={`h-4 w-4 transition-transform ${candidateSectionExpanded() ? 'rotate-180' : ''}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                      </svg>
                    </div>
                  </button>

                  <Show when={candidateSectionExpanded()}>
                    <div class="mt-3 space-y-3">
                      <div class="flex items-center justify-between rounded-2xl border border-[#2a3136] bg-[#13191d] px-3 py-2 text-[11px] text-[#8b949e]">
                        <span>第 {candidatePage()} / {candidatePageCount()} 页</span>
                        <div class="flex items-center gap-2">
                          <button
                            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50"
                            onClick={() => setCandidatePage((prev) => Math.max(1, prev - 1))}
                            disabled={candidatePage() <= 1}
                          >
                            上一页
                          </button>
                          <button
                            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50"
                            onClick={() => setCandidatePage((prev) => Math.min(candidatePageCount(), prev + 1))}
                            disabled={candidatePage() >= candidatePageCount()}
                          >
                            下一页
                          </button>
                        </div>
                      </div>
                      <Show
                        when={pagedCandidates().length > 0}
                        fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#0d1216] px-4 py-5 text-sm text-[#8b949e]">这个运行实例还没有候选汇总。</div>}
                      >
                        <For each={pagedCandidates()}>
                          {(candidate) => (
                            <details
                              class="rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3"
                              open={detailIsExpanded('candidate', candidate.candidate_id)}
                              onToggle={(event) => rememberDetailExpansion(
                                'candidate',
                                candidate.candidate_id,
                                event.currentTarget.open,
                              )}
                            >
                              <summary class="cursor-pointer list-none">
                                <div class="flex flex-wrap items-start justify-between gap-3">
                                  <div>
                                    <div class="text-sm font-semibold text-[#f5f7fa]">{candidate.candidate_id}</div>
                                    <div class="mt-1 text-[11px] text-[#8b949e]">{candidate.source_stage_id} · {statusLabel(candidate.status)}</div>
                                  </div>
                                  <Show when={candidate.rank !== null && candidate.rank !== undefined}>
                                    <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1 text-[11px] text-[#bdc8d1]">排名 {candidate.rank}</span>
                                  </Show>
                                </div>
                              </summary>
                              <div class="mt-3">
                                <Show when={candidate.summary}>
                                  <div class="text-sm text-[#c9d1d9]">{candidate.summary}</div>
                                </Show>
                                <Show when={Object.keys(candidate.score_card || {}).length > 0}>
                                  <div class="mt-3 flex flex-wrap gap-2">
                                    <For each={Object.entries(candidate.score_card || {})}>
                                      {(entry) => (
                                        <span class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1 text-[11px] text-[#8ed5ff]">{entry[0]}: {String(entry[1])}</span>
                                      )}
                                    </For>
                                  </div>
                                </Show>
                                <Show when={Object.keys(candidate.artifact_refs || {}).length > 0}>
                                  <div class="mt-3 flex flex-wrap gap-2">
                                    <For each={Object.entries(candidate.artifact_refs || {})}>
                                      {(entry) => (
                                        <button
                                          class="rounded-full border border-[#2a3136] bg-[#171c20] px-3 py-1 text-[11px] text-[#ffc176] transition-colors hover:border-[#f1a02b]/40 hover:text-[#ffe1b3]"
                                          onClick={() => sessionStore.requestFilePreview(String(entry[1] || ''))}
                                          title={`预览 ${entry[0]}`}
                                        >
                                          {entry[0]}: {String(entry[1])}
                                        </button>
                                      )}
                                    </For>
                                  </div>
                                </Show>
                              </div>
                            </details>
                          )}
                        </For>
                      </Show>
                    </div>
                  </Show>
                </div>

                <div class="mt-5">
                  <button
                    class="flex w-full items-center justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3 text-left transition-colors hover:border-[#3e484f] hover:bg-[#13191d]"
                    onClick={() => setEventSectionExpanded((prev) => !prev)}
                  >
                    <div>
                      <div class="text-xs font-semibold text-[#e6edf3]">系统事件</div>
                      <div class="mt-1 text-[11px] text-[#8b949e]">共 {eventSummary().total} 条</div>
                    </div>
                    <div class="flex items-center gap-2 text-[11px] text-[#8b949e]">
                      <span>{eventSectionExpanded() ? '收起' : '展开'}</span>
                      <svg
                        xmlns="http://www.w3.org/2000/svg"
                        class={`h-4 w-4 transition-transform ${eventSectionExpanded() ? 'rotate-180' : ''}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                      </svg>
                    </div>
                  </button>

                  <Show when={eventSectionExpanded()}>
                    <div class="mt-3 space-y-2">
                      <div class="flex items-center justify-between rounded-2xl border border-[#2a3136] bg-[#13191d] px-3 py-2 text-[11px] text-[#8b949e]">
                        <span>第 {eventPage()} / {eventPageCount()} 页</span>
                        <div class="flex items-center gap-2">
                          <button
                            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50"
                            onClick={() => setEventPage((prev) => Math.max(1, prev - 1))}
                            disabled={eventPage() <= 1}
                          >
                            上一页
                          </button>
                          <button
                            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-50"
                            onClick={() => setEventPage((prev) => Math.min(eventPageCount(), prev + 1))}
                            disabled={eventPage() >= eventPageCount()}
                          >
                            下一页
                          </button>
                        </div>
                      </div>
                      <Show
                          when={pagedWorkflowEvents().length > 0}
                        fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#0d1216] px-4 py-5 text-sm text-[#8b949e]">暂无运行事件。</div>}
                      >
                          <For each={pagedWorkflowEvents()}>
                          {(event) => (
                            <details
                              class="rounded-2xl border border-[#2a3136] bg-[#0d1216] px-4 py-3"
                              open={detailIsExpanded('event', event.event_id)}
                              onToggle={(toggleEvent) => rememberDetailExpansion(
                                'event',
                                event.event_id,
                                toggleEvent.currentTarget.open,
                              )}
                            >
                              <summary class="cursor-pointer list-none">
                                <div class="flex flex-wrap items-center justify-between gap-3">
                                  <div class="min-w-0">
                                    <div class="truncate text-sm font-semibold text-[#e6edf3]">{event.kind}</div>
                                    <div class="mt-1 text-[11px] text-[#8b949e]">{event.stage_id || '-'} · {event.source || 'workflow'}</div>
                                  </div>
                                  <span class="text-[11px] text-[#8b949e]">{formatTimestamp(event.created_at)}</span>
                                </div>
                              </summary>
                              <Show when={event.summary}>
                                <div class="mt-2 text-sm text-[#c9d1d9]">{event.summary}</div>
                              </Show>
                            </details>
                          )}
                        </For>
                      </Show>
                    </div>
                  </Show>
                </div>
              </Show>
            </div>
          </div>
          </Show>
        </Show>
      </div>
      <Show when={props.active !== false && selectedExecutionRecord()}>
        <Portal>
          <div
            class="fixed inset-0 z-[175] flex items-end justify-center bg-black/65 p-3 backdrop-blur-sm sm:items-center sm:p-6"
            onClick={() => {
              setSelectedExecutionRecord(null);
              setExecutionRecordMaximized(false);
            }}
          >
            <section
              role="dialog"
              aria-modal="true"
              aria-labelledby="workflow-execution-record-title"
              class={`relative flex flex-col overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0d1216] shadow-[0_28px_90px_rgba(0,0,0,0.52)] ${executionRecordMaximized()
                ? 'h-[calc(100dvh-1.5rem)] w-[calc(100vw-1.5rem)] sm:h-[calc(100dvh-3rem)] sm:w-[calc(100vw-3rem)]'
                : executionRecordSize()
                  ? ''
                  : 'h-[82dvh] w-full sm:h-[72vh] sm:max-h-[calc(100dvh-3rem)] sm:min-h-[420px] sm:w-[48rem] sm:min-w-[36rem] sm:max-w-[calc(100vw-3rem)]'}`}
              style={executionRecordDialogStyle()}
              onClick={(event) => event.stopPropagation()}
            >
              <header class="flex items-center justify-between gap-3 border-b border-[#26313a] px-4 py-3 sm:px-5">
                <div class="min-w-0">
                  <div class="flex min-w-0 items-center gap-2">
                    <h2 id="workflow-execution-record-title" class="truncate text-sm font-semibold text-[#eef4f7]">
                      {selectedExecutionRecord()?.title.replace(/^Workflow\s*·\s*/, '')}
                    </h2>
                    <span class={`shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-semibold ${statusClass(selectedExecutionRecord()?.status || '')}`}>
                      {statusLabel(selectedExecutionRecord()?.status || '')}
                    </span>
                  </div>
                  <div class="mt-1 truncate text-[11px] text-[#7f8b93]">
                    {selectedExecutionRecord()?.stageId || '未关联节点'} · {selectedExecutionRecord()?.agentName} · {selectedExecutionRecord()?.messages.length || 0} 条消息
                    <Show when={!executionRecordMaximized()}><span class="hidden sm:inline"> · 可拖动右下角调整大小</span></Show>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    class="h-8 rounded-lg border border-[#3e484f] bg-[#171c20] px-3 text-[11px] font-semibold text-[#b7c7d0] transition-colors hover:border-[#4f6571] hover:bg-[#1b2328]"
                    onClick={() => setExecutionRecordMaximized(previous => !previous)}
                    aria-pressed={executionRecordMaximized()}
                  >
                    {executionRecordMaximized() ? '还原小窗' : '放大'}
                  </button>
                  <button
                    type="button"
                    class="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[#3e484f] bg-[#171c20] text-[#b7c7d0] transition-colors hover:border-[#4f6571] hover:bg-[#1b2328]"
                    onClick={() => {
                      setSelectedExecutionRecord(null);
                      setExecutionRecordMaximized(false);
                    }}
                    title="关闭"
                    aria-label="关闭执行记录"
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M18 6 6 18" />
                      <path stroke-linecap="round" stroke-linejoin="round" d="m6 6 12 12" />
                    </svg>
                  </button>
                </div>
              </header>
              <div class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain bg-[#0a0f12] px-3 py-4 sm:px-5">
                <Show
                  when={(selectedExecutionRecord()?.messages.length || 0) > 0}
                  fallback={<div class="flex h-full items-center justify-center text-sm text-[#71808a]">这条执行记录还没有消息。</div>}
                >
                  <div class="space-y-4">
                    <For each={selectedExecutionRecord()?.messages || []}>{message => {
                      const fromUser = message.role === 'user';
                      const roleLabel = fromUser
                        ? '任务输入'
                        : message.role === 'assistant'
                          ? selectedExecutionRecord()?.agentName || 'Agent'
                          : message.role === 'tool'
                            ? '工具结果'
                            : message.role;
                      return (
                        <div class={`flex ${fromUser ? 'justify-end' : 'justify-start'}`}>
                          <div class="max-w-[88%] sm:max-w-[82%]">
                            <div class={`mb-1 px-1 text-[10px] font-semibold tracking-[0.08em] text-[#71808a] ${fromUser ? 'text-right' : ''}`}>
                              {roleLabel}
                            </div>
                            <div class={`rounded-2xl border px-3.5 py-3 ${fromUser ? 'rounded-br-md border-[#a8c8d8] bg-[#dcecf3]' : 'rounded-bl-md border-[#d8d1c6] bg-[#f3f0e9]'}`}>
                              <MarkdownContent
                                content={String(message.content || '')}
                                role={fromUser ? 'user' : 'assistant'}
                                renderKey={`workflow-record-dialog-${selectedExecutionRecord()?.sessionId}-${message.id}`}
                                class="workflow-record-message break-words text-xs leading-6"
                              />
                            </div>
                          </div>
                        </div>
                      );
                    }}</For>
                  </div>
                </Show>
              </div>
              <Show when={!executionRecordMaximized()}>
                <div
                  class="absolute bottom-0 right-0 z-10 hidden h-5 w-5 cursor-nwse-resize touch-none items-center justify-center text-[#5b6b74] transition-colors hover:text-[#8ed5ff] sm:flex"
                  role="separator"
                  aria-label="拖动调整窗口大小"
                  title="拖动调整大小（双击还原）"
                  onPointerDown={startExecutionRecordResize}
                  onDblClick={() => setExecutionRecordSize(null)}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
                    <path d="M8 17 17 8" />
                    <path d="M13 17 17 13" />
                    <path d="M3 17 17 3" />
                  </svg>
                </div>
              </Show>
            </section>
          </div>
        </Portal>
      </Show>
      <Show when={props.active !== false && finalReportDetailOpen() && finalReportText()}>
        <Portal>
          <div
            class="fixed inset-0 z-[170] flex items-end justify-center bg-black/65 p-3 backdrop-blur-sm sm:items-center sm:p-6"
            onClick={() => setFinalReportDetailOpen(false)}
          >
            <section
              role="dialog"
              aria-modal="true"
              aria-label="Workflow 完整最终报告"
              class="flex h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0d1216] shadow-[0_28px_90px_rgba(0,0,0,0.52)] sm:h-[82vh]"
              onClick={(event) => event.stopPropagation()}
            >
              <header class="flex items-center justify-between gap-3 border-b border-[#26313a] px-4 py-3 sm:px-5">
                <div class="min-w-0">
                  <div class="text-xs font-semibold uppercase tracking-[0.16em] text-[#9ee6bb]">Workflow 最终报告</div>
                  <div class="mt-1 truncate font-mono text-[11px] text-[#7f8b93]">当前工作流运行</div>
                </div>
                <button
                  type="button"
                  class="inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg border border-[#3e484f] bg-[#171c20] text-[#b7c7d0] transition-colors hover:border-[#4f6571] hover:bg-[#1b2328]"
                  onClick={() => setFinalReportDetailOpen(false)}
                  title="关闭"
                  aria-label="关闭完整报告"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M18 6 6 18" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="m6 6 12 12" />
                  </svg>
                </button>
              </header>
              <div class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 sm:px-6 sm:py-5">
                <MarkdownContent
                  content={finalReportText()}
                  role="assistant"
                  renderKey={`workflow-final-report-detail-${selectedRun()?.run_id || 'none'}`}
                  class="text-sm text-[#d8f7e4]"
                />
              </div>
            </section>
          </div>
        </Portal>
      </Show>
    </div>
  );
}
