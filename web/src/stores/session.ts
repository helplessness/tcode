import { createSignal, createRoot, createEffect } from 'solid-js';
import { generateId } from '../utils/id';
import { toVisibleTeamAgentName, usesFixedScheduler } from '../utils/teamRegistry';
import { preferenceKeys, readPreference } from '../utils/userPreferences';
import {
  sanitizeMessageContentBlocks,
  type MessageContentBlock,
} from '../utils/contentBlocks';
import type {
  PermissionRequestEvent,
  QuestionPayload as ProtocolQuestionPayload,
  QuestionRequestEvent,
  SessionTaskRuntimeSummary,
  TokenUsage,
  ToolCall as ProtocolToolCall,
} from '../api/generated/protocol';
import type { HarnessStateProjection, HarnessSwitchResponse } from '../api/harness';

type WithoutNull<T> = {
  [Key in keyof T]: Exclude<T[Key], null>;
};

const STORAGE_KEY = 'tcode_agent_session_summaries_v2';
const LEGACY_STORAGE_KEY = 'tcode_agent_sessions';
const STORAGE_CURRENT_KEY = 'tcode_agent_current_session';
const STORAGE_CONTINUED_ASYNC_JOBS_KEY = 'tcode_agent_continued_async_jobs';
const STORAGE_WORKFLOW_PROPOSAL_RUNS_KEY = 'tcode_agent_workflow_proposal_runs';
const STORAGE_IDENTITY_SCOPE_KEY = 'tcode_agent_identity_scope';
const SUBAGENT_REFRESH_DEBOUNCE_MS = 800;
const LEGACY_SYMPHONY_COMPLETION_NOTICE_PREFIX = '[系统通知] Symphony 异步任务完成';

export function toVisibleAgentName(agentName?: string | null): string {
  return toVisibleTeamAgentName(agentName);
}

export interface Message {
  id: string;
  created_at?: number;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  tool_calls?: ToolCall[];
  process_notes?: ProcessNote[];
  presentation?: string;
  content_blocks?: MessageContentBlock[];
  usage?: Usage;
  research_grounding?: Record<string, any>;
  verification?: MessageVerification;
  metadata?: {
    guidance?: {
      id?: string;
      sequence?: number;
      mode?: 'steer' | 'queue';
      target_turn_id?: string;
      applied_safe_point?: string;
      contract_revision_id?: string;
    };
    superseded_by_guidance?: boolean;
    stream_epoch?: number;
    [key: string]: any;
  };
}

export interface MessageVerification {
  advisory?: boolean;
  verification_mode?: 'light' | 'standard' | 'strict';
  issues?: string[];
  findings?: { status: 'failed' | 'unverified'; title: string; reason: string }[];
  delivery_state?: 'accepted' | 'modification_requested';
  verification_status: 'VERIFIED' | 'UNVERIFIED' | 'PARTIAL' | 'NOT_REQUIRED';
  confidence: number;
  confidence_components?: {
    objective_checks: number;
    evidence_coverage: number;
    independent_validator: number;
    agreement: number;
  };
  required_proof_level?: 'P0' | 'P1' | 'P2' | 'P3' | 'P4';
  achieved_proof_level?: 'P0' | 'P1' | 'P2' | 'P3' | 'P4';
  criteria_passed?: number;
  criteria_total?: number;
  criteria_failed?: number;
  criteria_unverified?: number;
  needs_user_confirmation?: boolean;
  user_confirmed?: boolean;
  superseded?: boolean;
  confirmation_state?: 'pending' | 'accepted' | 'modification_requested' | 'objective_failure' | 'repair_exhausted';
  repair_attempt?: number;
  max_repair_attempts?: number;
  validator?: 'deterministic' | 'semantic' | 'semantic_unavailable' | 'terminal_fallback' | 'disabled';
}

export function sanitizeMessageVerification(value: unknown): MessageVerification | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const payload = value as Record<string, any>;
  const status = String(payload.verification_status || '').trim().toUpperCase();
  if (!['VERIFIED', 'UNVERIFIED', 'PARTIAL', 'NOT_REQUIRED'].includes(status)) {
    return undefined;
  }
  const rawConfidence = Number(payload.confidence);
  const rawComponents = payload.confidence_components;
  const sanitizeScore = (score: unknown) => {
    const numeric = Number(score);
    return Number.isFinite(numeric) ? Math.max(0, Math.min(1, numeric)) : 0;
  };
  const proofLevel = (value: unknown) => {
    const normalized = String(value || '').trim().toUpperCase();
    return (['P0', 'P1', 'P2', 'P3', 'P4'] as const).find(
      (candidate) => candidate === normalized,
    );
  };
  const validator = String(payload.validator || '').trim().toLowerCase();
  const sanitizedValidator = (
    ['deterministic', 'semantic', 'semantic_unavailable', 'terminal_fallback', 'disabled'] as const
  ).find((candidate) => candidate === validator);
  const sanitized: MessageVerification = {
    verification_status: status as MessageVerification['verification_status'],
    confidence: Number.isFinite(rawConfidence)
      ? Math.max(0, Math.min(1, rawConfidence))
      : 0,
  };
  if (rawComponents && typeof rawComponents === 'object' && !Array.isArray(rawComponents)) {
    sanitized.confidence_components = {
      objective_checks: sanitizeScore(rawComponents.objective_checks),
      evidence_coverage: sanitizeScore(rawComponents.evidence_coverage),
      independent_validator: sanitizeScore(rawComponents.independent_validator),
      agreement: sanitizeScore(rawComponents.agreement),
    };
  }
  const requiredProofLevel = proofLevel(payload.required_proof_level);
  const achievedProofLevel = proofLevel(payload.achieved_proof_level);
  if (requiredProofLevel) sanitized.required_proof_level = requiredProofLevel;
  if (achievedProofLevel) sanitized.achieved_proof_level = achievedProofLevel;
  const rawCriteriaTotal = Number(payload.criteria_total);
  const rawCriteriaPassed = Number(payload.criteria_passed);
  if (Number.isInteger(rawCriteriaTotal) && rawCriteriaTotal >= 0) {
    sanitized.criteria_total = rawCriteriaTotal;
    if (Number.isInteger(rawCriteriaPassed) && rawCriteriaPassed >= 0) {
      sanitized.criteria_passed = Math.min(rawCriteriaPassed, rawCriteriaTotal);
    }
    for (const key of ['criteria_failed', 'criteria_unverified'] as const) {
      const count = Number(payload[key]);
      if (Number.isInteger(count) && count >= 0 && count <= rawCriteriaTotal - (sanitized.criteria_passed || 0)) sanitized[key] = count;
    }
  }
  if (payload.needs_user_confirmation !== undefined) {
    sanitized.needs_user_confirmation = Boolean(payload.needs_user_confirmation);
  }
  if (payload.advisory) {
    sanitized.advisory = true;
    sanitized.needs_user_confirmation = false;
    sanitized.issues = Array.isArray(payload.issues) ? payload.issues.filter((item: unknown): item is string => typeof item === 'string').slice(0, 5) : [];
    if (['accepted', 'modification_requested'].includes(payload.delivery_state)) sanitized.delivery_state = payload.delivery_state;
  }
  if (Array.isArray(payload.findings)) sanitized.findings = payload.findings.filter((item: any) =>
    item && ['failed', 'unverified'].includes(item.status) && typeof item.title === 'string' && typeof item.reason === 'string',
  ).slice(0, 10).map((item: any) => ({ status: item.status, title: item.title.slice(0, 160), reason: item.reason.slice(0, 160) }));
  if (['light', 'standard', 'strict'].includes(payload.verification_mode)) sanitized.verification_mode = payload.verification_mode;
  if (payload.user_confirmed !== undefined) {
    sanitized.user_confirmed = Boolean(payload.user_confirmed);
  }
  if (payload.superseded !== undefined) {
    sanitized.superseded = Boolean(payload.superseded);
  }
  const confirmationState = String(payload.confirmation_state || '').trim().toLowerCase();
  if (
    confirmationState === 'pending' ||
    confirmationState === 'accepted' ||
    confirmationState === 'modification_requested' ||
    confirmationState === 'objective_failure' ||
    confirmationState === 'repair_exhausted'
  ) {
    if (!sanitized.advisory) sanitized.confirmation_state = confirmationState;
  } else if (sanitized.needs_user_confirmation) {
    sanitized.confirmation_state = 'pending';
  }
  const repairAttempt = Number(payload.repair_attempt || 0);
  if (Number.isFinite(repairAttempt) && repairAttempt > 0) {
    sanitized.repair_attempt = Math.min(3, Math.floor(repairAttempt));
  }
  if (sanitizedValidator) sanitized.validator = sanitizedValidator;
  return sanitized;
}

export interface ProcessNote {
  id: string;
  phase?: string;
  status?: string;
  message: string;
  tool_name?: string;
  call_id?: string;
  elapsed_seconds?: number;
  current?: number;
  total?: number;
  unit?: string;
  seq?: number;
  stream?: string;
  timestamp: number;
}

// 进入页面状态前 sanitizeToolCall 已把 wire 协议中的 null 归一为 undefined。
export type ToolCall = WithoutNull<ProtocolToolCall>;

export type Usage = TokenUsage;

export interface UsageStats {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
}

export type PermissionRequest = Omit<PermissionRequestEvent, 'type'>;

export interface RunningTool {
  tool_name: string;
  tool_args?: Record<string, any>;
}

export type QuestionPayload = WithoutNull<ProtocolQuestionPayload>;

export type QuestionRequest = Omit<QuestionRequestEvent, 'type' | 'question'> & {
  question: QuestionPayload;
};

export interface QuestionHistoryItem {
  request_id: string;
  session_id: string;
  question: QuestionPayload;
  answer?: string;
  status: 'pending' | 'answered' | 'timeout' | 'error';
  created_at: number;
  resolved_at?: number;
}

export interface Session {
  session_id: string;
  title?: string;
  messages: Message[];
  created_at: number;
  updated_at: number;
  agent_name?: string;
  model?: string;
  status?: string;
  workspace_id?: string;
  workdir?: string;
  workdir_name?: string;
  task_runtime?: SessionTaskRuntimeSummary;
  is_archived?: boolean;
  archived_at?: number;
  async_completion_unread_count?: number;
  mode?: 'general' | 'research';
  research_id?: string;
  mode_source?: string;
  archive_cause?: string;
  archive_locked?: boolean;
  execution_profile?: 'fast' | 'standard' | 'deep' | 'orchestrated';
  scheduler_mode?: 'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder';
  reasoning_effort?: 'minimal' | 'low' | 'medium' | 'high';
  metadata?: Record<string, any>;
}

export interface FilePreviewRequest {
  path: string;
  key: number;
}

export interface SubagentState {
  parent_session_id: string;
  session_id: string;
  agent_name: string;
  display_name?: string;
  engine?: string;
  scheduler_mode?: string;
  status: 'running' | 'completed' | 'failed' | 'idle';
  message?: string;
  tools_used?: string[];
  conversation_with?: string[];
  last_response?: string;
  context?: {
    message_count?: number;
    tool_call_count?: number;
    last_user?: string;
    last_assistant?: string;
  };
  contract?: {
    goal?: string;
    deliverable?: string;
    stop_when?: string;
    allowed_tools?: 'all' | string[];
  };
  updated_at: number;
}

export interface SubagentChatEntry {
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: number;
}

type SessionRuntimeState = {
  isRunning: boolean;
  deliveryTurnRunning?: boolean;
  status: string;
  runningTool: RunningTool | null;
  permissionRequest: PermissionRequest | null;
  questionRequest: QuestionRequest | null;
};

type SessionMergeScope = 'active' | 'archived' | 'all';
type ContinuedAsyncJobsState = Record<string, Record<string, number>>;
type WorkflowProposalRunState = Record<string, Record<string, {
  run_id: string;
  template_key: string;
  created_at: number;
}>>;

export function sanitizeToolCall(toolCall: any): ToolCall | null {
  const toolName = String(toolCall?.tool_name || '').trim();
  if (!toolName) return null;

  const argumentsValue = toolCall?.arguments;
  const callId = String(toolCall?.call_id || '').trim();
  const rawState = String(toolCall?.state || '').trim();
  const state = (['pending', 'running', 'completed', 'error', 'skipped', 'interrupted'] as const).find(
    (candidate) => candidate === rawState,
  );
  const output =
    typeof toolCall?.output === 'string'
      ? toolCall.output
      : toolCall?.output == null
        ? undefined
        : String(toolCall.output);
  const error =
    typeof toolCall?.error === 'string'
      ? toolCall.error
      : toolCall?.error == null
        ? undefined
        : String(toolCall.error);
  const metadataValue = toolCall?.metadata;
  const metadata =
    metadataValue && typeof metadataValue === 'object' && !Array.isArray(metadataValue)
      ? (metadataValue as Record<string, any>)
      : undefined;

  return {
    tool_name: toolName,
    arguments:
      argumentsValue && typeof argumentsValue === 'object' && !Array.isArray(argumentsValue)
        ? (argumentsValue as Record<string, any>)
        : {},
    call_id: callId || undefined,
    state,
    output: output || undefined,
    error: error || undefined,
    start_time: typeof toolCall.start_time === 'number' && Number.isFinite(toolCall.start_time) ? toolCall.start_time : undefined,
    metadata,
  };
}

export function sanitizeProcessNote(note: any): ProcessNote | null {
  const message = String(note?.message || '').trim();
  if (!message) return null;
  const elapsedSeconds = Number(note?.elapsed_seconds);
  const current = note?.current == null ? Number.NaN : Number(note.current);
  const total = note?.total == null ? Number.NaN : Number(note.total);
  const seq = note?.seq == null ? Number.NaN : Number(note.seq);
  return {
    id: String(note?.id || '').trim() || generateId(),
    phase: String(note?.phase || '').trim() || undefined,
    status: String(note?.status || '').trim() || undefined,
    message,
    tool_name: String(note?.tool_name || '').trim() || undefined,
    call_id: String(note?.call_id || '').trim() || undefined,
    elapsed_seconds: Number.isFinite(elapsedSeconds) ? elapsedSeconds : undefined,
    current: Number.isFinite(current) && current >= 0 ? current : undefined,
    total: Number.isFinite(total) && total > 0 ? total : undefined,
    unit: String(note?.unit || '').trim() || undefined,
    seq: Number.isFinite(seq) && seq >= 0 ? seq : undefined,
    stream: String(note?.stream || '').trim() || undefined,
    timestamp: Number(note?.timestamp || Date.now()),
  };
}

function sanitizeUsage(usage: any): Usage | undefined {
  if (!usage || typeof usage !== 'object') return undefined;
  return {
    prompt_tokens: Number(usage.prompt_tokens || 0),
    completion_tokens: Number(usage.completion_tokens || 0),
    total_tokens: Number(usage.total_tokens || 0),
  };
}

function isLegacySymphonyCompletionNotice(message: any): boolean {
  const role = String(message?.role || '').trim().toLowerCase();
  const content = String(message?.content || '');
  return role === 'system' && content.startsWith(LEGACY_SYMPHONY_COMPLETION_NOTICE_PREFIX);
}

export function sanitizeVisibleMessages(
  messages: any[],
  options: {
    includeToolMessages?: boolean;
  } = {},
): Message[] {
  if (!Array.isArray(messages)) return [];
  const includeToolMessages = Boolean(options.includeToolMessages);
  return messages
    .filter((m) => {
      if (isLegacySymphonyCompletionNotice(m)) return false;
      if (String(m?.content || '').startsWith('[SIDE CHAT CONTEXT IMPORT]')) return false;
      const role = String(m?.role || '').trim().toLowerCase();
      if (role === 'assistant' && Boolean(m?.metadata?.superseded_by_guidance)) {
        return false;
      }
      if (role === 'user' || role === 'assistant' || role === 'system') return true;
      if (includeToolMessages && role === 'tool') return true;
      return false;
    })
    .map((m) => {
      const toolCalls = Array.isArray(m?.tool_calls)
        ? m.tool_calls
            .map((toolCall: any) => sanitizeToolCall(toolCall))
            .filter((toolCall: ToolCall | null): toolCall is ToolCall => toolCall !== null)
        : undefined;
      const processNotes = Array.isArray(m?.process_notes)
        ? m.process_notes
            .map((note: any) => sanitizeProcessNote(note))
            .filter((note: ProcessNote | null): note is ProcessNote => note !== null)
        : undefined;
      const contentBlocks = sanitizeMessageContentBlocks(m?.content_blocks);

      return {
        id: typeof m?.id === 'string' && m.id ? m.id : generateId(),
        created_at: Number.isFinite(Number(m?.created_at))
          ? (Number(m.created_at) < 1_000_000_000_000 ? Number(m.created_at) * 1000 : Number(m.created_at))
          : undefined,
        role: String(m?.role || 'assistant') as Message['role'],
        content: typeof m?.content === 'string' ? m.content : String(m?.content ?? ''),
        tool_calls: toolCalls && toolCalls.length > 0 ? toolCalls : undefined,
        process_notes: processNotes && processNotes.length > 0 ? processNotes : undefined,
        presentation: typeof m?.presentation === 'string' ? m.presentation : undefined,
        content_blocks: contentBlocks.length > 0 ? contentBlocks : undefined,
        usage: sanitizeUsage(m?.usage),
        research_grounding:
          m?.research_grounding && typeof m.research_grounding === 'object' && !Array.isArray(m.research_grounding)
            ? (m.research_grounding as Record<string, any>)
            : undefined,
        verification: sanitizeMessageVerification(m?.verification),
        metadata:
          m?.metadata && typeof m.metadata === 'object' && !Array.isArray(m.metadata)
            ? (m.metadata as Message['metadata'])
            : undefined,
      };
    });
}

function loadSessionsFromStorage(): Session[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    // Never parse the legacy value: it can contain years of messages and large
    // tool outputs. The server will repopulate summaries and selected messages.
    localStorage.removeItem(LEGACY_STORAGE_KEY);
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      if (!Array.isArray(parsed)) return [];
      return parsed.map(toSessionStorageSummary);
    }
  } catch (e) {
    console.error('Failed to load sessions from storage:', e);
  }
  return [];
}

export function toSessionStorageSummary(session: Session): Session {
  return {
    ...session,
    // The server is authoritative for messages. SessionList fetches the selected
    // session after bootstrap, so persisting these can only make startup slower.
    messages: [],
  };
}

export function serializeSessionsForStorage(sessions: Session[]): string {
  return JSON.stringify(sessions.map(toSessionStorageSummary));
}

function saveSessionsToStorage(sessions: Session[]) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_KEY, serializeSessionsForStorage(sessions));
  } catch (e) {
    console.error('Failed to save sessions to storage:', e);
  }
}

function loadCurrentSessionFromStorage(): string | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    return localStorage.getItem(STORAGE_CURRENT_KEY);
  } catch (e) {
    console.error('Failed to load current session from storage:', e);
  }
  return null;
}

function saveCurrentSessionToStorage(sessionId: string | null) {
  if (typeof localStorage === 'undefined') return;
  try {
    if (sessionId) {
      localStorage.setItem(STORAGE_CURRENT_KEY, sessionId);
    } else {
      localStorage.removeItem(STORAGE_CURRENT_KEY);
    }
  } catch (e) {
    console.error('Failed to save current session to storage:', e);
  }
}

function sanitizeAsyncJobIdentifier(identifier: string): string {
  return String(identifier || '').trim();
}

function loadContinuedAsyncJobsFromStorage(): ContinuedAsyncJobsState {
  if (typeof localStorage === 'undefined') return {};
  try {
    const stored = localStorage.getItem(STORAGE_CONTINUED_ASYNC_JOBS_KEY);
    if (!stored) return {};
    const parsed = JSON.parse(stored);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};

    const normalized: ContinuedAsyncJobsState = {};
    for (const [sessionId, rawJobs] of Object.entries(parsed)) {
      if (!sessionId || !rawJobs || typeof rawJobs !== 'object' || Array.isArray(rawJobs)) continue;
      const jobs: Record<string, number> = {};
      for (const [identifier, timestamp] of Object.entries(rawJobs as Record<string, unknown>)) {
        const key = sanitizeAsyncJobIdentifier(identifier);
        if (!key) continue;
        jobs[key] = Math.max(1, Number(timestamp || Date.now()));
      }
      if (Object.keys(jobs).length > 0) {
        normalized[sessionId] = jobs;
      }
    }
    return normalized;
  } catch (e) {
    console.error('Failed to load continued async jobs from storage:', e);
  }
  return {};
}

function saveContinuedAsyncJobsToStorage(value: ContinuedAsyncJobsState) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_CONTINUED_ASYNC_JOBS_KEY, JSON.stringify(value || {}));
  } catch (e) {
    console.error('Failed to save continued async jobs to storage:', e);
  }
}

function sanitizeWorkflowProposalKey(value: string): string {
  return String(value || '').trim();
}

function loadWorkflowProposalRunsFromStorage(): WorkflowProposalRunState {
  if (typeof localStorage === 'undefined') return {};
  try {
    const stored = localStorage.getItem(STORAGE_WORKFLOW_PROPOSAL_RUNS_KEY);
    if (!stored) return {};
    const parsed = JSON.parse(stored);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};

    const normalized: WorkflowProposalRunState = {};
    for (const [sessionId, rawItems] of Object.entries(parsed)) {
      if (!sessionId || !rawItems || typeof rawItems !== 'object' || Array.isArray(rawItems)) continue;
      const items: WorkflowProposalRunState[string] = {};
      for (const [proposalKey, rawRecord] of Object.entries(rawItems as Record<string, unknown>)) {
        const key = sanitizeWorkflowProposalKey(proposalKey);
        const record = rawRecord && typeof rawRecord === 'object' && !Array.isArray(rawRecord)
          ? rawRecord as Record<string, unknown>
          : {};
        const runId = String(record.run_id || '').trim();
        if (!key || !runId) continue;
        items[key] = {
          run_id: runId,
          template_key: String(record.template_key || '').trim(),
          created_at: Math.max(1, Number(record.created_at || Date.now())),
        };
      }
      if (Object.keys(items).length > 0) {
        normalized[sessionId] = items;
      }
    }
    return normalized;
  } catch (e) {
    console.error('Failed to load workflow proposal runs from storage:', e);
  }
  return {};
}

function saveWorkflowProposalRunsToStorage(value: WorkflowProposalRunState) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_WORKFLOW_PROPOSAL_RUNS_KEY, JSON.stringify(value || {}));
  } catch (e) {
    console.error('Failed to save workflow proposal runs to storage:', e);
  }
}

function createSessionStore() {
  const [harnessSwitches, setHarnessSwitches] = createSignal<Set<string>>(new Set());
  const isHarnessSwitchPending = (sessionId: string) => harnessSwitches().has(sessionId);
  const setHarnessSwitchPending = (sessionId: string, pending: boolean) => setHarnessSwitches(previous => {
    const next = new Set(previous);
    if (pending) next.add(sessionId); else next.delete(sessionId);
    return next;
  });
  const [sessions, setSessions] = createSignal<Session[]>(loadSessionsFromStorage());
  const [currentSessionId, setCurrentSessionId] = createSignal<string | null>(loadCurrentSessionFromStorage());
  const [continuedAsyncJobs, setContinuedAsyncJobs] =
    createSignal<ContinuedAsyncJobsState>(loadContinuedAsyncJobsFromStorage());
  const [workflowProposalRuns, setWorkflowProposalRuns] =
    createSignal<WorkflowProposalRunState>(loadWorkflowProposalRunsFromStorage());
  const [messages, setMessages] = createSignal<Message[]>([]);
  const [isRunningSignal, setIsRunningSignal] = createSignal(false);
  const [statusSignal, setStatusSignal] = createSignal<string>('');
  const [runningToolSignal, setRunningToolSignal] = createSignal<RunningTool | null>(null);
  const [usageStats, setUsageStats] = createSignal<UsageStats>({
    promptTokens: 0,
    completionTokens: 0,
    totalTokens: 0,
  });
  const [permissionRequestSignal, setPermissionRequestSignal] = createSignal<PermissionRequest | null>(null);
  const [questionRequestSignal, setQuestionRequestSignal] = createSignal<QuestionRequest | null>(null);
  const [sessionRuntimeById, setSessionRuntimeById] = createSignal<Record<string, SessionRuntimeState>>({});
  const [questionHistory, setQuestionHistory] = createSignal<QuestionHistoryItem[]>([]);
  const [selectedAgentName, setSelectedAgentName] = createSignal<string>('build');
  const [selectedRouteSymphonyOnMcpTool, setSelectedRouteSymphonyOnMcpTool] = createSignal<boolean>(false);
  const [selectedSchedulerMode, setSelectedSchedulerMode] = createSignal<'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder'>(readPreference(preferenceKeys.scheduler, 'supervisor'));
  const [selectedReasoningEffort, setSelectedReasoningEffort] = createSignal<'minimal' | 'low' | 'medium' | 'high'>(readPreference(preferenceKeys.reasoning, 'medium'));
  const [filePreviewRequest, setFilePreviewRequest] = createSignal<FilePreviewRequest | null>(null);
  const [subagentStates, setSubagentStates] = createSignal<SubagentState[]>([]);
  const [subagentChats, setSubagentChats] = createSignal<SubagentChatEntry[]>([]);
  const [subagentsVersion, setSubagentsVersion] = createSignal(0);
  let subagentRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  const [messageToolsCollapsed, setMessageToolsCollapsedState] = createSignal<Record<string, boolean>>({});
  const [messageToolDetailsCollapsed, setMessageToolDetailsCollapsedState] = createSignal<Record<string, boolean>>({});
  let suspendActiveSessionSync = false;

  const bindIdentityScope = (identityScope: string): boolean => {
    const normalized = String(identityScope || '').trim();
    if (!normalized) return false;
    let previous = '';
    try {
      previous = String(localStorage.getItem(STORAGE_IDENTITY_SCOPE_KEY) || '').trim();
      if (previous === normalized) return false;
      localStorage.setItem(STORAGE_IDENTITY_SCOPE_KEY, normalized);
    } catch (error) {
      console.error('Failed to bind browser identity scope:', error);
    }
    setSessions([]);
    setCurrentSessionId(null);
    setMessages([]);
    setContinuedAsyncJobs({});
    setWorkflowProposalRuns({});
    setSessionRuntimeById({});
    setQuestionHistory([]);
    setFilePreviewRequest(null);
    setSubagentStates([]);
    setSubagentChats([]);
    return previous !== normalized;
  };

  const getEffectiveSchedulerMode = (
    agentName?: string | null,
    schedulerMode?: 'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder'
  ) => (usesFixedScheduler(agentName) ? 'supervisor' : (schedulerMode || 'supervisor'));

  const defaultRuntimeState = (): SessionRuntimeState => ({
    isRunning: false,
    status: '',
    runningTool: null,
    permissionRequest: null,
    questionRequest: null,
  });

  const getSessionRuntime = (sessionId: string): SessionRuntimeState => {
    if (!sessionId) return defaultRuntimeState();
    return sessionRuntimeById()[sessionId] || defaultRuntimeState();
  };

  const patchSessionRuntime = (sessionId: string, patch: Partial<SessionRuntimeState>) => {
    if (!sessionId) return;
    setSessionRuntimeById(prev => {
      const current = prev[sessionId] || defaultRuntimeState();
      return {
        ...prev,
        [sessionId]: {
          ...current,
          ...patch,
        },
      };
    });
  };

  const syncRuntimeSignalsFromSession = (sessionId: string | null) => {
    if (!sessionId) {
      const state = defaultRuntimeState();
      setIsRunningSignal(state.isRunning);
      setStatusSignal(state.status);
      setRunningToolSignal(state.runningTool);
      setPermissionRequestSignal(state.permissionRequest);
      setQuestionRequestSignal(state.questionRequest);
      return;
    }
    const state = getSessionRuntime(sessionId);
    setIsRunningSignal(state.isRunning);
    setStatusSignal(state.status);
    setRunningToolSignal(state.runningTool);
    setPermissionRequestSignal(state.permissionRequest);
    setQuestionRequestSignal(state.questionRequest);
  };

  const setSessionIsRunning = (sessionId: string, value: boolean) => {
    patchSessionRuntime(sessionId, { isRunning: Boolean(value) });
    if (currentSessionId() === sessionId) {
      setIsRunningSignal(Boolean(value));
    }
  };

  const setSessionStatus = (sessionId: string, value: string) => {
    patchSessionRuntime(sessionId, { status: String(value || '') });
    if (currentSessionId() === sessionId) {
      setStatusSignal(String(value || ''));
    }
  };

  const setSessionRunningTool = (sessionId: string, value: RunningTool | null) => {
    patchSessionRuntime(sessionId, { runningTool: value || null });
    if (currentSessionId() === sessionId) {
      setRunningToolSignal(value || null);
    }
  };

  const setSessionPermissionRequest = (sessionId: string, value: PermissionRequest | null) => {
    patchSessionRuntime(sessionId, { permissionRequest: value || null });
    if (currentSessionId() === sessionId) {
      setPermissionRequestSignal(value || null);
    }
  };

  const setSessionQuestionRequest = (sessionId: string, value: QuestionRequest | null) => {
    patchSessionRuntime(sessionId, { questionRequest: value || null });
    if (currentSessionId() === sessionId) {
      setQuestionRequestSignal(value || null);
    }
  };

  const getSessionRuntimeState = (sessionId: string): SessionRuntimeState =>
    getSessionRuntime(sessionId);

  const setIsRunning = (value: boolean) => {
    const sid = currentSessionId();
    if (sid) {
      setSessionIsRunning(sid, value);
      return;
    }
    setIsRunningSignal(Boolean(value));
  };

  const setStatus = (value: string) => {
    const sid = currentSessionId();
    if (sid) {
      setSessionStatus(sid, value);
      return;
    }
    setStatusSignal(String(value || ''));
  };

  const setRunningTool = (value: RunningTool | null) => {
    const sid = currentSessionId();
    if (sid) {
      setSessionRunningTool(sid, value);
      return;
    }
    setRunningToolSignal(value || null);
  };

  const setPermissionRequest = (value: PermissionRequest | null) => {
    const sid = currentSessionId();
    if (sid) {
      setSessionPermissionRequest(sid, value);
      return;
    }
    setPermissionRequestSignal(value || null);
  };

  const setQuestionRequest = (value: QuestionRequest | null) => {
    const sid = currentSessionId();
    if (sid) {
      setSessionQuestionRequest(sid, value);
      return;
    }
    setQuestionRequestSignal(value || null);
  };

  const isRunning = () => isRunningSignal();
  const status = () => statusSignal();
  const runningTool = () => runningToolSignal();
  const permissionRequest = () => permissionRequestSignal();
  const questionRequest = () => questionRequestSignal();

  // Persist sessions to localStorage whenever they change
  createEffect(() => {
    saveSessionsToStorage(sessions());
  });

  // Persist current session ID to localStorage whenever it changes
  createEffect(() => {
    saveCurrentSessionToStorage(currentSessionId());
  });

  // Persist async continuation clicks so a refreshed old card stays marked.
  createEffect(() => {
    saveContinuedAsyncJobsToStorage(continuedAsyncJobs());
  });

  // Persist workflow proposal confirmations so a refreshed old card stays marked.
  createEffect(() => {
    saveWorkflowProposalRunsToStorage(workflowProposalRuns());
  });

  // Keep runtime UI state aligned with the active session, even when callers
  // directly invoke setCurrentSessionId instead of switchSession().
  createEffect(() => {
    const sid = currentSessionId();
    syncRuntimeSignalsFromSession(sid);
  });

  // Keep session.messages synchronized with the active chat messages
  createEffect(() => {
    if (suspendActiveSessionSync) return;
    const sessionId = currentSessionId();
    if (!sessionId) return;
    const currentMessages = messages();

    setSessions(prev => {
      const idx = prev.findIndex(s => s.session_id === sessionId);
      if (idx < 0) return prev;

      const existing = prev[idx];
      if (existing.messages === currentMessages) return prev;

      const next = [...prev];
      next[idx] = {
        ...existing,
        messages: currentMessages,
      };
      return next;
    });
  });

  const currentSession = () => {
    const id = currentSessionId();
    return id ? sessions().find(s => s.session_id === id) : null;
  };

  const sessionSortValue = (session: Session) =>
    Number(
      session.is_archived
        ? (session.archived_at || session.updated_at || session.created_at || 0)
        : (session.updated_at || session.created_at || 0)
    );

  const sortSessions = (items: Session[]) =>
    [...items].sort((a, b) => (
      sessionSortValue(b) - sessionSortValue(a)
      || String(a.session_id).localeCompare(String(b.session_id))
    ));

  const nextActiveSessionId = (items: Session[], excludedSessionId?: string) =>
    sortSessions(
      items.filter(
        (item) => item.session_id !== excludedSessionId && !item.is_archived,
      ),
    )[0]?.session_id || null;

  const clearCurrentSession = () => {
    setCurrentSessionId(null);
    saveCurrentSessionToStorage(null);
    setMessages([]);
    syncRuntimeSignalsFromSession(null);
  };

  const mergeSessions = (incomingSessions: Session[], scope: SessionMergeScope = 'all') => {
    const normalizedIncoming = incomingSessions.map((session) => ({
      ...session,
      messages: sanitizeVisibleMessages(session?.messages || []),
    }));

    let nextSessionsSnapshot: Session[] = [];
    const removedSessionIds = new Set<string>();
    setSessions((prev) => {
      const nextById = new Map(prev.map((item) => [item.session_id, item]));
      const incomingIds = new Set(
        normalizedIncoming.map((item) => item.session_id),
      );

      if (scope === 'active') {
        for (const existing of prev) {
          if (!existing.is_archived && !incomingIds.has(existing.session_id)) {
            nextById.delete(existing.session_id);
            removedSessionIds.add(existing.session_id);
          }
        }
      } else if (scope === 'archived') {
        for (const existing of prev) {
          if (existing.is_archived && !incomingIds.has(existing.session_id)) {
            nextById.delete(existing.session_id);
            removedSessionIds.add(existing.session_id);
          }
        }
      } else {
        for (const existing of prev) {
          if (!incomingIds.has(existing.session_id)) {
            removedSessionIds.add(existing.session_id);
          }
        }
        nextById.clear();
      }

      for (const session of normalizedIncoming) {
        const existing = nextById.get(session.session_id);
        nextById.set(session.session_id, {
          ...existing,
          ...session,
          messages:
            session.messages.length > 0
              ? session.messages
              : existing?.messages || [],
        });
      }

      nextSessionsSnapshot = sortSessions(Array.from(nextById.values()));
      return nextSessionsSnapshot;
    });

    if (removedSessionIds.size > 0) {
      setSessionRuntimeById((prev) => {
        const next = { ...prev };
        let changed = false;
        for (const sessionId of removedSessionIds) {
          if (sessionId in next) {
            delete next[sessionId];
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    }

    const activeSessionId = currentSessionId();
    if (
      activeSessionId
      && !nextSessionsSnapshot.some((item) => item.session_id === activeSessionId)
    ) {
      const fallbackSessionId = nextActiveSessionId(nextSessionsSnapshot, activeSessionId);
      if (fallbackSessionId) {
        switchSession(fallbackSessionId);
      } else {
        clearCurrentSession();
      }
    }
  };

  const upsertSession = (session: Session) => {
    const normalized = {
      ...session,
      messages: sanitizeVisibleMessages(session?.messages || []),
    };
    setSessions((prev) => {
      const existing = prev.find((item) => item.session_id === normalized.session_id);
      const next = prev.filter((item) => item.session_id !== normalized.session_id);
      next.push({
        ...existing,
        ...normalized,
        messages:
          normalized.messages.length > 0
            ? normalized.messages
            : existing?.messages || [],
      });
      return sortSessions(next);
    });
  };

  const markSessionArchived = (sessionId: string, archivedAt?: number | null) => {
    let nextSessionsSnapshot: Session[] = [];
    setSessions((prev) => {
      nextSessionsSnapshot = sortSessions(
        prev.map((session) =>
          session.session_id === sessionId
            ? {
                ...session,
                is_archived: true,
                archived_at: Number(archivedAt || Date.now()),
              }
            : session,
        ),
      );
      return nextSessionsSnapshot;
    });

    if (currentSessionId() === sessionId) {
      const fallbackSessionId = nextActiveSessionId(
        nextSessionsSnapshot,
        sessionId,
      );
      if (fallbackSessionId) {
        switchSession(fallbackSessionId);
      } else {
        clearCurrentSession();
      }
    }
  };

  const markSessionUnarchived = (sessionId: string) => {
    setSessions((prev) =>
      sortSessions(
        prev.map((session) =>
          session.session_id === sessionId
            ? {
                ...session,
                is_archived: false,
                archived_at: undefined,
                updated_at: Date.now(),
              }
            : session,
        ),
      ),
    );
  };

  const addMessage = (message: Message) => {
    setMessages(prev => [...prev, message]);
    // Update session's updated_at
    const sessionId = currentSessionId();
    if (sessionId) {
      setSessions(prev => {
        const updated = prev.map(s => 
          s.session_id === sessionId ? { ...s, updated_at: Date.now() } : s
        );
        saveSessionsToStorage(updated);
        return updated;
      });
    }
  };

  const addMessageForSession = (sessionId: string, message: Message) => {
    if (currentSessionId() === sessionId) {
      addMessage(message);
    } else {
      setSessions(previous => previous.map(session => session.session_id === sessionId
        ? { ...session, messages: [...session.messages, message], updated_at: Date.now() }
        : session));
    }
  };

  const updateLastMessage = (content: string) => {
    setMessages(prev => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (last && last.role === 'assistant') {
        updated[updated.length - 1] = { ...last, content: last.content + content };
      }
      return updated;
    });
  };

  const createSession = () => {
    const defaultAgentName = readPreference(preferenceKeys.agent, 'build');
    const newSession: Session = {
      session_id: generateId(),
      messages: [],
      created_at: Date.now(),
      updated_at: Date.now(),
      agent_name: defaultAgentName,
      scheduler_mode: getEffectiveSchedulerMode(defaultAgentName, selectedSchedulerMode()),
      reasoning_effort: selectedReasoningEffort(),
    };
    setSessions(prev => {
      const updated = [newSession, ...prev];
      saveSessionsToStorage(updated);
      return updated;
    });
    setCurrentSessionId(newSession.session_id);
    saveCurrentSessionToStorage(newSession.session_id);
    setMessages([]);
    setSelectedAgentName(defaultAgentName);
    patchSessionRuntime(newSession.session_id, defaultRuntimeState());
    syncRuntimeSignalsFromSession(newSession.session_id);
    return newSession;
  };

  const switchSession = (sessionId: string) => {
    suspendActiveSessionSync = true;
    setCurrentSessionId(sessionId);
    const session = sessions().find(s => s.session_id === sessionId);
    if (session) {
      setMessages(session.messages);
      if (session.agent_name) {
        setSelectedAgentName(toVisibleAgentName(session.agent_name));
      }
      setSelectedSchedulerMode(getEffectiveSchedulerMode(session.agent_name, session.scheduler_mode));
      if (session.reasoning_effort) {
        setSelectedReasoningEffort(session.reasoning_effort);
      }
    } else {
      setMessages([]);
    }
    syncRuntimeSignalsFromSession(sessionId);
    queueMicrotask(() => {
      suspendActiveSessionSync = false;
    });
  };

  const updateSessionAgent = (sessionId: string, agentName: string) => {
    setSessions(prev => prev.map(s => {
      if (s.session_id !== sessionId) {
        return s;
      }
      const nextSchedulerMode = getEffectiveSchedulerMode(agentName, s.scheduler_mode);
      if (
        String(s.agent_name || '') === String(agentName || '') &&
        String(s.scheduler_mode || '') === String(nextSchedulerMode || '')
      ) {
        return s;
      }
      return {
        ...s,
        agent_name: agentName,
        scheduler_mode: nextSchedulerMode,
      };
    }));
    if (currentSessionId() === sessionId) {
      const visibleAgentName = toVisibleAgentName(agentName);
      const nextVisibleAgentName = visibleAgentName || agentName;
      if (selectedAgentName() !== nextVisibleAgentName) {
        setSelectedAgentName(nextVisibleAgentName);
      }
      const nextSchedulerMode = getEffectiveSchedulerMode(nextVisibleAgentName, selectedSchedulerMode());
      if (selectedSchedulerMode() !== nextSchedulerMode) {
        setSelectedSchedulerMode(nextSchedulerMode);
      }
    }
  };

  const updateSessionModel = (sessionId: string, model: string) => {
    const normalizedModel = String(model || '').trim();
    setSessions(prev => {
      const current = prev.find(s => s.session_id === sessionId);
      if (!current || String(current.model || '').trim() === normalizedModel) {
        return prev;
      }
      const updated = prev.map(s => (
        s.session_id === sessionId ? { ...s, model: normalizedModel, updated_at: Date.now() } : s
      ));
      saveSessionsToStorage(updated);
      return updated;
    });
  };

  const updateSessionHarness = (sessionId: string, runtimeId: string, modelId = '') => {
    const normalizedRuntimeId = String(runtimeId || 'tcode-native').trim() || 'tcode-native';
    const normalizedModelId = String(modelId || '').trim();
    setSessions(prev => {
      const updated = prev.map(s => {
        if (s.session_id !== sessionId) return s;
        return {
          ...s,
          ...(normalizedModelId ? { model: normalizedModelId } : {}),
          metadata: {
            ...(s.metadata || {}),
            harness: {
              runtime_id: normalizedRuntimeId,
              model_id: normalizedModelId || String(s.model || '').trim(),
            },
          },
          updated_at: Date.now(),
        };
      });
      saveSessionsToStorage(updated);
      return updated;
    });
  };

  const applyServerHarnessState = (
    sessionId: string,
    value: HarnessSwitchResponse | HarnessStateProjection,
  ) => {
    const response = value as HarnessSwitchResponse;
    const projection = value as HarnessStateProjection;
    const isSwitchResponse = typeof response.current_runtime_id === 'string';
    const runtimeId = String(
      response.current_runtime_id || projection.runtime_id || 'tcode-native',
    ).trim() || 'tcode-native';
    const modelId = String(response.model_id || projection.model_id || '').trim();
    const handoff = response.handoff;
    const currentMetadata = sessions().find(item => item.session_id === sessionId)?.metadata?.harness;
    const pendingHandoffId = String(
      handoff?.handoff_id
      || projection.pending_handoff_id
      || (isSwitchResponse && response.switched === false
        ? currentMetadata?.pending_handoff_id
        : '')
      || '',
    ).trim();
    const summaryStatus = String(
      handoff?.summary_status
      || projection.summary_status
      || (isSwitchResponse && response.switched === false
        ? currentMetadata?.summary_status
        : '')
      || '',
    ).trim();
    const previousRuntimeId = String(
      response.previous_runtime_id
      || projection.previous_runtime_id
      || currentMetadata?.previous_runtime_id
      || '',
    ).trim();
    const handoffNoticePending = Boolean(
      pendingHandoffId
      && (
        projection.handoff_notice_pending
        ?? (handoff ? true : undefined)
        ?? (isSwitchResponse && response.switched === false
          ? currentMetadata?.handoff_notice_pending
          : response.mid_session)
        ?? true
      ),
    );
    setSessions(prev => {
      const updated = prev.map(session => (
        session.session_id !== sessionId
          ? session
          : {
              ...session,
              ...(modelId ? { model: modelId } : {}),
              metadata: {
                ...(session.metadata || {}),
                harness: {
                  ...(session.metadata?.harness || {}),
                  runtime_id: runtimeId,
                  model_id: modelId,
                  models: {
                    ...(session.metadata?.harness?.models || {}),
                    ...(projection.models || {}),
                    ...(modelId ? { [runtimeId]: modelId } : {}),
                  },
                  switch_version: Number(
                    response.switch_version ?? projection.switch_version ?? 0,
                  ),
                  pending_handoff_id: pendingHandoffId,
                  handoff_notice_pending: handoffNoticePending,
                  summary_status: summaryStatus,
                  truncated: Boolean(
                    handoff?.truncated
                    ?? projection.truncated
                    ?? (isSwitchResponse && response.switched === false
                      ? currentMetadata?.truncated
                      : false),
                  ),
                  warning_code: String(
                    handoff?.warning_code
                    || projection.warning_code
                    || (isSwitchResponse && response.switched === false
                      ? currentMetadata?.warning_code
                      : '')
                    || '',
                  ),
                  previous_runtime_id: previousRuntimeId,
                },
              },
              updated_at: Date.now(),
            }
      ));
      saveSessionsToStorage(updated);
      return updated;
    });
  };

  const updateSessionSchedulerMode = (
    sessionId: string,
    schedulerMode: 'supervisor' | 'chain' | 'swarm' | 'auto_swarm_builder'
  ) => {
    setSessions(prev => {
      const updated = prev.map(s => (s.session_id === sessionId ? { ...s, scheduler_mode: schedulerMode } : s));
      saveSessionsToStorage(updated);
      return updated;
    });
    if (currentSessionId() === sessionId) {
      setSelectedSchedulerMode(schedulerMode);
    }
  };

  const updateSessionReasoningEffort = (
    sessionId: string,
    reasoningEffort: 'minimal' | 'low' | 'medium' | 'high'
  ) => {
    setSessions(prev => {
      const updated = prev.map(s => (s.session_id === sessionId ? { ...s, reasoning_effort: reasoningEffort } : s));
      saveSessionsToStorage(updated);
      return updated;
    });
    if (currentSessionId() === sessionId) {
      setSelectedReasoningEffort(reasoningEffort);
    }
  };

  const deleteSession = (sessionId: string) => {
    let nextSessionsSnapshot: Session[] = [];
    setSessions(prev => {
      nextSessionsSnapshot = prev.filter(s => s.session_id !== sessionId);
      saveSessionsToStorage(nextSessionsSnapshot);
      return nextSessionsSnapshot;
    });
    setSessionRuntimeById(prev => {
      if (!(sessionId in prev)) return prev;
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });

    if (currentSessionId() === sessionId) {
      const fallbackSessionId = nextActiveSessionId(
        nextSessionsSnapshot,
        sessionId,
      );
      if (fallbackSessionId) {
        switchSession(fallbackSessionId);
      } else {
        clearCurrentSession();
      }
    }
  };

  const updateUsageStats = (usage: Usage) => {
    setUsageStats(prev => ({
      promptTokens: prev.promptTokens + (usage.prompt_tokens || 0),
      completionTokens: prev.completionTokens + (usage.completion_tokens || 0),
      totalTokens: prev.totalTokens + (usage.total_tokens || 0),
    }));
  };

  const replaceUsageStats = (usage: Usage) => {
    setUsageStats({
      promptTokens: Number(usage.prompt_tokens || 0),
      completionTokens: Number(usage.completion_tokens || 0),
      totalTokens: Number(usage.total_tokens || 0),
    });
  };

  const resetUsageStats = () => {
    setUsageStats({
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
    });
  };

  const addQuestionHistory = (req: QuestionRequest) => {
    setQuestionHistory(prev => {
      const exists = prev.some(item => item.request_id === req.request_id);
      if (exists) return prev;
      return [
        {
          request_id: req.request_id,
          session_id: req.session_id,
          question: req.question,
          status: 'pending',
          created_at: Date.now(),
        },
        ...prev,
      ];
    });
  };

  const resolveQuestionHistory = (
    requestId: string,
    answer: string,
    status: 'answered' | 'timeout' | 'error' = 'answered'
  ) => {
    setQuestionHistory(prev =>
      prev.map(item =>
        item.request_id === requestId
          ? {
              ...item,
              answer,
              status,
              resolved_at: Date.now(),
            }
          : item
      )
    );
  };

  const requestFilePreview = (path: string) => {
    if (!path) return;
    setFilePreviewRequest({ path, key: Date.now() });
  };

  const upsertSubagentState = (state: Partial<SubagentState> & { session_id: string }) => {
    const now = Date.now();
    setSubagentStates(prev => {
      const idx = prev.findIndex(s => s.session_id === state.session_id);
      const merged: SubagentState = {
        parent_session_id: state.parent_session_id || currentSessionId() || '',
        session_id: state.session_id,
        agent_name: state.agent_name || prev[idx]?.agent_name || 'subagent',
        display_name: state.display_name ?? prev[idx]?.display_name,
        engine: state.engine ?? prev[idx]?.engine,
        scheduler_mode: state.scheduler_mode ?? prev[idx]?.scheduler_mode,
        status: ((state.status as SubagentState['status']) || prev[idx]?.status || 'idle'),
        message: state.message ?? prev[idx]?.message,
        tools_used: state.tools_used ?? prev[idx]?.tools_used,
        conversation_with: state.conversation_with ?? prev[idx]?.conversation_with,
        last_response: state.last_response ?? prev[idx]?.last_response,
        context: state.context ?? prev[idx]?.context,
        contract: state.contract ?? prev[idx]?.contract,
        updated_at: now,
      };
      if (idx < 0) return [merged, ...prev];
      const next = [...prev];
      next[idx] = { ...next[idx], ...merged, updated_at: now };
      return next;
    });
  };

  const appendSubagentChat = (entry: SubagentChatEntry) => {
    setSubagentChats(prev => [...prev, entry]);
  };

  const getSubagentChats = (sessionId: string) =>
    subagentChats().filter(c => c.session_id === sessionId);

  const removeSubagentSession = (sessionId: string) => {
    setSubagentStates(prev => prev.filter(s => s.session_id !== sessionId));
    setSubagentChats(prev => prev.filter(c => c.session_id !== sessionId));
  };

  const refreshSubagents = () => {
    if (subagentRefreshTimer) return;
    subagentRefreshTimer = setTimeout(() => {
      subagentRefreshTimer = null;
      setSubagentsVersion(prev => prev + 1);
    }, SUBAGENT_REFRESH_DEBOUNCE_MS);
  };

  const isMessageToolsCollapsed = (messageId: string) =>
    messageToolsCollapsed()[messageId] ?? true;

  const messageToolsCollapsePreference = (messageId: string) =>
    messageToolsCollapsed()[messageId];

  const setMessageToolsCollapsed = (messageId: string, collapsed: boolean) => {
    if (!messageId) return;
    setMessageToolsCollapsedState(prev => ({
      ...prev,
      [messageId]: collapsed,
    }));
  };

  const isMessageToolDetailsCollapsed = (messageId: string, toolKey: string) =>
    messageToolDetailsCollapsed()[`${messageId}:${toolKey}`] ?? true;

  const setMessageToolDetailsCollapsed = (messageId: string, toolKey: string, collapsed: boolean) => {
    if (!messageId || !toolKey) return;
    setMessageToolDetailsCollapsedState(prev => ({
      ...prev,
      [`${messageId}:${toolKey}`]: collapsed,
    }));
  };

  const isAsyncJobContinued = (sessionId: string, identifier: string) => {
    const sid = String(sessionId || '').trim();
    const key = sanitizeAsyncJobIdentifier(identifier);
    if (!sid || !key) return false;
    return Boolean(continuedAsyncJobs()[sid]?.[key]);
  };

  const markAsyncJobContinued = (sessionId: string, identifier: string) => {
    const sid = String(sessionId || '').trim();
    const key = sanitizeAsyncJobIdentifier(identifier);
    if (!sid || !key) return;
    setContinuedAsyncJobs(prev => ({
      ...prev,
      [sid]: {
        ...(prev[sid] || {}),
        [key]: Date.now(),
      },
    }));
  };

  const unmarkAsyncJobContinued = (sessionId: string, identifier: string) => {
    const sid = String(sessionId || '').trim();
    const key = sanitizeAsyncJobIdentifier(identifier);
    if (!sid || !key) return;
    setContinuedAsyncJobs(prev => {
      const sessionItems = { ...(prev[sid] || {}) };
      if (!sessionItems[key]) return prev;
      delete sessionItems[key];
      const next = { ...prev };
      if (Object.keys(sessionItems).length > 0) next[sid] = sessionItems;
      else delete next[sid];
      return next;
    });
  };

  const workflowProposalRun = (sessionId: string, proposalKey: string) => {
    const sid = String(sessionId || '').trim();
    const key = sanitizeWorkflowProposalKey(proposalKey);
    if (!sid || !key) return null;
    return workflowProposalRuns()[sid]?.[key] || null;
  };

  const markWorkflowProposalRunCreated = (
    sessionId: string,
    proposalKey: string,
    runId: string,
    templateKey: string,
  ) => {
    const sid = String(sessionId || '').trim();
    const key = sanitizeWorkflowProposalKey(proposalKey);
    const normalizedRunId = String(runId || '').trim();
    if (!sid || !key || !normalizedRunId) return;
    setWorkflowProposalRuns(prev => ({
      ...prev,
      [sid]: {
        ...(prev[sid] || {}),
        [key]: {
          run_id: normalizedRunId,
          template_key: String(templateKey || '').trim(),
          created_at: Date.now(),
        },
      },
    }));
  };

  return {
    sessions,
    bindIdentityScope,
    addMessageForSession,
    setSessions,
    currentSessionId,
    isHarnessSwitchPending,
    setHarnessSwitchPending,
    setCurrentSessionId,
    currentSession,
    messages,
    setMessages,
    isRunning,
    setIsRunning,
    setSessionIsRunning,
    setSessionDeliveryTurnRunning: (sessionId: string, value: boolean) => patchSessionRuntime(sessionId, {deliveryTurnRunning: value}),
    status,
    setStatus,
    setSessionStatus,
    runningTool,
    setRunningTool,
    setSessionRunningTool,
    permissionRequest,
    setPermissionRequest,
    setSessionPermissionRequest,
    questionRequest,
    setQuestionRequest,
    setSessionQuestionRequest,
    getSessionRuntimeState,
    questionHistory,
    addQuestionHistory,
    resolveQuestionHistory,
    selectedAgentName,
    setSelectedAgentName,
    selectedRouteSymphonyOnMcpTool,
    setSelectedRouteSymphonyOnMcpTool,
    selectedSchedulerMode,
    setSelectedSchedulerMode,
    selectedReasoningEffort,
    setSelectedReasoningEffort,
    filePreviewRequest,
    requestFilePreview,
    subagentStates,
    subagentsVersion,
    refreshSubagents,
    upsertSubagentState,
    subagentChats,
    appendSubagentChat,
    getSubagentChats,
    removeSubagentSession,
    isMessageToolsCollapsed,
    messageToolsCollapsePreference,
    setMessageToolsCollapsed,
    isMessageToolDetailsCollapsed,
    setMessageToolDetailsCollapsed,
    isAsyncJobContinued,
    markAsyncJobContinued,
    unmarkAsyncJobContinued,
    workflowProposalRun,
    markWorkflowProposalRunCreated,
    usageStats,
    addMessage,
    updateLastMessage,
    createSession,
    switchSession,
    clearCurrentSession,
    deleteSession,
    mergeSessions,
    upsertSession,
    markSessionArchived,
    markSessionUnarchived,
    updateSessionAgent,
    updateSessionModel,
    updateSessionHarness,
    applyServerHarnessState,
    updateSessionSchedulerMode,
    updateSessionReasoningEffort,
    updateUsageStats,
    replaceUsageStats,
    resetUsageStats,
  };
}

export const sessionStore = createRoot(createSessionStore);
