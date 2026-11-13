import {
  EventType,
  HttpAgent,
  runHttpRequest,
  transformHttpEventStream,
  type HttpAgentFetchFn,
} from '@ag-ui/client';
import type { Message, RunAgentInput } from '@ag-ui/core';

export type TCodeRuntimeId = 'tcode-native' | 'opencode' | (string & {});

export type TCodeAgentOptions = {
  baseUrl: string;
  workspaceId: string;
  agentId?: string;
  threadId?: string;
  runtimeId?: TCodeRuntimeId;
  modelId?: string;
  executionProfile?: 'fast' | 'standard' | 'deep' | 'orchestrated';
  schedulerMode?: 'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder';
  reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high';
  disconnectPolicy?: 'cancel' | 'detach';
  clientRequestId?: string;
  clientCapabilities?: TCodeClientCapabilities;
  apiKey?: string;
  fetch?: HttpAgentFetchFn;
  initialMessages?: Message[];
};

export type TCodeClientCapabilities = {
  mcpApps?: boolean;
  iframe?: boolean;
  files?: boolean;
  images?: boolean;
};

export type TCodeAgentDescriptor = {
  id: string;
  name: string;
  defaultRuntimeId: string;
  protocol: string;
  protocolVersion: string;
  capabilities?: Record<string, unknown>;
};

export type TCodeAgentMessage = Message;

export type TCodeThreadSummary = {
  threadId: string;
  title: string;
  agentName: string;
  status: string;
  workspaceId: string;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
  messagePreview: string;
  isArchived: boolean;
};

export type TCodeThread = TCodeThreadSummary & {
  messages: Message[];
};

export type TCodeDeleteThreadOptions = {
  /** Bounds the wait, not the server-side cleanup. Defaults to 30 seconds. */
  timeoutMs?: number;
  signal?: AbortSignal;
};

export type TCodeRuntime = {
  runtimeId: string;
  displayName: string;
  available: boolean;
  enabled: boolean;
  release: string;
  models: string[];
  capabilities: Record<string, unknown>;
  diagnostics?: string[];
};

export type TCodeHarnessState = {
  session_id: string;
  runtime_id: string;
  model_id: string;
  models?: Record<string, string>;
  switch_version: number;
  pending_handoff_id?: string;
  handoff_notice_pending?: boolean;
  summary_status?: 'generated' | 'fallback' | '';
  truncated?: boolean;
  warning_code?: string;
  previous_runtime_id?: string;
};

export type TCodeHarnessSwitchRequest = {
  targetRuntimeId: string;
  modelId?: string;
  expectedCurrentRuntimeId: string;
  expectedSwitchVersion: number;
  idempotencyKey?: string;
};

export type TCodeGuidanceOptions = {
  workspaceId?: string;
  mode?: 'steer' | 'queue';
  clientRequestId?: string;
  targetTurnId?: string;
  applyPolicy?: 'next_safe_point' | 'after_current_tool';
  descendantPolicy?: 'preserve' | 'cancel_invalidated' | 'cancel_all';
  interrupt?: boolean;
  cancelDescendants?: boolean;
};

export type TCodeArtifact = {
  type: 'artifact';
  artifact_id?: string;
  title: string;
  uri: string;
  media_type: string;
  size?: number;
  [key: string]: unknown;
};

export type TCodeMCPAppBlock = {
  type: 'mcp_app';
  server_name: string;
  tool_name: string;
  resource_uri: string;
  resource_token: string;
  mime_type: 'text/html;profile=mcp-app';
  tool_input: Record<string, unknown>;
  tool_result: Record<string, unknown>;
  fallback: string;
};

const trimBase = (value: string) => String(value || '').replace(/\/$/, '');

class TCodeHttpAgent extends HttpAgent {
  readonly historyBaseUrl: string;
  readonly historyWorkspaceId: string;
  readonly historyApiKey: string;

  constructor(options: TCodeAgentOptions & { agentId: string }) {
    const baseUrl = trimBase(options.baseUrl);
    super({
      url: `${baseUrl}/api/ag-ui/v1/agents/${encodeURIComponent(options.agentId)}/run`,
      agentId: options.agentId,
      threadId: options.threadId,
      initialMessages: options.initialMessages || [],
      headers: options.apiKey ? { Authorization: `Bearer ${options.apiKey}` } : {},
      ...(options.fetch ? { fetch: options.fetch } : {}),
    });
    this.historyBaseUrl = baseUrl;
    this.historyWorkspaceId = options.workspaceId;
    this.historyApiKey = options.apiKey || '';
  }

  /**
   * CopilotKit v2 clears an agent before reconnecting an explicit thread and
   * expects connect() to replay the transcript. HttpAgent has no default
   * reconnect transport, so expose the persisted TCode session as a small
   * standards-compliant AG-UI event stream.
   */
  protected connect(input: RunAgentInput): ReturnType<HttpAgent['run']> {
    const responseStream = runHttpRequest(async () => {
      const query = new URLSearchParams({ workspace_id: this.historyWorkspaceId });
      const response = await this.fetch(
        `${this.historyBaseUrl}/api/sessions/${encodeURIComponent(this.threadId)}?${query}`,
        {
          method: 'GET',
          signal: this.abortController.signal,
          headers: {
            Accept: 'application/json',
            ...(this.historyApiKey ? { Authorization: `Bearer ${this.historyApiKey}` } : {}),
          },
        },
      );

      let messages: Message[] = [];
      if (response.status !== 404) {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(String(body.detail || body.message || `TCode request failed (${response.status})`));
        }
        messages = historyMessages(body.messages);
      }

      const events = [
        {
          type: EventType.RUN_STARTED,
          threadId: this.threadId,
          runId: input.runId,
        },
        {
          type: EventType.MESSAGES_SNAPSHOT,
          messages,
        },
        {
          type: EventType.RUN_FINISHED,
          threadId: this.threadId,
          runId: input.runId,
          outcome: { type: 'success' },
        },
      ];
      const payload = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
      return new Response(payload, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    return transformHttpEventStream(responseStream);
  }
}

export function createTCodeAgent(options: TCodeAgentOptions): HttpAgent {
  const agentId = options.agentId || 'default';
  const agent = new TCodeHttpAgent({
    ...options,
    agentId,
  });
  const run = agent.run.bind(agent);
  agent.run = (input) => {
    const forwarded = input.forwardedProps && typeof input.forwardedProps === 'object'
      ? input.forwardedProps as Record<string, unknown>
      : {};
    const existing = forwarded.tcode && typeof forwarded.tcode === 'object'
      ? forwarded.tcode as Record<string, unknown>
      : {};
    return run({
      ...input,
      forwardedProps: {
        ...forwarded,
        tcode: {
          workspaceId: options.workspaceId,
          runtimeId: options.runtimeId || 'tcode-native',
          modelId: options.modelId || '',
          ...(options.executionProfile ? { executionProfile: options.executionProfile } : {}),
          ...(options.schedulerMode ? { schedulerMode: options.schedulerMode } : {}),
          ...(options.reasoningEffort ? { reasoningEffort: options.reasoningEffort } : {}),
          ...(options.disconnectPolicy ? { disconnectPolicy: options.disconnectPolicy } : {}),
          ...(options.clientRequestId ? { clientRequestId: options.clientRequestId } : {}),
          ...(options.clientCapabilities ? { clientCapabilities: options.clientCapabilities } : {}),
          ...existing,
        },
      },
    });
  };
  return agent;
}

function threadSummary(item: Record<string, unknown>): TCodeThreadSummary {
  return {
    threadId: String(item.session_id || ''),
    title: String(item.title || item.message_preview || '未命名会话'),
    agentName: String(item.agent_name || ''),
    status: String(item.status || ''),
    workspaceId: String(item.workspace_id || ''),
    createdAt: Number(item.created_at || 0),
    updatedAt: Number(item.updated_at || 0),
    messageCount: Number(item.message_count || 0),
    messagePreview: String(item.message_preview || ''),
    isArchived: Boolean(item.is_archived),
  };
}

function historyMessages(items: unknown): Message[] {
  if (!Array.isArray(items)) return [];
  return items.flatMap((raw, index) => {
    if (!raw || typeof raw !== 'object') return [];
    const item = raw as Record<string, unknown>;
    const role = String(item.role || '');
    if (role !== 'user' && role !== 'assistant') return [];
    const content = typeof item.content === 'string' ? item.content : '';
    if (!content && role === 'user') return [];
    return [{
      id: String(item.id || `history-${index}`),
      role,
      content,
    } as Message];
  });
}

function waitForDeletionPoll(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, 1000);
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  });
}

export class TCodeGatewayClient {
  readonly baseUrl: string;
  readonly apiKey: string;
  readonly fetch: typeof globalThis.fetch;

  constructor(options: Pick<TCodeAgentOptions, 'baseUrl' | 'apiKey'> & { fetch?: typeof globalThis.fetch }) {
    this.baseUrl = trimBase(options.baseUrl);
    this.apiKey = options.apiKey || '';
    this.fetch = options.fetch || globalThis.fetch.bind(globalThis);
  }

  private async request(path: string, init: RequestInit = {}): Promise<any> {
    const response = await this.fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
        ...(init.headers || {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(String(body.detail || body.message || `TCode request failed (${response.status})`));
    return body;
  }

  async listAgents(): Promise<TCodeAgentDescriptor[]> {
    const body = await this.request('/api/ag-ui/v1/agents');
    return Array.isArray(body.agents) ? body.agents : [];
  }

  async getAgent(agentId = 'default'): Promise<TCodeAgentDescriptor> {
    return await this.request(`/api/ag-ui/v1/agents/${encodeURIComponent(agentId)}`);
  }

  async listRuntimes(workspaceId = '', includeModels = true): Promise<TCodeRuntime[]> {
    const query = new URLSearchParams({ includeModels: String(includeModels) });
    if (workspaceId) query.set('workspaceId', workspaceId);
    const body = await this.request(`/api/ag-ui/v1/runtimes?${query}`);
    return Array.isArray(body.runtimes) ? body.runtimes : [];
  }

  async listThreads(workspaceId: string, archived = false): Promise<TCodeThreadSummary[]> {
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      archived: String(archived),
    });
    const body = await this.request(`/api/sessions?${query}`);
    const items = Array.isArray(body.sessions) ? body.sessions : [];
    return items.map((item: Record<string, unknown>) => threadSummary(item));
  }

  async getThread(threadId: string, workspaceId: string): Promise<TCodeThread> {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    const body = await this.request(
      `/api/sessions/${encodeURIComponent(threadId)}?${query}`,
    );
    return {
      ...threadSummary(body),
      threadId: String(body.session_id || threadId),
      messages: historyMessages(body.messages),
    };
  }

  async getHarnessState(threadId: string, workspaceId: string): Promise<TCodeHarnessState> {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    return await this.request(
      `/api/sessions/${encodeURIComponent(threadId)}/harness-state?${query}`,
    );
  }

  async switchHarness(
    threadId: string,
    workspaceId: string,
    options: TCodeHarnessSwitchRequest,
  ): Promise<TCodeHarnessState & Record<string, unknown>> {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    return await this.request(
      `/api/sessions/${encodeURIComponent(threadId)}/harness-switch?${query}`,
      {
        method: 'POST',
        headers: options.idempotencyKey
          ? { 'Idempotency-Key': options.idempotencyKey }
          : {},
        body: JSON.stringify({
          target_runtime_id: options.targetRuntimeId,
          model_id: options.modelId || '',
          expected_current_runtime_id: options.expectedCurrentRuntimeId,
          expected_switch_version: options.expectedSwitchVersion,
        }),
      },
    );
  }

  async submitGuidance(
    threadId: string,
    message: string,
    options: TCodeGuidanceOptions = {},
  ): Promise<Record<string, unknown>> {
    const query = new URLSearchParams();
    if (options.workspaceId) query.set('workspace_id', options.workspaceId);
    return await this.request(`/api/chat/guide${query.size ? `?${query}` : ''}`, {
      method: 'POST',
      body: JSON.stringify({
        session_id: threadId,
        message,
        mode: options.mode,
        client_request_id: options.clientRequestId,
        target_turn_id: options.targetTurnId,
        apply_policy: options.applyPolicy || 'next_safe_point',
        descendant_policy: options.descendantPolicy || 'preserve',
        interrupt: Boolean(options.interrupt),
        cancel_descendants: Boolean(options.cancelDescendants),
      }),
    });
  }

  async listGuidance(threadId: string, workspaceId = ''): Promise<Array<Record<string, unknown>>> {
    const query = new URLSearchParams();
    if (workspaceId) query.set('workspace_id', workspaceId);
    const body = await this.request(
      `/api/chat/guidance/${encodeURIComponent(threadId)}${query.size ? `?${query}` : ''}`,
    );
    return Array.isArray(body.items) ? body.items : [];
  }

  async deleteGuidance(threadId: string, guidanceId: string, workspaceId = ''): Promise<void> {
    const query = new URLSearchParams();
    if (workspaceId) query.set('workspace_id', workspaceId);
    await this.request(
      `/api/chat/guidance/${encodeURIComponent(threadId)}/${encodeURIComponent(guidanceId)}${query.size ? `?${query}` : ''}`,
      { method: 'DELETE' },
    );
  }

  async listArtifacts(threadId: string, workspaceId: string): Promise<TCodeArtifact[]> {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    const body = await this.request(`/api/sessions/${encodeURIComponent(threadId)}?${query}`);
    const messages = Array.isArray(body.messages) ? body.messages : [];
    return messages.flatMap((message: Record<string, unknown>) => {
      const blocks = Array.isArray(message.content_blocks) ? message.content_blocks : [];
      return blocks.filter((block: unknown): block is TCodeArtifact => Boolean(
        block && typeof block === 'object'
        && (block as Record<string, unknown>).type === 'artifact'
        && typeof (block as Record<string, unknown>).uri === 'string',
      ));
    });
  }

  async downloadArtifact(artifact: TCodeArtifact, signal?: AbortSignal): Promise<Blob> {
    if (!artifact.uri.startsWith('/api/')) {
      throw new Error('Artifact URI must be a TCode API path.');
    }
    const response = await this.fetch(`${this.baseUrl}${artifact.uri}`, {
      signal,
      headers: {
        ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
      },
    });
    if (!response.ok) throw new Error(`Artifact download failed (${response.status})`);
    return await response.blob();
  }

  /** Submit deletion once, then wait for TCode's asynchronous session cleanup. */
  async deleteThread(
    threadId: string,
    workspaceId: string,
    options: TCodeDeleteThreadOptions = {},
  ): Promise<void> {
    if (!threadId.trim() || !workspaceId.trim()) {
      throw new Error('删除会话需要 threadId 和 workspaceId。');
    }
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new Error('timeoutMs 必须是正数。');
    }
    const controller = new AbortController();
    const abort = () => controller.abort(options.signal?.reason);
    if (options.signal?.aborted) abort();
    else options.signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => controller.abort(new Error(
      '删除结果尚未确认，服务端可能仍在处理，请刷新历史列表检查。',
    )), timeoutMs);
    const query = new URLSearchParams({ workspace_id: workspaceId });
    const path = `/api/sessions/${encodeURIComponent(threadId)}`;
    try {
      controller.signal.throwIfAborted();
      const accepted = await this.request(`${path}?${query}`, {
        method: 'DELETE', signal: controller.signal,
      });
      if (accepted.deleted_session_id !== threadId || accepted.pending !== true) {
        throw new Error(String(accepted.error || '服务端未确认会话删除请求。'));
      }
      while (true) {
        controller.signal.throwIfAborted();
        const status = await this.request(`${path}/delete-status?${query}`, {
          signal: controller.signal,
        });
        if (status.session_id !== threadId || typeof status.pending !== 'boolean') {
          throw new Error('服务端返回了无效的会话删除状态。');
        }
        if (!status.pending && !status.running) {
          if (status.error) throw new Error(String(status.error));
          return;
        }
        await waitForDeletionPoll(controller.signal);
      }
    } catch (reason) {
      if (controller.signal.aborted) throw controller.signal.reason;
      throw reason;
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', abort);
    }
  }

  async cancelRun(runId: string, threadId: string): Promise<void> {
    await this.request(`/api/ag-ui/v1/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ threadId }),
    });
  }

  async listInteractions(threadId: string): Promise<Array<Record<string, unknown>>> {
    const body = await this.request(`/api/ag-ui/v1/interactions?threadId=${encodeURIComponent(threadId)}`);
    return Array.isArray(body.interactions) ? body.interactions : [];
  }

  async resolveInteraction(interactionId: string, resolution: Record<string, unknown>): Promise<void> {
    await this.request(`/api/ag-ui/v1/interactions/${encodeURIComponent(interactionId)}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ resolution }),
    });
  }
}
