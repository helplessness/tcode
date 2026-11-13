import type { ToolCall } from '../stores/session';

export type ResearchActionStatus = 'ready' | 'blocked' | 'completed' | 'cancelled';
export type ResearchActionKind =
  | 'candidate_confirmation'
  | 'candidate_rename'
  | 'candidate_dismissal'
  | 'pack_build'
  | 'pack_download'
  | 'research_step';

export type ResearchActionPayload = {
  schema: 'tcode.research-action.v1';
  kind: ResearchActionKind;
  status: ResearchActionStatus;
  title: string;
  message?: string;
  action_id?: string;
  expires_at?: string;
  confirmation_hint?: string;
  candidate_id?: string;
  candidate_digest?: string;
  current_title?: string;
  proposed_title?: string;
  candidate?: {
    title?: string;
    stability_score?: number;
    state?: string;
    supporting_event_count?: number;
    confirmed_research_id?: string;
  };
  confirmed_research_id?: string;
  research_id?: string;
  pack_id?: string;
  archive_name?: string;
  archive_size?: number;
  privacy_profile?: string;
  support_counts?: Record<string, number>;
  readable_support_count?: number;
  technical_record_count?: number;
  representative_sources?: Array<{
    event_id?: string;
    category?: string;
    title?: string;
    summary?: string;
    occurred_at?: string;
    actor?: string;
  }>;
  preview?: {
    source_digest?: string;
    export_ready?: boolean;
    estimated_bytes?: number;
    counts?: Record<string, number>;
    exclusions?: Record<string, number>;
    privacy_report?: Record<string, any>;
    issues?: Array<{ code?: string; severity?: string; message?: string }>;
  };
  export?: Record<string, any>;
  details?: Record<string, unknown>;
  result?: Record<string, unknown>;
};

const KINDS = new Set<ResearchActionKind>([
  'candidate_confirmation',
  'candidate_rename',
  'candidate_dismissal',
  'pack_build',
  'pack_download',
  'research_step',
]);
const STATUSES = new Set<ResearchActionStatus>(['ready', 'blocked', 'completed', 'cancelled']);
const TOOL_NAMES = new Set(['research_workspace', 'research_action_commit']);

export const researchActionFromTool = (tool: ToolCall): ResearchActionPayload | null => {
  if (!TOOL_NAMES.has(String(tool.tool_name || '').trim())) return null;
  const raw = tool.metadata?.research_action as Record<string, any> | undefined;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (raw.schema !== 'tcode.research-action.v1') return null;
  if (!KINDS.has(raw.kind as ResearchActionKind)) return null;
  if (!STATUSES.has(raw.status as ResearchActionStatus)) return null;
  if (!String(raw.title || '').trim()) return null;
  return raw as ResearchActionPayload;
};

export const researchActionDisplayKey = (action: ResearchActionPayload): string => {
  const actionId = String(action.action_id || '').trim();
  if (actionId) return `action:${actionId}`;
  const packId = String(action.pack_id || '').trim();
  if (packId) return `pack:${packId}`;
  const candidateId = String(action.candidate_id || '').trim();
  if (candidateId) return `candidate:${candidateId}`;
  return '';
};

export const researchActionDisplayIndexes = (tools: ToolCall[]): Set<number> => {
  const visible = new Set<number>();
  const seen = new Set<string>();
  for (let index = tools.length - 1; index >= 0; index -= 1) {
    const action = researchActionFromTool(tools[index]);
    if (!action) continue;
    const key = researchActionDisplayKey(action);
    if (key && seen.has(key)) continue;
    visible.add(index);
    if (key) seen.add(key);
  }
  return visible;
};

export const researchSourceCategoryLabel = (category: string) => ({
  message: '对话',
  tool: '工具',
  mcp: 'MCP',
  artifact: '研究产物',
  workflow: '工作流',
  agent: '子 Agent',
  file: '文件',
  other: '其他',
}[String(category || '').trim()] || '其他');


/** Successful platform receipts only; the last receipt replaces its preview. */
export function researchActionReceipts(tools: ToolCall[]): ResearchActionPayload[] {
  const actions = new Map<string, ResearchActionPayload>();
  for (const tool of tools) {
    if (tool.state !== 'completed' || tool.error) continue;
    const action = researchActionFromTool(tool);
    if (!action) continue;
    const key = researchActionDisplayKey(action);
    if (key) actions.set(key, action);
  }
  return [...actions.values()];
}
