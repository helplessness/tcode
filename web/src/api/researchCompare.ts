export type Candidate = {
  candidate_id: string; enzyme_name?: string; candidate_kind?: string; candidate_role?: string;
  score?: number | null; confidence?: string; evidence_scope?: string; verification_note?: string;
  hard_conflict?: boolean; native_reaction_quote?: string; knowledge_gaps?: string[]; sources?: string[];
  supports?: Array<{ kind?: string; statement?: string; source_refs?: string[] }>;
  contradictions?: Array<{ kind?: string; statement?: string; source_refs?: string[] }>;
};
export type Comparison = {
  run_id: string; result_digest: string; candidates: Candidate[]; display_style: 'clay' | 'manual';
  selection_status: 'current' | 'stale';
  selection: { items: Array<{ item_id: string }>; inputs: { reason?: string }; result_digest: string } | null;
  decision_state: { enabled: boolean; status?: 'current' | 'stale'; record: DecisionRecord | null; projects: Array<{ research_id: string; title: string }> };
};
export type CandidateDecision = { candidate_id: string; state: 'adopt' | 'defer' | 'exclude'; reason: string };
export type DecisionRecord = { experience_id: string; revision: number; result_digest: string; decisions: CandidateDecision[]; research_id: string; occurred_at: string };
export type DecisionHistory = { records: DecisionRecord[]; next_before_revision: number };
export type Evidence = { status: string; message?: string; reference: string;
  claim?: { statement: string; attribution: string; source_scope: string; activity_status: string; sources: unknown[] }; scope?: string };

export async function comparisonRequest<T>(runId: string, sessionId: string, digest: string, suffix = '', body?: unknown, extra: Record<string, string> = {}): Promise<T> {
  const query = new URLSearchParams({ session_id: sessionId, result_digest: digest, ...extra });
  const response = await fetch(`/api/intelligent-ui/v1/comparisons/${encodeURIComponent(runId)}${suffix}?${query}`, {
    method: body ? 'POST' : 'GET', credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail === 'result_changed_reload_required'
    ? '研究结果已改变。请让 Agent 重新打开比较台并重新选择。'
    : value.detail === 'research_decision_changed_reload_required' ? '研究决策已被其他页面更新。请重新读取，再核对并保存；本次修改尚未保存。'
    : value.detail === 'research_project_unavailable' ? '关联课题不可用或已归档，请重新读取并选择。'
    : '无法读取或保存研究结果，请重新打开后再试。');
  return value as T;
}
