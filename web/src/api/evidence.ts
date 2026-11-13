import type { CitationRef } from '../utils/citations';

export type EvidenceImage = { index: number; kind: string; label: string; name: string };
export type EvidenceNode = { node_id: string; type: string; label: string; evidence_ids?: string[] };
export type EvidenceEdge = { edge_id: string; source: string; target: string; type: string };
export type EvidenceGraph = { nodes: EvidenceNode[]; edges: EvidenceEdge[]; truncated?: boolean };
export type EvidenceDoctor = {
  status: 'pass' | 'warn' | 'fail'; scope?: string; truncated?: boolean;
  storage_integrity?: string; checked_record_count: number; record_count: number; issue_count: number;
  issues: { severity: string; code: string; message: string; evidence_id?: string }[];
  asset_verification?: 'full' | 'lightweight';
  asset_checks?: { file_count: number; checked_file_count: number; verified_file_count: number };
};
export type EvidenceMaintenance = {
  status: 'idle' | 'queued' | 'running' | 'completed' | 'failed'; phase: string;
  processed: number; total: number; semantic: 'disabled' | 'unconfigured' | 'pending' | 'ready' | 'updating' | 'degraded' | 'unavailable';
  retryable: boolean; updated_at: number; lexical_ready?: boolean;
};
export type CitationRecord = {
  state: 'available' | 'missing' | 'withdrawn' | 'disabled' | 'forbidden' | 'historical';
  evidence_id?: string; title?: string; content?: string; quote?: string; kind?: string; status?: string;
  source_id?: string; source_path?: string; locator?: unknown; citations?: unknown[];
  content_hash?: string; source_revision?: number; technical?: Record<string, unknown>;
  images?: EvidenceImage[]; graph?: EvidenceGraph; doctor?: EvidenceDoctor;
  maintenance?: EvidenceMaintenance;
};

export async function resolveCitation(ref: CitationRef, workspaceId: string, sessionId: string, signal: AbortSignal): Promise<CitationRecord> {
  const response = await fetch('/api/evidence/resolve', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({ ...ref, workspace_id: workspaceId, session_id: sessionId }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(payload.detail || `读取来源失败 (${response.status})`));
  return payload as CitationRecord;
}

export function evidenceImageUrl(evidenceId: string, imageIndex: number, workspaceId: string) {
  return `/api/evidence/${encodeURIComponent(evidenceId)}/images/${imageIndex}?${new URLSearchParams({ workdir_name: workspaceId })}`;
}

export async function evidenceMaintenance(workspaceId: string, signal: AbortSignal, action?: { rebuild: boolean; verify_assets: boolean }): Promise<EvidenceMaintenance> {
  const response = await fetch(action ? '/api/evidence/maintenance' : `/api/evidence/maintenance/status?${new URLSearchParams({ workdir_name: workspaceId })}`, action
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal, body: JSON.stringify({ workspace_id: workspaceId, ...action }) }
    : { signal });
  if (!response.ok) throw new Error('读取维护状态失败，请重试。');
  return response.json();
}
