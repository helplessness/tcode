/** Chat sockets, streaming messages, guidance and interaction state. */
import { untrack } from 'solid-js';
import { confirmModelSwitch } from '../utils/modelSwitchNotice';
import { AGENT_EVENTS_PROTOCOL_VERSION } from '../protocol/generated';
import { runtimeWebSocketAdapter } from '../protocol/websocketAdapter';
import { shouldAcceptResponseModel } from '../components/composer/modelSelection';
import { browserLiveStore } from '../stores/browserLive';
import { composerIdentity } from '../stores/composer';
import {
  sanitizeMessageVerification,
  sanitizeToolCall,
  sessionStore,
  type Message,
  type QuestionRequest,
  type ToolCall,
} from '../stores/session';
import {
  chatSocketCloseStatusText,
  classifyChatSocketClose,
  isForegroundChatRunning,
  shouldAdoptSocketSession,
  type ChatSocketCloseDisposition,
} from '../utils/chatSocketLifecycle';
import { sanitizeMessageContentBlocks } from '../utils/contentBlocks';
import { generateId } from '../utils/id';
import { mergeAndStabilizeVisibleMessages } from '../utils/messageSnapshots';
import {
  agentEventDisplayMessage,
  agentEventStatusText,
  contextCompactionStatusText,
  formatAgentRunError,
  llmProgressStatusText,
  outcomeValidationStatusText,
  outcomeVerificationSummary,
  processNoteStatusText,
  streamHeartbeatStatusText,
} from '../utils/runStatus';
import { resolveExecutionAgentName } from '../utils/teamRegistry';
import { invalidateWorkflowRunCache } from './cache';
import type { WorkspaceFileRef } from './files';
import type {
  PermissionRequestEvent,
  QuestionRequestEvent,
  QuestionTimeoutEvent,
  ToolResultEvent,
  ToolUsingEvent,
  UsageStatusEvent,
} from './generated/protocol';
import { getSession, getSessionUsageSummary } from './sessions';

const API_BASE = '';

let activeWs: WebSocket | null = null;
let activeWsSessionId: string | null = null;
const activeChatSockets = new Map<string, WebSocket>();
const activeChatRejects = new Map<string, (reason?: any) => void>();
const cancelRequestedSessions = new Set<string>();
const pendingGuidanceCounts = new Map<string, number>();
const pendingGuidanceInterrupts = new Map<string, boolean>();
const workflowBackgroundSessions = new Set<string>();
const contextCompactingSessions = new Set<string>();
const supersededModelMessages = new Set<string>();
const latestModelStreamEpochs = new Map<string, number>();

function backgroundContinuationStatusText(hasBackground: boolean, hasGuidance: boolean, interruptGuidance = false): string {
  if (hasBackground) {
    return '后台编排继续执行中...';
  }
  if (hasGuidance) {
    return interruptGuidance ? '等待人工指导接入...' : '等待排队指导执行...';
  }
  return '';
}

function setPendingGuidanceCount(sessionId: string, count: number) {
  const normalized = Math.max(0, Number(count || 0));
  if (!sessionId) return;
  if (normalized <= 0) {
    pendingGuidanceCounts.delete(sessionId);
    return;
  }
  pendingGuidanceCounts.set(sessionId, normalized);
}

function getPendingGuidanceCount(sessionId: string): number {
  if (!sessionId) return 0;
  return Math.max(0, Number(pendingGuidanceCounts.get(sessionId) || 0));
}

function setPendingGuidanceInterrupt(sessionId: string, interrupt: boolean) {
  if (!sessionId) return;
  pendingGuidanceInterrupts.set(sessionId, Boolean(interrupt));
}

function getPendingGuidanceInterrupt(sessionId: string): boolean {
  if (!sessionId) return false;
  return Boolean(pendingGuidanceInterrupts.get(sessionId));
}

function updatePendingGuidanceMeta(
  sessionId: string,
  count: number,
  guidanceWorkerActive: boolean,
  interrupt: boolean,
) {
  setPendingGuidanceCount(sessionId, count);
  if (count > 0 || guidanceWorkerActive) {
    setPendingGuidanceInterrupt(sessionId, interrupt);
    return;
  }
  pendingGuidanceInterrupts.delete(sessionId);
}

function stableStringify(value: any): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableStringify(item)).join(',')}]`;
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, any>)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${JSON.stringify(k)}:${stableStringify(v)}`);
    return `{${entries.join(',')}}`;
  }
  return JSON.stringify(value);
}

export function hasActiveChatSocket(sessionId?: string): boolean {
  if (sessionId) {
    const socket = activeChatSockets.get(sessionId);
    return Boolean(socket && socket.readyState === WebSocket.OPEN);
  }
  return Array.from(activeChatSockets.values()).some(
    (socket) => socket.readyState === WebSocket.OPEN,
  );
}

function unregisterChatSocket(socket: WebSocket) {
  for (const [sessionId, candidate] of activeChatSockets.entries()) {
    if (candidate === socket) activeChatSockets.delete(sessionId);
  }
  if (activeWs === socket) {
    activeWs = null;
    activeWsSessionId = null;
  }
}

export function mergeToolCalls(existing: ToolCall[] = [], incoming: ToolCall[] = []): ToolCall[] {
  const merged = existing
    .map((toolCall) => sanitizeToolCall(toolCall))
    .filter((toolCall): toolCall is ToolCall => toolCall !== null);

  for (const rawToolCall of incoming) {
    const tc = sanitizeToolCall(rawToolCall);
    if (!tc) continue;

    const idx = merged.findIndex((x) => {
      if (tc.call_id && x.call_id) return x.call_id === tc.call_id;
      return (
        x.tool_name === tc.tool_name &&
        stableStringify(x.arguments || {}) === stableStringify(tc.arguments || {})
      );
    });

    if (idx >= 0) {
      const nextArguments = Object.keys(tc.arguments || {}).length > 0
        ? tc.arguments
        : (merged[idx].arguments || {});
      const currentState = String(merged[idx].state || '');
      const incomingState = String(tc.state || '');
      const currentIsTerminal = ['completed', 'error', 'skipped', 'interrupted'].includes(currentState);
      const incomingIsActive = ['pending', 'running'].includes(incomingState);
      merged[idx] = {
        tool_name: tc.tool_name || merged[idx].tool_name,
        arguments: nextArguments,
        call_id: tc.call_id || merged[idx].call_id,
        state: currentIsTerminal && incomingIsActive
          ? merged[idx].state
          : (tc.state || merged[idx].state),
        output: tc.output ?? merged[idx].output,
        error: tc.error ?? merged[idx].error,
        metadata: {
          ...(merged[idx].metadata || {}),
          ...(tc.metadata || {}),
        },
      };
    } else {
      merged.push(tc);
    }
  }

  return merged;
}

function normalizeSubagentFinalResponse(value: string): string {
  return String(value || '').replace(/^✅\s*最终回答:\s*/, '').trim();
}

function hasMatchingSubagentAssistantResponse(sessionId: string, responseText: string): boolean {
  const normalizedResponse = normalizeSubagentFinalResponse(responseText);
  if (!sessionId || !normalizedResponse) return false;

  return sessionStore.getSubagentChats(sessionId).some((entry) => {
    if (entry.role !== 'assistant') return false;
    return normalizeSubagentFinalResponse(entry.content) === normalizedResponse;
  });
}

function markLastAssistantCancelled() {
  sessionStore.setMessages(prev => {
    const updated = [...prev];
    const last = updated[updated.length - 1];
    if (!last || last.role !== 'assistant') return updated;

    const current = (last.content || '').trim();
    if (!current) {
      updated[updated.length - 1] = {
        ...last,
        content: '已中止',
      };
      return updated;
    }

    if (!current.includes('已中止')) {
      updated[updated.length - 1] = {
        ...last,
        content: `${last.content}\n\n[已中止]`,
      };
    }

    return updated;
  });
}

function getSessionMessagesSnapshot(targetSessionId: string): Message[] {
  if (!targetSessionId) return [];
  if (sessionStore.currentSessionId() === targetSessionId) {
    return [...sessionStore.messages()];
  }
  const session = sessionStore.sessions().find((item) => item.session_id === targetSessionId);
  return Array.isArray(session?.messages) ? [...session!.messages] : [];
}

function writeSessionMessages(targetSessionId: string, nextMessages: Message[]) {
  if (!targetSessionId) return;
  sessionStore.setSessions(prev => {
    const idx = prev.findIndex((item) => item.session_id === targetSessionId);
    if (idx < 0) return prev;
    const next = [...prev];
    next[idx] = {
      ...next[idx],
      messages: nextMessages,
      updated_at: Date.now(),
    };
    return next;
  });
  if (sessionStore.currentSessionId() === targetSessionId) {
    sessionStore.setMessages(nextMessages);
  }
}

function updateSessionMessages(
  targetSessionId: string,
  updater: (messages: Message[]) => Message[],
) {
  const base = getSessionMessagesSnapshot(targetSessionId);
  writeSessionMessages(targetSessionId, updater(base));
}

function markLastAssistantCompleted(targetSessionId: string) {
  updateSessionMessages(targetSessionId, (previous) => {
    const messages = [...previous];
    let index = messages.length - 1;
    while (index >= 0 && messages[index].role !== 'assistant') index -= 1;
    if (index < 0) return messages;
    const message = messages[index];
    messages[index] = {
      ...message,
      metadata: { ...(message.metadata || {}), completed_at: Date.now() },
    };
    return messages;
  });
}

function markLastAssistantCancelledForSession(targetSessionId: string) {
  updateSessionMessages(targetSessionId, (prev) => {
    const updated = [...prev];
    const last = updated[updated.length - 1];
    if (!last || last.role !== 'assistant') return updated;

    const current = (last.content || '').trim();
    if (!current) {
      updated[updated.length - 1] = {
        ...last,
        content: '已中止',
      };
      return updated;
    }

    if (!current.includes('已中止')) {
      updated[updated.length - 1] = {
        ...last,
        content: `${last.content}\n\n[已中止]`,
      };
    }

    return updated;
  });
}

function markLastAssistantErrorForSession(targetSessionId: string, errorText: string) {
  updateSessionMessages(targetSessionId, (prev) => {
    const updated = [...prev];
    const last = updated[updated.length - 1];
    if (!last || last.role !== 'assistant') return updated;

    const normalizedError = String(errorText || '').trim() || '请求失败';
    const rendered = `发生错误: ${normalizedError}`;
    updated[updated.length - 1] = {
      ...last,
      content: rendered,
    };
    return updated;
  });
}

export type SendMessageOptions = {
  async_continuation_id?: string;
  selected_skills?: string[];
  request_id?: string;
  stream?: boolean;
  output_format?: Record<string, any>;
  outcome_contract?: Record<string, any>;
  agent_name?: string;
  model?: string;
  runtime_id?: string;
  execution_profile?: 'fast' | 'standard' | 'deep' | 'orchestrated';
  reasoning_effort?: 'minimal' | 'low' | 'medium' | 'high';
  route_symphony_on_mcp_tool?: boolean;
  desktop_permission_mode?: 'ask' | 'auto';
  scheduler_mode?: 'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder';
  use_swarms?: boolean;
  swarm_topology?: 'concurrent' | 'sequential' | 'voting' | 'rearrange';
  external_swarm_communication_mode?: 'hybrid' | 'p2p' | 'broadcast' | 'handoff';
  external_swarm_bus?: 'memory' | 'redis' | 'kafka';
  external_swarm_rounds?: number;
  external_swarm_agent_timeout_seconds?: number;
  external_swarm_max_retries?: number;
  external_swarm_fallback_on_error?: boolean;
  workdir?: string;
  workdir_name?: string;
  file_refs?: WorkspaceFileRef[];
  research_context?: {
    schema: 'tcode.research-chat-context.v1';
    mode: 'sticky';
    assets: Array<{ asset_id: string; source_research_id?: string }>;
  };
  wiki_context?: {
    schema: 'tcode.wiki-chat-context.v1';
    mode: 'sticky';
    page: null | {
      page_id: string;
      source_id?: string;
    };
  };
};

export async function sendMessage(message: string, options: SendMessageOptions = {}): Promise<void> {
  const { currentSessionId, addMessage, setIsRunning, setStatus } = sessionStore;
  const stream = options.stream ?? true;
  const selectedAgentName = options.agent_name ?? sessionStore.selectedAgentName();
  const currentAgentName = sessionStore.currentSession()?.agent_name || '';
  const agentName = resolveExecutionAgentName(selectedAgentName, currentAgentName) || selectedAgentName;
  const executionProfile = options.execution_profile;
  const selectedModel = String(options.model || '').trim();
  const selectedRuntimeId = String(options.runtime_id || sessionStore.currentSession()?.metadata?.harness?.runtime_id || 'tcode-native').trim() || 'tcode-native';
  const reasoningEffort = options.reasoning_effort ?? sessionStore.selectedReasoningEffort();
  const routeSymphonyOnMcpTool =
    options.route_symphony_on_mcp_tool ?? sessionStore.selectedRouteSymphonyOnMcpTool();
  const schedulerMode = options.scheduler_mode ?? sessionStore.selectedSchedulerMode();
  const currentSession = sessionStore.currentSession();
  const selectedWorkdir = options.workdir ?? currentSession?.workdir;
  const selectedWorkdirName = options.workdir_name ?? currentSession?.workdir_name;
  
  let sessionId = currentSessionId();
  if (!sessionId) {
    throw new Error('没有活动会话。请先创建带工作目录的会话。');
  }
  cancelRequestedSessions.delete(sessionId);

  if (sessionStore.isHarnessSwitchPending(sessionId)) {
    throw new Error('Harness 正在切换，请等待交接完成后再发送或继续任务');
  }

  const existingSocket = activeChatSockets.get(sessionId);
  if (
    existingSocket &&
    (existingSocket.readyState === WebSocket.CONNECTING || existingSocket.readyState === WebSocket.OPEN)
  ) {
    throw new Error('当前会话已有请求正在建立连接或执行，请勿重复提交');
  }
  if (existingSocket) activeChatSockets.delete(sessionId);
  if (!await confirmModelSwitch(sessionId, selectedModel || currentSession?.model)) throw new Error('已取消使用切换后的模型发送');
  const latestSocket = activeChatSockets.get(sessionId);
  if (currentSessionId() !== sessionId || (latestSocket && (latestSocket.readyState === WebSocket.CONNECTING || latestSocket.readyState === WebSocket.OPEN))) throw new Error('会话状态已改变，请重新发送');
  const clientRequestId = String(options.request_id || generateId()).trim();
  const optimisticUserMessageId = generateId();
  const optimisticAssistantMessageId = generateId();

  addMessage({
    id: optimisticUserMessageId,
    role: 'user',
    content: message,
    created_at: Date.now(),
    metadata: { source_runtime_id: selectedRuntimeId },
  });

  addMessage({
    id: optimisticAssistantMessageId,
    role: 'assistant',
    content: '',
    metadata: { source_runtime_id: selectedRuntimeId },
  });

  setIsRunning(true);
  setStatus('思考中');
  if (sessionId) contextCompactingSessions.delete(sessionId);
  sessionStore.setRunningTool(null);
  sessionStore.setPermissionRequest(null);
  sessionStore.setQuestionRequest(null);

  return new Promise((resolve, reject) => {
    let closeDisposition: ChatSocketCloseDisposition = 'unsettled';
    let socketSessionId = String(sessionId || '').trim();
    let settled = false;
    let socketErrored = false;
    let ws: WebSocket;
    try {
      ws = new WebSocket(
        `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/chat?protocol_version=${encodeURIComponent(AGENT_EVENTS_PROTOCOL_VERSION)}`,
      );
    } catch (error) {
      sessionStore.setSessionIsRunning(sessionId, false);
      sessionStore.setSessionStatus(sessionId, '无法建立聊天连接');
      reject(error);
      return;
    }
    activeWs = ws;
    activeWsSessionId = sessionId || null;
    activeChatSockets.set(sessionId, ws);

    const settleResolved = () => {
      if (settled) return;
      settled = true;
      unregisterChatSocket(ws);
      for (const [targetSessionId, candidate] of activeChatRejects.entries()) {
        if (candidate === settleRejected) activeChatRejects.delete(targetSessionId);
      }
      resolve();
    };

    const settleRejected = (reason: unknown) => {
      if (settled) return;
      settled = true;
      unregisterChatSocket(ws);
      for (const [targetSessionId, candidate] of activeChatRejects.entries()) {
        if (candidate === settleRejected) activeChatRejects.delete(targetSessionId);
      }
      reject(reason);
    };
    activeChatRejects.set(sessionId, settleRejected);

    const syncSessionSnapshot = async (targetSessionId: string) => {
      if (!targetSessionId) return false;
      const latest = await getSession(targetSessionId, { bustCache: true });
      if (!latest || !Array.isArray(latest.messages)) return false;
      updateSessionMessages(targetSessionId, (current) =>
        mergeAndStabilizeVisibleMessages(current, latest.messages)
      );
      return true;
    };

    ws.onopen = () => {
      activeWsSessionId = sessionId || null;
      ws.send(JSON.stringify({
        message,
        async_continuation_id: options.async_continuation_id,
        selected_skills: options.selected_skills,
        session_id: sessionId,
        request_id: clientRequestId,
        stream,
        output_format: options.output_format,
        outcome_contract: options.outcome_contract,
        agent_name: agentName,
        model: selectedModel || undefined,
        runtime_id: selectedRuntimeId,
        execution_profile: executionProfile || undefined,
        reasoning_effort: reasoningEffort,
        route_symphony_on_mcp_tool: routeSymphonyOnMcpTool,
        desktop_permission_mode: options.desktop_permission_mode,
        // These settings belong to TCode Native's task tool, not external backends.
        ...(selectedRuntimeId === 'tcode-native' ? {
          scheduler_mode: schedulerMode,
          use_swarms: options.use_swarms,
          swarm_topology: options.swarm_topology,
          external_swarm_communication_mode: options.external_swarm_communication_mode,
          external_swarm_bus: options.external_swarm_bus,
          external_swarm_rounds: options.external_swarm_rounds,
          external_swarm_agent_timeout_seconds: options.external_swarm_agent_timeout_seconds,
          external_swarm_max_retries: options.external_swarm_max_retries,
          external_swarm_fallback_on_error: options.external_swarm_fallback_on_error,
        } : {}),
        workdir: selectedWorkdir,
        workdir_name: selectedWorkdirName,
        file_refs: options.file_refs,
        research_context: options.research_context,
        wiki_context: options.wiki_context,
      }));
    };

    ws.onmessage = async (event) => {
      try {
        const rawData: unknown = JSON.parse(event.data);
        const adaptedFrame = runtimeWebSocketAdapter.consume(rawData);
        const data = adaptedFrame.uiEvent;
        if (!data) return;
        const targetSessionId = String(data.session_id || sessionId || '').trim();
        if (targetSessionId) socketSessionId = targetSessionId;
        if (
          targetSessionId
          && cancelRequestedSessions.has(targetSessionId)
          && !(data.type === 'status' && data.status === 'cancelled')
        ) {
          return;
        }

        const upsertToolCallOnAssistant = (
          toolName: string,
          toolArgs: Record<string, any>,
          callId: string | undefined,
          scopeSessionId: string,
          messageId = '',
        ) => {
          updateSessionMessages(scopeSessionId, (prev) => {
            const updated = [...prev];
            let targetIndex = messageId
              ? updated.findIndex((item) => item.id === messageId && item.role === 'assistant')
              : -1;
            if (targetIndex < 0) {
              targetIndex = updated.length - 1;
              while (targetIndex >= 0 && updated[targetIndex]?.role !== 'assistant') {
                targetIndex -= 1;
              }
            }
            const target = updated[targetIndex];
            if (!target || target.role !== 'assistant') return updated;

            updated[targetIndex] = {
              ...target,
              tool_calls: mergeToolCalls(target.tool_calls || [], [
                {
                  tool_name: toolName,
                  arguments: toolArgs || {},
                  call_id: callId,
                  state: 'running',
                },
              ]),
            };
            return updated;
          });
        };

        const upsertStreamedAssistantText = (
          payload: {
            message_id?: string;
            delta?: string;
            content?: string;
            presentation?: string;
            stream_epoch?: number;
          },
          scopeSessionId: string,
        ) => {
          const messageId = String(payload.message_id || '').trim();
          const incomingEpoch = Number(payload.stream_epoch || 0);
          const latestEpoch = latestModelStreamEpochs.get(scopeSessionId) || 0;
          if (Number.isFinite(incomingEpoch) && incomingEpoch < latestEpoch) {
            return;
          }
          if (messageId && supersededModelMessages.has(`${scopeSessionId}:${messageId}`)) {
            return;
          }
          const delta = typeof payload.delta === 'string' ? payload.delta : '';
          const hasContent = typeof payload.content === 'string';
          updateSessionMessages(scopeSessionId, (prev) => {
            const updated = [...prev];
            let targetIndex = messageId
              ? updated.findIndex((item) => item.id === messageId)
              : -1;

            if (targetIndex < 0) {
              const lastIndex = updated.length - 1;
              const last = updated[lastIndex];
              const reusablePlaceholder = Boolean(
                last &&
                last.role === 'assistant' &&
                !String(last.content || '').trim() &&
                !(last.tool_calls && last.tool_calls.length > 0) &&
                !(last.process_notes && last.process_notes.length > 0),
              );
              if (reusablePlaceholder) {
                targetIndex = lastIndex;
              } else {
                updated.push({
                  id: messageId || generateId(),
                  created_at: Date.now(),
                  role: 'assistant',
                  content: '',
                  presentation: payload.presentation || 'model_output',
                });
                targetIndex = updated.length - 1;
              }
            }

            const target = updated[targetIndex];
            updated[targetIndex] = {
              ...target,
              id: messageId || target.id,
              created_at: target.created_at || Date.now(),
              content: hasContent
                ? String(payload.content || '')
                : `${target.content || ''}${delta}`,
              presentation: payload.presentation || target.presentation || 'model_output',
            };
            return updated;
          });
        };
        
        switch (data.type) {
          case 'session':
            if (data.session_id) {
              const incomingSessionId = String(data.session_id || '').trim();
              if (activeWs === ws) {
                activeWsSessionId = incomingSessionId;
              }
              if (incomingSessionId && incomingSessionId !== socketSessionId) {
                activeChatSockets.delete(socketSessionId);
                activeChatSockets.set(incomingSessionId, ws);
                activeChatRejects.delete(socketSessionId);
                activeChatRejects.set(incomingSessionId, settleRejected);
                socketSessionId = incomingSessionId;
              }
              if (
                shouldAdoptSocketSession(
                  sessionStore.currentSessionId(),
                  sessionId,
                  incomingSessionId,
                )
              ) {
                sessionStore.setCurrentSessionId(incomingSessionId);
              }
              if (data.agent_name) {
                sessionStore.updateSessionAgent(data.session_id, data.agent_name);
              }
              if (data.model) {
                const currentModel = String(
                  sessionStore.sessions().find(item => item.session_id === data.session_id)?.model || '',
                ).trim();
                // A response belongs to the model snapshot captured when this
                // request started. Do not let a late response roll back a model
                // the user selected while that request was in flight.
                if (shouldAcceptResponseModel(currentModel, selectedModel)) {
                  sessionStore.updateSessionModel(data.session_id, String(data.model || ''));
                }
              }
              if (data.workspace_id || data.workdir || data.workdir_name) {
                sessionStore.setSessions(prev => prev.map(s =>
                  s.session_id === data.session_id
                    ? {
                        ...s,
                        workspace_id: String(data.workspace_id || s.workspace_id || ''),
                        workdir: String(data.workdir || s.workdir || ''),
                        workdir_name: String(data.workdir_name || s.workdir_name || ''),
                      }
                    : s
                ));
              }
              if (data.research_context && typeof data.research_context === 'object') {
                sessionStore.setSessions(prev => prev.map(s =>
                  s.session_id === data.session_id
                    ? {
                        ...s,
                        metadata: {
                          ...(s.metadata || {}),
                          research_context: data.research_context,
                        },
                      }
                    : s
                ));
              }
              if (data.wiki_context && typeof data.wiki_context === 'object') {
                sessionStore.setSessions(prev => prev.map(s =>
                  s.session_id === data.session_id
                    ? {
                        ...s,
                        metadata: {
                          ...(s.metadata || {}),
                          wiki_context: data.wiki_context,
                        },
                      }
                    : s
                ));
              }
              if (data.scheduler_mode) {
                sessionStore.updateSessionSchedulerMode(data.session_id, data.scheduler_mode);
              }
            }
            break;
          
          case 'status':
            if (data.status === 'thinking') {
              if (targetSessionId) {
                sessionStore.setSessionStatus(targetSessionId, '思考中');
                sessionStore.setSessionRunningTool(targetSessionId, null);
              } else {
                setStatus('思考中');
                sessionStore.setRunningTool(null);
              }
            } else if (data.status === 'using_tool') {
              const toolEvent = data as ToolUsingEvent;
              if (targetSessionId) {
                sessionStore.setSessionStatus(targetSessionId, `正在使用工具：${toolEvent.tool_name}`);
                sessionStore.setSessionRunningTool(targetSessionId, {
                  tool_name: toolEvent.tool_name || 'unknown',
                  tool_args: toolEvent.tool_args || {},
                });
              } else {
                setStatus(`正在使用工具：${toolEvent.tool_name}`);
                sessionStore.setRunningTool({
                  tool_name: toolEvent.tool_name || 'unknown',
                  tool_args: toolEvent.tool_args || {},
                });
              }
              if (toolEvent.tool_name && targetSessionId) {
                upsertToolCallOnAssistant(
                  toolEvent.tool_name,
                  toolEvent.tool_args || {},
                  String(toolEvent.call_id || '').trim() || undefined,
                  targetSessionId,
                  String(toolEvent.message_id || '').trim(),
                );
              }
            } else if (data.status === 'usage') {
              const usageEvent = data as UsageStatusEvent;
              markLastAssistantCompleted(targetSessionId || sessionStore.currentSessionId() || '');
              if (targetSessionId) {
                sessionStore.setSessionStatus(targetSessionId, '已完成');
                sessionStore.setSessionRunningTool(targetSessionId, null);
              } else {
                setStatus('已完成');
                sessionStore.setRunningTool(null);
              }
              if (usageEvent.usage) {
                sessionStore.updateUsageStats(usageEvent.usage);
              }
              void getSessionUsageSummary().then((summary) => {
                if (summary?.total_usage) {
                  sessionStore.replaceUsageStats(summary.total_usage);
                }
              });
            } else if (data.status === 'cancelled') {
              closeDisposition = 'terminal';
              if (targetSessionId) contextCompactingSessions.delete(targetSessionId);
              if (targetSessionId) {
                workflowBackgroundSessions.delete(targetSessionId);
                sessionStore.setSessionStatus(targetSessionId, '已取消');
                sessionStore.setSessionRunningTool(targetSessionId, null);
              } else {
                setStatus('已取消');
                sessionStore.setRunningTool(null);
              }
              if (activeWs === ws && targetSessionId) {
                activeWsSessionId = null;
              }
              if (targetSessionId) {
                markLastAssistantCancelledForSession(targetSessionId);
              } else {
                markLastAssistantCancelled();
              }
              if (targetSessionId) {
                sessionStore.setSessionIsRunning(targetSessionId, false);
              } else {
                setIsRunning(false);
              }
              ws.close();
              settleRejected(new Error('已取消'));
            }
            break;

          case 'async_resume_rejected': {
            closeDisposition = 'terminal';
            if (targetSessionId) {
              updateSessionMessages(targetSessionId, current => current.filter(item =>
                item.id !== optimisticUserMessageId && item.id !== optimisticAssistantMessageId));
              sessionStore.setSessionIsRunning(targetSessionId, false);
              sessionStore.setSessionStatus(targetSessionId, String(data.error || '恢复任务未获受理'));
            }
            settleRejected(new Error(String(data.error || '恢复任务未获受理')));
            ws.close();
            return;
          }
          case 'run_conflict': {
            const conflictRunning = Boolean(data.running);
            closeDisposition = conflictRunning ? 'background' : 'terminal';
            if (targetSessionId) {
              updateSessionMessages(targetSessionId, (current) =>
                current.filter(
                  (item) =>
                    item.id !== optimisticUserMessageId &&
                    item.id !== optimisticAssistantMessageId,
                )
              );
              sessionStore.setSessionIsRunning(targetSessionId, conflictRunning);
              sessionStore.setSessionStatus(
                targetSessionId,
                conflictRunning
                  ? '当前会话已有任务执行中，已切换为状态同步'
                  : '该请求已经处理完成，正在同步结果',
              );
              await syncSessionSnapshot(targetSessionId);
            }
            ws.close();
            settleResolved();
            return;
          }

          case 'stream_heartbeat': {
            const heartbeatStatus = streamHeartbeatStatusText(
              data,
              Boolean(targetSessionId && contextCompactingSessions.has(targetSessionId)),
            );
            if (targetSessionId) {
              sessionStore.setSessionStatus(targetSessionId, heartbeatStatus);
            } else {
              setStatus(heartbeatStatus);
            }
            break;
          }
          
          case 'message':
            if (data.role === 'assistant' && targetSessionId) {
              updateSessionMessages(targetSessionId, (prev) => {
                const updated = [...prev];
                const incomingMessageId = String(data.message_id || '').trim();
                let targetIndex = incomingMessageId
                  ? updated.findIndex((item) => item.id === incomingMessageId)
                  : -1;
                if (targetIndex < 0) {
                  const lastIndex = updated.length - 1;
                  const last = updated[lastIndex];
                  if (
                    last &&
                    last.role === 'assistant' &&
                    !String(last.content || '').trim()
                  ) {
                    targetIndex = lastIndex;
                  } else {
                    updated.push({
                      id: incomingMessageId || generateId(),
                      created_at: Date.now(),
                      role: 'assistant',
                      content: '',
                    });
                    targetIndex = updated.length - 1;
                  }
                }
                const target = updated[targetIndex];
                if (target && target.role === 'assistant') {
                  const incomingToolCalls = Array.isArray(data.tool_calls) ? data.tool_calls : [];
                  const nextContent = typeof data.content === 'string'
                    ? data.content
                    : `${target.content || ''}${data.delta || ''}`;
                  const mergedToolCalls = mergeToolCalls(target.tool_calls || [], incomingToolCalls);
                  const incomingContentBlocks = sanitizeMessageContentBlocks(data.content_blocks);

                  updated[targetIndex] = {
                    ...target,
                    id: incomingMessageId || target.id,
                    created_at: target.created_at || Date.now(),
                    content: nextContent,
                    tool_calls: mergedToolCalls.length > 0 ? mergedToolCalls : undefined,
                    content_blocks: incomingContentBlocks.length > 0
                      ? incomingContentBlocks
                      : target.content_blocks,
                    presentation:
                      typeof data.presentation === 'string' && data.presentation
                        ? data.presentation
                        : target.presentation,
                    usage: data.usage || undefined,
                    research_grounding:
                      data.research_grounding && typeof data.research_grounding === 'object'
                        ? data.research_grounding
                        : target.research_grounding,
                    verification: sanitizeMessageVerification(
                      data.verification || data.outcome_validation,
                    ) || target.verification,
                    metadata:
                      data.metadata && typeof data.metadata === 'object'
                        ? data.metadata
                        : target.metadata,
                  };
                }
                return updated;
              });
            }
            break;

          case 'tool_result': {
            const toolEvent = data as ToolResultEvent;
            const sanitizedToolCall = sanitizeToolCall(toolEvent.tool_call);
            if (targetSessionId) {
              if (sanitizedToolCall) {
                updateSessionMessages(targetSessionId, (prev) => {
                  const updated = [...prev];
                  const messageId = String(toolEvent.message_id || '').trim();
                  let targetIndex = messageId
                    ? updated.findIndex((item) => item.id === messageId && item.role === 'assistant')
                    : -1;
                  if (targetIndex < 0) {
                    targetIndex = updated.length - 1;
                    while (targetIndex >= 0 && updated[targetIndex]?.role !== 'assistant') {
                      targetIndex -= 1;
                    }
                  }
                  const target = updated[targetIndex];
                  if (!target || target.role !== 'assistant') return updated;

                  const mergedToolCalls = mergeToolCalls(target.tool_calls || [], [sanitizedToolCall]);
                  updated[targetIndex] = {
                    ...target,
                    tool_calls: mergedToolCalls.length > 0 ? mergedToolCalls : undefined,
                  };
                  return updated;
                });
              }
              sessionStore.setSessionRunningTool(targetSessionId, null);
            } else if (!targetSessionId) {
              sessionStore.setRunningTool(null);
            }
            break;
          }

          case 'permission': {
            const permissionEvent = data as PermissionRequestEvent;
            if (permissionEvent.request_id && permissionEvent.tool_name) {
              const permission = {
                request_id: permissionEvent.request_id,
                session_id: permissionEvent.session_id || sessionId || '',
                tool_name: permissionEvent.tool_name,
                arguments: permissionEvent.arguments || {},
              };
              if (targetSessionId) {
                sessionStore.setSessionPermissionRequest(targetSessionId, permission);
                sessionStore.setSessionStatus(targetSessionId, `需要权限：${permissionEvent.tool_name}`);
              } else {
                sessionStore.setPermissionRequest(permission);
                setStatus(`需要权限：${permissionEvent.tool_name}`);
              }
            }
            break;
          }

          case 'question': {
            const questionEvent = data as QuestionRequestEvent;
            if (questionEvent.request_id && questionEvent.question) {
              const requestObj = {
                request_id: questionEvent.request_id,
                session_id: questionEvent.session_id || sessionId || '',
                question: {
                  ...questionEvent.question,
                  display: questionEvent.question.display || undefined,
                },
              };
              if (targetSessionId && sessionStore.getSessionRuntimeState(targetSessionId).questionRequest?.request_id !== requestObj.request_id) {
                sessionStore.setSessionQuestionRequest(targetSessionId, requestObj);
                sessionStore.setSessionStatus(targetSessionId, '需要回答问题');
              } else if (!targetSessionId) {
                sessionStore.setQuestionRequest(requestObj);
                setStatus('需要回答问题');
              }
              sessionStore.addQuestionHistory(requestObj);
            }
            break;
          }

          case 'question_timeout': {
            const timeoutEvent = data as QuestionTimeoutEvent;
            if (timeoutEvent.request_id) {
              sessionStore.resolveQuestionHistory(
                timeoutEvent.request_id,
                timeoutEvent.answer || '',
                'timeout'
              );
              if (targetSessionId && sessionStore.getSessionRuntimeState(targetSessionId).questionRequest?.request_id === timeoutEvent.request_id) {
                sessionStore.setSessionQuestionRequest(targetSessionId, null);
                sessionStore.setSessionStatus(targetSessionId, timeoutEvent.answer ? '问题已超时，已使用默认答案' : '问题等待已超时，未提交答案');
              } else if (!targetSessionId) {
                sessionStore.setQuestionRequest(null);
                setStatus(timeoutEvent.answer ? '问题已超时，已使用默认答案' : '问题等待已超时，未提交答案');
              }
            }
            break;
          }

          case 'agent_event':
            if (data.session_id) {
              const eventKind = String(data.kind || '').trim().toLowerCase();
              if (eventKind === 'guidance.lifecycle' && targetSessionId) {
                const lifecycleStatus = String(data.status || '').trim().toLowerCase();
                const labels: Record<string, string> = {
                  classifying: '正在理解追加指导',
                  pending: '已收到，将在下一个安全点应用',
                  claimed: '正在接入追加指导',
                  deferred: '追加指导正在等待当前交互结束',
                  applying: '正在应用追加指导并重新规划',
                  applied: '追加指导已应用',
                  superseded: '旧追加指导已被新指导替代',
                  cancelled: '追加指导已取消',
                  failed: '追加指导应用失败',
                };
                sessionStore.setSessionStatus(
                  targetSessionId,
                  labels[lifecycleStatus] || '追加指导状态已更新',
                );
                break;
              }
              if (eventKind === 'model_output.superseded' && targetSessionId) {
                const supersededMessageId = String(data.message_id || '').trim();
                if (supersededMessageId) {
                  supersededModelMessages.add(`${targetSessionId}:${supersededMessageId}`);
                  updateSessionMessages(targetSessionId, (messages) =>
                    messages.filter((message) => message.id !== supersededMessageId),
                  );
                }
                const nextEpoch = Number(data.stream_epoch || 0);
                if (Number.isFinite(nextEpoch)) {
                  latestModelStreamEpochs.set(
                    targetSessionId,
                    Math.max(latestModelStreamEpochs.get(targetSessionId) || 0, nextEpoch),
                  );
                }
                sessionStore.setSessionStatus(targetSessionId, '已接入新指导，正在重新规划');
                break;
              }
              if (eventKind === 'browser_session') {
                browserLiveStore.upsertFromAgentEvent(data, sessionId);
                const parentSessionId = String(data.parent_session_id || sessionId || '').trim();
                if (parentSessionId) {
                  sessionStore.setSessionStatus(
                    parentSessionId,
                    String(data.message || '浏览器实时画面已更新'),
                  );
                }
                break;
              }
              if (eventKind === 'model_output' && targetSessionId) {
                upsertStreamedAssistantText(
                  {
                    message_id: data.message_id,
                    delta: data.delta,
                    content:
                      String(data.status || '').trim().toLowerCase() === 'completed'
                        ? String(data.content || '')
                        : undefined,
                    presentation:
                      typeof data.presentation === 'string'
                        ? data.presentation
                        : 'model_output',
                    stream_epoch: Number(data.stream_epoch || 0),
                  },
                  targetSessionId,
                );
                sessionStore.setSessionStatus(
                  targetSessionId,
                  String(data.status || '').trim().toLowerCase() === 'completed'
                    ? '正在决定下一步'
                    : '正在生成说明',
                );
                break;
              }

              if (eventKind === 'context_compaction' && targetSessionId) {
                const compactionStatus = String(data.status || '').trim().toLowerCase();
                const statusText = contextCompactionStatusText(data);
                const eventParentSessionId = String(data.parent_session_id || sessionId || '').trim();
                if (compactionStatus === 'running') {
                  contextCompactingSessions.add(targetSessionId);
                } else {
                  contextCompactingSessions.delete(targetSessionId);
                }
                sessionStore.setSessionStatus(
                  targetSessionId,
                  statusText,
                );
                sessionStore.setSessionRunningTool(targetSessionId, null);
                if (eventParentSessionId && eventParentSessionId !== targetSessionId) {
                  sessionStore.upsertSubagentState({
                    parent_session_id: eventParentSessionId,
                    session_id: targetSessionId,
                    agent_name: data.agent_name || 'subagent',
                    display_name: data.display_name,
                    status: 'running',
                    message: statusText,
                  });
                }
                break;
              }

              if (eventKind === 'llm_progress' && targetSessionId) {
                sessionStore.setSessionStatus(
                  targetSessionId,
                  llmProgressStatusText(data),
                );
                sessionStore.setSessionRunningTool(targetSessionId, null);
                break;
              }

              if (eventKind === 'tool_runtime_progress' && targetSessionId) {
                const statusText = processNoteStatusText(data);
                const phase = String(data.phase || '').trim().toLowerCase();
                const eventParentSessionId = String(data.parent_session_id || sessionId || '').trim();
                sessionStore.setSessionStatus(targetSessionId, statusText);
                if (phase === 'tool_start' && data.tool_name) {
                  sessionStore.setSessionRunningTool(targetSessionId, {
                    tool_name: data.tool_name || 'unknown',
                    tool_args: data.tool_args || {},
                  });
                } else if (phase === 'tool_result' || phase === 'final_response' || phase === 'model_wait') {
                  sessionStore.setSessionRunningTool(targetSessionId, null);
                }
                if (eventParentSessionId && eventParentSessionId !== targetSessionId) {
                  const subagentStatus =
                    phase === 'tool_result' && String(data.status || '').trim().toLowerCase() === 'error'
                      ? 'running'
                      : data.status || 'running';
                  sessionStore.upsertSubagentState({
                    parent_session_id: eventParentSessionId,
                    session_id: targetSessionId,
                    agent_name: data.agent_name || 'subagent',
                    display_name: data.display_name,
                    status: subagentStatus,
                    message: statusText,
                  });
                }
                break;
              }

              if (eventKind === 'outcome_validation' && targetSessionId) {
                sessionStore.setSessionStatus(
                  targetSessionId,
                  outcomeValidationStatusText(data),
                );
                sessionStore.setSessionRunningTool(targetSessionId, null);
                break;
              }

              const completedResponseText =
                data.status === 'completed' && typeof data.last_response === 'string'
                  ? data.last_response.trim()
                  : '';
              const alreadyHasCompletedResponse = completedResponseText
                ? hasMatchingSubagentAssistantResponse(data.session_id, completedResponseText)
                : false;

              if (String(data.kind || '').trim().toLowerCase() === 'tool_progress' && targetSessionId) {
                const rawToolCall = data.tool_call && typeof data.tool_call === 'object'
                  ? data.tool_call
                  : {
                      tool_name: data.tool_name || 'unknown',
                      arguments: data.tool_args || {},
                      call_id: String(data.call_id || '').trim() || undefined,
                      state: String(data.status || '').trim().toLowerCase() || undefined,
                      output: typeof data.output === 'string' ? data.output : undefined,
                      error: typeof data.error === 'string' ? data.error : undefined,
                    };

                updateSessionMessages(targetSessionId, (prev) => {
                  const updated = [...prev];
                  const last = updated[updated.length - 1];
                  if (!last || last.role !== 'assistant') return updated;

                  const mergedToolCalls = mergeToolCalls(last.tool_calls || [], [rawToolCall]);
                  updated[updated.length - 1] = {
                    ...last,
                    tool_calls: mergedToolCalls.length > 0 ? mergedToolCalls : undefined,
                  };
                  return updated;
                });

                const normalizedStatus = String(data.status || '').trim().toLowerCase();
                if (normalizedStatus === 'running' && data.tool_name) {
                  sessionStore.setSessionStatus(targetSessionId, `正在使用工具：${data.tool_name}`);
                  sessionStore.setSessionRunningTool(targetSessionId, {
                    tool_name: data.tool_name || 'unknown',
                    tool_args: data.tool_args || {},
                  });
                } else if (normalizedStatus === 'completed' || normalizedStatus === 'error') {
                  sessionStore.setSessionRunningTool(targetSessionId, null);
                }

                break;
              }

              const eventParentSessionId = String(data.parent_session_id || sessionId || '').trim();
              const eventStatus = String(data.status || '').trim().toLowerCase();
              const eventDisplayName = String(data.display_name || data.agent_name || 'subagent').trim();
              if (String(data.tool_name || '').trim().toLowerCase() === 'browser') {
                const browserArgs = data.tool_args && typeof data.tool_args === 'object'
                  ? data.tool_args
                  : {};
                const agentSessionId = String(data.session_id || '').trim();
                browserLiveStore.upsert({
                  parentSessionId: eventParentSessionId,
                  agentSessionId,
                  browserSessionId: String(browserArgs.session || agentSessionId).trim(),
                  workspaceId: String(data.workspace_id || '').trim(),
                  status: 'starting',
                  action: String(browserArgs.action || '').trim(),
                  url: String(browserArgs.url || '').trim(),
                });
              }
              const workflowStageId = String(data.workflow_stage_id || '').trim();
              const isWorkflowAgentEvent = Boolean(data.workflow_run_id || workflowStageId || data.workflow_template_key);
              const eventMessage = String(data.message || '').trim();
              const workflowStatusText = isWorkflowAgentEvent
                ? [
                    workflowStageId ? `workflow ${workflowStageId}` : 'workflow',
                    `${eventDisplayName}${eventStatus ? ` ${eventStatus}` : ''}`,
                    eventMessage,
                  ].filter(Boolean).join(' · ')
                : agentEventStatusText(data, eventDisplayName);

              if (workflowStatusText) {
                if (targetSessionId && eventParentSessionId === targetSessionId) {
                  sessionStore.setSessionStatus(targetSessionId, workflowStatusText);
                  if (isWorkflowAgentEvent && ['running', 'busy'].includes(eventStatus)) {
                    sessionStore.setSessionIsRunning(targetSessionId, true);
                    workflowBackgroundSessions.add(targetSessionId);
                  }
                } else if (!targetSessionId && eventParentSessionId === sessionId) {
                  setStatus(workflowStatusText);
                  if (isWorkflowAgentEvent && ['running', 'busy'].includes(eventStatus)) {
                    setIsRunning(true);
                  }
                }
              }

              sessionStore.upsertSubagentState({
                parent_session_id: eventParentSessionId,
                session_id: data.session_id,
                agent_name: data.agent_name || 'subagent',
                display_name: data.display_name,
                engine: typeof data.engine === 'string' ? data.engine : undefined,
                scheduler_mode: data.scheduler_mode,
                status: data.status || 'idle',
                message: agentEventDisplayMessage(data),
                tools_used: Array.isArray(data.tools_used) ? data.tools_used : undefined,
                conversation_with: Array.isArray(data.conversation_with) ? data.conversation_with : undefined,
                last_response: typeof data.last_response === 'string' ? data.last_response : undefined,
                context: data.context && typeof data.context === 'object' ? data.context : undefined,
                contract: data.contract && typeof data.contract === 'object' ? data.contract : undefined,
              });

              if (data.tool_name) {
                const argsPreview = data.tool_args ? JSON.stringify(data.tool_args) : '';
                sessionStore.appendSubagentChat({
                  session_id: data.session_id,
                  role: 'assistant',
                  content: `🔧 ${data.tool_name}${argsPreview ? ` ${argsPreview}` : ''}`,
                  timestamp: Date.now(),
                });
              }

              if (Array.isArray(data.received_messages) && data.received_messages.length > 0) {
                for (const item of data.received_messages) {
                  const from = typeof item?.from === 'string' ? item.from : 'unknown';
                  const msg = typeof item?.message === 'string' ? item.message : '';
                  if (!msg) continue;
                  sessionStore.appendSubagentChat({
                    session_id: data.session_id,
                    role: 'system',
                    content: `⬇ 收到 ${from}: ${msg}`,
                    timestamp: Date.now(),
                  });
                }
              }

              if (Array.isArray(data.returned_messages) && data.returned_messages.length > 0) {
                for (const item of data.returned_messages) {
                  const to = typeof item?.to === 'string' ? item.to : 'unknown';
                  const msg = typeof item?.message === 'string' ? item.message : '';
                  if (!msg) continue;
                  sessionStore.appendSubagentChat({
                    session_id: data.session_id,
                    role: 'system',
                    content: `⬆ 发给 ${to}: ${msg}`,
                    timestamp: Date.now(),
                  });
                }
              }

              if (data.message) {
                sessionStore.appendSubagentChat({
                  session_id: data.session_id,
                  role: 'assistant',
                  content: `• ${data.message}`,
                  timestamp: Date.now(),
                });
              }

              if (
                data.status === 'completed' &&
                typeof data.last_response === 'string' &&
                data.last_response.trim() &&
                !alreadyHasCompletedResponse
              ) {
                const responseText = data.last_response.trim();
                sessionStore.appendSubagentChat({
                  session_id: data.session_id,
                  role: 'assistant',
                  content: `✅ 最终回答: ${responseText}`,
                  timestamp: Date.now(),
                });
              }
            }
            break;

          case 'team_delegate':
            if (data.session_id) {
              const displayName = String(data.display_name || data.agent_name || '团队');
              const reason = String(data.reason || '').trim();
              const routeRule = String(data.route_rule || '').trim();
              const routeSource = String(data.route_source || '').trim();
              const workersCreated = Math.max(0, Number(data.workers_created || 0));
              const routeHint = routeRule
                ? `命中规则 ${routeRule}${routeSource ? ` (${routeSource})` : ''}`
                : '';

              sessionStore.upsertSubagentState({
                parent_session_id: data.parent_session_id || sessionId || '',
                session_id: data.session_id,
                agent_name: data.agent_name || 'team',
                display_name: displayName,
                status: 'running',
                message: workersCreated > 0
                  ? `已拉起 ${workersCreated} 个团队成员${routeHint ? `，${routeHint}` : ''}`
                  : (routeHint ? `团队已接管当前任务，${routeHint}` : '团队已接管当前任务'),
              });
              sessionStore.appendSubagentChat({
                session_id: data.session_id,
                role: 'system',
                content: [
                  reason ? `已分配到 ${displayName}: ${reason}` : `已分配到 ${displayName}`,
                  routeHint,
                ].filter(Boolean).join('；'),
                timestamp: Date.now(),
              });
              sessionStore.refreshSubagents();
              setStatus(
                workersCreated > 0
                  ? `已分配到 ${displayName}，已拉起 ${workersCreated} 个成员${routeHint ? `，${routeHint}` : ''}`
                  : `已分配到 ${displayName}${routeHint ? `，${routeHint}` : ''}`,
              );
            }
            break;

          case 'workflow_auto_route':
            invalidateWorkflowRunCache();
            if (targetSessionId) {
              const run = data.run || {};
              const stage = String(run.current_stage_id || '').trim();
              const routePhase = String(data.route_phase || '').trim().toLowerCase();
              const pendingFields = Array.isArray(data.clarification_pending_fields)
                ? data.clarification_pending_fields
                    .map((item: any) => String(item || '').trim())
                    .filter(Boolean)
                : [];
              if (data.auto_execution_scheduled) {
                workflowBackgroundSessions.add(targetSessionId);
                sessionStore.setSessionIsRunning(targetSessionId, true);
              }
              const workflowStatusText =
                routePhase === 'clarification_pending'
                  ? (
                    pendingFields.length > 0
                      ? `workflow 待补充：${pendingFields.join('、')}`
                      : 'workflow 等待补充信息'
                  )
                  : (
                    stage
                      ? `已自动启动 workflow：${stage}`
                      : '已自动启动 workflow'
                  );
              sessionStore.setSessionStatus(
                targetSessionId,
                workflowStatusText,
              );
              sessionStore.setSessionRunningTool(targetSessionId, null);
            } else {
              setStatus('已自动启动 workflow');
              sessionStore.setRunningTool(null);
            }
            break;
          
          case 'error':
            console.error('Error:', data.error);
            const displayError = formatAgentRunError(data.error);
            closeDisposition = 'terminal';
            if (targetSessionId) contextCompactingSessions.delete(targetSessionId);
            if (targetSessionId) {
              workflowBackgroundSessions.delete(targetSessionId);
              sessionStore.setSessionRunningTool(targetSessionId, null);
              sessionStore.setSessionPermissionRequest(targetSessionId, null);
              sessionStore.setSessionStatus(targetSessionId, displayError);
            } else {
              sessionStore.setRunningTool(null);
              sessionStore.setPermissionRequest(null);
            }
            if (sessionStore.questionRequest()) {
              sessionStore.resolveQuestionHistory(
                sessionStore.questionRequest()!.request_id,
                '',
                'error'
              );
            }
            if (targetSessionId) {
              markLastAssistantErrorForSession(targetSessionId, displayError);
              sessionStore.setSessionIsRunning(targetSessionId, false);
            }
            if (!targetSessionId) {
              setIsRunning(false);
              setStatus(displayError);
            }
            ws.close();
            settleRejected(new Error(displayError));
            break;
        }

        if (data.done) {
          const outcomeStatus = String(data.outcome_status || '').trim().toLowerCase();
          const verificationSummary = outcomeVerificationSummary(data.outcome_validation);
          const payloadBackgroundActive = isForegroundChatRunning(data);
          if (targetSessionId && payloadBackgroundActive) {
            closeDisposition = 'background';
            workflowBackgroundSessions.add(targetSessionId);
            sessionStore.setSessionIsRunning(targetSessionId, true);
            sessionStore.setSessionStatus(
              targetSessionId,
              backgroundContinuationStatusText(true, false),
            );
            ws.close();
            settleResolved();
            return;
          }
          closeDisposition = 'terminal';
          if (targetSessionId) {
            workflowBackgroundSessions.delete(targetSessionId);
            contextCompactingSessions.delete(targetSessionId);
          }
          if (targetSessionId) {
            sessionStore.setSessionRunningTool(targetSessionId, null);
            sessionStore.setSessionPermissionRequest(targetSessionId, null);
          } else {
            sessionStore.setRunningTool(null);
            sessionStore.setPermissionRequest(null);
          }
          const remainingGuidance = targetSessionId ? getPendingGuidanceCount(targetSessionId) : 0;
          const interruptGuidance = targetSessionId ? getPendingGuidanceInterrupt(targetSessionId) : false;
          const backgroundActive = targetSessionId
            ? workflowBackgroundSessions.has(targetSessionId)
            : false;
          const nextStatus = backgroundContinuationStatusText(
            backgroundActive,
            remainingGuidance > 0,
            interruptGuidance,
          );
          if (remainingGuidance > 0 || backgroundActive) {
            if (targetSessionId) {
              sessionStore.setSessionIsRunning(targetSessionId, true);
              sessionStore.setSessionStatus(targetSessionId, nextStatus);
            } else {
              setIsRunning(true);
              setStatus(nextStatus);
            }
          } else {
            if (targetSessionId) {
              sessionStore.setSessionIsRunning(targetSessionId, false);
              sessionStore.setSessionStatus(
                targetSessionId,
                outcomeStatus === 'partial'
                  ? '部分完成'
                  : outcomeStatus === 'blocked'
                    ? '未验证'
                    : verificationSummary,
              );
            } else {
              setIsRunning(false);
              setStatus(
                outcomeStatus === 'partial'
                  ? '部分完成'
                  : outcomeStatus === 'blocked'
                    ? '未验证'
                    : verificationSummary,
              );
            }
          }
          ws.close();
          settleResolved();
        }
      } catch (e) {
        console.error('Parse error:', e);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      socketErrored = true;
      const targetSessionId = socketSessionId;
      if (targetSessionId) {
        sessionStore.setSessionStatus(targetSessionId, '连接出现异常，正在确认任务状态');
      } else {
        setStatus('连接出现异常，正在确认任务状态');
      }
    };

    ws.onclose = async (event) => {
      const targetSessionId = socketSessionId;
      unregisterChatSocket(ws);
      if (targetSessionId) contextCompactingSessions.delete(targetSessionId);
      if (settled) return;
      if (targetSessionId && cancelRequestedSessions.has(targetSessionId)) {
        sessionStore.setSessionRunningTool(targetSessionId, null);
        sessionStore.setSessionPermissionRequest(targetSessionId, null);
        sessionStore.setSessionQuestionRequest(targetSessionId, null);
        sessionStore.setSessionIsRunning(targetSessionId, false);
        sessionStore.setSessionStatus(targetSessionId, '已中止');
        closeDisposition = 'terminal';
        settleRejected(new Error('用户已中止'));
        return;
      }

      const online = typeof navigator === 'undefined' ? true : navigator.onLine;
      const closeKind = classifyChatSocketClose(event.code, online, closeDisposition);
      if (closeKind === 'auth' || closeKind === 'tenant') {
        const statusText = chatSocketCloseStatusText(closeKind);
        if (targetSessionId) {
          sessionStore.setSessionRunningTool(targetSessionId, null);
          sessionStore.setSessionPermissionRequest(targetSessionId, null);
          sessionStore.setSessionIsRunning(targetSessionId, false);
          sessionStore.setSessionStatus(targetSessionId, statusText);
        } else {
          sessionStore.setRunningTool(null);
          sessionStore.setPermissionRequest(null);
          setIsRunning(false);
          setStatus(statusText);
        }
        settleRejected(new Error(statusText));
        return;
      }

      const runtime = targetSessionId
        ? await getChatRuntimeStatus(targetSessionId)
        : null;
      if (targetSessionId) await syncSessionSnapshot(targetSessionId);

      if (runtime) {
        const running = isForegroundChatRunning(runtime);
        if (!running) {
          sessionStore.setSessionRunningTool(targetSessionId, null);
          sessionStore.setSessionPermissionRequest(targetSessionId, null);
        }
        sessionStore.setSessionIsRunning(targetSessionId, running);
        sessionStore.setSessionStatus(
          targetSessionId,
          running ? '连接已断开，任务在后台继续处理，正在同步状态' : '',
        );
        closeDisposition = running ? 'background' : 'terminal';
        settleResolved();
        return;
      }

      const statusText = chatSocketCloseStatusText(
        socketErrored && closeKind === 'network' ? 'network' : closeKind,
      );
      if (targetSessionId) {
        // Runtime status is temporarily unknown. Preserve the running state so
        // the visibility-aware poller can reconcile it when connectivity returns.
        sessionStore.setSessionIsRunning(targetSessionId, true);
        sessionStore.setSessionStatus(targetSessionId, statusText);
      } else {
        setIsRunning(true);
        setStatus(statusText);
      }
      closeDisposition = 'background';
      settleResolved();
    };
  });
}

export async function respondQuestion(requestId: string, answer: string, sessionId = sessionStore.currentSessionId()): Promise<void> {
  if (!sessionId) throw new Error('没有活动会话');

  const response = await fetch(`${API_BASE}/api/chat/question`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      session_id: sessionId,
      request_id: requestId,
      answer,
    }),
  });
  await requireInteractionSuccess(response);
  sessionStore.resolveQuestionHistory(requestId, answer, 'answered');
  if (sessionStore.getSessionRuntimeState(sessionId).questionRequest?.request_id === requestId) {
    sessionStore.setSessionQuestionRequest(sessionId, null);
    sessionStore.setSessionStatus(sessionId, '问题已回答，正在继续...');
  }
}

export async function respondQuestions(answers: Record<string, string>, sessionId: string): Promise<void> {
  const entries = Object.entries(answers);
  if (entries.length === 1) {
    await respondQuestion(entries[0][0], entries[0][1], sessionId);
    return;
  }
  const response = await fetch(`${API_BASE}/api/chat/question`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id:sessionId, answers}),
  });
  await requireInteractionSuccess(response);
  for (const [requestId, answer] of Object.entries(answers)) {
    sessionStore.resolveQuestionHistory(requestId, answer, 'answered');
  }
  const current = sessionStore.getSessionRuntimeState(sessionId).questionRequest;
  if (current && current.request_id in answers) {
    sessionStore.setSessionQuestionRequest(sessionId, null);
    sessionStore.setSessionStatus(sessionId, '问题组已回答，正在继续...');
  }
}

async function requireInteractionSuccess(response: Response): Promise<void> {
  if (response.ok) return;
  const data = await response.json().catch(() => ({}));
  const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message;
  throw new Error(response.status === 404
    ? '请求已过期或不可访问，请刷新会话确认最新状态。'
    : String(detail || `提交失败 (${response.status})，请重试。`));
}

export type OutcomeConfirmationDecision = 'accept' | 'modify';

export type OutcomeConfirmationResponse = {
  status: string;
  session_id: string;
  message_id: string;
  decision: OutcomeConfirmationDecision;
  confirmation_state: 'pending' | 'accepted' | 'modification_requested' | 'objective_failure' | 'repair_exhausted';
  verification?: Message['verification'];
  repair_guidance?: {
    message?: string;
    guidance_id?: string;
    sequence?: number;
    mode?: GuidanceMode;
    target_turn_id?: string;
    guidance_status?: GuidanceStatus;
  };
  timeline_message?: Message;
};

const outcomeConfirmationErrorMessage = (data: any) => {
  const detail = data?.detail;
  if (detail && typeof detail === 'object') {
    return String(detail.message || detail.code || '验收操作失败');
  }
  return String(detail || data?.error || '验收操作失败');
};

export async function confirmOutcomeResult(options: {
  sessionId: string;
  messageId: string;
  decision: OutcomeConfirmationDecision;
  feedback?: string;
  clientRequestId?: string;
  maxRepairAttempts?: number;
}): Promise<OutcomeConfirmationResponse> {
  const clientRequestId = options.clientRequestId || generateId();
  const resp = await fetch(`${API_BASE}/api/chat/outcome-confirmation`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      session_id: options.sessionId,
      message_id: options.messageId,
      decision: options.decision,
      feedback: options.feedback || '',
      client_request_id: clientRequestId,
      max_repair_attempts: options.maxRepairAttempts,
    }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(outcomeConfirmationErrorMessage(data));
  }

  const verification = sanitizeMessageVerification(data.verification);
  const repairGuidance = data.repair_guidance && typeof data.repair_guidance === 'object'
    ? data.repair_guidance
    : undefined;
  const timelineMessage = data.timeline_message && typeof data.timeline_message === 'object'
    ? data.timeline_message as Message
    : undefined;

  updateSessionMessages(options.sessionId, (previous) => {
    const updated = previous.map((message) => (
      message.id === options.messageId && verification
        ? { ...message, verification }
        : message
    ));
    if (timelineMessage && !updated.some((message) => message.id === timelineMessage.id)) {
      updated.push(timelineMessage);
    }
    const guidanceId = String(repairGuidance?.guidance_id || '').trim();
    const guidanceMessage = String(repairGuidance?.message || '').trim();
    if (
      guidanceMessage &&
      !updated.some((message) => (
        message.metadata?.guidance?.id === guidanceId ||
        (message.role === 'user' && message.content.trim() === guidanceMessage)
      ))
    ) {
      updated.push({
        id: generateId(),
        role: 'user',
        content: guidanceMessage,
        presentation: 'outcome_confirmation',
        metadata: {
          guidance: {
            id: guidanceId || undefined,
            sequence: Number.isFinite(Number(repairGuidance?.sequence))
              ? Number(repairGuidance?.sequence)
              : undefined,
            mode: repairGuidance?.mode || 'queue',
            target_turn_id: repairGuidance?.target_turn_id,
          },
        },
      });
    }
    return updated;
  });

  const pendingGuidanceCount = Math.max(0, Number(data.pending_guidance_count || 0));
  const guidanceWorkerActive = Boolean(data.guidance_worker_active);
  const running = Boolean(data.running || pendingGuidanceCount > 0 || guidanceWorkerActive);
  updatePendingGuidanceMeta(
    options.sessionId,
    pendingGuidanceCount,
    guidanceWorkerActive,
    Boolean(data.pending_guidance_interrupt),
  );
  sessionStore.setSessionIsRunning(options.sessionId, running);
  if (data.task_runtime) sessionStore.setSessions(previous => previous.map(session =>
    session.session_id === options.sessionId ? { ...session, task_runtime: data.task_runtime } : session,
  ));
  sessionStore.setSessionStatus(
    options.sessionId,
    data.confirmation_state === 'accepted'
      ? (verification?.advisory ? '已接受当前结果并完成' : '已确认当前结果')
      : data.confirmation_state === 'objective_failure'
        ? '客观验证未通过，需要修改'
        : data.confirmation_state === 'repair_exhausted'
          ? '已达到验证重试上限，可保存或发布为实验性 Workflow'
        : '修改要求已进入任务队列',
  );

  return {
    status: String(data.status || ''),
    session_id: String(data.session_id || options.sessionId),
    message_id: String(data.message_id || options.messageId),
    decision: data.decision === 'modify' ? 'modify' : 'accept',
    confirmation_state: (
      ['pending', 'accepted', 'modification_requested', 'objective_failure', 'repair_exhausted'].includes(
        String(data.confirmation_state || ''),
      )
        ? data.confirmation_state
        : 'pending'
    ),
    verification,
    repair_guidance: repairGuidance,
    timeline_message: timelineMessage,
  } as OutcomeConfirmationResponse;
}

export async function respondPermission(requestId: string, response: 'once' | 'always' | 'reject' | 'allow', sessionId = sessionStore.currentSessionId()): Promise<void> {
  if (!sessionId) throw new Error('没有活动会话');

  const result = await fetch(`${API_BASE}/api/chat/permission`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      session_id: sessionId,
      request_id: requestId,
      response,
    }),
  });
  await requireInteractionSuccess(result);
}

export async function cancelMessage(): Promise<void> {
  const { currentSessionId } = sessionStore;
  const sessionId = currentSessionId();
  const identity = composerIdentity();
  if (!sessionId) return;

  cancelRequestedSessions.add(sessionId);
  sessionStore.setSessionStatus(sessionId, '正在中止…');
  sessionStore.setSessionRunningTool(sessionId, null);
  sessionStore.setSessionPermissionRequest(sessionId, null);
  sessionStore.setSessionQuestionRequest(sessionId, null);

  try {
    const resp = await cancelSessionRun(sessionId, {
      clear_guidance: true,
    });
    if (composerIdentity() !== identity) return;

    sessionStore.setSessionRunningTool(sessionId, null);
    sessionStore.setSessionPermissionRequest(sessionId, null);
    sessionStore.setSessionQuestionRequest(sessionId, null);
    sessionStore.setSessionIsRunning(sessionId, false);
    sessionStore.setSessionStatus(sessionId, '已取消');
    markLastAssistantCancelledForSession(sessionId);

    const cancelledDescendants = Array.isArray((resp as any).cancelled_descendant_session_ids)
      ? (resp as any).cancelled_descendant_session_ids
      : [];
    for (const descendantSessionId of cancelledDescendants) {
      const normalizedId = String(descendantSessionId || '').trim();
      if (!normalizedId) continue;
      sessionStore.upsertSubagentState({
        parent_session_id: sessionId,
        session_id: normalizedId,
        status: 'idle',
        message: '已随主会话一起中止',
      });
      sessionStore.appendSubagentChat({
        session_id: normalizedId,
        role: 'system',
        content: '[已中止] 主会话已停止，该子代理回合已一并中止。',
        timestamp: Date.now(),
      });
    }

    const rejectActive = activeChatRejects.get(sessionId);
    if (rejectActive) {
      activeChatRejects.delete(sessionId);
      rejectActive(new Error('用户已取消'));
    }
  } catch (e) {
    cancelRequestedSessions.delete(sessionId);
    console.error('Cancel error:', e);
    if (composerIdentity() === identity) sessionStore.setSessionStatus(sessionId, e instanceof Error ? `取消失败: ${e.message}` : '取消失败，请重试');
    throw e;
  }
}

export type ChatRuntimeStatus = {
  session_id: string;
  running: boolean;
  chat_running?: boolean;
  background_running?: boolean;
  question_request?: QuestionRequest | null;
  question_requests?: QuestionRequest[];
  workflow_background_active?: boolean;
  child_session_active?: boolean;
  pending_guidance_count: number;
  pending_guidance_interrupt?: boolean;
  guidance_worker_active?: boolean;
  pending_steer_count?: number;
  pending_queue_count?: number;
};

export type GuidanceMode = 'steer' | 'queue';
export type GuidanceStatus =
  | 'classifying'
  | 'pending'
  | 'claimed'
  | 'deferred'
  | 'applying'
  | 'applied'
  | 'superseded'
  | 'cancelled'
  | 'failed';

export type ChatGuidanceQueueItem = {
  id: string;
  message: string;
  sequence?: number;
  mode?: GuidanceMode;
  status?: GuidanceStatus;
  target_turn_id?: string;
  received_phase?: string;
  claimed_safe_point?: string;
  deferred_reason?: string;
  effect?: string;
  failure_code?: string;
  version?: number;
  interrupt?: boolean;
  source?: string;
  created_at?: string | number;
  updated_at?: string | number;
  applied_at?: string | number;
};

export type ChatGuidanceQueue = ChatRuntimeStatus & {
  status: string;
  items: ChatGuidanceQueueItem[];
  deleted_guidance_id?: string;
};

export type CancelSessionOptions = {
  notify_parent?: boolean;
  auto_replan?: boolean;
  controller_session_id?: string;
  clear_guidance?: boolean;
};

function normalizeChatGuidanceQueue(
  data: any,
  sessionId: string,
  fallbackStatus = 'ok',
): ChatGuidanceQueue {
  const normalized: ChatGuidanceQueue = {
    status: String(data?.status || fallbackStatus),
    session_id: String(data?.session_id || sessionId),
    running: isForegroundChatRunning(data || {}),
    chat_running: typeof data?.chat_running === 'boolean' ? data.chat_running : undefined,
    background_running: Boolean(data?.background_running),
    workflow_background_active: Boolean(data?.workflow_background_active),
    child_session_active: Boolean(data?.child_session_active),
    pending_guidance_count: Math.max(0, Number(data?.pending_guidance_count || 0)),
    pending_guidance_interrupt: Boolean(data?.pending_guidance_interrupt),
    guidance_worker_active: Boolean(data?.guidance_worker_active),
    pending_steer_count: Math.max(0, Number(data?.pending_steer_count || 0)),
    pending_queue_count: Math.max(0, Number(data?.pending_queue_count || 0)),
    deleted_guidance_id: typeof data?.deleted_guidance_id === 'string' ? data.deleted_guidance_id : undefined,
    items: Array.isArray(data?.items)
      ? data.items
          .map((item: any) => ({
            id: String(item?.id || '').trim(),
            message: String(item?.message || item?.message_preview || '').trim(),
            sequence: Number.isFinite(Number(item?.sequence)) ? Number(item.sequence) : undefined,
            mode: item?.mode === 'steer' || item?.mode === 'queue' ? item.mode : undefined,
            status: typeof item?.status === 'string' ? item.status as GuidanceStatus : undefined,
            target_turn_id: String(item?.target_turn_id || '').trim() || undefined,
            received_phase: String(item?.received_phase || '').trim() || undefined,
            claimed_safe_point: String(item?.claimed_safe_point || '').trim() || undefined,
            deferred_reason: String(item?.deferred_reason || '').trim() || undefined,
            effect: String(item?.effect || '').trim() || undefined,
            failure_code: String(item?.failure_code || '').trim() || undefined,
            version: Number.isFinite(Number(item?.version)) ? Number(item.version) : undefined,
            interrupt: Boolean(item?.interrupt || item?.mode === 'steer'),
            source: String(item?.source || '').trim() || undefined,
            created_at: item?.created_at ?? undefined,
            updated_at: item?.updated_at ?? undefined,
            applied_at: item?.applied_at ?? undefined,
          }))
          .filter((item: ChatGuidanceQueueItem) => item.id && item.message)
      : [],
  };

  updatePendingGuidanceMeta(
    normalized.session_id,
    normalized.pending_guidance_count,
    Boolean(normalized.guidance_worker_active),
    Boolean(normalized.pending_guidance_interrupt),
  );
  return normalized;
}

export async function getChatGuidanceQueue(sessionId: string): Promise<ChatGuidanceQueue> {
  if (!sessionId) {
    throw new Error('没有活动会话');
  }

  const resp = await fetch(`${API_BASE}/api/chat/guidance/${encodeURIComponent(sessionId)}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data?.detail || data?.error || '读取排队指导失败'));
  }
  return normalizeChatGuidanceQueue(data, sessionId);
}

export async function deleteChatGuidanceItem(
  sessionId: string,
  guidanceId: string,
): Promise<ChatGuidanceQueue> {
  if (!sessionId) {
    throw new Error('没有活动会话');
  }
  if (!guidanceId) {
    throw new Error('缺少排队项 ID');
  }

  const resp = await fetch(
    `${API_BASE}/api/chat/guidance/${encodeURIComponent(sessionId)}/${encodeURIComponent(guidanceId)}`,
    { method: 'DELETE' },
  );
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data?.detail || data?.error || '删除排队指导失败'));
  }
  return normalizeChatGuidanceQueue(data, sessionId, 'deleted');
}

export async function getChatRuntimeStatus(sessionId: string): Promise<ChatRuntimeStatus | null> {
  // Callers may start polling inside a Solid effect: this snapshot must not
  // subscribe that effect to the runtime state it is about to update.
  const questionAtStart = untrack(() => sessionStore.getSessionRuntimeState(sessionId).questionRequest);
  try {
    const resp = await fetch(`${API_BASE}/api/chat/status/${encodeURIComponent(sessionId)}`);
    if (!resp.ok) return null;
    const data = await resp.json();
    const normalized: ChatRuntimeStatus = {
      session_id: String(data.session_id || sessionId),
      running: isForegroundChatRunning(data),
      chat_running: typeof data.chat_running === 'boolean' ? data.chat_running : undefined,
      background_running: Boolean(data.background_running),
      question_request: data.question_request?.request_id && data.question_request?.question
        && data.question_request.session_id === sessionId
        ? { ...data.question_request, question: { ...data.question_request.question, display: data.question_request.question.display || undefined } }
        : null,
      question_requests: Array.isArray(data.question_requests)
        ? data.question_requests.filter((item: QuestionRequest) => item.request_id && item.question && item.session_id === sessionId)
        : undefined,
      workflow_background_active: Boolean(data.workflow_background_active),
      child_session_active: Boolean(data.child_session_active),
      pending_guidance_count: Math.max(0, Number(data.pending_guidance_count || 0)),
      pending_guidance_interrupt: Boolean(data.pending_guidance_interrupt),
      guidance_worker_active: Boolean(data.guidance_worker_active),
      pending_steer_count: Math.max(0, Number(data.pending_steer_count || 0)),
      pending_queue_count: Math.max(0, Number(data.pending_queue_count || 0)),
    };
    sessionStore.setSessionDeliveryTurnRunning(sessionId, Boolean(
      (data.direct_running ?? data.running) || data.harness_running || data.pending_guidance_count,
    ));
    // Do not overwrite a newer socket event/answer with an in-flight HTTP snapshot.
    if ('question_request' in data && sessionStore.getSessionRuntimeState(sessionId).questionRequest === questionAtStart) {
      if (normalized.question_requests) {
        const liveIds = new Set(normalized.question_requests.map(item => item.request_id));
        for (const item of normalized.question_requests) {
          const resolved = sessionStore.questionHistory().some(history => history.request_id === item.request_id && history.status !== 'pending');
          if (!resolved) sessionStore.addQuestionHistory(item);
        }
        for (const item of sessionStore.questionHistory()) {
          if (item.session_id === sessionId && item.status === 'pending' && !liveIds.has(item.request_id)) {
            sessionStore.resolveQuestionHistory(item.request_id, '', 'error');
          }
        }
      }
      const pending = normalized.question_request;
      const alreadyResolved = pending && sessionStore.questionHistory().some(
        item => item.request_id === pending.request_id && item.status !== 'pending',
      );
      if (pending && !alreadyResolved) {
        // Preserve object identity so polling cannot erase an answer being typed.
        if (pending.request_id !== questionAtStart?.request_id) {
          sessionStore.setSessionQuestionRequest(sessionId, pending);
          sessionStore.addQuestionHistory(pending);
        }
        sessionStore.setSessionStatus(sessionId, '需要回答问题');
      } else if (!pending && questionAtStart) {
        sessionStore.setSessionQuestionRequest(sessionId, null);
        sessionStore.resolveQuestionHistory(questionAtStart.request_id, '', 'error');
      }
    }
    if (normalized.workflow_background_active && normalized.running) {
      workflowBackgroundSessions.add(normalized.session_id);
    } else if (!normalized.running) {
      workflowBackgroundSessions.delete(normalized.session_id);
    }
    updatePendingGuidanceMeta(
      normalized.session_id,
      normalized.pending_guidance_count,
      Boolean(normalized.guidance_worker_active),
      Boolean(normalized.pending_guidance_interrupt),
    );
    return normalized;
  } catch {
    return null;
  }
}

export type GuideRunningSessionOptions = {
  mode?: GuidanceMode;
  interrupt?: boolean;
  cancel_descendants?: boolean;
  client_request_id?: string;
  target_turn_id?: string;
  apply_policy?: 'next_safe_point' | 'after_current_tool';
  descendant_policy?: 'preserve' | 'cancel_invalidated' | 'cancel_all';
  supersedes_guidance_id?: string;
  output_format?: Record<string, any>;
  outcome_contract?: Record<string, any>;
  file_refs?: WorkspaceFileRef[];
  session_id?: string;
};

export type GuideSessionResponse = ChatRuntimeStatus & {
  status: string;
  guidance_id?: string;
  client_request_id?: string;
  requested_mode?: GuidanceMode;
  accepted_mode?: GuidanceMode;
  target_turn_id?: string;
  sequence?: number;
  guidance_status?: GuidanceStatus;
  apply_state?: string;
  downgrade_reason?: string;
  interrupt_requested?: boolean;
  cancel_descendants_requested?: boolean;
  goal_replaced?: boolean;
  cancelled_descendant_session_ids?: string[];
  cancelled_worker_session_ids?: string[];
};

export async function cancelSessionRun(
  sessionId: string,
  options: CancelSessionOptions = {},
): Promise<ChatRuntimeStatus & {
  status: string;
  cancelled_worker_task?: boolean;
  cancelled_descendant_session_ids?: string[];
  parent_notified_session_ids?: string[];
  replan_session_id?: string;
  replan_queued?: boolean;
}> {
  if (!sessionId) {
    throw new Error('没有活动会话');
  }

  const sessionSocket = activeChatSockets.get(sessionId);
  if (sessionSocket && sessionSocket.readyState === WebSocket.OPEN) {
    sessionSocket.send(
      JSON.stringify({
        action: 'cancel',
        session_id: sessionId,
      })
    );
  }

  const params = new URLSearchParams();
  params.set('session_id', sessionId);
  if (options.notify_parent) params.set('notify_parent', 'true');
  if (options.auto_replan) params.set('auto_replan', 'true');
  if (options.controller_session_id) params.set('controller_session_id', options.controller_session_id);
  if (options.clear_guidance) params.set('clear_guidance', 'true');

  const resp = await fetch(`${API_BASE}/api/chat/cancel?${params.toString()}`, {
    method: 'POST',
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data?.detail || data?.error || '取消请求失败'));
  }

  const normalized = {
    status: String(data.status || 'cancelled'),
    session_id: String(data.session_id || sessionId),
    running: isForegroundChatRunning(data),
    chat_running: typeof data.chat_running === 'boolean' ? data.chat_running : undefined,
    background_running: Boolean(data.background_running),
    pending_guidance_count: Math.max(0, Number(data.pending_guidance_count || 0)),
    pending_guidance_interrupt: Boolean(data.pending_guidance_interrupt),
    guidance_worker_active: Boolean(data.guidance_worker_active),
    cancelled_worker_task: Boolean(data.cancelled_worker_task),
    cancelled_descendant_session_ids: Array.isArray(data.cancelled_descendant_session_ids)
      ? data.cancelled_descendant_session_ids.map((item: any) => String(item || '')).filter(Boolean)
      : [],
    parent_notified_session_ids: Array.isArray(data.parent_notified_session_ids)
      ? data.parent_notified_session_ids.map((item: any) => String(item || '')).filter(Boolean)
      : [],
    replan_session_id: String(data.replan_session_id || ''),
    replan_queued: Boolean(data.replan_queued),
  };

  const socketToClose = activeChatSockets.get(normalized.session_id);
  if (socketToClose) {
    try {
      socketToClose.close();
    } catch {}
    unregisterChatSocket(socketToClose);
  }
  updatePendingGuidanceMeta(
    normalized.session_id,
    normalized.pending_guidance_count,
    Boolean(normalized.guidance_worker_active),
    Boolean(normalized.pending_guidance_interrupt),
  );
  return normalized;
}

export async function guideSession(
  sessionId: string,
  message: string,
  options: Omit<GuideRunningSessionOptions, 'session_id'> = {},
): Promise<GuideSessionResponse> {
  if (!sessionId) {
    throw new Error('没有活动会话');
  }

  const resp = await fetch(`${API_BASE}/api/chat/guide`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      mode: options.mode,
      client_request_id: options.client_request_id || generateId(),
      target_turn_id: options.target_turn_id,
      apply_policy: options.apply_policy || 'next_safe_point',
      descendant_policy: options.descendant_policy || 'preserve',
      supersedes_guidance_id: options.supersedes_guidance_id,
      interrupt: Boolean(options.interrupt),
      cancel_descendants: Boolean(options.cancel_descendants),
      output_format: options.output_format,
      outcome_contract: options.outcome_contract,
      file_refs: options.file_refs,
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
    throw new Error(String(detail || data?.error || '追加指导请求失败'));
  }

  const normalized = {
    status: String(data.status || 'queued'),
    guidance_id: typeof data.guidance_id === 'string' ? data.guidance_id : undefined,
    client_request_id: typeof data.client_request_id === 'string' ? data.client_request_id : undefined,
    requested_mode: data.requested_mode === 'steer' || data.requested_mode === 'queue' ? data.requested_mode : undefined,
    accepted_mode: data.accepted_mode === 'steer' || data.accepted_mode === 'queue' ? data.accepted_mode : undefined,
    target_turn_id: typeof data.target_turn_id === 'string' ? data.target_turn_id : undefined,
    sequence: Number.isFinite(Number(data.sequence)) ? Number(data.sequence) : undefined,
    guidance_status: typeof data.guidance_status === 'string' ? data.guidance_status as GuidanceStatus : undefined,
    apply_state: typeof data.apply_state === 'string' ? data.apply_state : undefined,
    downgrade_reason: typeof data.downgrade_reason === 'string' ? data.downgrade_reason : undefined,
    interrupt_requested: Boolean(data.interrupt_requested),
    cancel_descendants_requested: Boolean(data.cancel_descendants_requested),
    goal_replaced: Boolean(data.goal_replaced),
    cancelled_descendant_session_ids: Array.isArray(data.cancelled_descendant_session_ids)
      ? data.cancelled_descendant_session_ids
          .map((item: any) => String(item || ''))
          .filter(Boolean)
      : [],
    cancelled_worker_session_ids: Array.isArray(data.cancelled_worker_session_ids)
      ? data.cancelled_worker_session_ids
          .map((item: any) => String(item || ''))
          .filter(Boolean)
      : [],
    session_id: String(data.session_id || sessionId),
    running: isForegroundChatRunning(data),
    chat_running: typeof data.chat_running === 'boolean' ? data.chat_running : undefined,
    background_running: Boolean(data.background_running),
    pending_guidance_count: Math.max(0, Number(data.pending_guidance_count || 0)),
    pending_guidance_interrupt: Boolean(data.pending_guidance_interrupt),
    guidance_worker_active: Boolean(data.guidance_worker_active),
    pending_steer_count: Math.max(0, Number(data.pending_steer_count || 0)),
    pending_queue_count: Math.max(0, Number(data.pending_queue_count || 0)),
  };

  updatePendingGuidanceMeta(
    normalized.session_id,
    normalized.pending_guidance_count,
    Boolean(normalized.guidance_worker_active),
    Boolean(normalized.pending_guidance_interrupt),
  );
  if (normalized.interrupt_requested && activeWsSessionId === normalized.session_id) {
    activeWsSessionId = null;
  }
  return normalized;
}

export async function guideRunningSession(
  message: string,
  options: GuideRunningSessionOptions = {},
): Promise<GuideSessionResponse> {
  const sessionId = options.session_id || sessionStore.currentSessionId();
  if (!sessionId) {
    throw new Error('没有活动会话');
  }

  const normalized = await guideSession(sessionId, message, options);
  if (sessionStore.currentSessionId() === normalized.session_id) {
    sessionStore.setIsRunning(
      Boolean(
        normalized.running ||
        normalized.pending_guidance_count > 0 ||
        normalized.guidance_worker_active,
      ),
    );
  }
  return normalized;
}
