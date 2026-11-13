import { createGuidanceQueue } from './composer/createGuidanceQueue';
import { recordModelSwitch, confirmModelSwitch } from '../utils/modelSwitchNotice';
import { createFileMentions } from './composer/createFileMentions';
import {
  buildFileRefs as buildMessageFileRefs,
  buildFinalMessage as buildMessagePayload,
  isImageAttachment,
} from './composer/messagePayload';
import ExecutionSettings from './composer/ExecutionSettings';
import GoalModePanel from './GoalModePanel';
import HarnessSwitchNotice from './HarnessSwitchNotice';
import { createComposerLayout } from './composer/createComposerLayout';
import InteractionPanel from './composer/InteractionPanel';
import { resolveActiveModel } from './composer/modelSelection';
import { navigateInputHistory, type InputHistoryState } from './composer/inputHistory';
import SlashCommandMenu from './composer/SlashCommandMenu';
import { builtinCommands, filterCommands, findCommandTrigger, parseBuiltinCommand, skillCommandItems, type CommandItem } from './composer/slashCommands';
import { compactSession, getSkillCatalog, type SkillCatalogItem } from '../api/client';
import { createSignal, createUniqueId, Show, For, onMount, createEffect, onCleanup, createMemo, on, untrack } from 'solid-js';
import { sendMessage, cancelMessage, getAgents, getModels, getChatRuntimes, selectModel, getPluginCommands, bootstrapSession, closeSessionSubagents, guideRunningSession, askSideQuestion, chatWithSubagent, getTeamCatalog, type ChatRuntimeOption, type PluginCommand } from '../api/client';
import { HarnessSwitchApiError, switchHarness, type HarnessStateProjection } from '../api/harness';
import { sessionStore } from '../stores/session';
import { composerDrafts, composerKey, composerIdentity, type AttachedFile, type ComposerDraft, type StagedGuidance, type StructureRef } from '../stores/composer';
import FileUpload, { type UploadedFile } from './FileUpload';
import SketchPanel from './SketchPanel';
import { ZoomableImage } from './ImageLightbox';
import { generateId } from '../utils/id';
import { findFileMentionTrigger, removeFileMentionTrigger } from '../utils/fileMentions';
import {
  getInlineFileUrl,
  isImageFileRef,
} from '../utils/inlineFiles';
import {
  hydrateTeamCatalog,
  isFixedTeamBootstrapAgent,
  usesFixedScheduler,
} from '../utils/teamRegistry';
import { desktop } from '../desktop';

type ResearchChatAsset = {
  asset_id: string;
  pack_id?: string;
  title: string;
  source_research_id?: string;
};

type WikiChatPage = {
  page_id: string;
  source_id?: string;
  title: string;
  page_type?: string;
  status?: string;
  revision?: number;
  access?: 'owner' | 'shared_read_only' | string;
};

export default function MessageInput(props: { onOpenSkills?: () => void } = {}) {
  const [goalRunning, setGoalRunning] = createSignal(false);
  const [goalMode, setGoalMode] = createSignal(false);
  let submitGoalMessage: ((text: string) => Promise<boolean>) | undefined;
  const [goalControlMount, setGoalControlMount] = createSignal<HTMLDivElement>();
  const [desktopPermissionMode, setDesktopPermissionMode] = createSignal<'ask' | 'auto'>(
    localStorage.getItem('tcode.desktop.permission-mode') === 'auto' ? 'auto' : 'ask',
  );
  const updateDesktopPermissionMode = (mode: 'ask' | 'auto') => {
    setDesktopPermissionMode(mode);
    localStorage.setItem('tcode.desktop.permission-mode', mode);
  };
  const draftKey = () => composerKey(sessionStore.currentSessionId());
  const input = () => composerDrafts.get(draftKey()).text;
  const setInput = (next: string | ((previous: string) => string)) => {
    const text = typeof next === 'function' ? next(input()) : next;
    composerDrafts.update(draftKey(), { text });
    return text;
  };
  const [activeResearchAssets, setActiveResearchAssets] = createSignal<ResearchChatAsset[]>([]);
  const [activeWikiPage, setActiveWikiPage] = createSignal<WikiChatPage | null>(null);
  const [pluginCommands, setPluginCommands] = createSignal<PluginCommand[]>([]);
  const [highlightedCommandIndex, setHighlightedCommandIndex] = createSignal(0);
  const [isCommandMenuOpen, setIsCommandMenuOpen] = createSignal(false);
  const [commandView, setCommandView] = createSignal<'commands' | 'models' | 'skills' | 'help'>('commands');
  const [commandQuery, setCommandQuery] = createSignal('');
  const [commandFeedback, setCommandFeedback] = createSignal('');
  const [commandBusy, setCommandBusy] = createSignal(false);
  const [skillCatalog, setSkillCatalog] = createSignal<SkillCatalogItem[]>([]);
  const [skillsLoading, setSkillsLoading] = createSignal(false);
  const [skillsError, setSkillsError] = createSignal('');
  const selectedSkills = () => composerDrafts.get(draftKey()).selectedSkills || [];
  const setSelectedSkills = (names: string[]) => composerDrafts.update(draftKey(), { selectedSkills: names });
  const attachedFiles = () => composerDrafts.get(draftKey()).files;
  const structureRefs = () => composerDrafts.get(draftKey()).structureRefs || [];
  const setStructureRefs = (refs: StructureRef[]) => composerDrafts.update(draftKey(), { structureRefs: refs });
  const setAttachedFiles = (next: AttachedFile[] | ((previous: AttachedFile[]) => AttachedFile[])) => {
    const files = (typeof next === 'function' ? next(attachedFiles()) : next)
      .map(file => ({ ...file, sessionId: file.sessionId || sessionStore.currentSessionId() || undefined }));
    composerDrafts.update(draftKey(), { files });
    return files;
  };
  const pendingSubmissions = new Set<string>();
  const [guidanceSubmittingId, setGuidanceSubmittingId] = createSignal('');
  const [showUpload, setShowUpload] = createSignal(false);
  const uploadPanelId = createUniqueId();
  const [sketchSessionId, setSketchSessionId] = createSignal('');
  const attachSketch = async (file: File) => {
    const sessionId = sketchSessionId();
    const key = composerKey(sessionId);
    const identity = composerIdentity();
    if (!sessionId) throw new Error('请先选择会话');
    const form = new FormData();
    form.append('file', file);
    const response = await fetch(`/api/files/upload?${new URLSearchParams({ session_id: sessionId })}`, { method: 'POST', body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(String(data.detail || '草图上传失败，请重试'));
    if (identity !== composerIdentity()) throw new Error('账号已切换，请在当前账号重新添加草图');
    const path = String(data.display_path || data.container_path || data.path || '').trim();
    if (!path) throw new Error('上传未返回图片路径，请重试');
    const draft = composerDrafts.get(key);
    composerDrafts.update(key, { files: [...draft.files, { id: generateId(), name: file.name, path, file, mode: 'path', source: 'upload', sessionId }] });
  };
  let inputHistoryState: InputHistoryState = { index: -1, draft: '' };
  const [askingSideQuestion, setAskingSideQuestion] = createSignal(false);
  const [cancelling, setCancelling] = createSignal(false);
  const [lastSideQuestionResult, setLastSideQuestionResult] = createSignal<{
    sessionId: string;
    displayName: string;
    prompt: string;
    response: string;
  } | null>(null);
  const [agents, setAgents] = createSignal<
    Array<{ name: string; description: string; mode: string }>
  >([]);
  const [models, setModels] = createSignal<
    Array<{ id: string; provider_id: string; model_id: string; name: string; supports_images?: boolean | null }>
  >([]);
  const [modelLoadError, setModelLoadError] = createSignal('');
  const [modelsLoading, setModelsLoading] = createSignal(false);
  const [modelChangePending, setModelChangePending] = createSignal(false);
  const [runtimeOptions, setRuntimeOptions] = createSignal<ChatRuntimeOption[]>([
    {
      runtime_id: 'tcode-native',
      display_name: 'TCode',
      available: true,
      enabled: true,
      release: 'stable',
      models: [],
      capabilities: { text: true, images: true, tools: true, resume: true, cancel: true },
      source_type: 'builtin',
      source_name: 'tcode-agent',
      trust_state: 'builtin',
      compatibility_state: 'compatible',
    },
  ]);
  const [selectedRuntimeId, setSelectedRuntimeId] = createSignal('tcode-native');
  const [runtimeLoadError, setRuntimeLoadError] = createSignal('');
  const [runtimeChangePending, setRuntimeChangePending] = createSignal(false);
  const [dismissedHandoffNoticeId, setDismissedHandoffNoticeId] = createSignal('');
  const [preferredHarnessModel, setPreferredHarnessModel] = createSignal('deepseek/deepseek-v4-flash');
  let modelLoadGeneration = 0;
  let agentCatalogLoadGeneration = 0;


  const updateLocalWikiContext = (page: WikiChatPage | null) => {
    setActiveWikiPage(page);
    const sessionId = String(sessionStore.currentSessionId() || '').trim();
    if (!sessionId) return;
    sessionStore.setSessions((current) => current.map((session) => (
      session.session_id === sessionId
        ? {
            ...session,
            metadata: {
              ...(session.metadata || {}),
              wiki_context: {
                schema: 'tcode.wiki-chat-context.v1',
                mode: 'sticky',
                page,
              },
            },
          }
        : session
    )));
  };

  const openSideChatWorkspace = (detail: {
    parentSessionId?: string;
    sessionId?: string;
    pending?: boolean;
  }) => {
    if (typeof window === 'undefined') return;
    window.dispatchEvent(new CustomEvent('tcode-open-side-chat', {
      detail,
    }));
  };
  const [selectedModel, setSelectedModel] = createSignal('');
  const currentHarnessState = createMemo<HarnessStateProjection>(() => {
    const harness = sessionStore.currentSession()?.metadata?.harness || {};
    return {
      runtime_id: String(harness.runtime_id || 'tcode-native').trim() || 'tcode-native',
      model_id: String(harness.model_id || sessionStore.currentSession()?.model || '').trim(),
      models: harness.models && typeof harness.models === 'object' ? harness.models : {},
      switch_version: Number(harness.switch_version || 0),
      pending_handoff_id: String(harness.pending_handoff_id || '').trim(),
      handoff_notice_pending: Boolean(harness.handoff_notice_pending),
      summary_status: String(harness.summary_status || '') as HarnessStateProjection['summary_status'],
      truncated: Boolean(harness.truncated),
      warning_code: String(harness.warning_code || ''),
      previous_runtime_id: String(harness.previous_runtime_id || ''),
    };
  });
  const visibleHarnessNotice = createMemo(() => {
    const state = currentHarnessState();
    return Boolean(
      state.handoff_notice_pending
      && state.pending_handoff_id
      && state.pending_handoff_id !== dismissedHandoffNoticeId()
    );
  });
  const activeModel = createMemo(() => {
    const harnessModel = String(currentHarnessState().model_id || '').trim();
    const sessionModel = String(sessionStore.currentSession()?.model || '').trim();
    // selectedModel is updated only after the switch endpoint confirms.  A
    // background session refresh can still carry the prior harness projection,
    // so that stale projection must not roll the confirmed selection back.
    return resolveActiveModel(selectedModel(), harnessModel, sessionModel);
  });
  createEffect(() => {
    const session = sessionStore.currentSession();
    const raw = session?.metadata?.research_context?.assets;
    if (!Array.isArray(raw)) {
      setActiveResearchAssets([]);
      return;
    }
    setActiveResearchAssets(
      raw
        .filter((item: any) => item && typeof item === 'object' && String(item.asset_id || '').trim())
        .slice(0, 3)
        .map((item: any) => ({
          asset_id: String(item.asset_id || '').trim(),
          pack_id: String(item.pack_id || '').trim() || undefined,
          title: String(item.title || '导入研究资料').trim(),
          source_research_id: String(item.source_research_id || '').trim() || undefined,
        })),
    );
  });
  createEffect(() => {
    const raw = sessionStore.currentSession()?.metadata?.wiki_context?.page;
    if (!raw || typeof raw !== 'object' || !String(raw.page_id || '').trim()) {
      setActiveWikiPage(null);
      return;
    }
    setActiveWikiPage({
      page_id: String(raw.page_id || '').trim(),
      source_id: String(raw.source_id || '').trim() || undefined,
      title: String(raw.title || 'Wiki 页面').trim(),
      page_type: String(raw.page_type || '').trim() || undefined,
      status: String(raw.status || '').trim() || undefined,
      revision: Number(raw.revision || 0),
      access: String(raw.access || 'owner'),
    });
  });
  const visibleModelOptions = createMemo(() => {
    const availableModels = models();
    const currentModel = activeModel();
    if (!currentModel || availableModels.some((item) => item.id === currentModel)) {
      return availableModels;
    }
    return [
      {
        id: currentModel,
        provider_id: '',
        model_id: currentModel,
        name: currentModel,
      },
      ...availableModels,
    ];
  });
  const {
    activeGuidanceItems,
    latestActiveGuidance,
    guidanceStatusLabel,
    guidanceCancellable,
    refreshGuidanceQueue,
    handleDeleteGuidance,
    deletingGuidanceIds
  } = createGuidanceQueue();
  let textareaRef: HTMLTextAreaElement | undefined;
  const {
    composerCollapsed,
    isMinimalComposer,
    isTextareaResizing,
    manualTextareaHeight,
    autoSizeTextarea,
    resetManualTextareaHeight,
    startTextareaResize,
    resizeTextareaBy,
    getTextareaMinHeight,
    getTextareaMaxHeight,
  } = createComposerLayout({
    textarea: () => textareaRef,
  });

  const loadPluginCommandsForSession = async (workdir?: string | null) => {
    const commandData = await getPluginCommands(String(workdir || '').trim() || undefined);
    setPluginCommands(commandData);
    setHighlightedCommandIndex(prev => (commandData.length > 0 ? Math.min(prev, commandData.length - 1) : 0));
  };

  const loadAgentCatalogForSession = async (
    sessionId?: string | null,
    workdirName?: string | null,
  ) => {
    const generation = ++agentCatalogLoadGeneration;
    const [agentData, teamCatalog] = await Promise.all([
      getAgents(
        String(sessionId || '').trim() || undefined,
        String(workdirName || '').trim() || undefined,
      ),
      getTeamCatalog(
        String(sessionId || '').trim() || undefined,
        String(workdirName || '').trim() || undefined,
      ),
    ]);
    if (generation !== agentCatalogLoadGeneration) return;
    hydrateTeamCatalog(teamCatalog);

    // /api/agents applies Agent.hidden and already includes visible team aliases.
    // The team catalog also contains hidden teams for history and execution;
    // adding its entries here would bring hidden agents back into the selector.
    const selectableAgents = new Map<string, { name: string; description: string; mode: string }>();
    for (const agent of agentData) {
      const name = String(agent?.name || '').trim();
      if (!name) continue;
      selectableAgents.set(name, {
        name,
        description: String(agent?.description || ''),
        mode: String(agent?.mode || 'primary'),
      });
    }
    setAgents(Array.from(selectableAgents.values()));
  };

  const currentWorkspaceId = () => String(
    sessionStore.currentSession()?.workspace_id ||
    sessionStore.currentSession()?.workdir_name ||
    '',
  ).trim();

  const {
    fileMentionItems,
    highlightedFileMentionIndex,
    setHighlightedFileMentionIndex,
    isFileMentionMenuOpen,
    fileMentionLoading,
    fileMentionError,
    closeFileMentionMenu,
    refreshFileMentionMenu,
    applyFileMention,
    setFileMentionListRef,
  } = createFileMentions({
    workspaceId: currentWorkspaceId, input, setInput, attachedFiles, setAttachedFiles,
    textarea: () => textareaRef, autoSizeTextarea,
    closeCommandMenu: () => setIsCommandMenuOpen(false),
  });

  const buildFileRefs = () => buildMessageFileRefs(attachedFiles());
  const buildFinalMessage = () => buildMessagePayload(input(), attachedFiles(), structureRefs());

  const resetComposer = () => {
    closeFileMentionMenu();
    setInput('');
    setAttachedFiles([]);
    setStructureRefs([]);
    setShowUpload(false);
    if (textareaRef) {
      textareaRef.style.height = 'auto';
    }
  };

  const stagedGuidanceItems = () => composerDrafts.get(draftKey()).stagedGuidance || [];

  const handleGuideSubmit = async (
    mode: 'steer' | 'queue' = 'steer',
    staged?: StagedGuidance,
  ) => {
    if (selectedSkills().length) { setCommandFeedback('请等待当前任务完成后发送带 Skill 的任务'); return; }
    const sessionId = sessionStore.currentSessionId();
    const key = draftKey();
    const identity = composerIdentity();
    const snapshot: ComposerDraft = staged
      ? { text: staged.text, files: staged.files, structureRefs: staged.structureRefs || [] }
      : { text: input(), files: attachedFiles(), structureRefs: structureRefs() };
    const finalMessage = buildMessagePayload(snapshot.text, snapshot.files, snapshot.structureRefs || []);
    if (!sessionId || !finalMessage || pendingSubmissions.has(key)) return;
    const requestId = composerDrafts.guidanceRequest(key, snapshot, mode, generateId());
    const fileRefs = buildMessageFileRefs(snapshot.files);
    pendingSubmissions.add(key);
    setGuidanceSubmittingId(staged?.id || requestId);

    try {
      const resp = await guideRunningSession(finalMessage, {
        session_id: sessionId,
        client_request_id: requestId,
        mode,
        interrupt: false,
        cancel_descendants: false,
        descendant_policy: 'preserve',
        file_refs: fileRefs,
      });
      composerDrafts.guidanceAccepted(key, requestId);
      if (staged) composerDrafts.removeStagedGuidance(key, staged.id);
      else composerDrafts.accepted(key, snapshot);
      if (composerIdentity() !== identity) return;
      const acceptedMode = resp.accepted_mode || mode;
      sessionStore.addMessageForSession(sessionId, {
        id: generateId(),
        role: 'user',
        content: finalMessage,
        metadata: {
          guidance: {
            id: resp.guidance_id,
            sequence: resp.sequence,
            mode: acceptedMode,
            target_turn_id: resp.target_turn_id,
          },
        },
      });
      sessionStore.setSessionStatus(sessionId,
        resp.guidance_status === 'classifying'
          ? '正在理解追加指导'
          : acceptedMode === 'steer'
            ? '已收到，将在下一个安全点应用'
            : '已加入下一轮队列'
      );
      if (sessionStore.currentSessionId() === sessionId) void refreshGuidanceQueue(false);
    } catch (e) {
      console.error('Failed to guide running session:', e);
      const error = e instanceof Error ? `追加指导失败: ${e.message}` : '追加指导失败';
      if (!staged) composerDrafts.update(key, { failed: { ...snapshot, id: requestId, error } });
      if (composerIdentity() === identity) sessionStore.setSessionStatus(sessionId, error);
    } finally {
      pendingSubmissions.delete(key);
      setGuidanceSubmittingId(current => current === (staged?.id || requestId) ? '' : current);
    }
  };

  const stageRunningGuidance = () => {
    const finalMessage = buildFinalMessage();
    if (!finalMessage) return;
    if (attachedFiles().some(isImageAttachment)) {
      sessionStore.setStatus('运行中的追加指导暂不接收图片；请等待当前轮结束后再发送。');
      return;
    }
    const key = draftKey();
    composerDrafts.stageGuidance(
      key,
      { text: input(), files: attachedFiles(), structureRefs: structureRefs() },
      'steer',
      generateId(),
    );
    closeFileMentionMenu();
    setShowUpload(false);
    if (textareaRef) textareaRef.style.height = 'auto';
    sessionStore.setStatus('追加指导已暂存，点击输入框上方的“追加”后发送');
  };

  const resolveSideQuestionAgentName = () => {
    const currentAgentName = sessionStore.currentSession()?.agent_name || sessionStore.selectedAgentName();
    if (
      currentAgentName &&
      currentAgentName !== 'manager' &&
      !isFixedTeamBootstrapAgent(currentAgentName)
    ) {
      return currentAgentName;
    }
    return 'general';
  };

  const handleSideQuestionSubmit = async () => {
    if (selectedSkills().length) { setCommandFeedback('已选 Skill 的任务请使用当前会话的发送按钮'); return; }
    const parentSessionId = sessionStore.currentSessionId();
    const finalMessage = buildFinalMessage();
    if (!parentSessionId || !finalMessage || askingSideQuestion()) return;
    const fileRefs = buildFileRefs();

    const sideAgentName = resolveSideQuestionAgentName();
    const key = draftKey();
    const identity = composerIdentity();
    const snapshot = { text: input(), files: attachedFiles(), structureRefs: structureRefs() };
    setAskingSideQuestion(true);
    openSideChatWorkspace({
      parentSessionId,
      pending: true,
    });

    try {
      const resp = await askSideQuestion(parentSessionId, finalMessage, sideAgentName, {
        file_refs: fileRefs,
        createOnly: true,
      });
      if (!resp) {
        throw new Error('旁路问题没有返回结果');
      }
      if (composerIdentity() !== identity) return;
      if (sessionStore.currentSessionId() === parentSessionId) openSideChatWorkspace({parentSessionId,sessionId:resp.session_id});
      await chatWithSubagent(parentSessionId,resp.session_id,finalMessage,{integrateToParent:false,background:true});
      composerDrafts.accepted(key, snapshot);
      if (composerIdentity() !== identity) return;

      sessionStore.refreshSubagents();
      if (sessionStore.currentSessionId() === parentSessionId) openSideChatWorkspace({
        parentSessionId,
        sessionId: resp.session_id,
      });
    } catch (e) {
      console.error('Failed to ask side question:', e);
      const error = e instanceof Error ? `旁路提问失败: ${e.message}` : '旁路提问失败';
      composerDrafts.update(key, { failed: { ...snapshot, id: generateId(), error } });
    } finally {
      setAskingSideQuestion(false);
    }
  };

  const getCommandFullName = (command: PluginCommand): string =>
    command.name.includes(':') ? command.name : `${command.plugin}:${command.name}`;

  const getSlashCommandQuery = (value: string, caretPosition?: number): string | null => {
    return findCommandTrigger(value, caretPosition)?.query ?? null;
  };
  const availableSkillItems = (): CommandItem[] => skillCommandItems(skillCatalog(), sessionStore.selectedAgentName(), selectedRuntimeId());
  const filteredPluginCommands = () => {
    const view = commandView();
    if (view === 'models') {
      const options = selectedRuntimeId() === 'tcode-native' ? visibleModelOptions()
        : (runtimeOptions().find(runtime => runtime.runtime_id === selectedRuntimeId())?.models || [])
          .map(id => ({ id, name: id }));
      return filterCommands(options.map(model => ({ id: model.id, label: model.name || model.id, value: model.id,
        description: model.id, kind: 'model', current: model.id === activeModel(),
        disabled: sessionStore.isRunning() || modelChangePending() || runtimeChangePending() ? '请等待当前任务或切换完成' : undefined })), commandQuery());
    }
    if (view === 'skills') return filterCommands(availableSkillItems(), commandQuery());
    const trigger = findCommandTrigger(input(), textareaRef?.selectionStart);
    const query = view === 'help' ? commandQuery() : trigger?.query || '';
    if (trigger?.prefix === '$' && view === 'commands') return filterCommands(availableSkillItems(), query);
    const commands: CommandItem[] = builtinCommands.map(item => ({ ...item,
      disabled: item.id === 'compact' && (selectedRuntimeId() !== 'tcode-native' || sessionStore.isRunning())
        ? '请在空闲的 TCode 会话中压缩' : undefined }));
    return filterCommands([...commands, ...pluginCommands().map(command => ({
      id: getCommandFullName(command), label: `/${getCommandFullName(command)}`,
      description: command.description, kind: 'plugin' as const,
    })), ...(view === 'help' ? [] : availableSkillItems())], query);
  };
  const showCommandMenu = () => isCommandMenuOpen();
  const closeCommandMenu = () => {
    setIsCommandMenuOpen(false); setCommandView('commands'); setCommandQuery('');
    queueMicrotask(() => textareaRef?.focus());
  };
  const replaceCommandTrigger = (replacement = '') => {
    const current = input();
    const cursor = textareaRef?.selectionStart ?? current.length;
    const trigger = findCommandTrigger(current, cursor);
    const nextValue = trigger ? current.slice(0, trigger.start) + replacement + current.slice(trigger.end) : current;
    setInput(nextValue);
    queueMicrotask(() => {
      if (!textareaRef) return;
      textareaRef.value = nextValue;
      textareaRef.setSelectionRange((trigger?.start ?? cursor) + replacement.length, (trigger?.start ?? cursor) + replacement.length);
    });
  };
  const openCommandView = (view: 'models' | 'skills' | 'help') => {
    setCommandView(view); setCommandQuery(''); setHighlightedCommandIndex(0); setIsCommandMenuOpen(true);
  };
  const loadComposerSkills = async () => {
    setSkillsLoading(true); setSkillsError('');
    const identity = composerIdentity(); const key = catalogSessionKey();
    try {
      const workspace = sessionStore.currentSession()?.workspace_id || sessionStore.currentSession()?.workdir_name || '';
      const catalog = await getSkillCatalog(workspace);
      if (identity === composerIdentity() && key === catalogSessionKey()) setSkillCatalog(catalog.skills);
    } catch (error) {
      if (key === catalogSessionKey()) { setSkillCatalog([]); setSkillsError(error instanceof Error ? error.message : '技能加载失败'); }
    } finally { if (key === catalogSessionKey()) setSkillsLoading(false); }
  };
  const executeBuiltin = async (name: string, args = '') => {
    if (name === 'help') { openCommandView('help'); return; }
    if (name === 'skills' || name === 'skill') { openCommandView('skills'); void loadComposerSkills(); return; }
    if (name === 'model' && !args) { openCommandView('models'); return; }
    if (commandBusy()) return;
    const session = sessionStore.currentSession();
    const identity = composerIdentity();
    setCommandBusy(true); setCommandFeedback(''); closeCommandMenu();
    try {
      if (name === 'new') {
        const workspace = session?.workspace_id || session?.workdir_name;
        if (!workspace) throw new Error('请先选择工作空间');
        const created = await bootstrapSession(sessionStore.selectedAgentName(), undefined, workspace);
        if (identity !== composerIdentity()) return;
        sessionStore.upsertSession({ session_id: created.session_id, agent_name: created.agent_name,
          workspace_id: workspace, workdir_name: created.workdir_name || workspace, workdir: created.workdir,
          messages: [], created_at: Date.now(), updated_at: Date.now() });
        sessionStore.switchSession(created.session_id);
        setCommandFeedback('已在当前工作空间创建新会话');
      } else if (name === 'model') {
        if (sessionStore.isRunning()) throw new Error('请等待当前任务完成后切换模型');
        await handleModelChange(args);
        setCommandFeedback(modelLoadError() || `当前模型：${activeModel()}`);
      } else if (name === 'compact') {
        if (!session) throw new Error('请先创建会话');
        if (sessionStore.isRunning()) throw new Error('请等待当前任务完成后压缩');
        if (selectedRuntimeId() !== 'tcode-native') throw new Error('当前 Harness 不支持手动压缩');
        setCommandFeedback('正在压缩上下文…');
        const result = await compactSession(session.session_id);
        if (identity !== composerIdentity() || sessionStore.currentSessionId() !== session.session_id) return;
        setCommandFeedback(result.status === 'skipped' ? '当前上下文较短，无需压缩' : `上下文已压缩，节省约 ${Math.max(0, result.tokens_saved).toLocaleString()} tokens；历史已归档`);
      }
    } catch (error) { setCommandFeedback(error instanceof Error ? error.message : '命令执行失败'); }
    finally { setCommandBusy(false); }
  };
  const applyPluginCommand = (item: CommandItem) => {
    if (item.disabled) { setCommandFeedback(item.disabled); return; }
    if (item.kind === 'model') { void executeBuiltin('model', item.value); return; }
    if (item.kind === 'skill') {
      if (selectedSkills().length >= 8 && !selectedSkills().includes(item.value!)) { setCommandFeedback('每次最多选择 8 个技能'); return; }
      setSelectedSkills([...new Set([...selectedSkills(), item.value!])]);
      if (commandView() === 'commands') replaceCommandTrigger();
      closeCommandMenu(); return;
    }
    if (item.kind === 'builtin') { replaceCommandTrigger(); void executeBuiltin(item.id); return; }
    replaceCommandTrigger(`${item.label} `); closeCommandMenu();
  };
  const handleCommandKeys = (event: KeyboardEvent) => {
    if (event.isComposing) return;
    const items = filteredPluginCommands();
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (items.length) setHighlightedCommandIndex(index => (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length);
    } else if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault(); const item = items[highlightedCommandIndex()];
      if (item) {
        if (event.key === 'Tab' && item.kind === 'builtin' && commandView() === 'commands') { replaceCommandTrigger(`${item.label} `); closeCommandMenu(); }
        else applyPluginCommand(item);
      }
    } else if (event.key === 'Escape') {
      event.preventDefault();
      if (commandView() !== 'commands') { setCommandView('commands'); setCommandQuery(''); setHighlightedCommandIndex(0); textareaRef?.focus(); }
      else closeCommandMenu();
    }
  };

  const formatMentionFileSize = (size: number) => {
    if (!Number.isFinite(size) || size < 0) return '';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  };

  const openAttachmentPreview = (file: AttachedFile) => {
    if (file.path) {
      sessionStore.requestFilePreview(file.path);
      sessionStore.setStatus(`正在预览：${file.name}`);
      return;
    }
    sessionStore.setStatus('该附件无可预览路径');
  };

  const loadAvailableModels = async (sessionId?: string | null, refreshOnly = false) => {
    if (refreshOnly && selectedRuntimeId() !== 'tcode-native') return;
    const generation = ++modelLoadGeneration;
    setModelsLoading(true);
    const session = untrack(() => (
      sessionId
        ? sessionStore.sessions().find((item) => item.session_id === sessionId)
        : sessionStore.currentSession()
    ));
    const workspaceId = String(
      session?.workspace_id || session?.workdir_name || ''
    ).trim();
    try {
      const runtimeData = await getChatRuntimes(sessionId || undefined);
      if (generation !== modelLoadGeneration) return;
      const availableRuntimes = runtimeData.runtimes || [];
      setRuntimeOptions(availableRuntimes);
      setPreferredHarnessModel(runtimeData.preferred_model_id || 'deepseek/deepseek-v4-flash');
      setRuntimeLoadError('');
      if (selectedRuntimeId() !== 'tcode-native') {
        const runtime = availableRuntimes.find(
          item => item.runtime_id === selectedRuntimeId(),
        );
        if (!runtime?.available || !runtime.enabled) {
          throw new Error(runtime?.diagnostics?.[0] || '所选 Harness 未安装或未启用');
        }
        const runtimeModels = runtime.models.map(id => ({
          id,
          provider_id: id.includes('/') ? id.split('/', 1)[0] : '',
          model_id: id.includes('/') ? id.slice(id.indexOf('/') + 1) : id,
          name: id,
        }));
        setModels(runtimeModels);
        const storedModel = String(session?.metadata?.harness?.model_id || session?.model || '').trim();
        const preferred = runtimeData.preferred_model_id;
        const currentModel = (
          storedModel && runtime.models.includes(storedModel)
            ? storedModel
            : runtime.models.includes(preferred)
              ? preferred
              : runtime.models[0] || ''
        );
        setSelectedModel(currentModel);
        setModelLoadError('');
        setRuntimeLoadError('');
        return;
      }
      const modelData = await getModels(
        sessionId || undefined,
        workspaceId || undefined,
      );
      if (generation !== modelLoadGeneration) return;
      if (sessionId && sessionStore.currentSessionId() !== sessionId) return;

      setModelLoadError('');
      const availableModels = modelData.models || [];
      setModels(availableModels);
      // Refresh the catalogue without racing a model selection made while it loads.
      if (refreshOnly) return;
      const storedModel = String(
        untrack(() => sessionStore.sessions().find(
          (item) => item.session_id === sessionId,
        )?.model || ''),
      ).trim();
      const storedModelIsAvailable = Boolean(
        storedModel && availableModels.some((item) => item.id === storedModel)
      );
      const currentModel = (
        storedModelIsAvailable ? storedModel : modelData.current_model
      ) || (availableModels[0]?.id ?? '');
      setSelectedModel(currentModel);
      if (sessionId && currentModel && storedModel !== currentModel) {
        sessionStore.updateSessionModel(sessionId, currentModel);
      }
    } catch (error) {
      if (generation !== modelLoadGeneration) return;
      if (sessionId && sessionStore.currentSessionId() !== sessionId) return;
      const detail = error instanceof Error ? error.message : String(error || '未知错误');
      setModelLoadError(`模型列表加载失败：${detail}`);
      if (selectedRuntimeId() !== 'tcode-native') {
        setRuntimeLoadError(`Harness 加载失败：${detail}`);
      }
      console.error('Models error:', error);
    } finally {
      if (generation === modelLoadGeneration) setModelsLoading(false);
    }
  };

  onMount(() => {
    const dismissCommands = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Element && showCommandMenu() && target !== textareaRef
        && !target.closest('[data-testid="slash-command-menu"]')) {
        setIsCommandMenuOpen(false); setCommandView('commands'); setCommandQuery('');
      }
    };
    const focusComposer = () => {
      if (!textareaRef) return;
      textareaRef.focus();
      const cursor = textareaRef.value.length;
      textareaRef.setSelectionRange(cursor, cursor);
      textareaRef.scrollIntoView({ block: 'nearest' });
    };
    const prefillComposer = (event: Event) => {
      const detail = (event as CustomEvent<{ text?: string; append?: boolean }>).detail || {};
      const text = String(detail.text || '').trim();
      if (!text) return;
      setInput((current) => (
        detail.append && current.trim() ? `${current.trimEnd()}\n${text}` : text
      ));
      queueMicrotask(() => {
        if (!textareaRef) return;
        textareaRef.focus();
        textareaRef.value = input();
        const cursor = textareaRef.value.length;
        textareaRef.setSelectionRange(cursor, cursor);
        autoSizeTextarea(textareaRef);
      });
    };
    const setResearchContext = (event: Event) => {
      const detail = (event as CustomEvent<{
        assetId?: string;
        packId?: string;
        title?: string;
        sourceResearchId?: string;
      }>).detail || {};
      const assetId = String(detail.assetId || '').trim();
      if (!assetId) return;
      const selected: ResearchChatAsset = {
        asset_id: assetId,
        pack_id: String(detail.packId || '').trim() || undefined,
        title: String(detail.title || '导入研究资料').trim(),
        source_research_id: String(detail.sourceResearchId || '').trim() || undefined,
      };
      setActiveResearchAssets((current) => [
        selected,
        ...current.filter((item) => item.asset_id !== assetId),
      ].slice(0, 3));
    };
    const setWikiContext = (event: Event) => {
      const detail = (event as CustomEvent<Partial<WikiChatPage>>).detail || {};
      const pageId = String(detail.page_id || '').trim();
      if (!pageId) {
        updateLocalWikiContext(null);
        return;
      }
      updateLocalWikiContext({
        page_id: pageId,
        source_id: String(detail.source_id || '').trim() || undefined,
        title: String(detail.title || 'Wiki 页面').trim(),
        page_type: String(detail.page_type || '').trim() || undefined,
        status: String(detail.status || '').trim() || undefined,
        revision: Math.max(0, Number(detail.revision || 0)),
        access: String(detail.access || 'owner'),
      });
    };
    window.addEventListener('tcode-focus-composer', focusComposer as EventListener);
    document.addEventListener('pointerdown', dismissCommands);
    window.addEventListener('tcode-prefill-composer', prefillComposer as EventListener);
    window.addEventListener('tcode-set-research-context', setResearchContext as EventListener);
    window.addEventListener('tcode-set-wiki-context', setWikiContext as EventListener);
    onCleanup(() => {
      window.removeEventListener('tcode-focus-composer', focusComposer as EventListener);
      document.removeEventListener('pointerdown', dismissCommands);
      window.removeEventListener('tcode-prefill-composer', prefillComposer as EventListener);
      window.removeEventListener('tcode-set-research-context', setResearchContext as EventListener);
      window.removeEventListener('tcode-set-wiki-context', setWikiContext as EventListener);
    });

  });

  createEffect(on(
    () => sessionStore.currentSessionId(),
    (activeSessionId) => {
      inputHistoryState = { index: -1, draft: '' };
      setShowUpload(false);
      setLastSideQuestionResult(null);
      setDismissedHandoffNoticeId('');
      closeFileMentionMenu();
      const session = untrack(() => sessionStore.sessions().find(
        item => item.session_id === activeSessionId,
      ));
      const storedRuntime = String(session?.metadata?.harness?.runtime_id || 'tcode-native').trim();
      setSelectedRuntimeId(storedRuntime || 'tcode-native');
      void loadAvailableModels(activeSessionId || undefined).catch((e) => {
        console.error('Failed to refresh models:', e);
      });
    },
  ));

  createEffect(() => {
    const serverRuntime = currentHarnessState().runtime_id;
    if (!runtimeChangePending() && selectedRuntimeId() !== serverRuntime) {
      setSelectedRuntimeId(serverRuntime);
    }
  });

  createEffect(() => {
    const currentModel = activeModel();
    if (currentModel && selectedModel() !== currentModel) {
      setSelectedModel(currentModel);
    }
  });

  const catalogSessionKey = createMemo(() => {
    const session = sessionStore.currentSession();
    return JSON.stringify([session?.session_id, session?.workdir, session?.workdir_name]);
  });
  createEffect(on(catalogSessionKey, (key) => {
    const [sessionId, workdir, workdirName] = JSON.parse(key);
    void loadPluginCommandsForSession(workdir || undefined);
    void loadAgentCatalogForSession(sessionId, workdirName);
    setSkillCatalog([]);
    closeCommandMenu();
    setCommandFeedback('');
    void loadComposerSkills();
  }));

  const cleanupFixedTeamIfNeeded = async (
    existingSessionId: string | null,
    previousAgentName: string,
    nextAgentName: string,
  ) => {
    if (
      !existingSessionId ||
      !isFixedTeamBootstrapAgent(previousAgentName) ||
      previousAgentName === nextAgentName
    ) {
      return;
    }

    const result = await closeSessionSubagents(existingSessionId);
    const removedSessionIds = new Set<string>([
      ...(result.deleted_session_ids || []),
      ...((result.removed_worker_session_ids || []).map((item) => String(item || '').trim()).filter(Boolean)),
    ]);

    for (const sessionId of removedSessionIds) {
      sessionStore.removeSubagentSession(sessionId);
    }
    sessionStore.refreshSubagents();
  };

  const handleAgentChange = async (agentName: string) => {
    const existingSessionId = sessionStore.currentSessionId();
    const previousAgentName = sessionStore.currentSession()?.agent_name || sessionStore.selectedAgentName();

    sessionStore.setSelectedAgentName(agentName);

    if (usesFixedScheduler(agentName)) {
      sessionStore.setSelectedSchedulerMode('supervisor');
    }

    if (existingSessionId) {
      sessionStore.updateSessionAgent(existingSessionId, agentName);
      if (usesFixedScheduler(agentName)) {
        sessionStore.updateSessionSchedulerMode(existingSessionId, 'supervisor');
      }
    }

    if (
      existingSessionId &&
      isFixedTeamBootstrapAgent(previousAgentName) &&
      previousAgentName !== agentName
    ) {
      try {
        await cleanupFixedTeamIfNeeded(existingSessionId, previousAgentName, agentName);
      } catch (e) {
        console.error('Failed to close fixed team on agent switch:', e);
        sessionStore.setStatus('关闭固定团队失败');
      }
    }

    const fixedTeam = isFixedTeamBootstrapAgent(agentName);
    if (!existingSessionId) {
      if (fixedTeam) sessionStore.setStatus('请先创建并选择一个工作目录会话');
      return;
    }
    try {
      const resp = await bootstrapSession(agentName, existingSessionId);
      sessionStore.updateSessionAgent(existingSessionId, resp.agent_name || agentName);
      if (fixedTeam) {
        sessionStore.refreshSubagents();
        window.dispatchEvent(new CustomEvent('tcode-open-team-workspace'));
      }
    } catch (e) {
      console.error('Failed to switch agent:', e);
      sessionStore.setStatus(fixedTeam ? '固定团队初始化失败' : '模式切换同步失败');
    }

  };

  const handleReasoningEffortChange = (value: 'minimal' | 'low' | 'medium' | 'high') => {
    sessionStore.setSelectedReasoningEffort(value);
    const currentSessionId = sessionStore.currentSessionId();
    if (currentSessionId) {
      sessionStore.updateSessionReasoningEffort(currentSessionId, value);
    }
  };

  createEffect(() => {
    const commands = filteredPluginCommands();
    if (commands.length === 0) {
      setHighlightedCommandIndex(0);
      if (commandView() === 'commands' && getSlashCommandQuery(input(), textareaRef?.selectionStart) === null) {
        setIsCommandMenuOpen(false);
      }
      return;
    }
    setHighlightedCommandIndex(prev => Math.min(prev, commands.length - 1));
  });

  const handleRuntimeChange = async (runtimeId: string) => {
    if (runtimeChangePending()) return;
    const target = runtimeOptions().find(item => item.runtime_id === runtimeId);
    if (!target?.available || !target.enabled) {
      setRuntimeLoadError(target?.diagnostics?.[0] || '该 Harness 未安装或未启用');
      return;
    }
    const previousRuntime = selectedRuntimeId();
    const previousModel = activeModel();
    const sessionId = sessionStore.currentSessionId();
    if (!sessionId) {
      setRuntimeLoadError('请先创建会话再切换 Harness');
      return;
    }
    sessionStore.setHarnessSwitchPending(sessionId, true);
    setRuntimeChangePending(true);
    setRuntimeLoadError('');
    sessionStore.setStatus('正在准备 Harness 交接上下文');
    try {
      const preferred = preferredHarnessModel();
      const rememberedModel = String(currentHarnessState().models?.[runtimeId] || '').trim();
      const targetModel = runtimeId === 'tcode-native'
        ? rememberedModel || preferred
        : rememberedModel && target.models.includes(rememberedModel)
          ? rememberedModel
          : target.models.includes(preferred)
            ? preferred
            : target.models[0] || '';
      const response = await switchHarness(
        sessionId,
        {
          target_runtime_id: runtimeId,
          model_id: targetModel,
          expected_current_runtime_id: currentHarnessState().runtime_id,
          expected_switch_version: currentHarnessState().switch_version,
        },
        `switch-${generateId()}`,
      );
      sessionStore.applyServerHarnessState(sessionId, response);
      setSelectedRuntimeId(response.current_runtime_id);
      setDismissedHandoffNoticeId('');
      setSelectedModel(targetModel);
      await loadAvailableModels(sessionId);
      sessionStore.setStatus(
        response.handoff?.summary_status === 'fallback'
          ? `Harness 已切换：${target.display_name}，已使用基础上下文`
          : `Harness 已切换：${target.display_name}${targetModel ? ` · ${targetModel}` : ''}`,
      );
    } catch (error) {
      if (error instanceof HarnessSwitchApiError && error.currentState) {
        sessionStore.applyServerHarnessState(sessionId, error.currentState);
        setSelectedRuntimeId(error.currentState.runtime_id);
        setSelectedModel(error.currentState.model_id || previousModel);
      } else {
        setSelectedRuntimeId(previousRuntime);
        setSelectedModel(previousModel);
      }
      const detail = error instanceof Error ? error.message : String(error || '未知错误');
      setRuntimeLoadError(`Harness 切换失败：${detail}`);
      sessionStore.setStatus(`Harness 切换失败：${detail}`);
    } finally {
      sessionStore.setHarnessSwitchPending(sessionId, false);
      setRuntimeChangePending(false);
    }
  };

  const handleModelChange = async (model: string) => {
    if (sessionStore.isRunning()) { sessionStore.setStatus('任务运行中不能切换模型，请等待结束或先停止任务'); return; }
    if (modelChangePending() || runtimeChangePending() || model === activeModel()) return;
    setModelChangePending(true);
    // Invalidate a model-list response that started before this selection.
    modelLoadGeneration += 1;
    const prev = activeModel();
    const sessionId = sessionStore.currentSessionId() || undefined;
    const workspaceId = String(
      sessionStore.currentSession()?.workspace_id
      || sessionStore.currentSession()?.workdir_name
      || '',
    ).trim() || undefined;
    try {
      if (!sessionId) throw new Error('请先创建会话再切换模型');
      if (selectedRuntimeId() !== 'tcode-native') {
        const runtime = runtimeOptions().find(
          item => item.runtime_id === selectedRuntimeId(),
        );
        if (!runtime?.models.includes(model)) {
          throw new Error(`模型不属于 ${selectedRuntimeId()}：${model}`);
        }
        const response = await switchHarness(
          sessionId,
          {
            target_runtime_id: selectedRuntimeId(),
            model_id: model,
            expected_current_runtime_id: currentHarnessState().runtime_id,
            expected_switch_version: currentHarnessState().switch_version,
          },
          `model-${generateId()}`,
        );
        sessionStore.applyServerHarnessState(sessionId, response);
        setSelectedModel(response.model_id || model);
        recordModelSwitch(sessionId, prev, response.model_id || model);
        setModelLoadError('');
        sessionStore.setStatus(`模型已切换：${model}`);
        return;
      }
      const result = await selectModel(model, sessionId, workspaceId);
      const confirmedModel = result.model || model;
      const response = await switchHarness(
        sessionId,
        {
          target_runtime_id: 'tcode-native',
          model_id: confirmedModel,
          expected_current_runtime_id: currentHarnessState().runtime_id,
          expected_switch_version: currentHarnessState().switch_version,
        },
        `model-${generateId()}`,
      );
      sessionStore.applyServerHarnessState(sessionId, response);
      setSelectedModel(confirmedModel);
      recordModelSwitch(sessionId, prev, confirmedModel);
      await loadAvailableModels(sessionId);
      sessionStore.setStatus(`模型已切换：${confirmedModel}`);
    } catch (e) {
      console.error('Failed to switch model:', e);
      setSelectedModel(prev);
      const detail = e instanceof Error ? e.message : String(e || '未知错误');
      setModelLoadError(`切换模型失败：${detail}`);
      sessionStore.setStatus(`切换模型失败：${detail}`);
    } finally {
      setModelChangePending(false);
    }
  };

  const isLikelyTextFile = (file: File): boolean => {
    const name = file.name.toLowerCase();
    const binaryDocumentExts = [
      '.xlsx', '.xls', '.xlsm', '.docx', '.doc', '.pptx', '.ppt', '.pdf', '.zip'
    ];
    if (binaryDocumentExts.some(ext => name.endsWith(ext))) {
      return false;
    }

    const type = file.type.toLowerCase();
    if (
      type.startsWith('text/') ||
      type.includes('json') ||
      type.includes('xml') ||
      type.includes('javascript') ||
      type.includes('typescript') ||
      type.includes('yaml')
    ) {
      return true;
    }

    const textExts = [
      '.txt', '.md', '.py', '.js', '.ts', '.tsx', '.jsx', '.json', '.yaml', '.yml',
      '.xml', '.html', '.css', '.java', '.go', '.rs', '.sh', '.sql', '.toml', '.ini', '.cfg'
    ];
    return textExts.some(ext => name.endsWith(ext));
  };

  const switchFileMode = async (targetId: string, nextMode: 'path' | 'content') => {
    const target = attachedFiles().find(f => f.id === targetId);
    if (!target) return;
    if (target.portableRef) {
      sessionStore.setStatus('Artifact/Bundle 附件固定使用引用模式');
      return;
    }

    if (nextMode === 'content') {
      if (target.file && !isLikelyTextFile(target.file)) {
        sessionStore.setStatus(`${target.name} 是二进制文件，请保持路径模式`);
        return;
      }
      if (!target.content && target.file) {
        try {
          const content = await target.file.text();
          setAttachedFiles(prev =>
            prev.map(f => (f.id === targetId ? { ...f, content, mode: 'content' } : f))
          );
          return;
        } catch (e) {
          console.error('Failed to read file content:', e);
          setAttachedFiles(prev =>
            prev.map(f => (f.id === targetId ? { ...f, mode: 'path' } : f))
          );
          return;
        }
      }

      setAttachedFiles(prev =>
        prev.map(f => (f.id === targetId ? { ...f, mode: f.content ? 'content' : 'path' } : f))
      );
      return;
    }

    setAttachedFiles(prev => prev.map(f => (f.id === targetId ? { ...f, mode: 'path' } : f)));
  };

  const handleSubmit = async (e: Event) => {
    e.preventDefault();
    const builtin = parseBuiltinCommand(input());
    if (builtin) {
      if ((builtin.name === 'skill' || builtin.name === 'skills') && builtin.arguments) {
        const [name, ...task] = builtin.arguments.split(/\s+/);
        const item = availableSkillItems().find(item => item.value === name);
        if (!item || item.disabled) { setCommandFeedback(item?.disabled || `技能不可用：${name}`); return; }
        if (selectedSkills().length >= 8 && !selectedSkills().includes(name)) { setCommandFeedback('每次最多选择 8 个技能'); return; }
        setSelectedSkills([...new Set([...selectedSkills(), name])]); setInput(task.join(' '));
        if (!task.length) { closeCommandMenu(); return; }
      } else {
        if (builtin.arguments && !['model'].includes(builtin.name)) { setCommandFeedback(`/${builtin.name} 不接受参数`); return; }
        setInput(''); void executeBuiltin(builtin.name, builtin.arguments); return;
      }
    }
    if (commandBusy()) return;
    e.preventDefault();
    const finalMessage = buildFinalMessage();
    if (!finalMessage) return;
    const modelNoticeSession = sessionStore.currentSessionId();
    const modelNoticeIdentity = composerIdentity();
    const requestedModel = activeModel();
    if (modelNoticeSession && !sessionStore.isRunning()) {
      if (!await confirmModelSwitch(modelNoticeSession, requestedModel)) return;
      if (composerIdentity() !== modelNoticeIdentity || activeModel() !== requestedModel || sessionStore.isRunning()) return;
    }
    if (goalMode()) {
      if (selectedSkills().length) { setCommandFeedback('目标模式暂不支持指定 Skill，请让 Agent 按需加载技能。'); return; }
      if (attachedFiles().filter(isImageAttachment).length > 2) { setCommandFeedback('每轮最多上传 2 张图片供 Agent 视觉处理。'); return; }
      if (runtimeChangePending() || modelChangePending() || !submitGoalMessage) { sessionStore.setStatus('目标模式仍在加载，请稍后发送'); return; }
      const identity = composerIdentity();
      const sessionId = sessionStore.currentSessionId();
      if (await submitGoalMessage(finalMessage)) {
        if (identity === composerIdentity() && sessionId === sessionStore.currentSessionId() && buildFinalMessage() === finalMessage) resetComposer();
        sessionStore.setStatus(goalRunning() ? '指导已保存，将在下一轮接收' : '目标已提交');
      }
      return;
    }

    // Capture sticky context before sendMessage updates the session's running
    // state. That update can synchronously rerun the session metadata effects;
    // on a newly selected Wiki page the persisted context is not available yet,
    // so reading the signals later would incorrectly send `page: null`.
    const selectedResearchAssets = activeResearchAssets();
    const selectedWikiPage = activeWikiPage();
    const fileRefs = buildFileRefs();
    const submittedSessionId = sessionStore.currentSessionId();
    const submittedIdentity = composerIdentity();

    if (!sessionStore.currentSessionId()) {
      sessionStore.setStatus('请先在左侧创建会话并选择工作目录，再发送消息');
      return;
    }

    if (sessionStore.isRunning()) {
      if (selectedSkills().length) { setCommandFeedback('请等待当前任务完成后发送带 Skill 的任务'); return; }
      stageRunningGuidance();
      return;
    }

    if (runtimeChangePending() || modelChangePending()) {
      sessionStore.setStatus('Harness 或模型仍在切换，请等待服务端确认后再发送');
      return;
    }

    const imageFiles = attachedFiles().filter(isImageAttachment);
    if (imageFiles.length > 2) {
      sessionStore.setStatus('每轮最多上传 2 张图片供 Agent 视觉处理。');
      return;
    }
    if (currentHarnessState().pending_handoff_id) {
      setDismissedHandoffNoticeId(currentHarnessState().pending_handoff_id || '');
    }
    const submittedSkills = [...selectedSkills()];
    if (submittedSkills.length && selectedRuntimeId() !== 'tcode-native') { setCommandFeedback('当前 Harness 不支持 TCode Skill 选择'); return; }
    resetComposer();
    setSelectedSkills([]);

    void sendMessage(finalMessage, {
      agent_name: sessionStore.selectedAgentName(),
      selected_skills: submittedSkills,
      model: activeModel(),
      runtime_id: currentHarnessState().runtime_id,
      reasoning_effort: sessionStore.selectedReasoningEffort(),
      route_symphony_on_mcp_tool: sessionStore.selectedRouteSymphonyOnMcpTool(),
      ...(desktop ? { desktop_permission_mode: desktopPermissionMode() } : {}),
      scheduler_mode: sessionStore.selectedSchedulerMode(),
      research_context: {
        schema: 'tcode.research-chat-context.v1',
        mode: 'sticky',
        assets: selectedResearchAssets.map((item) => ({
          asset_id: item.asset_id,
          ...(item.source_research_id ? { source_research_id: item.source_research_id } : {}),
        })),
      },
      wiki_context: {
        schema: 'tcode.wiki-chat-context.v1',
        mode: 'sticky',
        page: selectedWikiPage
          ? {
              page_id: selectedWikiPage.page_id,
              ...(selectedWikiPage.source_id ? { source_id: selectedWikiPage.source_id } : {}),
            }
          : null,
      },
      file_refs: fileRefs,
    }).catch((error) => {
      console.error('Failed to send message:', error);
      if (submittedSessionId && submittedIdentity === composerIdentity()) {
        sessionStore.setSessionStatus(submittedSessionId, error instanceof Error ? error.message : '提交失败');
      }
    });
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    // Ignore Enter key during IME composition (e.g., Chinese/Japanese input)
    if (e.isComposing) {
      return;
    }
    if (e.key === 'Backspace' && !input() && selectedSkills().length && !showCommandMenu()) {
      e.preventDefault(); setSelectedSkills(selectedSkills().slice(0, -1)); return;
    }

    if (isFileMentionMenuOpen()) {
      const items = fileMentionItems();

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (items.length > 0) {
          setHighlightedFileMentionIndex(prev => (prev + 1) % items.length);
        }
        return;
      }

      if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (items.length > 0) {
          setHighlightedFileMentionIndex(prev => (prev - 1 + items.length) % items.length);
        }
        return;
      }

      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        const selected = items[highlightedFileMentionIndex()];
        if (selected) {
          applyFileMention(selected);
        }
        return;
      }

      if (e.key === 'Escape') {
        e.preventDefault();
        const current = input();
        const cursor = textareaRef?.selectionStart ?? current.length;
        const trigger = findFileMentionTrigger(current, cursor);
        closeFileMentionMenu();
        if (trigger) {
          const updated = removeFileMentionTrigger(current, trigger);
          setInput(updated.value);
          queueMicrotask(() => {
            if (!textareaRef) return;
            textareaRef.value = updated.value;
            textareaRef.selectionStart = updated.cursor;
            textareaRef.selectionEnd = updated.cursor;
          });
        }
        return;
      }
    }

    if (showCommandMenu()) {
      handleCommandKeys(e);
      if (e.defaultPrevented) return;
    }

    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      const textarea = e.currentTarget as HTMLTextAreaElement;
      const collapsed = textarea.selectionStart === textarea.selectionEnd;
      const onFirstLine = !textarea.value.slice(0, textarea.selectionStart).includes('\n');
      const navigating = inputHistoryState.index >= 0;
      if (collapsed && (navigating || (e.key === 'ArrowUp' && onFirstLine))) {
        const entries = sessionStore.currentSession()?.messages
          .filter(message => message.role === 'user' && message.content.trim())
          .map(message => message.content.trim()) || [];
        const result = navigateInputHistory(entries, input(), inputHistoryState, e.key === 'ArrowUp' ? 'older' : 'newer');
        if (result) {
          e.preventDefault(); inputHistoryState = result.state; setInput(result.value);
          queueMicrotask(() => { if (!textareaRef) return; textareaRef.value = result.value; textareaRef.setSelectionRange(result.value.length, result.value.length); autoSizeTextarea(textareaRef); });
          return;
        }
      }
    }

    if (!goalMode() && sessionStore.isRunning() && e.key === 'Enter' && e.altKey) {
      e.preventDefault();
      void handleSideQuestionSubmit();
      return;
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleInput = (e: Event) => {
    const target = e.target as HTMLTextAreaElement;
    inputHistoryState = { index: -1, draft: '' };
    setInput(target.value);
    setCommandView('commands'); setCommandQuery(''); setHighlightedCommandIndex(0);
    const fileMention = findFileMentionTrigger(target.value, target.selectionStart);
    if (fileMention) {
      refreshFileMentionMenu(target.value, target.selectionStart);
    } else {
      closeFileMentionMenu();
      setIsCommandMenuOpen(getSlashCommandQuery(target.value, target.selectionStart) !== null);
    }
    autoSizeTextarea(target);
  };

  const handleTextareaClick = (e: Event) => {
    const target = e.currentTarget as HTMLTextAreaElement;
    if (findFileMentionTrigger(target.value, target.selectionStart)) {
      refreshFileMentionMenu(target.value, target.selectionStart);
    } else {
      closeFileMentionMenu();
      setIsCommandMenuOpen(getSlashCommandQuery(target.value, target.selectionStart) !== null);
    }
  };

  const handleCancel = async () => {
    if (cancelling()) return;
    setCancelling(true);
    try {
      await cancelMessage();
      void refreshGuidanceQueue(false);
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div data-composer class="shrink-0 border-t px-3 pb-2 pt-0 backdrop-blur-xl md:px-4" style={{ background: 'color-mix(in srgb, var(--tcode-bg) 72%, transparent)', 'border-color': 'var(--tcode-border)' }}>
      <GoalModePanel desktopPermissionMode={desktop ? desktopPermissionMode() : undefined} controlMount={goalControlMount()} sessionId={sessionStore.currentSessionId() || undefined} workspaceId={currentWorkspaceId()} identityKey={composerIdentity()} native={currentHarnessState().runtime_id === 'tcode-native'} onRunning={setGoalRunning} onMode={setGoalMode} onSubmitReady={handler => {submitGoalMessage = handler;}} />
      <div class="composer-resize-row mx-auto flex max-w-6xl items-center gap-2">
        <div role="separator" tabindex="0" aria-label="调整输入区高度" aria-orientation="horizontal"
          aria-valuemin={getTextareaMinHeight()} aria-valuemax={getTextareaMaxHeight()}
          aria-valuenow={Math.round(manualTextareaHeight() ?? getTextareaMinHeight())}
          class="group flex h-5 flex-1 cursor-row-resize touch-none items-center justify-center"
          onPointerDown={startTextareaResize} onDblClick={resetManualTextareaHeight}
          onKeyDown={event=>{
            if (!['ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
            event.preventDefault();
            if (event.key==='Home') resetManualTextareaHeight();
            else resizeTextareaBy(event.key==='End'?getTextareaMaxHeight():event.key==='ArrowUp'?24:-24);
          }}
          title="向上拖动放大输入区，向下缩小；双击恢复默认；方向键微调">
          <span class={`h-1 w-16 rounded-full ${isTextareaResizing() ? 'bg-[#38bdf8]' : 'bg-[#2a3136] group-hover:bg-[#4d5961]'}`} />
        </div>
        <Show when={manualTextareaHeight() !== null}><button type="button" class="text-[10px]" onClick={resetManualTextareaHeight}>恢复默认高度</button></Show>
      </div>
      <form onSubmit={handleSubmit} class="mx-auto max-w-6xl">
        <Show when={commandFeedback()}><div role="status" class="mb-2 flex items-center justify-between gap-2 text-xs text-[#aab6bf]">
          <span>{commandFeedback()}</span><button type="button" aria-label="关闭命令提示" onClick={() => setCommandFeedback('')}>×</button>
        </div></Show>
        <Show when={selectedSkills().length}><div class="mb-2 flex flex-wrap gap-2">
          <For each={selectedSkills()}>{name => <span data-testid="selected-skill" class="composer-skill-chip">
            <span>${name}</span><button type="button" aria-label={`移除技能 ${name}`} onClick={() => setSelectedSkills(selectedSkills().filter(item => item !== name))}>×</button>
          </span>}</For>
        </div></Show>
        <Show when={structureRefs().length > 0}><div class="mb-2 flex flex-wrap items-center gap-2" aria-label="已加入提问的结构位点">
          <For each={structureRefs()}>{ref => <span class="inline-flex items-center gap-1 rounded-full border border-[#38bdf8]/40 bg-[#38bdf8]/10 px-2 py-1 text-xs text-[#c8ecff]">
            <button type="button" title={`在三维结构中聚焦 ${ref.path} ${ref.chain}:${ref.residue_number}`} onClick={() => window.dispatchEvent(new CustomEvent('tcode-focus-structure-ref', { detail: ref }))}>{ref.label} · {ref.chain || '_'}:{ref.name}{ref.residue_number}{ref.insertion_code}</button>
            <button type="button" aria-label={`移除 ${ref.label}`} onClick={() => setStructureRefs(structureRefs().filter(item => item.id !== ref.id))}>×</button>
          </span>}</For>
        </div></Show>
        <Show when={composerDrafts.get(draftKey()).failed}>
          {(failure) => <div role="alert" class="mb-3 rounded-xl border border-red-500/40 p-3 text-sm text-red-300">
            {failure().error}。内容已保留，可恢复后重试。
            <button type="button" class="ml-2 underline" onClick={() => composerDrafts.restoreFailed(draftKey())}>恢复提交内容</button>
          </div>}
        </Show>
        <Show when={activeWikiPage()}>
          {(page) => (
            <div class="mb-2 flex flex-wrap items-center gap-2 rounded-xl border border-[#a78bfa]/30 bg-[#a78bfa]/8 px-3 py-2">
              <span class="text-[10px] font-semibold uppercase tracking-[0.14em] text-[#c4b5fd]">
                当前 Wiki
              </span>
              <span class="inline-flex min-w-0 items-center gap-1.5 rounded-full border border-[#a78bfa]/30 bg-[#a78bfa]/10 px-2.5 py-1 text-xs text-[#eee8ff]">
                <span class="max-w-[320px] truncate">{page().title}</span>
                <Show when={page().access === 'shared_read_only'}>
                  <span class="text-[9px] text-[#ffc176]">共享只读</span>
                </Show>
                <button
                  type="button"
                  class="rounded-full px-1 text-[#9d8fc2] hover:bg-[#a78bfa]/15 hover:text-white"
                  aria-label={`移除 Wiki 上下文 ${page().title}`}
                  title="从后续对话背景中移除"
                  onClick={() => updateLocalWikiContext(null)}
                  disabled={sessionStore.isRunning()}
                >
                  ×
                </button>
              </span>
              <span class="text-[11px] text-[#9085ad]">只传定位信息；内容由 Agent 按需检索</span>
            </div>
          )}
        </Show>
        <Show when={activeResearchAssets().length > 0}>
          <div class="mb-2 flex flex-wrap items-center gap-2 rounded-xl border border-[#31536a] bg-[#10202a] px-3 py-2">
            <span class="text-[10px] font-semibold uppercase tracking-[0.14em] text-[#8ed5ff]">
              本会话背景资料
            </span>
            <For each={activeResearchAssets()}>
              {(asset) => (
                <span class="inline-flex min-w-0 items-center gap-1.5 rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2.5 py-1 text-xs text-[#c8ecff]">
                  <span class="max-w-[280px] truncate">{asset.title}</span>
                  <button
                    type="button"
                    class="rounded-full px-1 text-[#7fa5b9] hover:bg-[#38bdf8]/15 hover:text-white"
                    aria-label={`移除研究资料 ${asset.title}`}
                    title="从后续对话背景中移除"
                    onClick={() => setActiveResearchAssets((current) => current.filter((item) => item.asset_id !== asset.asset_id))}
                    disabled={sessionStore.isRunning()}
                  >
                    ×
                  </button>
                </span>
              )}
            </For>
            <span class="text-[11px] text-[#7f9aaa]">自动检索已有索引；过期索引在后台刷新</span>
          </div>
        </Show>
        <Show when={visibleHarnessNotice()}>
          <HarnessSwitchNotice
            state={currentHarnessState()}
            onDismiss={() => setDismissedHandoffNoticeId(
              currentHarnessState().pending_handoff_id || '',
            )}
          />
        </Show>
        <InteractionPanel mode="permissions" />

        <Show when={latestActiveGuidance()}>
          {(item) => (
            <div
              data-guidance-pending-strip
              class="mb-2 flex min-w-0 items-center gap-2 px-1 text-[11px] text-[#8b949e]"
              role="status"
            >
              <span class="h-2 w-2 shrink-0 animate-pulse rounded-full bg-[#8ed5ff]" />
              <span class="shrink-0 text-[#b8c8d2]">{guidanceStatusLabel(item().status)}</span>
              <span class="min-w-0 flex-1 truncate" title={item().message}>{item().message}</span>
              <Show when={activeGuidanceItems().length > 1}>
                <span class="shrink-0">+{activeGuidanceItems().length - 1}</span>
              </Show>
              <Show when={guidanceCancellable(item().status)}>
                <button
                  type="button"
                  class="shrink-0 text-[#9faeb7] transition-colors hover:text-[#ffb6b0] disabled:opacity-50"
                  disabled={deletingGuidanceIds().has(item().id)}
                  onClick={() => void handleDeleteGuidance(item().id)}
                  title="取消这条追加指导"
                  aria-label="取消追加指导"
                >
                  {deletingGuidanceIds().has(item().id) ? '取消中' : '取消'}
                </button>
              </Show>
            </div>
          )}
        </Show>

        <Show when={attachedFiles().length > 0}>
          <div class="mb-4 space-y-2">
            <For each={attachedFiles()}>
              {(file) => (
                <div class="flex flex-col justify-between gap-3 rounded-2xl border border-[#2a3136] bg-[#13191d] px-4 py-3 md:flex-row md:items-center">
                  <div class="flex min-w-0 flex-1 items-center gap-3">
                    <Show when={file.path && isImageFileRef(file.path)}>
                      <div class="relative h-20 w-20 shrink-0 rounded-xl border border-[#303a41] bg-[#0b1013]">
                        <ZoomableImage
                          src={getInlineFileUrl(file.path || '', currentWorkspaceId())}
                          alt={file.name || '上传图片预览'}
                          downloadName={file.name}
                          class="h-full w-full rounded-xl object-contain"
                          loading="lazy"
                        />
                        <button type="button" title="删除图片附件" aria-label={`删除图片：${file.name}`} class="absolute -right-2 -top-2 flex h-6 w-6 items-center justify-center rounded-full border border-[#5a2d32] bg-[#2d1418] text-[#ffb6b0] hover:bg-[#3a171c]" onClick={() => setAttachedFiles(prev => prev.filter(f => f.id !== file.id))}>×</button>
                      </div>
                    </Show>
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4 text-[#8ed5ff]">
                          <path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.9 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6Zm2 16H8v-2h8v2Zm0-4H8v-2h8v2Zm-3-5V3.5L18.5 9H13Z" />
                        </svg>
                        <button
                          type="button"
                          class="truncate text-left text-sm font-semibold text-[#8ed5ff] hover:underline"
                          onClick={() => openAttachmentPreview(file)}
                          title="预览文件"
                        >
                          {file.name}
                        </button>
                        <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-1 text-[10px] text-[#9faeb7]">
                          {file.source === 'mention'
                            ? '@ Workspace 文件'
                            : file.mode === 'path' ? '路径模式' : '内容模式'}
                        </span>
                      </div>
                      <Show when={file.path}>
                        <div class="mt-1 truncate text-xs text-[#8b949e]">{file.path}</div>
                      </Show>
                    </div>
                  </div>

                  <div class="flex items-center gap-2">
                    <Show when={file.source !== 'mention' && !file.portableRef && !isImageAttachment(file)}>
                      <button
                        type="button"
                        class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                        onClick={() => switchFileMode(file.id, file.mode === 'path' ? 'content' : 'path')}
                        title="切换附件传递模式"
                      >
                        切换为{file.mode === 'path' ? '内容' : '路径'}
                      </button>
                    </Show>
                    <Show when={!file.path || !isImageFileRef(file.path)}><button
                      type="button"
                      onClick={() => setAttachedFiles(prev => prev.filter(f => f.id !== file.id))}
                      class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-[#5a2d32] bg-[#2d1418] text-[#ffb6b0] transition-colors hover:bg-[#3a171c]"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
                        <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12Z" />
                      </svg>
                    </button></Show>
                  </div>
                </div>
              )}
            </For>
          </div>
        </Show>

        <Show when={sketchSessionId()}>
          <SketchPanel onClose={() => setSketchSessionId('')} onComplete={attachSketch} />
        </Show>
        <Show when={showUpload()}>
          <div id={uploadPanelId} role="region" aria-label="上传附件" class="mb-4 rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
            <div class="mb-3 flex items-center justify-between gap-3">
              <span class="text-sm font-semibold" style={{ color:'var(--tcode-text)' }}>上传附件</span>
              <button type="button" class="composer-upload-close" onClick={() => setShowUpload(false)} aria-label="关闭上传区域">
                <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>关闭
              </button>
            </div>
            <FileUpload
              onUpload={async (files: UploadedFile[]) => {
                const newFiles = files.map(uploadedFile => ({
                  id: generateId(),
                  name: uploadedFile.filename,
                  path: uploadedFile.path,
                  file: uploadedFile.file,
                  mode: 'path' as const,
                  source: 'upload' as const,
                  sessionId: uploadedFile.sessionId,
                  portableRef: uploadedFile.artifactRef || uploadedFile.bundleRef,
                }));
                setAttachedFiles(prev => [...prev, ...newFiles]);
              }}
            />
          </div>
        </Show>

        <Show when={lastSideQuestionResult()}>
          {(item) => (
            <div class="mb-4 rounded-2xl border border-[#7b61ff]/28 bg-[#18122a] px-4 py-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div class="min-w-0">
                  <div class="text-xs font-semibold uppercase tracking-[0.16em] text-[#c7b8ff]">
                    {item().displayName} · {item().sessionId}
                  </div>
                  <div class="mt-1 line-clamp-1 text-[11px] text-[#b7acd9]">
                    旁支已创建：{item().prompt}
                  </div>
                </div>
                <div class="flex items-center gap-2">
                  <button
                    type="button"
                    class="rounded-full border border-[#7b61ff]/35 bg-[#7b61ff]/14 px-3 py-1.5 text-xs font-semibold text-[#d8ceff]"
                    onClick={() => openSideChatWorkspace({
                      parentSessionId: sessionStore.currentSessionId() || '',
                      sessionId: item().sessionId,
                    })}
                    title="打开旁支聊天"
                  >
                    打开旁支
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-[#4b3a77] bg-[#211933] px-3 py-1.5 text-xs font-semibold text-[#c7b8ff]"
                    onClick={() => setLastSideQuestionResult(null)}
                    title="关闭提示"
                  >
                    关闭
                  </button>
                </div>
              </div>
            </div>
          )}
        </Show>

        <Show when={stagedGuidanceItems().length > 0}>
          <div
            data-guidance-staging
            class="mb-2 overflow-hidden rounded-xl border"
            style={{ background: 'color-mix(in srgb, var(--tcode-surface) 90%, transparent)', 'border-color': 'var(--tcode-border)' }}
            aria-label="待确认的追加指导"
          >
            <For each={stagedGuidanceItems()}>
              {(item, index) => (
                <div class="flex min-w-0 items-center gap-2 px-2.5 py-2" classList={{ 'border-t': index() > 0 }} style={{ 'border-color': 'var(--tcode-border)' }}>
                  <span class="shrink-0 rounded-md bg-[#38bdf8]/10 px-2 py-1 text-[10px] font-semibold text-[#8ed5ff]">
                    待追加
                  </span>
                  <span class="min-w-0 flex-1 truncate text-xs" style={{ color: 'var(--tcode-text-muted)' }} title={item.text}>
                    {item.text}
                  </span>
                  <button
                    type="button"
                    data-testid="confirm-staged-guidance"
                    class="shrink-0 rounded-md px-2.5 py-1.5 text-xs font-semibold text-[#8ed5ff] transition-colors hover:bg-[#38bdf8]/12 active:translate-y-px disabled:cursor-wait disabled:opacity-50"
                    disabled={Boolean(guidanceSubmittingId())}
                    onClick={() => void handleGuideSubmit(item.mode, item)}
                    title="现在追加到运行中的任务"
                  >
                    {guidanceSubmittingId() === item.id ? '追加中' : '追加'}
                  </button>
                  <button
                    type="button"
                    class="shrink-0 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-white/5"
                    style={{ color: 'var(--tcode-text-muted)' }}
                    disabled={Boolean(guidanceSubmittingId())}
                    onClick={() => {
                      composerDrafts.restoreStagedGuidance(draftKey(), item.id);
                      queueMicrotask(() => {
                        textareaRef?.focus();
                        if (textareaRef) autoSizeTextarea(textareaRef);
                      });
                    }}
                    title="退回输入框继续编辑"
                  >
                    编辑
                  </button>
                </div>
              )}
            </For>
          </div>
        </Show>

        <div class={`composer-input-card rounded-xl border shadow-[0_12px_30px_rgba(0,0,0,0.16)] backdrop-blur-xl ${isMinimalComposer() ? 'p-2' : 'p-2.5 md:p-3'}`} style={{ background: 'color-mix(in srgb, var(--tcode-surface) 82%, transparent)', 'border-color': 'var(--tcode-border)' }}>
          <div class="relative">
            <textarea
              ref={textareaRef}
              value={input()}
              onInput={handleInput}
              onKeyDown={handleKeyDown}
              onClick={handleTextareaClick}
              placeholder="下达新指令，输入 / 选择命令、$ 选择 Skill、@ 引用文件…"
              rows={1}
              class={`w-full resize-none border-0 bg-transparent px-1 outline-none placeholder:text-[var(--tcode-text-muted)] ${isMinimalComposer() ? 'py-1 text-[14px] leading-6' : 'py-1.5 text-[15px] leading-6'}`}
              style={{ resize: 'none', 'overflow-y': 'auto', color: 'var(--tcode-text)' }}
            />

            <Show when={isFileMentionMenuOpen()}>
              <div class="absolute bottom-full left-0 right-0 z-30 mb-3 overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d] shadow-[0_20px_40px_rgba(0,0,0,0.28)]">
                <div class="flex items-center justify-between gap-3 border-b border-[#2a3136] bg-[#0f1418] px-4 py-3">
                  <span class="text-[11px] uppercase tracking-[0.18em] text-[#8b949e]">
                    Workspace 文件 · @ 选择
                  </span>
                  <span class="text-[10px] text-[#6f7d87]">↑↓ 选择 · Enter 引用 · Esc 关闭</span>
                </div>
                <div
                  ref={setFileMentionListRef}
                  class="tcode-scrollbar max-h-[272px] overflow-y-auto py-2"
                >
                  <Show when={fileMentionLoading()}>
                    <div class="px-4 py-4 text-xs text-[#9faeb7]">正在搜索当前 Workspace...</div>
                  </Show>
                  <Show when={!fileMentionLoading() && Boolean(fileMentionError())}>
                    <div class="px-4 py-4 text-xs text-[#ffb6b0]">{fileMentionError()}</div>
                  </Show>
                  <Show when={!fileMentionLoading() && !fileMentionError() && fileMentionItems().length === 0}>
                    <div class="px-4 py-4 text-xs text-[#9faeb7]">没有匹配的文件</div>
                  </Show>
                  <For each={fileMentionItems()}>
                    {(item, index) => (
                      <button
                        type="button"
                        data-file-mention-index={index()}
                        class={`h-16 w-full px-4 py-3 text-left transition-colors ${index() === highlightedFileMentionIndex() ? 'bg-[#182027]' : 'hover:bg-[#171f24]'}`}
                        onMouseEnter={() => setHighlightedFileMentionIndex(index())}
                        onMouseDown={(event) => {
                          event.preventDefault();
                          applyFileMention(item);
                        }}
                      >
                        <div class="flex items-center justify-between gap-4">
                          <div class="min-w-0 flex-1">
                            <div class="truncate text-sm font-semibold text-[#e6edf3]">{item.name}</div>
                            <div class="mt-1 truncate font-mono text-[11px] text-[#8b949e]">{item.path}</div>
                          </div>
                          <div class="flex shrink-0 items-center gap-2 text-[10px] text-[#6f7d87]">
                            <span>{item.origin === 'uploaded' ? '已上传' : '新产物'}</span>
                            <span>{formatMentionFileSize(item.size)}</span>
                          </div>
                        </div>
                      </button>
                    )}
                  </For>
                </div>
              </div>
            </Show>

            <Show when={showCommandMenu()}>
              <SlashCommandMenu title={commandView() === 'models' ? '选择模型' : commandView() === 'skills' ? '选择 Skill' : commandView() === 'help' ? '命令帮助 · 选择条目执行' : '命令与 Skills'}
                query={commandQuery()} secondary={commandView() !== 'commands'} items={filteredPluginCommands()}
                index={highlightedCommandIndex()} loading={commandView() === 'skills' ? skillsLoading() : commandView() === 'models' ? modelsLoading() : false}
                error={commandView() === 'models' ? modelLoadError() : commandView() === 'skills' ? skillsError() : ''}
                onQuery={query => { setCommandQuery(query); setHighlightedCommandIndex(0); }} onHighlight={setHighlightedCommandIndex}
                onSelect={applyPluginCommand} onBack={() => { setCommandView('commands'); setCommandQuery(''); setHighlightedCommandIndex(0); textareaRef?.focus(); }}
                onKeyDown={handleCommandKeys} onManageSkills={commandView() === 'skills' ? () => { closeCommandMenu(); props.onOpenSkills?.(); } : undefined} />
            </Show>
          </div>

          <div class="composer-config-row">
            <details class="composer-goal-slot" ref={element => onMount(() => {
              const pane = element.closest('.composer-input-card')!;
              let compact: boolean | undefined;
              const observer = new ResizeObserver(() => {
                const next = pane.clientWidth - 24 <= 620;
                if (next !== compact) element.open = !next;
                compact = next;
              });
              observer.observe(pane);
              const dismiss = (event: PointerEvent) => {
                if (compact && element.open && !element.contains(event.target as Node)) element.open = false;
              };
              const escape = (event: KeyboardEvent) => {
                if (compact && element.open && event.key === 'Escape') {
                  element.open = false;
                  element.querySelector('summary')?.focus();
                }
              };
              document.addEventListener('pointerdown', dismiss);
              element.addEventListener('keydown', escape);
              onCleanup(() => {
                observer.disconnect();
                document.removeEventListener('pointerdown', dismiss);
                element.removeEventListener('keydown', escape);
              });
            })}>
              <summary aria-label="展开目标模式" title="目标模式">◎</summary>
              <div ref={setGoalControlMount} class="composer-goal-control" />
            </details>
            <ExecutionSettings
              agents={agents}
              handleAgentChange={handleAgentChange}
              handleReasoningEffortChange={handleReasoningEffortChange}
              runtime={{selected:selectedRuntimeId,available:runtimeOptions,error:runtimeLoadError,pending:runtimeChangePending,onChange:handleRuntimeChange}}
              model={{selected:activeModel,available:models,visible:visibleModelOptions,error:modelLoadError,pending:modelChangePending,onChange:handleModelChange,onRefresh:()=>{if(!modelsLoading()) void loadAvailableModels(sessionStore.currentSessionId(), true);}}}
            />
          </div>
          <div class="composer-toolbar">
            <div class="composer-tools-left">
              <button
                type="button"
                onClick={() => setShowUpload(!showUpload())}
                class="composer-upload-toggle inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[#3e484f] bg-[#171c20] text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
                title={showUpload() ? '收起上传区域' : '添加附件'}
                aria-label={showUpload() ? '收起上传区域' : '添加附件'}
                aria-expanded={showUpload()}
                aria-controls={uploadPanelId}
              >
                <Show when={showUpload()} fallback={
                <svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-6 w-6">
                  <path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5a2.5 2.5 0 0 1 5 0v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5a2.5 2.5 0 0 0 5 0V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6H16.5z" />
                </svg>
                }><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" class="h-6 w-6"><path d="m6 6 12 12M18 6 6 18" /></svg></Show>
              </button>
              <button
                type="button"
                onClick={() => setSketchSessionId(sessionStore.currentSessionId() || '')}
                disabled={!sessionStore.currentSessionId()}
                class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[#3e484f] bg-[#171c20] text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-40"
                title="绘制草图"
                aria-label="绘制草图"
              >
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="h-6 w-6"><path d="M3 17C8 10 13 2 15 4S5 20 10 20s9-14 11-10-5 8-1 9" /></svg>
              </button>
              <Show when={desktop}>
                <select
                  aria-label="权限模式"
                  title="请求批准：包括文件读取在内的操作按权限规则确认；目标后台遇到审批会停下。自动批准不绕过拒绝规则或沙箱。"
                  class="h-10 rounded-full border border-[#3e484f] bg-[#171c20] px-3 text-sm font-medium text-[#bdc8d1]"
                  value={desktopPermissionMode()}
                  onChange={(event) => updateDesktopPermissionMode(event.currentTarget.value as 'ask' | 'auto')}
                  disabled={sessionStore.isRunning()}
                >
                  <option value="ask">请求批准</option>
                  <option value="auto">自动批准</option>
                </select>
              </Show>
              <span class="composer-key-hint">{goalMode() ? (goalRunning() ? '回车补充指导' : '回车发送目标或回答') : sessionStore.isRunning() ? '回车暂存' : '回车发送'}</span>
              <Show when={!composerCollapsed()}>
                <span>Shift+回车换行</span>
              </Show>
              <Show when={sessionStore.isRunning() && !goalMode()}>
                <Show when={!composerCollapsed()}>
                  <span>Alt+回车旁路提问</span>
                </Show>
              </Show>
            </div>

            <div class="composer-tools-right">
            <Show
              when={sessionStore.isRunning()}
              fallback={
                <button
                  type="submit"
                  disabled={
                    runtimeChangePending()
                    || modelChangePending()
                    || (!input().trim() && attachedFiles().length === 0)
                  }
                  class={`inline-flex items-center justify-center gap-2 rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 font-semibold text-[#9ee6bb] disabled:opacity-50 ${isMinimalComposer() ? 'px-3 py-2 text-xs' : 'px-5 py-3 text-sm'}`}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class={isMinimalComposer() ? 'h-3.5 w-3.5' : 'h-4 w-4'}>
                    <path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z" />
                  </svg>
                  {isMinimalComposer() ? '发送' : '发送指令'}
                </button>
              }
            >
              <div class="flex flex-wrap items-center gap-2">
                <button
                  type="submit"
                  disabled={
                    runtimeChangePending()
                    || modelChangePending()
                    || (!input().trim() && attachedFiles().length === 0)
                  }
                  class={`inline-flex items-center justify-center gap-2 rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 font-semibold text-[#8ed5ff] disabled:opacity-50 ${isMinimalComposer() ? 'px-3 py-2 text-xs' : 'px-4 py-2.5 text-sm'}`}
                  title={goalMode() ? '指导保存到当前目标，在下一轮接收' : '先暂存到输入框上方，确认后追加'}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class={isMinimalComposer() ? 'h-3.5 w-3.5' : 'h-4 w-4'}>
                    <path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z" />
                  </svg>
                  {goalMode() ? '发送指导' : isMinimalComposer() ? '暂存' : '暂存指导'}
                </button>
                <Show when={!goalMode()}>
                <button
                  type="button"
                  onClick={() => {
                    void handleSideQuestionSubmit();
                  }}
                  disabled={askingSideQuestion() || (!input().trim() && attachedFiles().length === 0)}
                  class={`inline-flex items-center justify-center rounded-full border border-[#7b61ff]/28 bg-[#7b61ff]/12 font-semibold text-[#d8ceff] disabled:opacity-50 ${isMinimalComposer() ? 'px-3 py-2 text-xs' : 'px-4 py-2.5 text-sm'}`}
                  title="主任务继续运行，这条消息走旁路子会话"
                >
                  {askingSideQuestion() ? '处理中...' : isMinimalComposer() ? '旁路' : '旁路提问'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    void handleGuideSubmit('queue');
                  }}
                  disabled={!input().trim() && attachedFiles().length === 0}
                  class={`inline-flex items-center justify-center gap-2 rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/12 font-semibold text-[#ffe1b3] disabled:opacity-50 ${isMinimalComposer() ? 'px-3 py-2 text-xs' : 'px-4 py-2.5 text-sm'}`}
                  title="明确作为下一轮消息执行"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class={isMinimalComposer() ? 'h-3.5 w-3.5' : 'h-4 w-4'}>
                    <path d="M19 11H13V5h-2v6H5v2h6v6h2v-6h6z" />
                  </svg>
                  {isMinimalComposer() ? '排队' : '排队追加'}
                </button>
                <button
                  type="button"
                  onClick={handleCancel}
                  disabled={cancelling()}
                  aria-busy={cancelling()}
                  class={`inline-flex items-center justify-center gap-2 rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 font-semibold text-[#ffd0cb] ${isMinimalComposer() ? 'px-3 py-2 text-xs' : 'px-4 py-2.5 text-sm'}`}
                  title={cancelling() ? '正在向后端发送中止请求' : '中止当前回合及其执行中的工作'}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class={isMinimalComposer() ? 'h-3.5 w-3.5' : 'h-4 w-4'}>
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm5 11H7v-2h10v2z" />
                  </svg>
                  {cancelling() ? '正在中止…' : isMinimalComposer() ? '停止' : '停止当前回合'}
                </button>
                </Show>
              </div>
            </Show>
            </div>
          </div>
        </div>

        <Show when={!composerCollapsed()}>
          <div class="mt-3 hidden text-center text-xs text-[#8b949e] sm:block">
            {goalMode() ? '从聊天框发送目标或补充指导；暂停、恢复与停止请使用目标状态入口。' : sessionStore.isRunning()
              ? '运行中按回车先暂存到上方，点击“追加”才发送；Alt+回车可旁路提问，黄色按钮会排队执行。'
              : '按回车发送消息，Shift+回车换行。'}
          </div>
        </Show>
      </form>
    </div>
  );
}
