import { toolDisplayLabel } from '../utils/userFacingLabels';
import FeedbackDialog, { confirmAction, showFeedback } from './FeedbackDialog';
import { For, Show, createSignal, createEffect, createMemo, onMount, onCleanup } from 'solid-js';
import { Portal } from 'solid-js/web';
import {
  sanitizeMessageVerification,
  sanitizeToolCall,
  sessionStore,
  type ToolCall,
} from '../stores/session';
import {
  getMCPServers,
  getSessions,
  getSession,
  getSessionUsageSummary,
  markSessionAsyncCompletionsRead,
  deleteSession as deleteSessionApi,
  archiveSession as archiveSessionApi,
  renameSession as renameSessionApi,
  bindSessionToResearch,
  unbindSessionFromResearch,
  bootstrapSession,
  getWorkdirs,
  createWorkdir,
  deleteWorkdir,
  getDeletedWorkdirs,
  getWorkdirPurgeStatus,
  purgeWorkdir,
  restoreWorkdir,
  type DeletedWorkdirItem,
  type MCPServerStatus,
  type WorkdirItem,
} from '../api/client';
import { listResearchProjects } from '../api/research';
import { isFixedTeamBootstrapAgent } from '../utils/teamRegistry';
import { restoredSchedulerMode } from '../utils/schedulerCapabilities';
import { mergeWithLocalSyntheticMessages, shouldApplyHydratedSession, shouldApplySessionSelection } from '../utils/messageSnapshots';
import {
  taskRuntimeSessionOutcomeBadge,
} from '../utils/sessionRuntimeNotices';
import { preferenceKeys, readPreference } from '../utils/userPreferences';
import UsageDetailModal from './UsageDetailModal';
import { reorderSessions, sessionDragSort } from '../utils/sessionReorder';
import { parseWorkspaceList, sessionWorkspaceKey, workspaceAccentHue, type WorkspaceListPreferences } from '../utils/sessionWorkspaces';
import { watchWorkspaceMenus, watchSessionMenu } from '../utils/workspaceMenu';

interface SessionListProps {
  embedded?: boolean;
  onSelect?: () => void;
  width: number;
  onWidthChange?: (width: number) => void;
  onHide?: () => void;
  openCreateRequest?: number;
  onOpenHome?: () => void;
  onOpenHistory?: () => void;
}

export default function SessionList(props: SessionListProps) {
  let disposed = false;
  let selectionRequestRevision = 0;
  let loadingSessionId = '';
  const inFlight = new Set<string>();
  const [mcpServers, setMcpServers] = createSignal<MCPServerStatus[]>([]);
  const [showWorkdirModal, setShowWorkdirModal] = createSignal(false);
  const [workdirs, setWorkdirs] = createSignal<WorkdirItem[]>([]);
  const [deletedWorkdirs, setDeletedWorkdirs] = createSignal<DeletedWorkdirItem[]>([]);
  const [maxWorkspacesPerUser, setMaxWorkspacesPerUser] = createSignal(1);
  const [remainingWorkspaceSlots, setRemainingWorkspaceSlots] = createSignal(0);
  const [selectedWorkdirName, setSelectedWorkdirName] = createSignal('');
  const [creatingSession, setCreatingSession] = createSignal(false);
  const [newWorkdirName, setNewWorkdirName] = createSignal('');
  const [workdirError, setWorkdirError] = createSignal('');
  const [deletingWorkdirName, setDeletingWorkdirName] = createSignal('');
  const [purgingWorkdirName, setPurgingWorkdirName] = createSignal('');
  const [restoringWorkdirName, setRestoringWorkdirName] = createSignal('');
  const [purgeConfirmation, setPurgeConfirmation] = createSignal<{ name: string; residual: boolean; typed: string } | null>(null);
  const [showUsageDetail, setShowUsageDetail] = createSignal(false);
  const [deletingSessionIds, setDeletingSessionIds] = createSignal<Record<string, boolean>>({});
  const [contextMenu, setContextMenu] = createSignal<{sessionId:string;title:string;x:number;y:number;researchId:string;researchTitle:string;researchUnavailableReason:string;isResearch:boolean} | null>(null);
  const [renamingSession, setRenamingSession] = createSignal<{sessionId:string;title:string} | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = createSignal<{sessionId:string;title:string} | null>(null);
  const [sessionSearch, setSessionSearch] = createSignal('');
  const [searchOpen, setSearchOpen] = createSignal(false);
  let searchInput!: HTMLInputElement;
  let searchButton!: HTMLButtonElement;
  const closeSessionSearch = () => { setSessionSearch(''); setSearchOpen(false); searchButton.focus(); };
  const [sessionFilter, setSessionFilter] = createSignal<'all' | 'active' | 'attention' | 'completed'>('all');
  const [pinnedSessions, setPinnedSessions] = createSignal<string[]>(
    (() => { try { const value = JSON.parse(localStorage.getItem('tcode.pinned-sessions') || '[]'); return Array.isArray(value) ? value.map(String) : []; } catch { return []; } })(),
  );
  let recentSessionsRef: HTMLDivElement | undefined;
  let sessionScroll!: HTMLDivElement;
  const orderKey = `tcode.session-order:${readPreference('tcode_agent_identity_scope', 'local')}`;
  const [manualOrder, setManualOrder] = createSignal<string[]>((() => {
    try { const value: unknown = JSON.parse(localStorage.getItem(orderKey) || '[]'); return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : []; } catch { return []; }
  })());
  const [dragSnapshot, setDragSnapshot] = createSignal<ReturnType<typeof sessionStore.sessions>>();
  const [sortAnnouncement, setSortAnnouncement] = createSignal('');
  const desktop=()=>document.documentElement.dataset.desktop==='true';
  const workspacePreferenceKey=`${orderKey}:workspaces`;
  const [workspacePreferences,setWorkspacePreferences]=createSignal(parseWorkspaceList(readPreference(workspacePreferenceKey,'')));
  const grouped=()=>desktop()&&workspacePreferences().grouped;
  const [renamingWorkspace,setRenamingWorkspace]=createSignal<{id:string;name:string}|null>(null);
  function saveWorkspaces(change:Partial<WorkspaceListPreferences>){
    const next={...workspacePreferences(),...change};setWorkspacePreferences(next);
    try{localStorage.setItem(workspacePreferenceKey,JSON.stringify(next));}
    catch{setSortAnnouncement('工作区显示已更新，但本地存储不可用，重启后不会保留');}
  }
  onMount(() => {
    onCleanup(watchWorkspaceMenus(sessionScroll));
    const dispose = sessionDragSort(sessionScroll, {
      start: () => {setDragSnapshot(visibleSessions());setContextMenu(null);sessionScroll.querySelectorAll<HTMLDetailsElement>('.session-workspace-menu[open]').forEach(menu=>menu.open=false);},
      drop: (id, target, kind) => kind==='workspace'?moveWorkspace(id,target):persistSessionOrder(id, target),
      end: () => setDragSnapshot(undefined),
    });
    onCleanup(dispose);
    const dismiss=(event:PointerEvent)=>sessionScroll.querySelectorAll<HTMLDetailsElement>('.session-workspace-menu[open]').forEach(menu=>{if(!menu.contains(event.target as Node))menu.open=false;});
    document.addEventListener('pointerdown',dismiss);
    onCleanup(()=>document.removeEventListener('pointerdown',dismiss));
  });

  const normalizeTimestampMs = (value: unknown, fallback = Date.now()) => {
    const raw = Number(value);
    if (!Number.isFinite(raw) || raw <= 0) return fallback;
    // 10~11 位通常是秒级时间戳，统一转成毫秒。
    if (raw < 1_000_000_000_000) return raw * 1000;
    return raw;
  };

  const getSessionDisplayTimestamp = (session: { updated_at?: number; created_at?: number }) => {
    const updatedAt = normalizeTimestampMs(session.updated_at, 0);
    if (updatedAt > 0) return updatedAt;
    return normalizeTimestampMs(session.created_at, Date.now());
  };


  const loadMCPStatus = async () => {
    const servers = await getMCPServers({ refreshFailed: true });
    if (disposed) return;
    setMcpServers(servers);
  };

  const loadUsageSummary = async () => {
    const summary = await getSessionUsageSummary({ includeArchived: true });
    if (disposed) return;
    if (summary?.total_usage) {
      sessionStore.replaceUsageStats(summary.total_usage);
    }
  };

  const loadSessions = async () => {
    try {
      const sessions = await getSessions();
      if (disposed) return;
      const frontendSessions = sessions.map(s => ({
        title: s.title,
        status: s.status,
        is_archived: Boolean(s.is_archived),
        archived_at: s.archived_at ? normalizeTimestampMs(s.archived_at) : undefined,
        session_id: s.session_id,
        messages: [],
        created_at: normalizeTimestampMs(s.created_at),
        updated_at: normalizeTimestampMs(s.updated_at),
        agent_name: s.agent_name,
        workspace_id: String(s.workspace_id || ''),
        workdir: s.workdir,
        workdir_name: s.workdir_name,
        task_runtime: s.task_runtime,
        async_completion_unread_count: Number(s.async_completion_unread_count || 0),
        mode: s.mode === 'research' ? 'research' as const : 'general' as const,
        research_id: String(s.research_id || ''),
        mode_source: String(s.mode_source || 'default'),
        archive_cause: String(s.archive_cause || ''),
        archive_locked: Boolean(s.archive_locked),
        scheduler_mode: sessionStore.sessions().find(item => item.session_id === s.session_id)?.scheduler_mode || 'supervisor' as const,
      }));
      sessionStore.mergeSessions(frontendSessions, 'active');

      if (sessions.length > 0) {
        const backendIds = new Set(frontendSessions.map(s => s.session_id));
        // Always hydrate the active session from backend after refresh.
        const currentId = sessionStore.currentSessionId();
        const targetSessionId =
          currentId && backendIds.has(currentId) ? currentId : sessions[0].session_id;

        if (targetSessionId) {
          const sessionData = await getSession(targetSessionId);
          if (disposed) return;
          if (sessionData) {
            if (!shouldApplyHydratedSession(sessionStore.currentSessionId(), targetSessionId)) return;
            const activeSessionId = sessionStore.currentSessionId();
            const localMessages = shouldApplyHydratedSession(activeSessionId, targetSessionId)
              ? sessionStore.messages()
              : sessionStore.sessions().find(
                  (item) => item.session_id === targetSessionId,
                )?.messages || [];
            const messages = mergeWithLocalSyntheticMessages(localMessages, sessionData.messages.map((m: any) => ({
              id: m.id,
              role: m.role,
              content: m.content,
              presentation: m.presentation,
              content_blocks: m.content_blocks,
              research_grounding: m.research_grounding,
              verification: sanitizeMessageVerification(m.verification),
              metadata: m.metadata,
              tool_calls: Array.isArray(m.tool_calls)
                ? m.tool_calls
                    .map((tc: any) => sanitizeToolCall(tc))
                    .filter((tc: ToolCall | null): tc is ToolCall => tc !== null)
                : undefined,
              usage: m.usage ? {
                prompt_tokens: m.usage.prompt_tokens,
                completion_tokens: m.usage.completion_tokens,
                total_tokens: m.usage.total_tokens,
              } : undefined,
            })));
            sessionStore.upsertSession({
              session_id: targetSessionId,
              title: sessionData.title,
              status: sessionData.status,
              is_archived: Boolean(sessionData.is_archived),
              archived_at: sessionData.archived_at
                ? normalizeTimestampMs(sessionData.archived_at)
                : undefined,
              messages,
              created_at: normalizeTimestampMs(sessionData.created_at),
              updated_at: normalizeTimestampMs(sessionData.updated_at),
              agent_name: sessionData.agent_name || '',
              workspace_id: String(sessionData.workspace_id || ''),
              workdir: String(sessionData.workdir || ''),
              workdir_name: String(sessionData.workdir_name || ''),
              mode: sessionData.mode === 'research' ? 'research' : 'general',
              research_id: String(sessionData.research_id || ''),
              mode_source: String(sessionData.mode_source || 'default'),
              archive_cause: String(sessionData.archive_cause || ''),
              archive_locked: Boolean(sessionData.archive_locked),
              scheduler_mode: restoredSchedulerMode(sessionData.metadata),
              metadata: sessionData.metadata || {},
            });
            if (shouldApplyHydratedSession(sessionStore.currentSessionId(), targetSessionId)) {
              sessionStore.switchSession(targetSessionId);
            }
          }
        }
      }
    } catch (e) {
      console.error('Failed to load sessions:', e);
    }
  };

  const refreshTaskRuntimeSummaries = async () => {
    try {
      const summaries = await getSessions();
      if (disposed) return;
      if (summaries.length === 0) return;
      const runtimeBySession = new Map(
        summaries.map(item => [item.session_id, {
          taskRuntime: item.task_runtime,
          unreadCount: Number(item.async_completion_unread_count || 0),
          updatedAt: normalizeTimestampMs(item.updated_at),
        }]),
      );
      sessionStore.setSessions(prev => prev.map(session => {
        const update = runtimeBySession.get(session.session_id);
        return update ? {
          ...session,
          task_runtime: update.taskRuntime,
          async_completion_unread_count: update.unreadCount,
          updated_at: Math.max(session.updated_at || 0, update.updatedAt || 0),
        } : session;
      }));
    } catch (e) {
      console.error('Failed to refresh session runtime summaries:', e);
    }
  };

  onMount(() => {
    const poll = async (key: string, action: () => Promise<void>) => {
      if (disposed || document.hidden || inFlight.has(key)) return;
      inFlight.add(key);
      try { await action(); }
      catch (error) { console.error('Sidebar refresh failed:', error); }
      finally { inFlight.delete(key); }
    };
    void poll('mcp', loadMCPStatus);
    const mcpInterval = setInterval(() => void poll('mcp', loadMCPStatus), 30000);
    const runtimeInterval = setInterval(() => void poll('runtime', refreshTaskRuntimeSummaries), 10000);
    const usageInterval = setInterval(() => void poll('usage', loadUsageSummary), 10000);
    const closeContextMenu = (event?: Event) => {
      if (event?.target instanceof Element && event.target.closest('.session-context-menu')) return;
      setContextMenu(null);
    };
    const closeContextMenuOnKey = (event: KeyboardEvent) => { if (event.key === 'Escape') closeContextMenu(); };
    document.addEventListener('pointerdown', closeContextMenu);
    document.addEventListener('keydown', closeContextMenuOnKey);
    window.addEventListener('blur', closeContextMenu);
    void loadSessions();
    void poll('usage', loadUsageSummary);
    onCleanup(() => {
      disposed = true;
      clearInterval(mcpInterval);
      clearInterval(runtimeInterval);
      clearInterval(usageInterval);
      document.removeEventListener('pointerdown', closeContextMenu);
      document.removeEventListener('keydown', closeContextMenuOnKey);
      window.removeEventListener('blur', closeContextMenu);
    });
  });

  createEffect(() => {
    const request = props.openCreateRequest;
    if (!request) return;
    void openWorkdirModal();
  });

  const formatTime = (timestamp: number) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return '刚刚';
    if (diffMins < 60) return `${diffMins} 分钟前`;
    if (diffHours < 24) return `${diffHours} 小时前`;
    if (diffDays < 7) return `${diffDays} 天前`;
    return date.toLocaleDateString();
  };

  const workspaceMutationActive = () => Boolean(
    deletingWorkdirName() || purgingWorkdirName() || restoringWorkdirName(),
  );

  const refreshWorkdirs = async () => {
    const [payload, deleted] = await Promise.all([
      getWorkdirs(),
      getDeletedWorkdirs(),
    ]);
    setWorkdirs(payload.workdirs || []);
    setDeletedWorkdirs(deleted);
    setMaxWorkspacesPerUser(payload.max_workspaces_per_user);
    setRemainingWorkspaceSlots(payload.remaining_workspace_slots);
    return payload.workdirs || [];
  };

  const openWorkdirModal = async () => {
    setWorkdirError('');
    const current = await refreshWorkdirs();
    if (current.length > 0) {
      setSelectedWorkdirName(current[0].name);
    }
    setShowWorkdirModal(true);
  };

  const handleNewSession = () => {
    void openWorkdirModal();
  };

  const handleCreateWorkdir = async () => {
    const name = newWorkdirName().trim();
    if (!name) return;
    setWorkdirError('');
    const deletedMatch = deletedWorkdirs().find((item) => item.workspace_id === name);
    if (deletedMatch) {
      if (deletedMatch.status === 'purged') {
        const confirmed = await confirmAction(
          `Workspace“${name}”已经永久删除，但检测到系统残留目录。\n\n继续后会清理残留并创建一个全新的 Workspace；旧文件和历史不会恢复。是否继续？`,
        );
        if (!confirmed) return;
      } else {
        if (deletedMatch.status !== 'deleted') {
          setWorkdirError(
            deletedMatch.status === 'deleting'
              ? `同名 Workspace“${name}”上次删除未完成，请先在回收站中恢复或重试删除。`
              : deletedMatch.status === 'purge_failed'
              ? `同名 Workspace“${name}”永久删除失败，请先在回收站中重试清理。`
              : `同名 Workspace“${name}”正在永久删除，请等待清理完成后再创建。`,
          );
          return;
        }
        if (!deletedMatch.files_present) {
          setWorkdirError(`同名 Workspace“${name}”仍在回收站记录中，但文件已不存在，请先永久删除该记录。`);
          return;
        }
        const confirmed = await confirmAction(
          `同名 Workspace“${name}”位于回收站。\n\n是否恢复原 Workspace（包括保留的文件和历史会话）？`,
        );
        if (!confirmed) return;
        setRestoringWorkdirName(name);
        try {
          await restoreWorkdir(name);
          const current = await refreshWorkdirs();
          setSelectedWorkdirName(name || current[0]?.name || '');
          setNewWorkdirName('');
        } catch (e) {
          setWorkdirError(e instanceof Error ? e.message : '恢复 Workspace 失败');
        } finally {
          setRestoringWorkdirName('');
        }
        return;
      }
    }
    try {
      const created = await createWorkdir(name);
      await refreshWorkdirs();
      if (created?.name) {
        setSelectedWorkdirName(created.name);
      }
      setNewWorkdirName('');
      if (created?.warning) {
        setWorkdirError(created.warning);
      }
    } catch (e) {
      setWorkdirError(e instanceof Error ? e.message : '创建工作目录失败');
    }
  };

  const handleDeleteWorkdir = async (name: string) => {
    const confirmed = await confirmAction(
      `确定删除工作目录“${name}”吗？\n\n运行中的任务会停止，相关沙盒容器会被删除；宿主机上的文件和历史会话会保留。`,
    );
    if (!confirmed) return;
    setDeletingWorkdirName(name);
    setWorkdirError('');
    try {
      await deleteWorkdir(name);
      const remaining = await refreshWorkdirs();
      await loadSessions();
      if (selectedWorkdirName() === name) {
        setSelectedWorkdirName(remaining[0]?.name || '');
      }
    } catch (e) {
      setWorkdirError(e instanceof Error ? e.message : '删除工作目录失败');
    } finally {
      setDeletingWorkdirName('');
    }
  };

  const handleRestoreWorkdir = async (name: string) => {
    setRestoringWorkdirName(name);
    setWorkdirError('');
    try {
      await restoreWorkdir(name);
      const current = await refreshWorkdirs();
      setSelectedWorkdirName(name || current[0]?.name || '');
    } catch (e) {
      setWorkdirError(e instanceof Error ? e.message : '恢复 Workspace 失败');
    } finally {
      setRestoringWorkdirName('');
    }
  };

  const handlePurgeWorkdir = (name: string) => {
    const isPurgedResidual = deletedWorkdirs().some(
      (item) => item.workspace_id === name && item.status === 'purged',
    );
    setWorkdirError('');
    setPurgeConfirmation({ name, residual: isPurgedResidual, typed: '' });
  };

  const handleConfirmPurgeWorkdir = async () => {
    const confirmation = purgeConfirmation();
    if (!confirmation) return;
    if (confirmation.typed !== confirmation.name) {
      setWorkdirError('输入的 Workspace 名称不匹配，未执行永久删除。');
      return;
    }
    const name = confirmation.name;
    setPurgeConfirmation(null);
    setPurgingWorkdirName(name);
    setWorkdirError('');
    try {
      await purgeWorkdir(name);
      for (let attempt = 0; attempt < 600; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const status = await getWorkdirPurgeStatus(name);
        await refreshWorkdirs();
        if (status.status === 'purged') return;
        if (status.status === 'purge_failed') {
          throw new Error(status.error || '永久删除失败，可以在回收站中重试。');
        }
        if (status.status === 'purging' && !status.running) {
          await purgeWorkdir(name);
        }
      }
      throw new Error('永久删除仍在后台运行，请稍后重新打开回收站查看。');
    } catch (e) {
      setWorkdirError(e instanceof Error ? e.message : '永久删除 Workspace 失败');
      await refreshWorkdirs().catch(() => undefined);
    } finally {
      setPurgingWorkdirName('');
    }
  };

  const handleConfirmCreateSession = async () => {
    const selected = selectedWorkdirName().trim();
    if (!selected) {
      setWorkdirError('请选择工作目录');
      return;
    }
    setCreatingSession(true);
    setWorkdirError('');
    try {
      const agentName = readPreference(preferenceKeys.agent, 'build');
      const resp = await bootstrapSession(agentName, undefined, selected);
      await loadSessions();
      if (resp?.session_id) {
        await handleSelectSession(resp.session_id);
      }
      setShowWorkdirModal(false);
    } catch (e) {
      setWorkdirError(e instanceof Error ? e.message : '创建会话失败');
    } finally {
      setCreatingSession(false);
    }
  };

  const handleSelectSession = async (sessionId: string) => {
    if (
      sessionStore.currentSessionId() === sessionId
      && (loadingSessionId === sessionId || sessionStore.messages().length > 0)
    ) {
      props.onOpenHome?.();
      if (window.innerWidth < 1024) props.onSelect?.();
      return;
    }
    const requestRevision = ++selectionRequestRevision;
    loadingSessionId = sessionId;
    // Switch session in store first
    sessionStore.switchSession(sessionId);
    props.onOpenHome?.();
    
    // Load messages from backend
    try {
      const sessionData = await getSession(sessionId);
      if (sessionData && sessionData.messages) {
        if (!shouldApplySessionSelection(
          sessionStore.currentSessionId(),
          sessionId,
          requestRevision,
          selectionRequestRevision,
        )) return;
        const activeSessionId = sessionStore.currentSessionId();
        const localMessages = shouldApplyHydratedSession(activeSessionId, sessionId)
          ? sessionStore.messages()
          : sessionStore.sessions().find(
              (item) => item.session_id === sessionId,
            )?.messages || [];
        const messages = mergeWithLocalSyntheticMessages(localMessages, sessionData.messages.map((m: any) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          presentation: m.presentation,
          content_blocks: m.content_blocks,
          research_grounding: m.research_grounding,
          verification: sanitizeMessageVerification(m.verification),
          metadata: m.metadata,
          tool_calls: Array.isArray(m.tool_calls)
            ? m.tool_calls
                .map((tc: any) => sanitizeToolCall(tc))
                .filter((tc: ToolCall | null): tc is ToolCall => tc !== null)
            : undefined,
          usage: m.usage ? {
            prompt_tokens: m.usage.prompt_tokens,
            completion_tokens: m.usage.completion_tokens,
            total_tokens: m.usage.total_tokens,
          } : undefined,
        })));
        sessionStore.upsertSession({
          session_id: sessionId,
          title: sessionData.title,
          status: sessionData.status,
          is_archived: Boolean(sessionData.is_archived),
          archived_at: sessionData.archived_at
            ? normalizeTimestampMs(sessionData.archived_at)
            : undefined,
          messages,
          created_at: normalizeTimestampMs(sessionData.created_at),
          updated_at: normalizeTimestampMs(sessionData.updated_at),
          agent_name: sessionData.agent_name || '',
          workspace_id: String(sessionData.workspace_id || ''),
          workdir: String(sessionData.workdir || ''),
          workdir_name: String(sessionData.workdir_name || ''),
          mode: sessionData.mode === 'research' ? 'research' : 'general',
          research_id: String(sessionData.research_id || ''),
          mode_source: String(sessionData.mode_source || 'default'),
          archive_cause: String(sessionData.archive_cause || ''),
          archive_locked: Boolean(sessionData.archive_locked),
          scheduler_mode: restoredSchedulerMode(sessionData.metadata),
          metadata: sessionData.metadata || {},
        });
        if (shouldApplyHydratedSession(sessionStore.currentSessionId(), sessionId)) {
          sessionStore.switchSession(sessionId);
        }
        if (Number(sessionData.metadata?.async_completion_events?.length || 0) > 0) {
          void markSessionAsyncCompletionsRead(sessionId).catch((error) => {
            console.error('Failed to mark async completions read:', error);
          });
          sessionStore.setSessions((prev) => prev.map((item) => (
            item.session_id === sessionId
              ? { ...item, async_completion_unread_count: 0 }
              : item
          )));
        }
        const sessionAgentName = sessionData.agent_name || '';
        if (sessionAgentName && isFixedTeamBootstrapAgent(sessionAgentName)) {
          try {
            const resp = await bootstrapSession(sessionAgentName, sessionId);
            sessionStore.updateSessionAgent(sessionId, resp.agent_name || sessionAgentName);
            sessionStore.refreshSubagents();
          } catch (e) {
            console.error('Failed to bootstrap fixed team on session switch:', e);
            sessionStore.setStatus('固定团队初始化失败');
          }
        }
      }
    } catch (e) {
      console.error('Failed to load session messages:', e);
    } finally {
      if (requestRevision === selectionRequestRevision) loadingSessionId = '';
    }
    
    // Only call onSelect on mobile (screen width < 1024px)
    if (window.innerWidth < 1024) {
      props.onSelect?.();
    }
  };

  const handleDeleteSession = async (sessionId: string) => {
    if (deletingSessionIds()[sessionId]) return;
    setDeletingSessionIds(prev => ({ ...prev, [sessionId]: true }));
    sessionStore.setStatus('正在删除会话...');
    sessionStore.deleteSession(sessionId);
    try {
      await deleteSessionApi(sessionId);
      sessionStore.setStatus('');
    } catch (err) {
      console.error('Failed to delete session on backend:', err);
      sessionStore.setStatus(err instanceof Error ? err.message : '删除会话失败');
      await loadSessions();
    } finally {
      setDeletingSessionIds(prev => {
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
    }
  };

  const handleArchiveSession = async (e: Event, sessionId: string) => {
    e.stopPropagation();
    try {
      const archived = await archiveSessionApi(sessionId);
      sessionStore.markSessionArchived(
        sessionId,
        archived.archived_at ? archived.archived_at * 1000 : Date.now(),
      );
    } catch (err) {
      console.error('Failed to archive session on backend:', err);
    }
  };

  const handleRenameSession = async (sessionId: string, nextTitle: string) => {
    if (renamingSession()?.sessionId !== sessionId) return;
    const trimmed = nextTitle.trim();
    setRenamingSession(null);
    if (!trimmed) return;

    try {
      const renamed = await renameSessionApi(sessionId, trimmed);
      sessionStore.setSessions((prev) =>
        prev.map((session) =>
          session.session_id === sessionId
            ? {
                ...session,
                title: renamed.title,
                updated_at: normalizeTimestampMs(renamed.updated_at),
              }
            : session,
        ),
      );
      if (sessionStore.currentSessionId() === sessionId) {
        sessionStore.setMessages((prev) => [...prev]);
      }
    } catch (err) {
      console.error('Failed to rename session:', err);
      await showFeedback(err instanceof Error ? err.message : '重命名会话失败');
    }
  };

  const directServerCount = () => mcpServers().filter((server) => server.user_callable).length;
  const directConnectedCount = () => mcpServers().filter((server) => server.user_callable && server.connected).length;
  const routedServerCount = () => mcpServers().filter((server) => !server.user_callable).length;
  const togglePinnedSession = (event: Event, sessionId: string) => {
    event.stopPropagation();
    setPinnedSessions(previous => {
      const next = previous.includes(sessionId) ? previous.filter(id => id !== sessionId) : [...previous, sessionId];
      localStorage.setItem('tcode.pinned-sessions', JSON.stringify(next));
      return next;
    });
  };
  const openSessionMenu = async (event: MouseEvent, sessionId: string, title: string) => {
    event.preventDefault();
    event.stopPropagation();
    const session = sessionStore.sessions().find(item => item.session_id === sessionId);
    const pendingMenu = {
      sessionId, title, researchId: '', researchTitle: '',
      researchUnavailableReason: '正在读取研究课题…',
      isResearch: session?.mode === 'research',
      x: event.clientX,
      y: event.clientY,
    };
    setContextMenu(pendingMenu);
    let researchId = '';
    let researchTitle = '';
    let researchUnavailableReason = '';
    if (session?.mode !== 'research') {
      const workspaceId = String(
        session?.workspace_id
        || session?.metadata?.workspace_id
        || session?.workdir_name
        || '',
      ).trim();
      if (!workspaceId) {
        researchUnavailableReason = '该会话缺少 Workspace 信息';
      } else {
        try {
          const result = await listResearchProjects(workspaceId, false);
          const project = (result.projects || []).find(item => item.status !== 'archived');
          researchId = project?.research_id || '';
          researchTitle = project?.title || '';
          if (!project) researchUnavailableReason = '请先在研究中心确认当前课题';
        } catch (error) {
          researchUnavailableReason = '研究课题读取失败，请重试';
          console.error('Failed to load current Research Project:', error);
        }
      }
    }
    // Do not reopen a dismissed menu or overwrite a newer right-click request.
    if (contextMenu() === pendingMenu) setContextMenu({ ...pendingMenu, researchId, researchTitle, researchUnavailableReason });
  };
  const handleBindSessionToResearch = async (event: Event, menu: NonNullable<ReturnType<typeof contextMenu>>) => {
    event.stopPropagation();
    if (!await confirmAction(`将“${menu.title || '当前会话'}”关联到课题“${menu.researchTitle}”？\n会话将出现在该课题中，并受课题归档规则影响；关联不代表全部消息都纳入研究档案。`)) return;
    try {
      const updated = await bindSessionToResearch(menu.sessionId, menu.researchId);
      sessionStore.setSessions(previous => previous.map(item => item.session_id === menu.sessionId ? {
        ...item,
        mode: 'research',
        research_id: menu.researchId,
        mode_source: updated.mode_source || 'user_confirmed',
        metadata: {...(item.metadata || {}), mode: 'research', research_id: menu.researchId, mode_source: updated.mode_source || 'user_confirmed'},
      } : item));
      if (sessionStore.currentSessionId() === menu.sessionId) sessionStore.setMessages(previous => [...previous]);
    } catch (error) {
      await showFeedback(error instanceof Error ? error.message : '未能关联到课题');
    }
  };
  const handleUnbindSessionFromResearch = async (event: Event, menu: NonNullable<ReturnType<typeof contextMenu>>) => {
    event.stopPropagation();
    if (!await confirmAction(`解除“${menu.title || '当前会话'}”与课题的关联？聊天记录和已经产生的研究数据仍会保留。`)) return;
    try {
      const updated = await unbindSessionFromResearch(menu.sessionId);
      sessionStore.setSessions(previous => previous.map(item => {
        if (item.session_id !== menu.sessionId) return item;
        const metadata = {...(item.metadata || {})};
        delete metadata.research_id;
        metadata.mode = 'general';
        metadata.mode_source = updated.mode_source || 'user_unbound';
        return {...item, mode: 'general', research_id: undefined, mode_source: updated.mode_source || 'user_unbound', metadata};
      }));
      if (sessionStore.currentSessionId() === menu.sessionId) sessionStore.setMessages(previous => [...previous]);
    } catch (error) {
      await showFeedback(error instanceof Error ? error.message : '未能解除课题关联');
    }
  };
  const orderedSessions = createMemo(() => {
    const pinned = new Set(desktop()?[]:pinnedSessions());
    const rank = new Map(manualOrder().map((id,index) => [id,index]));
    return sessionStore.sessions().filter(session => !session.is_archived).sort((a,b) =>
      Number(pinned.has(b.session_id))-Number(pinned.has(a.session_id)) ||
      (rank.get(a.session_id) ?? -1)-(rank.get(b.session_id) ?? -1) ||
      normalizeTimestampMs(b.created_at,0)-normalizeTimestampMs(a.created_at,0) || a.session_id.localeCompare(b.session_id));
  });
  const visibleSessions = createMemo(() => {
    if (dragSnapshot()) return dragSnapshot()!;
    const query = sessionSearch().trim().toLocaleLowerCase();
    const filter = sessionFilter();
    return orderedSessions().filter(session => {
      if (session.is_archived) return false;
      if (query && ![session.title, session.workdir_name, session.workdir, workspacePreferences().names[sessionWorkspaceKey(session)]].some(value => String(value || '').toLocaleLowerCase().includes(query))) return false;
      const runtime = sessionStore.getSessionRuntimeState(session.session_id);
      const needsAttention = Boolean(runtime.permissionRequest || runtime.questionRequest || String(runtime.status || '').includes('待'));
      const active = Boolean(runtime.isRunning || runtime.runningTool);
      const completed = ['completed', 'complete', 'succeeded', 'success'].includes(String(session.status || '').toLowerCase()) || taskRuntimeSessionOutcomeBadge(session.task_runtime)?.label === '已完成';
      return filter === 'all' || (filter === 'active' && active) || (filter === 'attention' && needsAttention) || (filter === 'completed' && completed);
    });
  });
  const sortGroup = (id:string) => {
    const moving=visibleSessions().find(session=>session.session_id===id);
    return visibleSessions().filter(session => desktop()?!!moving&&sessionWorkspaceKey(session)===sessionWorkspaceKey(moving):pinnedSessions().includes(session.session_id)===pinnedSessions().includes(id)).map(session=>session.session_id);
  };
  function persistSessionOrder(id:string,target:string) {
    const order = reorderSessions(orderedSessions().map(session=>session.session_id),sortGroup(id),id,target);
    setManualOrder(order);
    try {localStorage.setItem(orderKey,JSON.stringify(order));setSortAnnouncement('会话顺序已保存');}
    catch {setSortAnnouncement('会话顺序已调整，但本地存储不可用，重启后不会保留');}
  }
  const moveSession = (id:string,delta:number) => {const group=sortGroup(id),target=group[group.indexOf(id)+delta];if(target)persistSessionOrder(id,target);};
  const workspaceGroups=createMemo(()=>{
    const groups=new Map<string,{id:string;title:string;sessions:ReturnType<typeof sessionStore.sessions>}>();
    for(const session of visibleSessions()){
      const id=sessionWorkspaceKey(session);
      if(!groups.has(id))groups.set(id,{id,title:workspacePreferences().names[id]||session.workdir_name||session.workspace_id||session.workdir?.split('/').filter(Boolean).slice(-1)[0]||'未分配工作区',sessions:[]});
      groups.get(id)!.sessions.push(session);
    }
    const preferences=workspacePreferences(),rank=new Map(preferences.order.map((id,i)=>[id,i]));
    return [...groups.values()].sort((a,b)=>Number(preferences.pinned.includes(b.id))-Number(preferences.pinned.includes(a.id))||(rank.get(a.id)??-1)-(rank.get(b.id)??-1));
  });
  const workspaceMoveGroup=(id:string)=>workspaceGroups().filter(group=>workspacePreferences().pinned.includes(group.id)===workspacePreferences().pinned.includes(id)).map(group=>group.id);
  function moveWorkspace(id:string,target:string){
    const all=[...new Set([...workspacePreferences().order,...orderedSessions().map(sessionWorkspaceKey)])];
    saveWorkspaces({order:reorderSessions(all,workspaceMoveGroup(id),id,target)});
  }
  const stepWorkspace=(id:string,delta:number)=>{const group=workspaceMoveGroup(id),target=group[group.indexOf(id)+delta];if(target)moveWorkspace(id,target);};
  const toggleWorkspace=(key:'pinned'|'collapsed',id:string)=>{const ids=workspacePreferences()[key];saveWorkspaces({[key]:ids.includes(id)?ids.filter(value=>value!==id):[...ids,id]});};
  const renderedGroups=createMemo(()=>grouped()?workspaceGroups().map(group=>group.id):['flat']);

  return (
    <div data-embedded={props.embedded} class="relative flex h-full min-h-0 flex-col overflow-hidden border-r border-[#2a3136] bg-[#0c1115] text-[#dee3e8]" style={{ width: props.embedded ? '100%' : `${props.width}px` }}>
      <div class="session-shortcuts border-b border-[#2a3136] px-4 pb-4 pt-5">
        <div class="flex items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <div class="flex h-10 w-10 items-center justify-center rounded-xl border border-[#38bdf8]/35 bg-[#38bdf8]/12 text-[#8ed5ff] shadow-[0_0_18px_rgba(56,189,248,0.18)]">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5">
                <path d="M12 3 1 9l11 6 9-4.91V17h2V9L12 3Zm0 9.74L5.04 9 12 5.26 18.96 9 12 12.74ZM5 12.91v4.85L12 21l7-3.24v-4.85l-7 3.82-7-3.82Z" />
              </svg>
            </div>
            <div>
              <div class="text-lg font-bold tracking-tight text-[#f7fafc]">Tcode 助手</div>
              <div class="text-[10px] uppercase tracking-[0.28em] text-[#7f8b93]">编排指挥中心</div>
            </div>
          </div>
          <Show when={!props.embedded}><button
            onClick={() => props.onHide?.()}
            class="inline-flex h-8 w-8 items-center justify-center rounded border border-[#303539] bg-[#171c20] text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            title="隐藏侧栏"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z" />
            </svg>
          </button></Show>
        </div>

        <button
          onClick={handleNewSession}
          class="mt-5 flex w-full items-center justify-center gap-2 rounded-xl border border-[#38bdf8]/35 bg-[#38bdf8]/12 px-4 py-3 text-sm font-semibold text-[#8ed5ff] transition-colors hover:bg-[#38bdf8]/18"
        >
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
            <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2Z" />
          </svg>
          新建任务
        </button>

        <div class="session-shortcuts-secondary mt-5 space-y-1 text-xs">
          <button
            type="button"
            aria-label="主页"
            title="主页"
            class="flex w-full items-center gap-3 rounded-lg border border-[#38bdf8]/25 bg-[#38bdf8]/10 px-3 py-2 text-[#8ed5ff] transition-colors hover:bg-[#38bdf8]/16"
            onClick={() => props.onOpenHome?.()}
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z" />
            </svg>
            <span>主页</span>
          </button>
          <button
            type="button"
            aria-label="历史记录"
            title="历史记录"
            class="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[#7f8b93] transition-colors hover:bg-[#171c20] hover:text-[#dee3e8]"
            onClick={() => props.onOpenHistory?.()}
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M13 3a9 9 0 1 0 8.95 10H20a7 7 0 1 1-2.05-4.95l-3.95 3.95H21V5l-3.64 3.64A8.96 8.96 0 0 0 13 3Z" />
            </svg>
            <span>历史记录</span>
          </button>
        </div>
      </div>

      <div class="session-list-header shrink-0 px-2 py-2">
        <div ref={recentSessionsRef} class="mb-2 flex items-center justify-between">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#7bd0ff]">最近任务</div>
          </div>
          <div class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#bdc8d1]">
            {visibleSessions().length}
          </div>
        </div>
        <div class="session-list-tools" classList={{'session-list-tools--workspaces':desktop()}}>
          <div class="session-search-morph" data-open={searchOpen()} onFocusOut={event => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null) && !sessionSearch()) setSearchOpen(false);
          }} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeSessionSearch(); } }}>
            <button ref={searchButton} type="button" class="session-search-toggle" aria-label="搜索会话" title="搜索会话" aria-expanded={searchOpen()}
              onClick={() => { setSearchOpen(true); queueMicrotask(() => searchInput.focus()); }}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4.5 4.5" /></svg>
            </button>
            <input ref={searchInput} type="search" value={sessionSearch()} onInput={event=>setSessionSearch(event.currentTarget.value)} placeholder="搜索任务或研究空间" aria-label="搜索任务" tabIndex={searchOpen() ? 0 : -1} disabled={!searchOpen()} />
            <button type="button" class="session-search-close" aria-label="清除并收起搜索" title="收起搜索" tabIndex={searchOpen() ? 0 : -1} disabled={!searchOpen()} onClick={closeSessionSearch}>×</button>
          </div>
          <select value={sessionFilter()} onChange={event=>setSessionFilter(event.currentTarget.value as 'all' | 'active' | 'attention' | 'completed')} aria-label="筛选任务状态">
            <option value="all">全部状态</option><option value="active">运行中</option><option value="attention">待处理</option><option value="completed">已完成</option>
          </select>
          <Show when={desktop()}><button type="button" class="session-workspace-toggle" aria-label="按工作区分组" aria-pressed={grouped()} title={grouped()?'关闭工作区分组':'按工作区分组'} onClick={()=>{setContextMenu(null);saveWorkspaces({grouped:!grouped()});}}>
            <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3M6 13h12M6 17h8"/></svg>
          </button></Show>
        </div>
      </div>
      <div class="sr-only" role="status" aria-live="polite">{sortAnnouncement()}</div>
      <div ref={sessionScroll} class="session-list-scroll min-h-0 flex-1 overflow-y-auto px-2 py-2" data-context-open={Boolean(contextMenu())}>
        <Show
          when={visibleSessions().length > 0}
          fallback={
            <div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#171c20]/80 px-4 py-6 text-center text-[#8b949e]">
              <div class="text-sm">{sessionSearch().trim()||sessionFilter()!=='all'?'没有匹配的任务':'还没有任务'}</div>
              <button
                onClick={handleNewSession}
                class="mt-3 rounded-lg border border-[#38bdf8]/35 bg-[#38bdf8]/10 px-3 py-2 text-xs font-semibold text-[#8ed5ff] transition-colors hover:bg-[#38bdf8]/18"
              >
                创建第一个任务
              </button>
            </div>
          }
        >
          <div class="space-y-1">
            <For each={renderedGroups()}>{id=>{
              const group={id,get title(){return workspaceGroups().find(item=>item.id===id)?.title||'';},get sessions(){return grouped()?workspaceGroups().find(item=>item.id===id)?.sessions||[]:visibleSessions();}};
              return <section classList={{'session-workspace-group':grouped()}} style={grouped()?{'--workspace-hue':String(workspaceAccentHue(group.id))}:undefined} data-current={grouped()&&group.sessions.some(session=>session.session_id===sessionStore.currentSessionId())?'true':undefined} data-workspace-sort-id={grouped()?group.id:undefined} data-sort-group={grouped()?String(workspacePreferences().pinned.includes(group.id)):undefined}>
              <Show when={grouped()}>
                <div class="session-workspace-heading" role="button" tabIndex={0} aria-label={`工作区：${group.title}`} aria-expanded={!workspacePreferences().collapsed.includes(group.id)||!!sessionSearch().trim()} title="点击折叠；长按拖动工作区；Alt + ↑/↓ 排序"
                  onClick={()=>toggleWorkspace('collapsed',group.id)} onKeyDown={event=>{if(event.target!==event.currentTarget)return;if(event.altKey&&['ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();stepWorkspace(group.id,event.key==='ArrowUp'?-1:1);}else if(['Enter',' '].includes(event.key)){event.preventDefault();toggleWorkspace('collapsed',group.id);}}}>
                  <svg class="session-workspace-chevron" aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 5 7 7-7 7"/></svg>
                  <span class="session-workspace-icon" aria-hidden="true"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M3 11h18"/></svg></span>
                  <Show when={workspacePreferences().pinned.includes(group.id)}><span class="session-pin-mark" aria-label="已置顶">◆</span></Show>
                  <span class="session-workspace-name" title={group.title}>{group.title}</span><span class="session-workspace-count" aria-label={`${group.sessions.length} 个会话`} title={sessionSearch().trim()?`${group.sessions.length} 个匹配会话`:`${group.sessions.length} 个会话`}><svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M20 14a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2Z"/></svg><span>{group.sessions.length}</span></span>
                  <details class="session-workspace-menu" onClick={event=>event.stopPropagation()} onKeyDown={event=>{event.stopPropagation();if(event.key==='Escape'){event.currentTarget.open=false;event.currentTarget.querySelector('summary')?.focus();}}}>
                    <summary aria-label={`工作区操作：${group.title}`}>⋯</summary>
                    <div onClick={event=>{if((event.target as Element).closest('button'))event.currentTarget.closest('details')!.open=false;}}>
                      <button type="button" onClick={()=>toggleWorkspace('pinned',group.id)}>{workspacePreferences().pinned.includes(group.id)?'取消置顶':'置顶工作区'}</button>
                      <button type="button" onClick={()=>setRenamingWorkspace({id:group.id,name:group.title})}>重命名</button>
                      <button type="button" disabled={workspaceMoveGroup(group.id).indexOf(group.id)<=0} onClick={()=>stepWorkspace(group.id,-1)}>上移</button>
                      <button type="button" disabled={workspaceMoveGroup(group.id).indexOf(group.id)>=workspaceMoveGroup(group.id).length-1} onClick={()=>stepWorkspace(group.id,1)}>下移</button>
                    </div>
                  </details>
                </div>
              </Show>
              <div class="space-y-1" hidden={grouped()&&workspacePreferences().collapsed.includes(group.id)&&!sessionSearch().trim()}>
            <For each={group.sessions}>
              {(session) => {
                const runtime = () => sessionStore.getSessionRuntimeState(session.session_id);
                const outcomeBadge = () => taskRuntimeSessionOutcomeBadge(session.task_runtime);
                const runtimeLabel = () => {
                  const runtimeStatus = String(runtime().status || '');
                  if (runtime().permissionRequest) return '等待权限';
                  if (runtime().questionRequest) return '等待回答';
                  if (runtimeStatus.includes('待接入指导（单步）') || runtimeStatus.includes('待继续（单步）')) return '待接入';
                  if (runtimeStatus.includes('待执行追加指导（单步）')) return '待执行';
                  if (runtime().runningTool?.tool_name) {
                    return toolDisplayLabel(runtime().runningTool?.tool_name);
                  }
                  if (runtime().isRunning) return '运行中';
                  return outcomeBadge()?.label || '空闲';
                };
                const runtimeBadgeClass = () => {
                  const runtimeStatus = String(runtime().status || '');
                  if (runtime().permissionRequest) {
                    return 'border-[#f1a02b]/30 bg-[#f1a02b]/12 text-[#ffc176]';
                  }
                  if (runtime().questionRequest) {
                    return 'border-[#38bdf8]/30 bg-[#38bdf8]/12 text-[#8ed5ff]';
                  }
                  if (runtimeStatus.includes('待接入指导（单步）') || runtimeStatus.includes('待继续（单步）')) {
                    return 'border-[#4ecdc4]/30 bg-[#4ecdc4]/12 text-[#9df3ed]';
                  }
                  if (runtimeStatus.includes('待执行追加指导（单步）')) {
                    return 'border-[#f5c451]/30 bg-[#f5c451]/12 text-[#ffe49a]';
                  }
                  if (runtime().runningTool?.tool_name || runtime().isRunning) {
                    return 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]';
                  }
                  return outcomeBadge()?.class || 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]';
                };
                const sessionTitle = () => session.title || session.messages[0]?.content || '新会话';
                const isResearchSession = () => session.mode === 'research' || session.metadata?.mode === 'research';
                const sessionDescription = () => [sessionTitle(), isResearchSession() ? '已关联课题' : '', session.workdir_name, formatTime(getSessionDisplayTimestamp(session)), runtimeLabel()].filter(Boolean).join(' · ');

                return <div
                  role="button"
                  tabindex="0"
                  title={`${sessionDescription()} · 长按拖动排序；Alt + ↑/↓ 调整位置`}
                  data-session-id={session.session_id}
                  data-session-pinned={!desktop()&&pinnedSessions().includes(session.session_id)}
                  data-sort-group={desktop()?sessionWorkspaceKey(session):undefined}
                  aria-label={sessionDescription()}
                  aria-current={sessionStore.currentSessionId() === session.session_id ? 'true' : undefined}
                  data-research-session={isResearchSession() ? 'true' : undefined}
                  data-context-focused={contextMenu()?.sessionId === session.session_id}
                  onClick={() => { void handleSelectSession(session.session_id); }}
                  onContextMenu={(event) => void openSessionMenu(event, session.session_id, sessionTitle())}
                  onKeyDown={(event) => {
                    if (event.target === event.currentTarget && event.altKey && ['ArrowUp','ArrowDown'].includes(event.key)) {
                      event.preventDefault();moveSession(session.session_id,event.key==='ArrowUp'?-1:1);return;
                    }
                    if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) {
                      event.preventDefault();
                      void handleSelectSession(session.session_id);
                    }
                  }}
                  class={`session-row group w-full rounded-md border p-2 text-left transition-colors ${sessionStore.currentSessionId() === session.session_id ? 'border-[#38bdf8]/38 bg-[#132028]' : 'border-transparent hover:border-[#3e484f] hover:bg-[#171f24]'}`}
                >
                  <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0 flex-1">
                      <div class="session-row-title truncate text-xs font-semibold text-[#f5f7fa]">
                        <Show when={!desktop()&&pinnedSessions().includes(session.session_id)}><span class="session-pin-mark" title="已置顶">◆</span></Show>
                        <Show when={isResearchSession()}><span class="session-research-mark" title="已关联课题">已关联</span></Show>
                        {sessionTitle()}
                      </div>
                      <div class="session-row-meta mt-1 flex items-center gap-1 text-[10px] text-[#7f8b93]">
                        <Show when={session.workdir_name}><span class="session-row-workspace truncate">{session.workdir_name}</span></Show>
                        <span class="session-row-time">{formatTime(getSessionDisplayTimestamp(session))}</span>
                            <span class={`session-row-status truncate rounded-full border px-1 ${runtimeBadgeClass()}`}>
                              {runtimeLabel()}
                            </span>
                      </div>
                    </div>
                    <button class="session-row-actions"
                        aria-label={`会话操作：${sessionTitle()}`}
                        title="会话操作"
                        onClick={(event) => void openSessionMenu(event, session.session_id, sessionTitle())}
                      >⋯</button>
                  </div>
                </div>;
              }}
            </For>
              </div>
            </section>;}}</For>
          </div>
        </Show>
      </div>

      <Show when={contextMenu()} keyed>{menu =>
        <Portal><div ref={panel => onCleanup(watchSessionMenu(panel, sessionScroll, menu))} class="session-context-menu" role="menu" style={{left:`${menu.x}px`,top:`${menu.y}px`}}>
          <button role="menuitem" disabled={sortGroup(menu.sessionId).indexOf(menu.sessionId)<=0} onClick={()=>{moveSession(menu.sessionId,-1);setContextMenu(null);}}>上移</button>
          <button role="menuitem" disabled={sortGroup(menu.sessionId).indexOf(menu.sessionId)>=sortGroup(menu.sessionId).length-1} onClick={()=>{moveSession(menu.sessionId,1);setContextMenu(null);}}>下移</button>
          <Show when={!desktop()}><button role="menuitem" onClick={event=>{setContextMenu(null);togglePinnedSession(event,menu.sessionId);}}>{pinnedSessions().includes(menu.sessionId)?'取消置顶':'置顶'}</button></Show>
          <button role="menuitem" onClick={event=>{event.stopPropagation();setContextMenu(null);setRenamingSession({sessionId:menu.sessionId,title:menu.title});}}>重命名</button>
          <Show when={!menu.isResearch}><button role="menuitem" disabled={!menu.researchId} title={menu.researchUnavailableReason || undefined} onClick={event=>{if(!menu.researchId)return;setContextMenu(null);void handleBindSessionToResearch(event,menu);}}>{menu.researchId ? '关联到课题' : menu.researchUnavailableReason}</button></Show>
          <Show when={menu.isResearch}><button role="menuitem" onClick={event=>{setContextMenu(null);void handleUnbindSessionFromResearch(event,menu);}}>解除课题关联</button></Show>
          <Show when={!menu.isResearch && !menu.researchId}><button role="menuitem" onClick={() => {setContextMenu(null);window.dispatchEvent(new CustomEvent('tcode-open-research-center'));}}>查看研究课题与候选</button></Show>
          <button role="menuitem" onClick={event=>{setContextMenu(null);void handleArchiveSession(event,menu.sessionId);}}>归档</button>
          <button role="menuitem" class="danger" disabled={Boolean(deletingSessionIds()[menu.sessionId])} onClick={event=>{event.stopPropagation();setContextMenu(null);setDeleteConfirmation({sessionId:menu.sessionId,title:menu.title});}}>删除</button>
        </div></Portal>
      }</Show>

      <Show when={renamingWorkspace()}>{target=><FeedbackDialog title="重命名工作区" onClose={()=>setRenamingWorkspace(null)}>
        <form onSubmit={event=>{event.preventDefault();const name=target().name.trim();if(!name)return;saveWorkspaces({names:{...workspacePreferences().names,[target().id]:name}});setRenamingWorkspace(null);}}>
          <p>只修改会话列表中的显示名称，不修改磁盘目录或会话归属。</p>
          <input class="input w-full mt-3" aria-label="工作区显示名称" maxLength={100} value={target().name} ref={element=>queueMicrotask(()=>{element.focus();element.select();})} onInput={event=>setRenamingWorkspace({id:target().id,name:event.currentTarget.value})}/>
          <div class="feedback-dialog-actions"><button type="button" onClick={()=>setRenamingWorkspace(null)}>取消</button><button type="submit" class="feedback-primary" disabled={!target().name.trim()}>保存名称</button></div>
        </form>
      </FeedbackDialog>}</Show>
      <Show when={renamingSession()}>{target =>
        <FeedbackDialog title="重命名会话" onClose={()=>setRenamingSession(null)}>
          <form onSubmit={event=>{event.preventDefault();void handleRenameSession(target().sessionId,target().title);}}>
            <input
              ref={element=>queueMicrotask(()=>{element.focus();element.select();})}
              class="mt-3 h-9 w-full rounded border border-[var(--tcode-border)] bg-[var(--tcode-bg)] px-3 text-sm text-[var(--tcode-text)] outline-none focus:border-[var(--tcode-primary)]"
              value={target().title}
              aria-label="输入新的会话名称"
              onInput={event=>setRenamingSession({sessionId:target().sessionId,title:event.currentTarget.value})}
              onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();setRenamingSession(null);}}}
            />
            <div class="feedback-dialog-actions">
              <button type="button" class="btn btn-sm" onClick={()=>setRenamingSession(null)}>取消</button>
              <button type="submit" class="feedback-primary" disabled={!target().title.trim()}>保存名称</button>
            </div>
          </form>
        </FeedbackDialog>
      }</Show>

      <Show when={deleteConfirmation()} keyed>{target =>
        <FeedbackDialog title="确认删除会话？" tone="danger" onClose={()=>setDeleteConfirmation(null)}>
            <p>“{target.title || '未命名会话'}”将被永久删除，此操作无法撤销。</p>
            <div class="feedback-dialog-actions">
              <button autofocus onClick={()=>setDeleteConfirmation(null)}>取消</button>
              <button class="feedback-primary" disabled={Boolean(deletingSessionIds()[target.sessionId])} onClick={()=>{setDeleteConfirmation(null);void handleDeleteSession(target.sessionId);}}>确认删除</button>
            </div>
        </FeedbackDialog>
      }</Show>

      <details class="studio-session-status border-t border-[#2a3136] px-4 py-4">
        <summary>系统状态与用量</summary>
        <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
          <div class="mt-3 space-y-2 text-xs text-[#bdc8d1]">
            <div class="flex items-center justify-between">
              <span class="text-[#7f8b93]">直连 MCP</span>
              <span>{directConnectedCount()}/{directServerCount()}</span>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-[#7f8b93]">队列 MCP</span>
              <span>{routedServerCount()}</span>
            </div>
            <button
              type="button"
              class="flex w-full items-center justify-between rounded-lg border border-transparent px-1.5 py-1 text-left transition-colors hover:border-[#7bd0ff]/30 hover:bg-[#12212a]"
              onClick={() => setShowUsageDetail(true)}
              title="查看每个 session 与 agent 的 token 用量"
            >
              <span class="text-[#7f8b93]">累计令牌</span>
              <span class="font-mono text-[#7ee787]" title={sessionStore.usageStats().totalTokens.toLocaleString()}>{(sessionStore.usageStats().totalTokens / 1e8).toFixed(4)} 亿</span>
            </button>
            <button
              type="button"
              class="flex w-full items-center justify-between rounded-lg border border-transparent px-1.5 py-1 text-left transition-colors hover:border-[#7bd0ff]/30 hover:bg-[#12212a]"
              onClick={() => setShowUsageDetail(true)}
              title="查看输入/输出 token 明细"
            >
              <span class="text-[#7f8b93]">输入 / 输出</span>
              <span class="font-mono" title={`${sessionStore.usageStats().promptTokens.toLocaleString()} / ${sessionStore.usageStats().completionTokens.toLocaleString()}`}>{(sessionStore.usageStats().promptTokens / 1e8).toFixed(4)} / {(sessionStore.usageStats().completionTokens / 1e8).toFixed(4)} 亿</span>
            </button>
          </div>
        </div>
      </details>

      <Show when={showUsageDetail()}>
        <UsageDetailModal onClose={() => setShowUsageDetail(false)} />
      </Show>

      {/* Resize handle */}
      <Show when={!props.embedded}>
      <div
        class="absolute bottom-0 right-0 top-0 z-50 w-1 cursor-col-resize transition-colors hover:bg-[#38bdf8]"
        onMouseDown={(e) => {
          e.preventDefault();
          const handleMouseMove = (moveEvent: MouseEvent) => {
            const newWidth = Math.max(200, Math.min(500, moveEvent.clientX));
            props.onWidthChange?.(newWidth);
          };
          const handleMouseUp = () => {
            document.removeEventListener('mousemove', handleMouseMove);
            document.removeEventListener('mouseup', handleMouseUp);
          };
          document.addEventListener('mousemove', handleMouseMove);
          document.addEventListener('mouseup', handleMouseUp);
        }}
      />
      </Show>

      <Show when={showWorkdirModal()}>
        <div class="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
          <div class="w-full max-w-xl overflow-hidden rounded-2xl border border-[#2a3136] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.5)]">
            <div class="flex items-center justify-between border-b border-[#2a3136] px-5 py-4">
              <div>
                <div class="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#7bd0ff]">创建会话</div>
                <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">新建会话</div>
              </div>
              <button
                class="inline-flex h-8 items-center justify-center rounded border border-[#303539] bg-[#171c20] px-3 text-xs text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                onClick={() => setShowWorkdirModal(false)}
                disabled={creatingSession() || workspaceMutationActive()}
              >
                关闭
              </button>
            </div>
            <div class="space-y-4 px-5 py-5">
              <div class="flex items-center justify-between gap-3 text-xs text-[#9faeb7]">
                <span>选择工作目录。会话创建后不可更改。</span>
                <span>Workspace {workdirs().length}/{maxWorkspacesPerUser()}</span>
              </div>
              <div class="max-h-56 overflow-y-auto rounded-2xl border border-[#2a3136] bg-[#0f1418]">
                <For each={workdirs()}>
                  {(dir) => (
                    <div class="flex min-h-12 items-center border-b border-[#2a3136] last:border-b-0 hover:bg-[#151d22]">
                      <label class="flex min-w-0 flex-1 cursor-pointer items-center gap-3 px-4 py-3">
                        <input
                          type="radio"
                          name="workdir"
                          class="h-4 w-4 shrink-0 accent-[#38bdf8]"
                          checked={selectedWorkdirName() === dir.name}
                          onChange={() => setSelectedWorkdirName(dir.name)}
                          disabled={workspaceMutationActive()}
                        />
                        <span class="truncate text-sm text-[#e6edf3]" title={dir.name}>{dir.name}</span>
                      </label>
                      <button
                        type="button"
                        class="mr-3 inline-flex h-8 shrink-0 items-center justify-center rounded border border-[#6b2d2d] px-2.5 text-xs text-[#ff9c96] transition-colors hover:bg-[#3a1d1d] disabled:cursor-not-allowed disabled:opacity-50"
                        title={`删除工作目录 ${dir.name}`}
                        aria-label={`删除工作目录 ${dir.name}`}
                        onClick={() => void handleDeleteWorkdir(dir.name)}
                        disabled={workspaceMutationActive() || creatingSession()}
                      >
                        {deletingWorkdirName() === dir.name ? '删除中...' : '删除'}
                      </button>
                    </div>
                  )}
                </For>
              </div>
              <Show when={deletedWorkdirs().length > 0}>
                <div class="space-y-2">
                  <div class="flex items-center justify-between text-xs text-[#9faeb7]">
                    <span>回收站与残留</span>
                    <span>{deletedWorkdirs().length} 个 Workspace</span>
                  </div>
                  <div class="max-h-40 overflow-y-auto rounded-lg border border-[#3a3030] bg-[#120f10]">
                    <For each={deletedWorkdirs()}>
                      {(dir) => (
                        <div class="flex min-h-12 items-center gap-2 border-b border-[#30282a] px-3 py-2 last:border-b-0">
                          <div class="min-w-0 flex-1">
                            <div class="truncate text-sm text-[#e6edf3]" title={dir.workspace_id}>
                              {dir.workspace_id}
                            </div>
                            <div class="truncate text-[11px] text-[#9a8588]">
                              {dir.status === 'deleting'
                                ? `上次删除未完成：${dir.last_error || '可恢复 Workspace 或重试删除'}`
                                : dir.status === 'purging'
                                ? dir.purge_running
                                  ? '正在永久删除'
                                  : '清理已中断，可以继续清理'
                                : dir.status === 'purge_failed'
                                  ? `删除失败：${dir.last_error || '未知错误'}`
                                  : dir.status === 'purged'
                                    ? '已永久删除，但检测到残留文件；不能恢复'
                                    : '文件和历史仍保留'}
                            </div>
                          </div>
                          <Show when={dir.status === 'deleted' || dir.status === 'deleting'}>
                            <button
                              type="button"
                              class="inline-flex h-8 shrink-0 items-center justify-center rounded border border-[#303539] px-2.5 text-xs text-[#9faeb7] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50"
                              title={`恢复 Workspace ${dir.workspace_id}`}
                              onClick={() => void handleRestoreWorkdir(dir.workspace_id)}
                              disabled={workspaceMutationActive() || !dir.files_present}
                            >
                              {restoringWorkdirName() === dir.workspace_id ? '恢复中...' : '恢复'}
                            </button>
                          </Show>
                          <Show
                            when={dir.status === 'deleting'}
                            fallback={(
                              <button
                                type="button"
                                class="inline-flex h-8 shrink-0 items-center justify-center rounded border border-[#7c3030] px-2.5 text-xs text-[#ff9c96] transition-colors hover:bg-[#3a1d1d] disabled:opacity-50"
                                title={dir.status === 'purged'
                                  ? `清理 Workspace ${dir.workspace_id} 的残留文件`
                                  : `永久删除 Workspace ${dir.workspace_id}`}
                                onClick={() => void handlePurgeWorkdir(dir.workspace_id)}
                                disabled={workspaceMutationActive() || (dir.status === 'purging' && dir.purge_running)}
                              >
                                {purgingWorkdirName() === dir.workspace_id || (dir.status === 'purging' && dir.purge_running)
                                  ? '清理中...'
                                  : dir.status === 'purged'
                                    ? '清理残留'
                                    : dir.status === 'purge_failed' || dir.status === 'purging'
                                      ? '重试清理'
                                      : '永久删除'}
                              </button>
                            )}
                          >
                            <button
                              type="button"
                              class="inline-flex h-8 shrink-0 items-center justify-center rounded border border-[#7c3030] px-2.5 text-xs text-[#ff9c96] transition-colors hover:bg-[#3a1d1d] disabled:opacity-50"
                              title={`重试删除 Workspace ${dir.workspace_id}`}
                              onClick={() => void handleDeleteWorkdir(dir.workspace_id)}
                              disabled={workspaceMutationActive()}
                            >
                              {deletingWorkdirName() === dir.workspace_id ? '删除中...' : '重试删除'}
                            </button>
                          </Show>
                        </div>
                      )}
                    </For>
                  </div>
                </div>
              </Show>
              <div class="flex gap-2">
                <input
                  class="flex-1 rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-2 text-sm text-[#e6edf3] outline-none transition-colors placeholder:text-[#6f7d87] focus:border-[#7bd0ff]/45"
                  placeholder="新增目录名"
                  value={newWorkdirName()}
                  onInput={(e) => { setNewWorkdirName(e.currentTarget.value); setWorkdirError(''); }}
                  disabled={workspaceMutationActive() || remainingWorkspaceSlots() <= 0}
                />
                <button
                  class="inline-flex items-center justify-center rounded-xl border border-[#38bdf8]/35 bg-[#38bdf8]/12 px-4 text-sm font-semibold text-[#8ed5ff] transition-colors hover:bg-[#38bdf8]/18 disabled:opacity-50"
                  onClick={handleCreateWorkdir}
                  disabled={!newWorkdirName().trim() || creatingSession() || workspaceMutationActive() || remainingWorkspaceSlots() <= 0}
                >
                  新增目录
                </button>
              </div>
              <Show when={remainingWorkspaceSlots() <= 0}>
                <div class="text-xs text-[#d7ad65]">已达到 Workspace 数量上限；删除一个 Workspace 后才能继续创建。</div>
              </Show>
              <Show when={workdirError()}>
                <div class="text-xs text-[#ff9c96]">{workdirError()}</div>
              </Show>
            </div>
            <div class="flex justify-end gap-2 border-t border-[#2a3136] px-5 py-4">
              <button
                class="inline-flex items-center justify-center rounded-xl border border-[#303539] bg-[#171c20] px-4 py-2 text-sm text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                onClick={() => setShowWorkdirModal(false)}
                disabled={creatingSession() || workspaceMutationActive()}
              >
                取消
              </button>
              <button
                class="inline-flex items-center justify-center rounded-xl border border-[#38bdf8]/35 bg-[#38bdf8] px-4 py-2 text-sm font-semibold text-[#043245] transition-colors hover:bg-[#67caf5] disabled:cursor-not-allowed disabled:opacity-50"
                onClick={handleConfirmCreateSession}
                disabled={creatingSession() || workspaceMutationActive() || !selectedWorkdirName().trim()}
              >
                {creatingSession() ? '创建中...' : '创建会话'}
              </button>
            </div>
          </div>
        </div>
      </Show>

      <Show when={purgeConfirmation()} keyed>
        {(confirmation) => (
          <div class="fixed inset-0 z-[110] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
            <div
              class="w-full max-w-md overflow-hidden rounded-2xl border border-[#5a3030] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.55)]"
              role="dialog"
              aria-modal="true"
              aria-labelledby="purge-workspace-title"
            >
              <div class="border-b border-[#3a2c2d] px-5 py-4">
                <div id="purge-workspace-title" class="text-sm font-semibold text-[#ffb0aa]">
                  {confirmation.residual ? '清理残留文件' : '永久删除 Workspace'}
                </div>
                <div class="mt-2 text-xs leading-5 text-[#b9a6a8]">
                  {confirmation.residual
                    ? `“${confirmation.name}”已永久删除。此操作将清理残留文件，且无法恢复。`
                    : `此操作将清除“${confirmation.name}”的文件、Memory、会话历史和关联任务，且无法恢复。`}
                </div>
              </div>
              <form
                class="space-y-3 px-5 py-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void handleConfirmPurgeWorkdir();
                }}
              >
                <label class="block text-xs text-[#9faeb7]" for="purge-workspace-confirmation">
                  输入 Workspace 名称 <span class="font-semibold text-[#e6edf3]">{confirmation.name}</span> 以确认
                </label>
                <input
                  id="purge-workspace-confirmation"
                  autofocus
                  class="w-full rounded-xl border border-[#3d3536] bg-[#0f1418] px-3 py-2 text-sm text-[#e6edf3] outline-none transition-colors placeholder:text-[#6f6668] focus:border-[#c95b55]"
                  value={confirmation.typed}
                  onInput={(event) => setPurgeConfirmation({ ...confirmation, typed: event.currentTarget.value })}
                  autocomplete="off"
                />
                <Show when={workdirError()}>
                  <div class="text-xs text-[#ff9c96]">{workdirError()}</div>
                </Show>
                <div class="flex justify-end gap-2 pt-1">
                  <button
                    type="button"
                    class="rounded-xl border border-[#303539] bg-[#171c20] px-4 py-2 text-sm text-[#9faeb7] hover:text-[#e6edf3]"
                    onClick={() => {
                      setPurgeConfirmation(null);
                      setWorkdirError('');
                    }}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    class="rounded-xl border border-[#7c3030] bg-[#6b2929] px-4 py-2 text-sm font-semibold text-[#ffd6d2] transition-colors hover:bg-[#813333] disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={confirmation.typed !== confirmation.name}
                  >
                    {confirmation.residual ? '确认清理' : '确认永久删除'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </Show>
    </div>
  );
}
