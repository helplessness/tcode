import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactDOM from 'react-dom/client';
import { CopilotChat, CopilotKitProvider } from '@copilotkit/react-core/v2';
import '@copilotkit/react-ui/v2/styles.css';
import {
  createTCodeAgent,
  TCodeGatewayClient,
  type TCodeAgentMessage,
  type TCodeRuntime,
  type TCodeThreadSummary,
} from '@tcode-ai/ag-ui-client';
import './styles.css';

type Workspace = {
  name: string;
  path: string;
};

const DEFAULT_MODEL = 'deepseek/deepseek-v4-flash';
const params = new URLSearchParams(window.location.search);
const requestedWorkspace = params.get('workspace') || '';
const requestedRuntime = params.get('runtime') || 'tcode-native';
const requestedModel = params.get('model') || DEFAULT_MODEL;
const requestedThread = params.get('thread') || '';
const gateway = new TCodeGatewayClient({ baseUrl: '', apiKey: '' });

function newThreadId() {
  return crypto.randomUUID();
}

function runtimeLabel(runtime: TCodeRuntime) {
  if (runtime.available && runtime.enabled) return runtime.displayName;
  return `${runtime.displayName}（${runtime.available ? '未启用' : '不可用'}）`;
}

function threadLabel(thread: TCodeThreadSummary) {
  const preview = thread.messagePreview.trim();
  if (preview) return preview;
  if (thread.title && thread.title !== 'AG-UI session') return thread.title;
  return '新会话';
}

function threadTime(timestamp: number) {
  if (!timestamp) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(timestamp * 1000));
}

function App() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState('');
  const [runtimes, setRuntimes] = useState<TCodeRuntime[]>([]);
  const [runtimeId, setRuntimeId] = useState('');
  const [modelId, setModelId] = useState(requestedModel);
  const [threadId, setThreadId] = useState(() => requestedThread || newThreadId());
  const [threads, setThreads] = useState<TCodeThreadSummary[]>([]);
  const [threadMessages, setThreadMessages] = useState<TCodeAgentMessage[]>([]);
  const [loadingThreads, setLoadingThreads] = useState(false);
  const [loadingThread, setLoadingThread] = useState(Boolean(requestedThread));
  const [historyError, setHistoryError] = useState('');
  const [deletionError, setDeletionError] = useState('');
  const [deletingThreadId, setDeletingThreadId] = useState('');
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(true);
  const [loadingRuntimes, setLoadingRuntimes] = useState(false);
  const [error, setError] = useState('');
  const initialThreadRef = useRef(requestedThread);
  const threadRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const deletionRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    historyRequestRef.current += 1;
    deletionRef.current?.abort();
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadWorkspaces() {
      setLoadingWorkspaces(true);
      setError('');
      try {
        const response = await fetch('/api/workdirs', {
          headers: { Accept: 'application/json' },
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(String(body.detail || `Workspace 加载失败 (${response.status})`));
        }
        const items = (Array.isArray(body.workdirs) ? body.workdirs : [])
          .map((item: Record<string, unknown>) => ({
            name: String(item.name || ''),
            path: String(item.path || ''),
          }))
          .filter((item: Workspace) => item.name);
        if (cancelled) return;
        setWorkspaces(items);
        const selected = items.some((item: Workspace) => item.name === requestedWorkspace)
          ? requestedWorkspace
          : items[0]?.name || '';
        setWorkspaceId(selected);
        if (!selected) setError('当前账号没有可用的 Workspace。');
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : 'Workspace 加载失败');
        }
      } finally {
        if (!cancelled) setLoadingWorkspaces(false);
      }
    }
    void loadWorkspaces();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setRuntimes([]);
      setRuntimeId('');
      return;
    }
    let cancelled = false;
    async function loadRuntimes() {
      setLoadingRuntimes(true);
      setError('');
      try {
        const items = (await gateway.listRuntimes(workspaceId, true))
          .filter((runtime) => runtime.runtimeId !== 'direct-echo');
        if (cancelled) return;
        setRuntimes(items);
        const usable = items.filter((runtime) => runtime.available && runtime.enabled);
        const preferred = usable.find((runtime) => runtime.runtimeId === requestedRuntime)
          || usable.find((runtime) => runtime.runtimeId === 'tcode-native')
          || usable[0];
        setRuntimeId((current) => (
          usable.some((runtime) => runtime.runtimeId === current)
            ? current
            : preferred?.runtimeId || ''
        ));
        if (!preferred && usable.length === 0) setError('当前 Workspace 没有可用的 Harness。');
      } catch (reason) {
        if (!cancelled) {
          setRuntimes([]);
          setRuntimeId('');
          setError(reason instanceof Error ? reason.message : 'Harness 加载失败');
        }
      } finally {
        if (!cancelled) setLoadingRuntimes(false);
      }
    }
    void loadRuntimes();
    return () => { cancelled = true; };
  }, [workspaceId]);

  const selectedRuntime = useMemo(
    () => runtimes.find((runtime) => runtime.runtimeId === runtimeId),
    [runtimes, runtimeId],
  );

  const modelOptions = useMemo(() => {
    const values = selectedRuntime?.models || [];
    const unique = Array.from(new Set([DEFAULT_MODEL, ...values].filter(Boolean)));
    return unique.sort((left, right) => {
      if (left === DEFAULT_MODEL) return -1;
      if (right === DEFAULT_MODEL) return 1;
      return left.localeCompare(right);
    });
  }, [selectedRuntime]);

  useEffect(() => {
    if (modelOptions.includes(modelId)) return;
    setModelId(modelOptions[0] || DEFAULT_MODEL);
  }, [modelId, modelOptions]);

  const loadThread = useCallback(async (nextThreadId: string) => {
    if (!workspaceId || !nextThreadId) return;
    const request = ++threadRequestRef.current;
    setLoadingThread(true);
    setHistoryError('');
    try {
      const thread = await gateway.getThread(nextThreadId, workspaceId);
      if (request !== threadRequestRef.current) return;
      setThreadMessages(thread.messages);
      setThreadId(thread.threadId);
    } catch (reason) {
      if (request !== threadRequestRef.current) return;
      setHistoryError(reason instanceof Error ? reason.message : '会话加载失败');
    } finally {
      if (request === threadRequestRef.current) setLoadingThread(false);
    }
  }, [workspaceId]);

  const refreshThreads = useCallback(async (showLoading = true) => {
    if (!workspaceId || deletionRef.current) return [];
    const request = ++historyRequestRef.current;
    if (showLoading) setLoadingThreads(true);
    try {
      const items = await gateway.listThreads(workspaceId);
      if (request !== historyRequestRef.current) return [];
      // AG-UI sessions created by older servers use the general agent; newer
      // servers retain the selected TCode agent and the stable AG-UI title.
      const visible = items.filter((item) => (
        item.agentName === 'general' || item.title === 'AG-UI session'
      ));
      setThreads(visible);
      setHistoryError('');
      return visible;
    } catch (reason) {
      if (showLoading && request === historyRequestRef.current) {
        setHistoryError(reason instanceof Error ? reason.message : '历史会话加载失败');
      }
      return [];
    } finally {
      if (showLoading && request === historyRequestRef.current) setLoadingThreads(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    if (!workspaceId) {
      setThreads([]);
      setLoadingThreads(false);
      return;
    }
    let cancelled = false;
    async function initializeHistory() {
      const items = await refreshThreads(true);
      if (cancelled) return;
      const initialThread = initialThreadRef.current;
      initialThreadRef.current = '';
      if (initialThread && items.some((item) => item.threadId === initialThread)) {
        await loadThread(initialThread);
      } else if (initialThread) {
        // A freshly created thread is written to the URL before its first run
        // reaches the server. Keep that id as an empty conversation instead
        // of treating a normal page refresh as a missing-history error.
        setThreadId(initialThread);
        setThreadMessages([]);
        setLoadingThread(false);
      } else {
        setLoadingThread(false);
      }
    }
    void initializeHistory();
    const timer = window.setInterval(() => {
      if (!cancelled) void refreshThreads(false);
    }, 8000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [loadThread, refreshThreads, workspaceId]);

  useEffect(() => {
    if (!workspaceId || !runtimeId || !modelId) return;
    const next = new URL(window.location.href);
    next.searchParams.set('workspace', workspaceId);
    next.searchParams.set('runtime', runtimeId);
    next.searchParams.set('model', modelId);
    next.searchParams.set('thread', threadId);
    window.history.replaceState({}, '', next);
  }, [workspaceId, runtimeId, modelId, threadId]);

  const agent = useMemo(() => {
    if (!workspaceId || !runtimeId || !modelId) return null;
    return createTCodeAgent({
      baseUrl: '',
      workspaceId,
      runtimeId,
      modelId,
      // Match the native Web composer's default execution settings. These are
      // harness inputs, not CopilotKit-side prompts or a separate agent policy.
      ...(runtimeId === 'tcode-native' ? {
        executionProfile: 'standard' as const,
        schedulerMode: 'supervisor' as const,
        reasoningEffort: 'medium' as const,
      } : {}),
      agentId: 'default',
      threadId,
      initialMessages: threadMessages,
    });
  }, [workspaceId, runtimeId, modelId, threadId, threadMessages]);

  const resetConversation = () => {
    threadRequestRef.current += 1;
    setThreadId(newThreadId());
    setThreadMessages([]);
    setLoadingThread(false);
    setHistoryError('');
  };

  const selectWorkspace = (nextWorkspace: string) => {
    if (nextWorkspace === workspaceId) return;
    historyRequestRef.current += 1;
    setThreads([]);
    setDeletionError('');
    setLoadingRuntimes(true);
    setWorkspaceId(nextWorkspace);
    setModelId(DEFAULT_MODEL);
    resetConversation();
  };

  const selectRuntime = (nextRuntime: string) => {
    if (nextRuntime === runtimeId) return;
    setRuntimeId(nextRuntime);
    setModelId(DEFAULT_MODEL);
    resetConversation();
  };

  const selectModel = (nextModel: string) => {
    if (nextModel === modelId) return;
    setModelId(nextModel);
    resetConversation();
  };

  const deleteThread = async (thread: TCodeThreadSummary) => {
    if (!workspaceId || deletionRef.current || loadingThread) return;
    if (thread.threadId === threadId && agent?.isRunning) {
      setDeletionError('请先停止当前回复，再删除这个会话。');
      return;
    }
    if (!window.confirm(`删除会话“${threadLabel(thread)}”？\n聊天记录及关联子会话会被删除，无法撤销。Workspace 中的普通文件保留。`)) return;

    const controller = new AbortController();
    deletionRef.current = controller;
    historyRequestRef.current += 1;
    setLoadingThreads(false);
    setDeletingThreadId(thread.threadId);
    setDeletionError('');
    try {
      await gateway.deleteThread(thread.threadId, workspaceId, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setThreads((items) => items.filter((item) => item.threadId !== thread.threadId));
      if (thread.threadId === threadId) resetConversation();
    } catch (reason) {
      if (!controller.signal.aborted) {
        setDeletionError(reason instanceof Error ? reason.message : '会话删除失败');
      }
    } finally {
      if (deletionRef.current === controller) {
        deletionRef.current = null;
        setDeletingThreadId('');
      }
    }
  };

  const deletingCurrentThread = deletingThreadId === threadId;
  const ready = Boolean(agent) && !loadingWorkspaces && !loadingRuntimes && !loadingThread && !deletingCurrentThread;

  return (
    <main>
      <header className="app-header">
        <div className="brand">
          <strong>TCode × CopilotKit</strong>
          <span>AG-UI Harness Console</span>
        </div>
        <div className="runtime-status" aria-live="polite">
          <span className={`status-dot ${ready ? 'ready' : ''}`} />
          {ready ? '已连接' : '正在配置'}
        </div>
      </header>

      <section className="configuration" aria-label="Agent 运行配置">
        <label>
          <span>Workspace</span>
          <select
            aria-label="Workspace"
            value={workspaceId}
            disabled={Boolean(deletingThreadId) || loadingWorkspaces || workspaces.length === 0}
            onChange={(event) => selectWorkspace(event.target.value)}
          >
            {loadingWorkspaces && <option value="">正在加载…</option>}
            {!loadingWorkspaces && workspaces.length === 0 && <option value="">没有可用 Workspace</option>}
            {workspaces.map((workspace) => (
              <option key={workspace.name} value={workspace.name}>{workspace.name}</option>
            ))}
          </select>
        </label>

        <label>
          <span>Harness</span>
          <select
            aria-label="Harness"
            value={runtimeId}
            disabled={Boolean(deletingThreadId) || !workspaceId || loadingRuntimes || runtimes.length === 0}
            onChange={(event) => selectRuntime(event.target.value)}
          >
            {loadingRuntimes && <option value="">正在检测…</option>}
            {!loadingRuntimes && runtimes.length === 0 && <option value="">没有可用 Harness</option>}
            {runtimes.map((runtime) => (
              <option
                key={runtime.runtimeId}
                value={runtime.runtimeId}
                disabled={!runtime.available || !runtime.enabled}
              >
                {runtimeLabel(runtime)}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>Model</span>
          <select
            aria-label="Model"
            value={modelId}
            disabled={Boolean(deletingThreadId) || !runtimeId || loadingRuntimes}
            onChange={(event) => selectModel(event.target.value)}
          >
            {modelOptions.map((model) => (
              <option key={model} value={model}>{model}</option>
            ))}
          </select>
        </label>

        <button type="button" className="new-chat" onClick={resetConversation} disabled={!ready || Boolean(deletingThreadId)}>
          新建会话
        </button>
      </section>

      {error && <div className="configuration-error" role="alert">{error}</div>}

      <section className="conversation-layout">
        <aside className="thread-sidebar" aria-label="历史会话">
          <div className="thread-sidebar-header">
            <div>
              <strong>历史会话</strong>
              <span>{threads.length} 个</span>
            </div>
            <button
              type="button"
              onClick={() => void refreshThreads(true)}
              disabled={!workspaceId || loadingThreads || Boolean(deletingThreadId)}
              aria-label="刷新历史会话"
            >
              {loadingThreads ? '…' : '↻'}
            </button>
          </div>
          {historyError && <div className="thread-error" role="alert">{historyError}</div>}
          {deletionError && <div className="thread-error" role="alert">{deletionError}</div>}
          <div className="thread-list">
            {loadingThreads && threads.length === 0 && (
              <div className="thread-placeholder">正在加载历史会话…</div>
            )}
            {!loadingThreads && threads.length === 0 && (
              <div className="thread-placeholder">还没有历史会话</div>
            )}
            {threads.map((thread) => (
              <div
                key={thread.threadId}
                className={`thread-row ${thread.threadId === threadId ? 'active' : ''}`}
              >
                <button
                  type="button"
                  className="thread-item"
                  aria-current={thread.threadId === threadId ? 'page' : undefined}
                  disabled={loadingThread || Boolean(deletingThreadId)}
                  onClick={() => void loadThread(thread.threadId)}
                >
                  <strong>{threadLabel(thread)}</strong>
                  <span>
                    {threadTime(thread.updatedAt)}
                    {thread.messageCount > 0 ? ` · ${thread.messageCount} 条消息` : ''}
                  </span>
                </button>
                <button
                  type="button"
                  className="thread-delete"
                  aria-label={`删除会话：${threadLabel(thread)}`}
                  disabled={loadingThread || Boolean(deletingThreadId)}
                  onClick={() => void deleteThread(thread)}
                >
                  {deletingThreadId === thread.threadId ? '删除中…' : '删除'}
                </button>
              </div>
            ))}
          </div>
        </aside>

        <section className="chat-shell">
          {agent && ready ? (
            <CopilotKitProvider
              key={`${workspaceId}:${runtimeId}:${modelId}:${threadId}`}
              selfManagedAgents={{ default: agent }}
            >
              <CopilotChat
                agentId="default"
                threadId={threadId}
                labels={{ welcomeMessageText: `当前使用 ${workspaceId} · ${selectedRuntime?.displayName || runtimeId}` }}
              />
            </CopilotKitProvider>
          ) : (
            <div className="empty-state">
              <strong>{error ? '暂时无法启动 Agent' : deletingCurrentThread ? '正在删除会话' : loadingThread ? '正在恢复会话' : '正在加载运行配置'}</strong>
              <span>{error || (deletingCurrentThread ? '等待服务端清理完成。' : loadingThread ? '正在读取历史消息。' : '正在读取可用的 Workspace、Harness 和模型。')}</span>
            </div>
          )}
        </section>
      </section>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><App /></React.StrictMode>,
);
