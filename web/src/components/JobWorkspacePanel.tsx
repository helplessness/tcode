import { Show, For, createEffect, createMemo, createSignal, onCleanup } from 'solid-js';
import { toolDisplayLabel } from '../utils/userFacingLabels';
import { localJobResultRoot } from '../utils/jobResultLocation';
import {
  cancelRuntimeJob,
  createJobPreviewTicket,
  getJobTimeline,
  getJobWorkspace,
  getRuntimeJob,
  getRuntimeJobEvents,
  recoverJobArtifacts,
  resumeJob,
  retryJob,
  type RuntimeJob,
  type RuntimeJobEvent,
  type JobWorkspaceArtifact,
  type JobTimelineEntry,
  type JobWorkspaceArtifactSource,
  type JobWorkspacePreviewEntry,
  type JobWorkspace,
  type SymphonyIssueCancelAction,
} from '../api/client';
import FileBrowser from './FileBrowser';
import JobActionControls from './JobActionControls';
import { sessionStore, type FilePreviewRequest } from '../stores/session';

interface JobWorkspacePanelProps {
  workspace?: JobWorkspace | null;
  title?: string;
  onBrowserOpenChange?: (open: boolean) => void;
  onActionComplete?: () => void;
}

function formatPath(value?: string) {
  const text = String(value || '').trim();
  return text || '-';
}

function describeCancelAction(action: SymphonyIssueCancelAction) {
  const kind = String(action.kind || '').trim();
  const kindLabel =
    kind === 'adapter_runner'
      ? '子代理执行进程'
      : kind === 'job_protocol_process_group'
        ? '异步作业进程'
        : kind || '后台进程';
  const status = action.terminated ? '已终止' : '未终止';
  const details = [
    action.pid ? `PID ${action.pid}` : '',
    action.job_id ? `job ${action.job_id}` : '',
    action.process_id ? `process ${action.process_id}` : '',
  ].filter(Boolean).join(' · ');
  const reason = String(action.reason || '').trim();
  return [kindLabel, status, details, reason ? `原因: ${reason}` : '']
    .filter(Boolean)
    .join(' · ');
}

function formatTimestamp(value?: string) {
  const text = String(value || '').trim();
  if (!text) return '-';
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return parsed.toLocaleString();
}

function describeLifecycleStatus(workspace: JobWorkspace) {
  const raw = String(
    workspace.lifecycle_status ||
    workspace.status ||
    workspace.issue_state ||
    ''
  ).trim();
  const labels: Record<string, string> = {
    completed: '已完成',
    done: '已完成',
    closed: '已关闭',
    failed: '失败',
    cancelled: '已取消',
    canceled: '已取消',
    terminated: '已终止',
    running: '运行中',
    queued: '排队中',
    pending: '待处理',
    reviewing: '审核中',
    blocked: '已阻塞',
  };
  return labels[raw.toLowerCase()] || raw || '-';
}

function relativePathFromRoot(path: string, root: string) {
  const normalizedPath = String(path || '').trim();
  const normalizedRoot = String(root || '').replace(/\/+$/, '').trim();
  if (!normalizedPath) return '';
  if (!normalizedRoot) return normalizedPath;
  if (normalizedPath === normalizedRoot) return '';
  if (normalizedPath.startsWith(`${normalizedRoot}/`)) {
    return normalizedPath.slice(normalizedRoot.length + 1);
  }
  return normalizedPath;
}

function resolveWorkspaceJobId(workspace?: JobWorkspace | null) {
  if (!workspace) return '';
  return String(
    workspace.job_id ||
    workspace.job_ref?.job_id ||
    workspace.job_ref?.id ||
    ''
  ).trim();
}

export default function JobWorkspacePanel(props: JobWorkspacePanelProps) {
  const [hydratedWorkspace, setHydratedWorkspace] = createSignal<JobWorkspace | null>(null);
  const [timeline, setTimeline] = createSignal<JobTimelineEntry[]>([]);
  const [runtimeJob, setRuntimeJob] = createSignal<RuntimeJob | null>(null);
  const [runtimeEvents, setRuntimeEvents] = createSignal<RuntimeJobEvent[]>([]);
  const [runtimeJobError, setRuntimeJobError] = createSignal('');
  const [runtimeCancelLoading, setRuntimeCancelLoading] = createSignal(false);
  const [loadingWorkspace, setLoadingWorkspace] = createSignal(false);
  const [openingPreview, setOpeningPreview] = createSignal(false);
  const [actionLoading, setActionLoading] = createSignal<'retry' | 'resubmit' | 'resume' | 'copy' | ''>('');
  const [actionError, setActionError] = createSignal('');
  const [recoveryOpen, setRecoveryOpen] = createSignal(false);
  const [bundleReference, setBundleReference] = createSignal('');
  const [recovering, setRecovering] = createSignal(false);
  const [recoveryMessage, setRecoveryMessage] = createSignal('');
  const [recoveryError, setRecoveryError] = createSignal('');
  const [browserOpen, setBrowserOpen] = createSignal(false);
  const [browserMode, setBrowserMode] = createSignal<'workspace' | 'artifacts'>('workspace');
  const [browserRoot, setBrowserRoot] = createSignal('');
  const [browserArtifactRoot, setBrowserArtifactRoot] = createSignal('');
  const [browserInitialPath, setBrowserInitialPath] = createSignal('');
  const [browserDisplayPath, setBrowserDisplayPath] = createSignal('');
  const [browserPreviewRequest, setBrowserPreviewRequest] = createSignal<FilePreviewRequest | null>(null);

  const effectiveWorkspace = createMemo<JobWorkspace>(() => ({
    ...(props.workspace || {}),
    ...(hydratedWorkspace() || {}),
  }));
  const effectiveJobId = createMemo(() => resolveWorkspaceJobId(effectiveWorkspace()));
  const recoveryAllowed = () => Boolean(effectiveJobId() && effectiveWorkspace().issue_identifier
    && ['failed', 'cancelled', 'canceled', 'completed', 'done', 'closed'].includes(String(
      effectiveWorkspace().tracker_status || effectiveWorkspace().lifecycle_status || effectiveWorkspace().status || '',
    ).toLowerCase()));
  const recoverArtifacts = async () => {
    if (recovering() || !recoveryAllowed()) return;
    const sessionId = sessionStore.currentSessionId();
    const jobId = effectiveJobId();
    setRecovering(true); setRecoveryError(''); setRecoveryMessage('');
    try {
      const data = await recoverJobArtifacts(jobId, {
        session_id: effectiveWorkspace().session_id || sessionId || '',
        workspace_id: effectiveWorkspace().workspace_id || sessionStore.currentSession()?.workspace_id || '',
        tracker_identifier: effectiveWorkspace().issue_identifier || '', bundle_id: bundleReference().trim(),
      });
      if (sessionId !== sessionStore.currentSessionId() || jobId !== effectiveJobId()) return;
      setHydratedWorkspace({...effectiveWorkspace(), ...data.workspace});
      setRecoveryMessage(`已取回并校验 ${data.recovery.total_files} 个文件。原任务状态保留，未重新运行计算。`);
      props.onActionComplete?.();
    } catch (error) {
      if (sessionId === sessionStore.currentSessionId() && jobId === effectiveJobId()) setRecoveryError(error instanceof Error ? error.message : '取回失败，未重新提交计算');
    } finally { setRecovering(false); }
  };
  createEffect(() => {
    effectiveJobId();
    setRecoveryOpen(false); setBundleReference(''); setRecoveryMessage(''); setRecoveryError('');
  });

  const hasWorkspace = createMemo(() => {
    const workspace = effectiveWorkspace();
    return Boolean(
      workspace.job_id ||
      workspace.job_ref?.job_id ||
      workspace.job_ref?.id ||
      workspace.run_id ||
      workspace.workdir ||
      workspace.tool ||
      workspace.task_type ||
      workspace.issue_identifier ||
      workspace.session_id ||
      workspace.parent_session_id ||
      workspace.status ||
      workspace.issue_state ||
      workspace.lifecycle_status ||
      workspace.context_file ||
      workspace.log_file ||
      (workspace.orchestration && Object.keys(workspace.orchestration).length > 0) ||
      (workspace.tool_contract && Object.keys(workspace.tool_contract).length > 0) ||
      (workspace.action_policy && Object.keys(workspace.action_policy).length > 0) ||
      (Array.isArray(workspace.preview_ports) && workspace.preview_ports.length > 0) ||
      (Array.isArray(workspace.expected_artifacts) && workspace.expected_artifacts.length > 0) ||
      (Array.isArray(workspace.timeline) && workspace.timeline.length > 0) ||
      (workspace.input_snapshot && Object.keys(workspace.input_snapshot).length > 0) ||
      (workspace.input_summary && Object.keys(workspace.input_summary).length > 0)
    );
  });

  const firstArtifactEntry = createMemo<JobWorkspaceArtifact | null>(() => {
    const workspace = effectiveWorkspace();
    const entries = Array.isArray(workspace.artifact_entries) ? workspace.artifact_entries : [];
    if (entries.length > 0) {
      return entries[0];
    }
    const artifacts = Array.isArray(workspace.artifacts) && workspace.artifacts.length > 0
      ? workspace.artifacts
      : Array.isArray(workspace.expected_artifacts)
        ? workspace.expected_artifacts
        : [];
    const firstPath = String(artifacts[0] || '').trim();
    if (!firstPath) return null;
    return {
      path: firstPath,
      type: firstPath.includes('.') ? 'file' : 'directory',
    };
  });

  const firstArtifact = createMemo(() => {
    return String(firstArtifactEntry()?.path || '').trim();
  });

  const artifactSource = createMemo<JobWorkspaceArtifactSource>(() => {
    const source = effectiveWorkspace().artifact_source;
    return source && typeof source === 'object' ? source : {};
  });

  const previewEntries = createMemo<JobWorkspacePreviewEntry[]>(() => {
    const entries = effectiveWorkspace().preview_entries;
    return Array.isArray(entries) ? entries : [];
  });

  const firstPreviewPort = createMemo(() => {
    const ports = effectiveWorkspace().preview_ports || [];
    return ports.length > 0 ? Number(ports[0]) : 0;
  });

  const timelineEntries = createMemo<JobTimelineEntry[]>(() => {
    const canonical = runtimeEvents().map((event) => ({
      kind: event.phase || event.status,
      at: event.created_at ? new Date(event.created_at * 1000).toISOString() : undefined,
      title: `${event.status || 'unknown'} · Cursor ${event.cursor}`,
      source: runtimeJob()?.provider || 'task-runtime',
      details: event.phase || '',
      metadata: event.metadata,
    }));
    if (canonical.length > 0) return canonical;
    if (timeline().length > 0) return timeline();
    const fromWorkspace = effectiveWorkspace().timeline;
    return Array.isArray(fromWorkspace) ? fromWorkspace : [];
  });

  const supportsResume = createMemo(() => {
    const workspace = effectiveWorkspace();
    return Boolean(workspace.resume_supported || String(workspace.resume_command || '').trim());
  });
  const actionPolicy = createMemo<Record<string, any>>(() => {
    const policy = effectiveWorkspace().action_policy;
    return policy && typeof policy === 'object' ? policy : {};
  });

  const isTerminalState = createMemo(() => {
    const normalized = String(
      effectiveWorkspace().lifecycle_status ||
      effectiveWorkspace().status ||
      effectiveWorkspace().issue_state ||
      ''
    ).trim().toLowerCase();
    return normalized === 'completed'
      || normalized === 'done'
      || normalized === 'closed'
      || normalized === 'failed'
      || normalized === 'cancelled'
      || normalized === 'canceled'
      || normalized === 'terminated';
  });

  const retryReason = createMemo(() => {
    const workspace = effectiveWorkspace();
    const policyRetry = actionPolicy().retry;
    if (policyRetry && typeof policyRetry === 'object') {
      return String(policyRetry.reason || '').trim();
    }
    const explicit = String(workspace.retry_reason || workspace.resubmit_reason || '').trim();
    if (explicit) return explicit;
    if (!effectiveJobId()) {
      return '当前任务还没有真正生成 job_id，通常仍停留在 Todo 或人工审核阶段。';
    }
    if (!isTerminalState()) {
      return `当前任务状态是 ${describeLifecycleStatus(workspace)}，只有终态任务才能重试。`;
    }
    return '';
  });

  const retryAvailable = createMemo(() => {
    const workspace = effectiveWorkspace();
    const policyRetry = actionPolicy().retry;
    if (policyRetry && typeof policyRetry === 'object') return Boolean(policyRetry.available);
    if (workspace.retry_available === false || workspace.resubmit_available === false) return false;
    return !retryReason();
  });

  const resumeReason = createMemo(() => {
    const workspace = effectiveWorkspace();
    const policyResume = actionPolicy().resume;
    if (policyResume && typeof policyResume === 'object') {
      return String(policyResume.reason || '').trim();
    }
    const explicit = String(workspace.resume_reason || '').trim();
    if (explicit) return explicit;
    if (!effectiveJobId()) {
      return '当前任务还没有真正生成 job_id，暂时不能恢复。';
    }
    if (!supportsResume()) {
      return '当前执行层没有声明 resume 能力，所以只能重试，不能恢复。';
    }
    if (!isTerminalState()) {
      return `当前任务状态是 ${describeLifecycleStatus(workspace)}，只有终态任务才能恢复。`;
    }
    return '';
  });

  const resumeAvailable = createMemo(() => {
    const workspace = effectiveWorkspace();
    const policyResume = actionPolicy().resume;
    if (policyResume && typeof policyResume === 'object') return Boolean(policyResume.available);
    if (workspace.resume_available === false) return false;
    return !resumeReason();
  });

  const dirname = (path: string) => path.split('/').slice(0, -1).join('/') || '/';

  const openBrowser = (
    path?: string,
    type?: 'file' | 'directory',
    mode: 'workspace' | 'artifacts' = 'workspace',
  ) => {
    const nextPath = String(path || '').trim();
    const workspaceRoot = String(effectiveWorkspace().workdir || '').trim();
    const artifactRoot = String(artifactSource().root || '').trim();
    let nextRoot = mode === 'artifacts' ? artifactRoot : workspaceRoot;
    let nextDisplayPath = nextPath || nextRoot;
    let preview: FilePreviewRequest | null = null;

    if (mode === 'artifacts') {
      if (nextPath) {
        const relativePath = relativePathFromRoot(nextPath, artifactRoot);
        if (type === 'directory') {
          preview = null;
          nextDisplayPath = nextPath;
          nextRoot = artifactRoot;
          setBrowserPreviewRequest(null);
          setBrowserInitialPath(relativePathFromRoot(nextPath, artifactRoot));
          setBrowserDisplayPath(nextDisplayPath);
          setBrowserArtifactRoot(artifactRoot);
          setBrowserRoot('');
          setBrowserMode('artifacts');
          setBrowserOpen(true);
          return;
        }
        preview = relativePath ? { path: relativePath, key: Date.now() } : null;
        nextDisplayPath = nextPath;
      }
      setBrowserArtifactRoot(artifactRoot);
      setBrowserInitialPath('');
      setBrowserRoot('');
      setBrowserMode('artifacts');
      setBrowserDisplayPath(nextDisplayPath || artifactRoot);
      setBrowserPreviewRequest(preview);
      setBrowserOpen(true);
      return;
    }

    if (nextPath) {
      if (type === 'directory') {
        nextRoot = nextPath;
      } else if (type === 'file') {
        nextRoot = dirname(nextPath);
        preview = { path: nextPath, key: Date.now() };
      } else if (nextPath.startsWith('/')) {
        nextRoot = dirname(nextPath);
        preview = { path: nextPath, key: Date.now() };
      } else {
        preview = { path: nextPath, key: Date.now() };
      }
    }

    setBrowserMode('workspace');
    setBrowserArtifactRoot('');
    setBrowserInitialPath('');
    setBrowserRoot(nextRoot || workspaceRoot);
    setBrowserDisplayPath(nextPath || workspaceRoot);
    setBrowserPreviewRequest(preview);
    setBrowserOpen(true);
  };

  const closeBrowser = () => {
    setBrowserOpen(false);
  };

  const refreshWorkspace = async (jobId: string) => {
    const [workspaceData, timelineData] = await Promise.all([
      getJobWorkspace(jobId),
      getJobTimeline(jobId),
    ]);
    setHydratedWorkspace({
      ...(workspaceData.workspace || {}),
      lifecycle_status: timelineData.lifecycle_status || workspaceData.workspace?.lifecycle_status,
      timeline: Array.isArray(timelineData.timeline) ? timelineData.timeline : workspaceData.workspace?.timeline,
    });
    setTimeline(Array.isArray(timelineData.timeline) ? timelineData.timeline : []);
  };

  const openPreview = async (requestedPort?: number) => {
    const jobId = effectiveJobId();
    const port = requestedPort || firstPreviewPort();
    if (!jobId || !port) return;

    setOpeningPreview(true);
    try {
      const data = await createJobPreviewTicket(jobId, port);
      if (!data.open_url) {
        throw new Error('预览地址为空');
      }
      window.open(data.open_url, '_blank', 'noopener,noreferrer');
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      sessionStore.setStatus(`打开预览失败: ${msg}`);
    } finally {
      setOpeningPreview(false);
    }
  };

  const openPreviewEntry = async (entry: JobWorkspacePreviewEntry) => {
    const kind = String(entry.type || '').trim();
    if (kind === 'service' && entry.port) {
      await openPreview(Number(entry.port));
      return;
    }
    const openUrl = String(entry.open_url || '').trim();
    if (!openUrl) return;
    window.open(openUrl, '_blank', 'noopener,noreferrer');
  };

  const handleRetry = async () => {
    const jobId = effectiveJobId();
    if (!jobId) return;
    setActionError('');
    setActionLoading('retry');
    try {
      const data = await retryJob(jobId, { reuse_issue: true });
      sessionStore.setStatus(`已重新提交任务，新的 job_id: ${data.job_id}`);
      await refreshWorkspace(String(data.job_id || jobId));
      props.onActionComplete?.();
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setActionError(msg);
      sessionStore.setStatus(`重试失败: ${msg}`);
    } finally {
      setActionLoading('');
    }
  };

  const handleResubmit = async () => {
    const jobId = effectiveJobId();
    if (!jobId) return;
    setActionError('');
    setActionLoading('resubmit');
    try {
      const data = await retryJob(jobId, { reuse_issue: false });
      sessionStore.setStatus(`已按原参数新建任务，新的 job_id: ${data.job_id}`);
      await refreshWorkspace(String(data.job_id || jobId));
      props.onActionComplete?.();
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setActionError(msg);
      sessionStore.setStatus(`重新提交失败: ${msg}`);
    } finally {
      setActionLoading('');
    }
  };

  const handleResume = async () => {
    const jobId = effectiveJobId();
    if (!jobId) return;
    setActionError('');
    setActionLoading('resume');
    try {
      const data = await resumeJob(jobId, { reuse_issue: true });
      sessionStore.setStatus(`已提交恢复任务，新的 job_id: ${data.job_id}`);
      await refreshWorkspace(String(data.job_id || jobId));
      props.onActionComplete?.();
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setActionError(msg);
      sessionStore.setStatus(`恢复失败: ${msg}`);
    } finally {
      setActionLoading('');
    }
  };

  createEffect(() => {
    const workspace = props.workspace || null;
    // Do not let hydration of the previous snapshot mask a new tracker state.
    setHydratedWorkspace(null);
    if (!workspace) {
      setHydratedWorkspace(null);
      setTimeline([]);
      setLoadingWorkspace(false);
      return;
    }
    const jobId = resolveWorkspaceJobId(workspace);
    if (!jobId) {
      setHydratedWorkspace(null);
      setTimeline(Array.isArray(workspace.timeline) ? workspace.timeline : []);
      setLoadingWorkspace(false);
      return;
    }

    let cancelled = false;
    setLoadingWorkspace(true);
    void Promise.all([getJobWorkspace(jobId), getJobTimeline(jobId)])
      .then(([workspaceData, timelineData]) => {
        if (cancelled) return;
        setHydratedWorkspace({
          ...(workspaceData.workspace || {}),
          lifecycle_status: timelineData.lifecycle_status || workspaceData.workspace?.lifecycle_status,
          timeline: Array.isArray(timelineData.timeline) ? timelineData.timeline : workspaceData.workspace?.timeline,
        });
        setTimeline(Array.isArray(timelineData.timeline) ? timelineData.timeline : []);
        setActionError('');
      })
      .catch(() => {
        if (cancelled) return;
        setHydratedWorkspace(null);
        setTimeline([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingWorkspace(false);
      });

    onCleanup(() => {
      cancelled = true;
    });
  });

  createEffect(() => {
    // 统一 Job Runtime 只读取当前 Owner 可见的 Session 与 Workspace。
    const jobId = effectiveJobId();
    const currentSession = sessionStore.currentSession();
    const sessionId = String(
      effectiveWorkspace().session_id || currentSession?.session_id || '',
    ).trim();
    const workspaceId = String(
      effectiveWorkspace().workspace_id || currentSession?.workspace_id || '',
    ).trim();
    setRuntimeJob(null);
    setRuntimeEvents([]);
    setRuntimeJobError('');
    if (!jobId || !sessionId || !workspaceId) {
      return;
    }

    let cancelled = false;
    void getRuntimeJob(jobId, sessionId, workspaceId, {
      trackerIdentifier: effectiveWorkspace().issue_identifier,
    })
      .then(async (job) => {
        if (cancelled || !job) return;
        const eventPage = await getRuntimeJobEvents(jobId, sessionId, workspaceId);
        if (cancelled) return;
        setRuntimeJob(job);
        setRuntimeEvents(eventPage.events);
        setRuntimeJobError('');
      })
      .catch((error) => {
        if (cancelled) return;
        setRuntimeJob(null);
        setRuntimeEvents([]);
        setRuntimeJobError(error instanceof Error ? error.message : '统一任务运行时不可用');
      });

    onCleanup(() => {
      cancelled = true;
    });
  });

  /** 使用当前 Owner 和最新 Cursor 取消统一 Job，并立即更新面板投影。 */
  const handleRuntimeCancel = async () => {
    const job = runtimeJob();
    const currentSession = sessionStore.currentSession();
    const sessionId = String(
      effectiveWorkspace().session_id || currentSession?.session_id || '',
    ).trim();
    const workspaceId = String(
      effectiveWorkspace().workspace_id || currentSession?.workspace_id || '',
    ).trim();
    if (!job || !sessionId || !workspaceId) return;
    setRuntimeCancelLoading(true);
    try {
      const updated = await cancelRuntimeJob(
        effectiveJobId(),
        sessionId,
        workspaceId,
        job.cursor,
      );
      setRuntimeJob(updated);
      const eventPage = await getRuntimeJobEvents(
        effectiveJobId(),
        sessionId,
        workspaceId,
      );
      setRuntimeEvents(eventPage.events);
      setRuntimeJobError('');
      sessionStore.setStatus(`任务 ${effectiveJobId()} 已取消`);
    } catch (error) {
      const message = error instanceof Error ? error.message : '取消统一任务失败';
      setRuntimeJobError(message);
      sessionStore.setStatus(`取消任务失败: ${message}`);
    } finally {
      setRuntimeCancelLoading(false);
    }
  };

  createEffect(() => {
    props.onBrowserOpenChange?.(browserOpen());
  });

  onCleanup(() => {
    props.onBrowserOpenChange?.(false);
  });

  const panelButtonClass = 'rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-2 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50';
  const previewButtonClass = 'rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 px-3 py-2 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50';

  return (
    <Show when={hasWorkspace()}>
      <div class="overflow-hidden rounded-3xl border border-[#2a3136] bg-[#13191d] shadow-[0_14px_34px_rgba(0,0,0,0.16)]">
        <div class="flex items-center justify-between border-b border-[#2a3136] bg-[#10161a] px-4 py-4">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#7bd0ff]">
            {props.title || '任务工作区'}
            </div>
            <div class="mt-1 text-xs text-[#8b949e]">作业目录、时间线与恢复动作</div>
          </div>
          <Show when={loadingWorkspace()}>
            <div class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7]">同步中...</div>
          </Show>
        </div>

        <div class="space-y-3 p-4 text-[11px] text-[#c9d1d9]">
          <div>操作: {toolDisplayLabel(effectiveWorkspace().tool || effectiveWorkspace().task_type)}</div>
          <div>状态: {describeLifecycleStatus(effectiveWorkspace())}</div>
          <div class="flex flex-wrap gap-2 border-t border-[#2a3136] pt-3">
            <button
              class={panelButtonClass}
              onClick={() => openBrowser()}
              disabled={!effectiveWorkspace().workdir}
            >
              查看工作区
            </button>
            <button
              class={panelButtonClass}
              onClick={() => openBrowser(effectiveWorkspace().log_file)}
              disabled={!effectiveWorkspace().log_file}
            >
              查看日志
            </button>
            <button
              class={panelButtonClass}
              onClick={() => {
                const localRoot = localJobResultRoot(effectiveWorkspace());
                if (localRoot) {
                  openBrowser(localRoot, 'directory', 'workspace');
                  setBrowserInitialPath(localRoot);
                  return;
                }
                openBrowser(
                  firstArtifactEntry()?.path || artifactSource().root || effectiveWorkspace().workdir,
                  firstArtifactEntry()?.type,
                  effectiveJobId() && artifactSource().browse_api === 'job_artifacts' ? 'artifacts' : 'workspace'
                );
              }}
              disabled={!firstArtifact() && !effectiveWorkspace().workdir}
            >
              查看结果文件
            </button>
            <Show when={firstPreviewPort() > 0 && effectiveJobId()}>
              <button
                class={previewButtonClass}
                onClick={() => void openPreview()}
                disabled={openingPreview()}
              >
                {openingPreview() ? '打开中...' : '打开预览'}
              </button>
            </Show>
            <Show when={effectiveJobId()}>
              <JobActionControls
                loading={actionLoading()}
                retryAvailable={retryAvailable()}
                retryReason={retryReason()}
                resumeAvailable={resumeAvailable()}
                resumeReason={resumeReason()}
                onRetry={() => void handleRetry()}
                onResubmit={() => void handleResubmit()}
                onResume={() => void handleResume()}
              />
            </Show>
            <Show when={recoveryAllowed()}><button type="button" class={panelButtonClass} onClick={() => setRecoveryOpen(!recoveryOpen())} aria-expanded={recoveryOpen()}>取回已有产物</button></Show>
          </div>

          <Show when={recoveryOpen()}>
            <div class="space-y-2 rounded-xl border border-current/20 p-3" aria-busy={recovering()}>
              <p class="text-sm">仅下载并校验已生成的文件，不重新运行计算；保留原任务的超时或失败记录。产物包必须带有当前任务与运行的归属信息。</p>
              <label class="block text-sm">产物包引用
                <input class="input input-bordered mt-1 w-full" value={bundleReference()} disabled={recovering()} onInput={e => setBundleReference(e.currentTarget.value)} placeholder="粘贴已有产物包引用" />
              </label>
              <button type="button" class={panelButtonClass} disabled={recovering() || !/^bundle_[A-Za-z0-9_-]{1,100}$/.test(bundleReference().trim())} onClick={() => void recoverArtifacts()}>{recovering() ? '正在下载并校验…' : '取回文件，不重新运行'}</button>
              <Show when={recoveryError()}><p role="alert" class="text-sm">{recoveryError()}</p></Show>
              <Show when={recoveryMessage()}><p role="status" class="text-sm">{recoveryMessage()}</p></Show>
            </div>
          </Show>
          <Show when={effectiveWorkspace().artifact_recovery}><p class="text-sm">已取回 {effectiveWorkspace().artifact_recovery?.total_files} 个已有文件；原任务状态保持不变。</p></Show>

          <div>提交时间: {formatTimestamp(effectiveWorkspace().submitted_at)}</div>
          <div>开始时间: {formatTimestamp(effectiveWorkspace().started_at)}</div>
          <div>完成时间: {formatTimestamp(effectiveWorkspace().completed_at)}</div>
          <div>取消时间: {formatTimestamp(effectiveWorkspace().cancelled_at)}</div>
          <Show when={effectiveWorkspace().retry_of || effectiveWorkspace().resume_of}>
            <div>
              任务谱系:
              {' '}
              {formatPath(
                effectiveWorkspace().resume_of
                  ? `从 ${effectiveWorkspace().resume_of} 恢复`
                  : `从 ${effectiveWorkspace().retry_of} 重试`
              )}
            </div>
          </Show>
          <Show when={effectiveWorkspace().review_lineage && Object.keys(effectiveWorkspace().review_lineage || {}).length > 0}>
            <div>
              审核轨迹:
              {' '}
              {formatPath(
                `${String(effectiveWorkspace().review_lineage?.identifier || '').trim()} ${String(effectiveWorkspace().review_lineage?.status || '').trim()}`
              )}
            </div>
          </Show>
          <Show when={runtimeJob()}>
            {(jobAccessor) => (
              <div class="space-y-2 border-t border-[#2a3136] pt-2">
                <div class="text-[10px] text-[#8b949e]">统一任务运行时</div>
                <div>状态: {jobAccessor().status} · {jobAccessor().phase || '-'}</div>
                <details><summary class="cursor-pointer">高级：运行时技术信息</summary>
                  <div>Provider: {jobAccessor().provider}</div>
                  <div>Cursor: {jobAccessor().cursor}</div>
                  <div>Workspace Owner: {jobAccessor().owner.workspace_id}</div>
                </details>
                <Show when={jobAccessor().cancellation_supported && !['completed', 'failed', 'cancelled'].includes(jobAccessor().status)}>
                  <button
                    class={panelButtonClass}
                    onClick={() => void handleRuntimeCancel()}
                    disabled={runtimeCancelLoading()}
                  >
                    {runtimeCancelLoading() ? '取消中...' : '取消任务'}
                  </button>
                </Show>
              </div>
            )}
          </Show>
          <Show when={runtimeJobError()}>
            <div class="rounded-2xl border border-[#5a1e23] bg-[#201317] p-3 text-[10px] text-[#ffb6b0] whitespace-pre-wrap break-words">
              统一任务运行时: {runtimeJobError()}
            </div>
          </Show>
          <Show when={previewEntries().length > 0}>
            <div class="space-y-2 border-t border-[#2a3136] pt-2">
              <div class="text-[10px] text-[#8b949e]">可用预览</div>
              <For each={previewEntries()}>
                {(entry) => (
                  <div class="flex items-center justify-between gap-2">
                    <div class="min-w-0 truncate">
                      {formatPath(`${entry.label || entry.type || '预览'}${entry.path ? ` · ${entry.path}` : entry.port ? ` · 端口 ${entry.port}` : ''}`)}
                    </div>
                    <button
                      class={previewButtonClass}
                      onClick={() => void openPreviewEntry(entry)}
                    >
                      打开
                    </button>
                  </div>
                )}
              </For>
            </div>
          </Show>
          <Show when={timelineEntries().length > 0}>
            <details class="space-y-2 border-t border-[#2a3136] pt-2">
              <summary class="cursor-pointer">任务时间线（{timelineEntries().length}）</summary>
              <div class="space-y-1">
                <For each={timelineEntries()}>
                  {(entry) => (
                    <div class="rounded-2xl border border-[#2a3136] bg-[#171c20] px-3 py-2">
                      <div class="text-[10px] text-[#e6edf3]">{entry.title || entry.kind || '事件'}</div>
                      <div class="text-[10px] text-[#8b949e]">
                        {formatTimestamp(entry.at)} · {entry.source || '-'}
                      </div>
                      <Show when={String(entry.details || '').trim()}>
                        <div class="text-[10px] text-[#c9d1d9] whitespace-pre-wrap break-words">{entry.details}</div>
                      </Show>
                    </div>
                  )}
                </For>
              </div>
            </details>
          </Show>
          <Show when={effectiveWorkspace().cancel_actions && (effectiveWorkspace().cancel_actions || []).length > 0}>
            <div class="space-y-1 border-t border-[#2a3136] pt-2">
              <div class="text-[10px] text-[#8b949e]">停止结果</div>
              <For each={effectiveWorkspace().cancel_actions || []}>
                {(action) => <div>{describeCancelAction(action)}</div>}
              </For>
            </div>
          </Show>
          <Show when={Object.keys(actionPolicy()).length > 0}>
            <details class="space-y-1 border-t border-[#2a3136] pt-2">
              <summary class="cursor-pointer">高级：动作策略与不可用原因</summary>
              <For each={['retry', 'resume', 'cancel', 'continue']}>
                {(name) => {
                  const action = actionPolicy()[name];
                  if (!action || typeof action !== 'object') return null;
                  return (
                    <div>
                      {name}: {action.available ? '可用' : '不可用'}
                      <Show when={String(action.reason || '').trim()}>
                        {' · '}{String(action.reason || '').trim()}
                      </Show>
                    </div>
                  );
                }}
              </For>
            </details>
          </Show>

          <Show when={effectiveJobId() && retryReason()}>
            <div class="text-[10px] text-[#d29922]">
              当前不可重试: {retryReason()}
            </div>
          </Show>
          <Show when={effectiveJobId() && !retryReason() && resumeReason()}>
            <div class="text-[10px] text-[#8b949e]">
              恢复不可用: {resumeReason()}
            </div>
          </Show>
          <Show when={!effectiveJobId()}>
            <div class="text-[10px] text-[#8b949e]">
              当前任务还没有真正生成 `job_id`，通常说明仍处于 Todo 或人工审核阶段，所以暂时不能重试。
            </div>
          </Show>
          <Show when={actionError()}>
            <div class="rounded-2xl border border-[#5a1e23] bg-[#201317] p-3 text-[10px] text-[#ffb6b0] whitespace-pre-wrap break-words">
              最近一次操作失败: {actionError()}
            </div>
          </Show>

          <Show when={browserOpen()}>
            <div class="fixed inset-0 z-[110] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onClick={closeBrowser}>
              <div
                class="h-[82vh] w-full max-w-6xl overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#11171b] shadow-[0_28px_80px_rgba(0,0,0,0.46)]"
                onClick={(e) => e.stopPropagation()}
              >
                <div class="flex items-center justify-between border-b border-[#2a3136] bg-[#10161a] px-5 py-4">
                  <div class="truncate text-sm font-semibold text-[#e6edf3]">
                    {formatPath(browserDisplayPath() || browserRoot() || effectiveWorkspace().workdir)}
                  </div>
                  <button class={panelButtonClass} onClick={closeBrowser}>关闭</button>
                </div>
                <div class="h-[calc(82vh-56px)]">
                  <FileBrowser
                    workspaceId={browserMode() === 'workspace' ? effectiveWorkspace().workspace_id : undefined}
                    workdir={browserMode() === 'workspace' ? (browserRoot() || effectiveWorkspace().workdir) : undefined}
                    jobId={browserMode() === 'artifacts' ? effectiveJobId() : undefined}
                    artifactRoot={browserMode() === 'artifacts' ? (browserArtifactRoot() || artifactSource().root) : undefined}
                    initialPath={browserInitialPath() || undefined}
                    previewRequest={browserPreviewRequest()}
                  />
                </div>
              </div>
            </div>
          </Show>
        </div>
      </div>
    </Show>
  );
}
