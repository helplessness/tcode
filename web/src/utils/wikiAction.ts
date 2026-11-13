import type { ToolCall } from '../stores/session';

export type WikiActionOperation = {
  type: 'archive_page' | 'restore_page' | 'upsert_page' | string;
  page_id: string;
  title: string;
  page_type?: string;
  current_status?: string;
  target_status?: string;
  revision?: number;
};

export type WikiActionPayload = {
  schema: 'tcode.wiki-action.v1';
  kind: 'proposal_review';
  status: 'ready' | 'blocked' | 'completed';
  title: string;
  message?: string;
  proposal_id: string;
  proposal_hash: string;
  proposal_status: string;
  workspace_id?: string;
  operations: WikiActionOperation[];
  direct_confirm_allowed: boolean;
};

const ACTION_STATUSES = new Set(['ready', 'blocked', 'completed']);

export const wikiActionFromTool = (tool: ToolCall): WikiActionPayload | null => {
  if (String(tool.tool_name || '').trim() !== 'paper_wiki_stage_changes') return null;
  const raw = tool.metadata?.wiki_action as Record<string, any> | undefined;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (raw.schema !== 'tcode.wiki-action.v1' || raw.kind !== 'proposal_review') return null;
  if (!ACTION_STATUSES.has(String(raw.status || ''))) return null;
  if (!String(raw.proposal_id || '').trim() || !String(raw.proposal_hash || '').trim()) return null;
  if (!Array.isArray(raw.operations) || raw.operations.length === 0) return null;
  return raw as WikiActionPayload;
};

export const wikiActionOperationLabel = (type: string) => ({
  archive_page: '归档',
  restore_page: '恢复',
  upsert_page: '更新',
}[String(type || '').trim()] || '变更');
