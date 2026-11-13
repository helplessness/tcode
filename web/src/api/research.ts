export type ResearchCandidateState =
  | 'emerging'
  | 'stable'
  | 'merged'
  | 'split'
  | 'dismissed'
  | 'confirmed';

export type ResearchCandidate = {
  candidate_id: string;
  suggested_title: string;
  title_source: string;
  goal_summary: string;
  keywords: string[];
  supporting_event_count: number;
  session_count: number;
  artifact_count: number;
  tool_count: number;
  explicit_goal: boolean;
  stability_score: number;
  state: ResearchCandidateState;
  first_seen_at: string;
  last_seen_at: string;
  merged_into: string;
  split_from: string[];
  confirmed_research_id: string;
  generation: number;
};

export type ResearchProject = {
  research_id: string;
  title: string;
  description: string;
  status: 'active' | 'paused' | 'archived' | string;
  origin_candidate_ids: string[];
  created_at: string;
  updated_at: string;
  archived_at: string;
};

export type ResearchCenter = {
  current_project: ResearchProject | null;
  candidates: ResearchCandidate[];
  history: ResearchProject[];
  candidate_limit: number;
  project_limit: number;
};

export type ResearchProjectDeletion = {
  deletion_id: string;
  research_id: string;
  records_deleted: Record<string, number>;
  private_session_event_count: number;
  reassigned_event_count: number;
  local_files_removed: number;
  local_files_preserved: number;
  imported_papers_removed: number;
  imported_wiki_pages_removed: number;
  evidence_records_removed: number;
  cleanup_error_count: number;
};

export type ResearchSourceCategory =
  | 'message'
  | 'tool'
  | 'mcp'
  | 'artifact'
  | 'workflow'
  | 'agent'
  | 'file'
  | 'other';

export type ResearchProjectProvenance = {
  project: ResearchProject;
  origin_candidates: ResearchCandidate[];
  confirmation: null | {
    operation_id: string;
    action: string;
    actor_id: string;
    reason: string;
    occurred_at: string;
    before: Record<string, any>;
    after: Record<string, any>;
  };
  initial_intent: string;
  counts: {
    events: number;
    sessions: number;
    tools: number;
    files: number;
    artifacts: number;
    artifact_groups: number;
    agents: number;
    activities: number;
  };
  source_counts: Partial<Record<ResearchSourceCategory, number>>;
  key_sources: Array<{
    event_id: string;
    kind: 'user_intent' | 'file' | 'tool_result' | 'agent_result' | string;
    category: ResearchSourceCategory;
    title: string;
    summary: string;
    occurred_at: string;
    can_exclude: boolean;
  }>;
  activities: Array<{
    activity_id: string;
    activity_type: string;
    title: string;
    summary: string;
    status: string;
    started_at: string;
    ended_at: string;
    artifact_group_count: number;
    agent_calls: ResearchActivity['agent_calls'];
  }>;
  history: Array<{
    operation_id: string;
    action: string;
    actor_id: string;
    reason: string;
    occurred_at: string;
    before: Record<string, any>;
    after: Record<string, any>;
  }>;
};

export type ResearchEvent = {
  event_id: string;
  event_type: string;
  session_id: string;
  turn_id: string;
  trace_id: string;
  actor_type: string;
  actor_id: string;
  source_kind: string;
  source_ref: string;
  occurred_at: string;
  payload: Record<string, any>;
};

export type ResearchEventLink = {
  link_id: string;
  event_id: string;
  candidate_id: string;
  research_id: string;
  state: string;
  relevance: string;
  decision_source: string;
  confidence: number;
  reason_code: string;
  expires_at: string;
};

export type ResearchActivity = {
  activity_id: string;
  schema_version: number;
  activity_key: string;
  activity_type: string;
  title: string;
  summary: string;
  status: 'observed' | 'running' | 'completed' | 'failed' | 'incomplete' | string;
  review_state: 'pending' | 'included' | 'observed' | string;
  recommendation: 'include' | 'review' | string;
  confidence: number;
  reason_code: string;
  candidate_id: string;
  research_id: string;
  event_ids: string[];
  semantic_event_ids: string[];
  control_event_ids: string[];
  session_ids: string[];
  trace_ids: string[];
  tool_calls: Array<{
    call_id: string;
    name: string;
    state: 'running' | 'completed' | 'failed' | string;
    event_ids: string[];
    started_at: string;
    ended_at: string;
    producer_agent_run_id: string;
    producer_session_id: string;
    parent_assignment_id: string;
  }>;
  agent_calls: Array<{
    assignment_id: string;
    session_id: string;
    agent_run_id: string;
    parent_session_id: string;
    parent_agent_run_id: string;
    parent_tool_call_id: string;
    name: string;
    task: string;
    state: 'running' | 'completed' | 'partial' | 'failed' | 'cancelled' | string;
    event_ids: string[];
    started_at: string;
    ended_at: string;
    error: string;
    next_action: string;
    result_summary: string;
    completed_work: unknown[];
    failed_work: unknown[];
    review_verdict: string;
    recovery_required: boolean;
  }>;
  workflow_calls: Array<{
    run_id: string;
    template_key: string;
    state: 'running' | 'completed' | 'failed' | string;
    event_ids: string[];
    stages: Array<{
      stage_key: string;
      stage_id: string;
      attempt: number;
      name: string;
      state: string;
      summary: string;
      error: string;
      event_ids: string[];
      artifact_refs: string[];
    }>;
    artifact_refs: string[];
    started_at: string;
    ended_at: string;
    producer_agent_run_id: string;
    producer_session_id: string;
    parent_assignment_id: string;
  }>;
  agent_runs: Array<{
    agent_run_id: string;
    session_id: string;
    parent_agent_run_id: string;
    parent_session_id: string;
    assignment_id: string;
    parent_tool_call_id: string;
    name: string;
    task: string;
    state: 'running' | 'completed' | 'partial' | 'waiting' | 'failed' | 'cancelled' | string;
    is_root: boolean;
    event_ids: string[];
    child_agent_run_ids: string[];
    tool_call_ids: string[];
    workflow_run_ids: string[];
    file_ids: string[];
    artifact_ids: string[];
    started_at: string;
    ended_at: string;
    error: string;
    next_action: string;
    result_summary: string;
    completed_work: unknown[];
    failed_work: unknown[];
    review_verdict: string;
    recovery_required: boolean;
  }>;
  file_refs: Array<{
    file_id: string;
    path: string;
    filename: string;
    sha256: string;
    size: number;
    operations: string[];
    event_ids: string[];
    producer_agent_run_ids: string[];
    producer_session_ids: string[];
    producer_tool_call_ids: string[];
  }>;
  artifact_refs: Array<{
    artifact_id: string;
    ref: string;
    event_id: string;
    event_ids: string[];
    kind: string;
    producer_agent_run_id: string;
    producer_session_id: string;
    producer_tool_call_id: string;
    parent_assignment_id: string;
  }>;
  artifact_groups: ResearchArtifactGroup[];
  category_counts: Partial<Record<'message' | 'tool' | 'mcp' | 'artifact' | 'workflow' | 'agent' | 'file' | 'other', number>>;
  started_at: string;
  ended_at: string;
  source_digest: string;
  rebuilt_at: string;
};

export type ResearchArtifactGroup = {
  group_id: string;
  group_type: 'paper_record' | string;
  title: string;
  summary: string;
  status: 'completed' | 'failed' | string;
  doc_id: string;
  doi: string;
  pmid: string;
  version_count: number;
  latest_version: string;
  artifact_count: number;
  audit_artifact_count: number;
  counts: Partial<Record<'pages' | 'tables' | 'page_images' | 'table_images' | 'figure_images' | 'l1_passages' | 'l2_facts' | 'evidence_records', number>>;
  primary_artifacts: Array<{
    kind: string;
    label: string;
    ref: string;
    event_id: string;
    version: string;
  }>;
  event_ids: string[];
  updated_at: string;
};

export type ProvenanceValue = {
  value: string;
  provenance: 'observed' | 'user_stated' | 'verified' | 'inferred' | string;
  source_event_ids: string[];
};

export type ResearchEpisode = {
  episode_id: string;
  episode_key: string;
  research_id: string;
  revision: number;
  title: string;
  goal: ProvenanceValue;
  summary: ProvenanceValue;
  outcome_status: string;
  event_ids: string[];
  session_ids: string[];
  trace_ids: string[];
  artifact_refs: Array<{ ref: string; event_id: string; kind: string; provenance: string }>;
  tool_calls: Array<{ name: string; status: string; event_id: string; source_ref: string }>;
  decisions: ProvenanceValue[];
  failed_paths: ProvenanceValue[];
  open_questions: ProvenanceValue[];
  next_actions: ProvenanceValue[];
  started_at: string;
  ended_at: string;
  boundary_reason: string;
  source_digest: string;
  state: string;
};

export type ResearchCheckpoint = {
  checkpoint_id: string;
  research_id: string;
  revision: number;
  research_goal: string;
  current_state: string;
  confirmed_facts: Array<Record<string, any>>;
  working_hypotheses: Array<Record<string, any>>;
  recent_progress: Array<Record<string, any>>;
  decisions: Array<Record<string, any>>;
  failed_paths: Array<Record<string, any>>;
  known_conflicts: Array<Record<string, any>>;
  important_artifacts: Array<Record<string, any>>;
  open_questions: Array<Record<string, any>>;
  next_actions: Array<Record<string, any>>;
  generated_at: string;
  token_estimate: number;
  budget_status: string;
  state: string;
};

export type ResearchHypothesis = {
  hypothesis_id: string;
  research_id: string;
  statement: string;
  rationale: string;
  evidence_refs: string[];
  counter_evidence_refs: string[];
  test_plan: string;
  falsification_condition: string;
  status: string;
  revision: number;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type ResearchExperiment = {
  experiment_id: string;
  research_id: string;
  hypothesis_id: string;
  hypothesis_revision: number;
  workflow_run_id: string;
  parent_experiment_id: string | null;
  source_asset_id: string;
  source_experiment_id: string;
  title: string;
  reason: string;
  status: string;
  decision: string;
  result_summary: string;
  run_snapshot: {
    workflow_run_id?: string; template_key?: string; template_digest?: string; session_id?: string; status?: string;
    input?: Record<string, any>;
    environment?: { model?: string; backend?: string; git_commit?: string; git_dirty?: boolean | null; workspace_digest?: string };
    stages?: Array<{ stage_id: string; status: string; result_summary: string; artifacts: Record<string, any>; validation: Record<string, any> }>;
  };
  execution_mode: 'shared_workspace' | 'isolated_directory' | 'git_worktree';
  execution_workdir: string;
  execution_manifest: Record<string, any>;
  input_artifacts: Array<Record<string, any>>;
  output_artifacts: Array<Record<string, any>>;
  metrics: Record<string, any>;
  base_commit: string;
  result_commit: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  frozen_at: string;
};

export type ResearchContinuation = {
  continuation_id: string; research_id: string; status: string;
  max_runs: number; max_minutes: number; runs_started: number;
  max_tokens: number; max_tool_calls: number; max_failures: number;
  tokens_used: number; tool_calls_used: number; failures_used: number;
  max_cost_micros: number; cost_used_micros: number;
  cost_status?: 'known' | 'partial' | 'unknown'; active_runs?: number | null;
  best_experiment_id: string; final_summary: string;
  checkpoint_every: number; selected_hypothesis_id: string;
  stop_reason: string; created_at: string; updated_at: string;
};

export type ResearchContinuationContext = {
  available: boolean;
  active: boolean;
  injected: boolean;
  decision: 'inject' | 'skip' | 'block' | string;
  reason: string;
  research_id: string;
  research_title: string;
  checkpoint_id: string;
  checkpoint_revision: number;
  relevance_score: number;
  matched_terms: string[];
  token_estimate: number;
  token_budget: number;
  target_tokens: number;
  budget_status: string;
  conflict_gate: 'clear' | 'required' | 'overflow' | string;
  conflict_ids: string[];
  included_counts: Record<string, number>;
  omitted_counts: Record<string, number>;
  context_data: Record<string, any>;
  prompt_text: string;
};

export type ResearchFinding = {
  finding_id: string;
  research_id: string;
  episode_id: string;
  statement: string;
  scope: Record<string, any>;
  status: string;
  provenance: string;
  evidence_ids: string[];
  source_event_ids: string[];
  current_revision: number;
  created_at: string;
  updated_at: string;
};

export type ResearchConflict = {
  conflict_id: string;
  research_id: string;
  conflict_type: string;
  status: 'open' | 'under_review' | 'resolved' | string;
  original_refs: string[];
  challenging_refs: string[];
  original_evidence_ids: string[];
  challenging_evidence_ids: string[];
  summary: string;
  detected_by: string;
  detected_at: string;
  current_resolution_id: string;
  updated_at: string;
};

export type ResearchKnowledgeLink = {
  link_id: string;
  link_key: string;
  research_id: string;
  subject_type: 'project' | 'episode' | 'finding' | 'conflict' | 'wiki_claim' | string;
  subject_id: string;
  object_type: 'paper' | 'passage' | 'evidence' | 'wiki_page' | 'wiki_claim' | 'finding' | string;
  object_id: string;
  relation: string;
  side: string;
  state: string;
  source_revision: number;
  source_hash: string;
  metadata: Record<string, any>;
  discovered_by: string;
  revision: number;
  updated_at: string;
};

export type ResearchKnowledgeIssue = {
  severity: 'error' | 'warning' | 'info' | string;
  code: string;
  message: string;
  [key: string]: any;
};

export type ResearchKnowledgeLint = {
  run_id: string;
  research_id: string;
  status: 'pass' | 'warning' | 'error' | string;
  export_ready: boolean;
  checked_link_count: number;
  counts: Record<string, number>;
  issues: ResearchKnowledgeIssue[];
  completed_at: string;
};

export type ResearchKnowledgeSnapshot = {
  research_id: string;
  count: number;
  object_counts: Record<string, number>;
  links: ResearchKnowledgeLink[];
  lint: ResearchKnowledgeLint | null;
  settings?: {
    knowledge_links_enabled?: boolean;
    knowledge_links_active?: boolean;
  };
};

export type ResearchPackPreview = {
  research_id: string;
  privacy_profile: 'strict' | 'standard' | 'full-research' | string;
  attribution_mode: string;
  source_digest: string;
  export_ready: boolean;
  estimated_bytes: number;
  counts: Record<string, number>;
  exclusions: Record<string, number>;
  privacy_report: Record<string, any>;
  compatibility_report: Record<string, any>;
  lint: ResearchKnowledgeLint | Record<string, any>;
  lineage: Record<string, any>;
  issues: ResearchKnowledgeIssue[];
  scope: ResearchPackScope;
  durability_counts: Record<string, number>;
  routing_counts: Record<string, number>;
  dependency_counts: Record<string, number>;
  model_call_count: number;
};

export type ResearchPackScope = {
  mode: 'project' | 'workspace_period';
  source_research_ids: string[];
  started_at: string;
  ended_at: string;
};

export type ResearchPackExport = {
  pack_id: string;
  research_id: string;
  scope_mode: 'project' | 'workspace_period' | string;
  scope: ResearchPackScope | Record<string, any>;
  started_at: string;
  ended_at: string;
  source_research_ids: string[];
  schema_version: string;
  status: 'building' | 'completed' | 'failed' | string;
  privacy_profile: string;
  attribution_mode: string;
  source_digest: string;
  manifest: Record<string, any>;
  privacy_report: Record<string, any>;
  compatibility_report: Record<string, any>;
  archive_name: string;
  archive_sha256: string;
  archive_size: number;
  created_at: string;
  completed_at: string;
  error: string;
  download_ready: boolean;
};

export type ResearchPackImport = {
  import_id: string;
  inspection_id: string;
  pack_id: string;
  asset_id: string;
  local_research_id: string;
  schema_version: string;
  status: 'inspected' | 'importing' | 'completed' | 'failed' | 'cancelled' | 'expired' | string;
  duplicate_action: string;
  archive_name: string;
  archive_sha256: string;
  archive_size: number;
  source_manifest: Record<string, any>;
  inspection_report: {
    valid?: boolean;
    checksums_verified?: boolean;
    content_safety_scanned?: boolean;
    automatic_execution_allowed?: boolean;
    file_count?: number;
    archive_bytes?: number;
    uncompressed_bytes?: number;
    contents?: Record<string, number>;
    conflict_summary?: Record<string, any>;
    lineage?: Record<string, any>;
    researches?: Record<string, any>[];
    scope?: ResearchPackScope | Record<string, any>;
    lineage_availability?: Record<string, any>;
    import_preview?: Record<string, number>;
    duplicate?: {
      already_imported: boolean;
      existing_asset_id: string;
      allowed_actions: string[];
    };
    issues?: ResearchKnowledgeIssue[];
  };
  id_mapping: Record<string, any>;
  compatibility_report: Record<string, any>;
  created_at: string;
  inspected_at: string;
  imported_at: string;
  expires_at: string;
  error: string;
  import_ready: boolean;
};

export type ResearchPackAsset = {
  asset_id: string;
  pack_id: string;
  pack_schema_version: string;
  title: string;
  status: string;
  scope_mode: 'project' | 'workspace_period' | string;
  started_at: string;
  ended_at: string;
  source_research_ids: string[];
  manifest: Record<string, any>;
  summary: Record<string, any>;
  lineage: Record<string, any>;
  source_digest: string;
  archive_sha256: string;
  imported_at: string;
  created_at: string;
};

export type ResearchPackSourceScope = {
  scope_id: string;
  asset_id: string;
  scope_type: 'project' | 'unassigned' | string;
  source_research_id: string;
  title: string;
  summary: string;
  keywords: string[];
  state: string;
};

export type ResearchPackOverlay = {
  overlay_id: string;
  asset_id: string;
  parent_pack_ids: string[];
  status: string;
  source_digest: string;
  created_at: string;
  updated_at: string;
};

export type ResearchPackReadOverview = {
  schema: string;
  asset: ResearchPackAsset;
  scopes: ResearchPackSourceScope[];
  overlays: ResearchPackOverlay[];
  parent_object_counts: Record<string, number>;
  overlay_object_counts: Record<string, number>;
  conflict_gate: Record<string, any>;
  lineage_availability: Record<string, any>;
  document_index?: ResearchDocumentIndexState;
  read_contract: {
    levels: string[];
    pagination: string;
    search_unit?: string;
    search_returns_source_spans?: boolean;
    parent_objects_are_immutable: boolean;
    overlay_objects_are_append_only: boolean;
    content_trust: string;
    automatic_execution_allowed: boolean;
  };
};

export type ResearchDocumentIndexState = {
  status?: 'missing' | 'ready' | 'invalid' | string;
  collection_id?: string;
  document_count?: number;
  chunk_count?: number;
  changed_document_count?: number;
  embedded_chunk_count?: number;
  built_at?: string;
  source_fingerprint?: string;
  fts_available?: boolean;
  completeness?: {
    complete?: boolean;
    expected_parent_objects?: number;
    loaded_parent_objects?: number;
    expected_overlay_objects?: number;
    loaded_overlay_objects?: number;
  };
  vector?: Record<string, any>;
  layout?: string;
  layers?: Record<string, ResearchDocumentIndexState>;
};

export type ResearchPackSearchNeighbor = {
  chunk_id: string;
  ordinal: number;
  heading_path: string;
  content: string;
  line_start: number;
  line_end: number;
  byte_start: number;
  byte_end: number;
};

export type ResearchPackSearchHit = {
  chunk_id: string;
  document_id: string;
  asset_id: string;
  layer: 'parent' | 'overlay' | string;
  object_id: string;
  object_type: string;
  source_object_id: string;
  local_object_id: string;
  source_research_ids: string[];
  logical_path: string;
  title: string;
  heading_path: string;
  content: string;
  line_start: number;
  line_end: number;
  byte_start: number;
  byte_end: number;
  ordinal: number;
  immutable: boolean;
  score: number;
  lexical_score: number;
  vector_score: number;
  neighbors: ResearchPackSearchNeighbor[];
  metadata: Record<string, any>;
};

export type ResearchPackSearchResponse = ResearchPackReadObjectPage & {
  search_mode: 'hybrid' | 'vector' | 'fts' | string;
  hits: ResearchPackSearchHit[];
  index: ResearchDocumentIndexState;
  vector?: Record<string, any>;
  query_hash: string;
  query_stored: boolean;
  index_stale?: boolean;
  query_ms?: number;
};

export type ResearchPackAssetDetail = {
  asset: ResearchPackAsset;
  scopes: ResearchPackSourceScope[];
  overlays: ResearchPackOverlay[];
  object_counts: Record<string, number>;
  overlay_object_counts: Record<string, number>;
};

export type ResearchPackReadObjectPage = {
  schema: string;
  asset_id: string;
  object_type: string;
  source_research_id: string;
  query: string;
  total_count: number;
  loaded_count: number;
  page_complete: boolean;
  next_cursor: string;
  items: Array<Record<string, any>>;
  conflict_gate: Record<string, any>;
  content_trust: string;
  automatic_execution_allowed: boolean;
};

export type ResearchHealth = {
  status: string;
  event_count: number;
  attribution?: {
    active_link_count: number;
    states: Record<string, number>;
    correction_count: number;
  };
  topics?: {
    states: Record<string, number>;
    active_project_count: number;
    pending_research_event_count: number;
    open_merge_suggestion_count: number;
    last_run?: { status: string; started_at: string; completed_at: string } | null;
  };
  activities?: {
    count: number;
    review_states: Record<string, number>;
    statuses: Record<string, number>;
  };
  derivations?: {
    episode_states: Record<string, number>;
    checkpoint_states: Record<string, number>;
    finding_states: Record<string, number>;
    conflict_states: Record<string, number>;
    last_run?: { status: string; started_at: string; completed_at: string } | null;
  };
  knowledge?: {
    object_types: Record<string, number>;
    states: Record<string, number>;
    latest_lint?: Record<string, any> | null;
  };
  settings?: {
    topics_active?: boolean;
    activities_enabled?: boolean;
    activities_active?: boolean;
    activity_incomplete_minutes?: number;
    topic_auto_discovery?: boolean;
    topic_max_candidates?: number;
    topic_max_projects_per_workspace?: number;
    episodes_enabled?: boolean;
    episodes_active?: boolean;
    checkpoints_active?: boolean;
    conflicts_active?: boolean;
    knowledge_links_enabled?: boolean;
    knowledge_links_active?: boolean;
    resume_context_active?: boolean;
    resume_context_enabled?: boolean;
    resume_context_target_tokens?: number;
    resume_context_min_relevance?: number;
    resume_context_max_findings?: number;
    resume_context_max_episodes?: number;
    resume_context_audit_enabled?: boolean;
    research_pack_enabled?: boolean;
    research_pack_active?: boolean;
    research_pack_max_events?: number;
    research_pack_max_bytes?: number;
    research_pack_import_enabled?: boolean;
    research_pack_import_active?: boolean;
    research_pack_import_max_archive_bytes?: number;
    research_pack_import_max_uncompressed_bytes?: number;
    research_pack_import_max_files?: number;
  };
};

export const workspaceQuery = (workspaceId?: string) => {
  const params = new URLSearchParams();
  if (String(workspaceId || '').trim()) params.set('workspace_id', String(workspaceId).trim());
  return params;
};

export async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const upstreamSignal = init?.signal;
  const method = String(init?.method || 'GET').toUpperCase();
  const timeoutMs = method === 'GET' ? 15_000 : 120_000;
  const forwardAbort = () => controller.abort();
  upstreamSignal?.addEventListener('abort', forwardAbort, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(String(payload.detail || payload.message || `请求失败 (${response.status})`));
    }
    return payload as T;
  } catch (error) {
    if (controller.signal.aborted && !upstreamSignal?.aborted) {
      throw new Error(`研究中心请求超时（${Math.round(timeoutMs / 1000)} 秒），请稍后重试`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    upstreamSignal?.removeEventListener('abort', forwardAbort);
  }
}

const jsonPost = <T>(url: string, payload: Record<string, unknown>, workspaceId?: string) =>
  requestJson<T>(`${url}?${workspaceQuery(workspaceId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

const jsonDelete = <T>(url: string, payload: Record<string, unknown>, workspaceId?: string) =>
  requestJson<T>(`${url}?${workspaceQuery(workspaceId)}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

export const getResearchHealth = (workspaceId?: string) =>
  requestJson<ResearchHealth>(`/api/research/health?${workspaceQuery(workspaceId)}`);

export const runTopicDiscovery = (workspaceId?: string, limit = 1000) =>
  jsonPost<{ status: string; report: Record<string, any> }>(
    '/api/research/topics/run',
    { limit },
    workspaceId,
  );

export const listResearchCandidates = (workspaceId?: string, includeInactive = false) => {
  const params = workspaceQuery(workspaceId);
  params.set('include_inactive', String(includeInactive));
  return requestJson<{ count: number; candidates: ResearchCandidate[]; settings?: Record<string, any> }>(
    `/api/research/candidates?${params}`,
  );
};

export const getResearchCandidate = (candidateId: string, workspaceId?: string) =>
  requestJson<{ candidate: ResearchCandidate; candidate_digest?: string; events: ResearchEvent[]; event_count: number }>(
    `/api/research/candidates/${encodeURIComponent(candidateId)}?${workspaceQuery(workspaceId)}`,
  );

export const renameResearchCandidate = (
  candidateId: string,
  title: string,
  workspaceId?: string,
) => jsonPost<{ candidate: ResearchCandidate }>(
  `/api/research/candidates/${encodeURIComponent(candidateId)}/rename`,
  { title },
  workspaceId,
);

export const confirmResearchCandidate = (
  candidateId: string,
  workspaceId?: string,
  expectedCandidateDigest = '',
) => jsonPost<{ candidate: ResearchCandidate; project: ResearchProject; bound_session_ids: string[] }>(
  `/api/research/candidates/${encodeURIComponent(candidateId)}/confirm`,
  expectedCandidateDigest ? { expected_candidate_digest: expectedCandidateDigest } : {},
  workspaceId,
);

export const dismissResearchCandidate = (
  candidateId: string,
  reason: string,
  workspaceId?: string,
) => jsonPost<{ candidate: ResearchCandidate }>(
  `/api/research/candidates/${encodeURIComponent(candidateId)}/dismiss`,
  { reason },
  workspaceId,
);

export const mergeResearchCandidates = (
  candidateIds: string[],
  title: string,
  workspaceId?: string,
) => jsonPost<{ candidate: ResearchCandidate }>(
  '/api/research/candidates/merge',
  { candidate_ids: candidateIds, title },
  workspaceId,
);

export const listResearchProjects = (workspaceId?: string, includeArchived = true) => {
  const params = workspaceQuery(workspaceId);
  params.set('include_archived', String(includeArchived));
  return requestJson<{ count: number; projects: ResearchProject[] }>(
    `/api/research/projects?${params}`,
  );
};

export const listResearchExperimentRuns = (researchId: string, workspaceId?: string, page = 1) => {
  const params = workspaceQuery(workspaceId); params.set('page', String(page));
  return requestJson<{items: Array<{run_id: string; title: string; status: string; updated_at: number | string}>; pagination: {page: number; pages: number}}>(`/api/research/projects/${encodeURIComponent(researchId)}/experiment-runs?${params}`);
};

export const getResearchCenter = (workspaceId?: string) =>
  requestJson<ResearchCenter>(`/api/research/center?${workspaceQuery(workspaceId)}`);

export const getResearchProjectProvenance = (
  researchId: string,
  workspaceId?: string,
) => requestJson<ResearchProjectProvenance>(
  `/api/research/projects/${encodeURIComponent(researchId)}/provenance?${workspaceQuery(workspaceId)}`,
);

export const setResearchProjectStatus = (
  researchId: string,
  status: 'activate' | 'archive',
  workspaceId?: string,
) => jsonPost<{ project: ResearchProject; archived_session_ids?: string[]; restored_session_ids?: string[] }>(
  `/api/research/projects/${encodeURIComponent(researchId)}/${status}`,
  {},
  workspaceId,
);

export const deleteResearchProject = (
  researchId: string,
  confirmation: 'yes',
  previewToken: string,
  workspaceId?: string,
) => jsonDelete<{ status: 'deleted'; deletion: ResearchProjectDeletion; deleted_session_ids: string[] }>(
  `/api/research/projects/${encodeURIComponent(researchId)}`,
  { confirmation, preview_token: previewToken },
  workspaceId,
);

export const getResearchProjectDeletionPreview = (
  researchId: string,
  workspaceId?: string,
) => requestJson<{
  project: ResearchProject;
  research_session_count: number;
  research_session_ids: string[];
  preview_token: string;
  warning: string;
}>(`/api/research/projects/${encodeURIComponent(researchId)}/deletion-preview?${workspaceQuery(workspaceId)}`);

export const listResearchOrganizeEvents = (
  workspaceId?: string,
  state = 'pending',
  relevance = 'research',
) => {
  const params = workspaceQuery(workspaceId);
  params.set('state', state);
  params.set('relevance', relevance);
  params.set('limit', '500');
  return requestJson<{
    count: number;
    items: Array<{ event: ResearchEvent; link: ResearchEventLink }>;
  }>(`/api/research/organize/events?${params}`);
};

export const listResearchActivities = (
  workspaceId?: string,
  reviewState = 'pending',
) => {
  const params = workspaceQuery(workspaceId);
  params.set('review_state', reviewState);
  params.set('limit', '500');
  return requestJson<{
    count: number;
    activities: ResearchActivity[];
    settings?: Record<string, any>;
  }>(`/api/research/organize/activities?${params}`);
};

export const includeResearchActivity = (
  activityId: string,
  candidateId: string,
  workspaceId?: string,
) => jsonPost<Record<string, any>>(
  `/api/research/activities/${encodeURIComponent(activityId)}/include`,
  { candidate_id: candidateId },
  workspaceId,
);

export const excludeResearchActivity = (
  activityId: string,
  reason: string,
  workspaceId?: string,
) => jsonPost<Record<string, any>>(
  `/api/research/activities/${encodeURIComponent(activityId)}/exclude`,
  { reason },
  workspaceId,
);

export const includeResearchEvent = (
  eventId: string,
  candidateId: string,
  workspaceId?: string,
) => jsonPost<Record<string, any>>(
  `/api/research/events/${encodeURIComponent(eventId)}/include`,
  { candidate_id: candidateId },
  workspaceId,
);

export const moveResearchEvent = (
  eventId: string,
  candidateId: string,
  workspaceId?: string,
) => jsonPost<Record<string, any>>(
  `/api/research/events/${encodeURIComponent(eventId)}/move`,
  { candidate_id: candidateId },
  workspaceId,
);

export const excludeResearchEvent = (
  eventId: string,
  reason: string,
  workspaceId?: string,
) => jsonPost<Record<string, any>>(
  `/api/research/events/${encodeURIComponent(eventId)}/exclude`,
  { reason },
  workspaceId,
);

export const listResearchHypotheses = (researchId: string, workspaceId?: string) =>
  requestJson<{ count: number; hypotheses: ResearchHypothesis[] }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/hypotheses?${workspaceQuery(workspaceId)}`,
  );

export const createResearchHypothesis = (
  researchId: string,
  payload: Pick<ResearchHypothesis, 'statement' | 'rationale' | 'test_plan' | 'falsification_condition'>,
  workspaceId?: string,
) => jsonPost<{ hypothesis: ResearchHypothesis }>(
  `/api/research/projects/${encodeURIComponent(researchId)}/hypotheses`,
  { ...payload, evidence_refs: [], counter_evidence_refs: [] },
  workspaceId,
);
export const generateResearchHypotheses = (researchId: string, limit = 3, workspaceId?: string) =>
  jsonPost<{ count: number; hypotheses: ResearchHypothesis[] }>(`/api/research/projects/${encodeURIComponent(researchId)}/hypotheses/generate`, { limit }, workspaceId);

export const reviseResearchHypothesis = (
  hypothesisId: string,
  payload: Pick<ResearchHypothesis, 'statement' | 'rationale' | 'test_plan' | 'falsification_condition'>,
  workspaceId?: string,
) => requestJson<{ hypothesis: ResearchHypothesis }>(
  `/api/research/hypotheses/${encodeURIComponent(hypothesisId)}?${workspaceQuery(workspaceId)}`,
  { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...payload, evidence_refs: [], counter_evidence_refs: [] }) },
);

export const setResearchHypothesisStatus = (hypothesisId: string, status: string, workspaceId?: string) =>
  jsonPost<{ hypothesis: ResearchHypothesis }>(
    `/api/research/hypotheses/${encodeURIComponent(hypothesisId)}/status`,
    { status },
    workspaceId,
  );

export const listResearchExperiments = (researchId: string, workspaceId?: string) =>
  requestJson<{ count: number; experiments: ResearchExperiment[] }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/experiments?${workspaceQuery(workspaceId)}`,
  );

export const createResearchExperiment = (
  researchId: string,
  payload: Pick<ResearchExperiment, 'hypothesis_id' | 'title' | 'reason' | 'workflow_run_id' | 'parent_experiment_id' | 'execution_mode'> & { input_artifacts?: Array<Record<string, any>> },
  workspaceId?: string,
) => jsonPost<{ experiment: ResearchExperiment }>(
  `/api/research/projects/${encodeURIComponent(researchId)}/experiments`,
  payload,
  workspaceId,
);

export const updateResearchExperiment = (
  experimentId: string,
  payload: Pick<ResearchExperiment, 'status' | 'decision' | 'result_summary' | 'workflow_run_id'>,
  workspaceId?: string,
) => requestJson<{ experiment: ResearchExperiment }>(
  `/api/research/experiments/${encodeURIComponent(experimentId)}?${workspaceQuery(workspaceId)}`,
  { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
);

export const compareResearchExperiments = (leftId: string, rightId: string, workspaceId?: string) =>
  requestJson<{ left: ResearchExperiment; right: ResearchExperiment; same_hypothesis: boolean; differences: Record<string, { left: any; right: any }> }>(
    `/api/research/experiments/${encodeURIComponent(leftId)}/compare/${encodeURIComponent(rightId)}?${workspaceQuery(workspaceId)}`,
  );

export const getResearchContinuation = (researchId: string, workspaceId?: string) =>
  requestJson<{ continuation: ResearchContinuation | null }>(`/api/research/projects/${encodeURIComponent(researchId)}/continuation?${workspaceQuery(workspaceId)}`);
export const startResearchContinuation = (researchId: string, maxRuns: number, maxMinutes: number, workspaceId?: string, limits: { max_tokens?: number; max_tool_calls?: number; max_failures?: number; max_cost_micros?: number } = {}) =>
  jsonPost<{ continuation: ResearchContinuation }>(`/api/research/projects/${encodeURIComponent(researchId)}/continuation/start`, { max_runs: maxRuns, max_minutes: maxMinutes, checkpoint_every: 1, ...limits }, workspaceId);
export const controlResearchContinuation = (researchId: string, action: 'pause' | 'resume' | 'reconcile', workspaceId?: string) =>
  jsonPost<{ continuation: ResearchContinuation }>(`/api/research/projects/${encodeURIComponent(researchId)}/continuation/${action}`, {}, workspaceId);
export const createResearchReproduction = (researchId: string, assetId: string, sourceExperimentId: string, workspaceId?: string, sessionId?: string) =>
  jsonPost<{ hypothesis: ResearchHypothesis; experiment: ResearchExperiment; source: Record<string, any>; reproduction_ready: boolean; compatibility_issues: string[] }>(`/api/research/projects/${encodeURIComponent(researchId)}/reproductions`, { asset_id: assetId, source_experiment_id: sourceExperimentId, session_id: sessionId || '' }, workspaceId);
export const compareResearchReproduction = (experimentId: string, workspaceId?: string) =>
  requestJson<{ original: Record<string, any>; reproduction: ResearchExperiment; differences: Record<string, { original: any; reproduction: any }> }>(`/api/research/experiments/${encodeURIComponent(experimentId)}/reproduction-compare?${workspaceQuery(workspaceId)}`);

export const listResearchEpisodes = (
  researchId: string,
  workspaceId?: string,
  includeInactive = false,
) => {
  const params = workspaceQuery(workspaceId);
  params.set('include_inactive', String(includeInactive));
  return requestJson<{ count: number; episodes: ResearchEpisode[] }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/episodes?${params}`,
  );
};

export const refreshResearchEpisodes = (researchId: string, workspaceId?: string) =>
  jsonPost<{ status: string; report: Record<string, any> }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/episodes/refresh`,
    { force_open: true },
    workspaceId,
  );

export const getResearchCheckpoint = (researchId: string, workspaceId?: string) =>
  requestJson<{ checkpoint: ResearchCheckpoint | null }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/checkpoint?${workspaceQuery(workspaceId)}`,
  );

export const previewResearchContext = (researchId: string, workspaceId?: string) =>
  requestJson<{ capsule: Record<string, any> }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/context-preview?${workspaceQuery(workspaceId)}`,
  );

export const previewResearchContinuationContext = (
  researchId: string,
  question: string,
  workspaceId?: string,
) => jsonPost<{ context: ResearchContinuationContext; settings: Record<string, any> }>(
  `/api/research/projects/${encodeURIComponent(researchId)}/continuation-context/preview`,
  { question },
  workspaceId,
);

export const listResearchFindings = (researchId: string, workspaceId?: string) =>
  requestJson<{ count: number; findings: ResearchFinding[] }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/findings?${workspaceQuery(workspaceId)}`,
  );

export const createResearchFinding = (
  researchId: string,
  payload: {
    statement: string;
    provenance: string;
    source_event_ids: string[];
    evidence_ids?: string[];
    episode_id?: string;
    scope?: Record<string, any>;
  },
  workspaceId?: string,
) => jsonPost<{ created: boolean; finding: ResearchFinding }>(
  `/api/research/projects/${encodeURIComponent(researchId)}/findings`,
  payload,
  workspaceId,
);

export const challengeResearchFinding = (
  findingId: string,
  payload: {
    statement: string;
    provenance: string;
    source_event_ids: string[];
    evidence_ids?: string[];
    episode_id?: string;
    conflict_type?: string;
    summary?: string;
  },
  workspaceId?: string,
) => jsonPost<{ challenger: ResearchFinding; conflict: ResearchConflict }>(
  `/api/research/findings/${encodeURIComponent(findingId)}/challenge`,
  payload,
  workspaceId,
);

export const listResearchConflicts = (researchId: string, workspaceId?: string) =>
  requestJson<{ count: number; conflicts: ResearchConflict[] }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/conflicts?${workspaceQuery(workspaceId)}`,
  );

export const reviewResearchConflict = (
  conflictId: string,
  reason: string,
  workspaceId?: string,
) => jsonPost<{ conflict: ResearchConflict }>(
  `/api/research/conflicts/${encodeURIComponent(conflictId)}/review`,
  { reason },
  workspaceId,
);

export const resolveResearchConflict = (
  conflictId: string,
  payload: { result: string; reason: string; evidence_ids: string[] },
  workspaceId?: string,
) => jsonPost<{ conflict: ResearchConflict }>(
  `/api/research/conflicts/${encodeURIComponent(conflictId)}/resolve`,
  payload,
  workspaceId,
);

export const reopenResearchConflict = (
  conflictId: string,
  reason: string,
  evidenceIds: string[],
  workspaceId?: string,
) => jsonPost<{ conflict: ResearchConflict }>(
  `/api/research/conflicts/${encodeURIComponent(conflictId)}/reopen`,
  { reason, evidence_ids: evidenceIds },
  workspaceId,
);

export const getResearchKnowledge = (researchId: string, workspaceId?: string) =>
  requestJson<ResearchKnowledgeSnapshot>(
    `/api/research/projects/${encodeURIComponent(researchId)}/knowledge?${workspaceQuery(workspaceId)}`,
  );

export const refreshResearchKnowledge = (researchId: string, workspaceId?: string) =>
  jsonPost<{ status: string; report: Record<string, any> }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/knowledge/refresh`,
    {},
    workspaceId,
  );

export const lintResearchKnowledge = (researchId: string, workspaceId?: string) =>
  jsonPost<{ lint: ResearchKnowledgeLint }>(
    `/api/research/projects/${encodeURIComponent(researchId)}/knowledge/lint`,
    {},
    workspaceId,
  );

export const createResearchKnowledgeLink = (
  researchId: string,
  payload: {
    subject_type: string;
    subject_id: string;
    object_type: string;
    object_id: string;
    relation: string;
    side?: string;
    metadata?: Record<string, any>;
  },
  workspaceId?: string,
) => jsonPost<{ link: ResearchKnowledgeLink }>(
  `/api/research/projects/${encodeURIComponent(researchId)}/knowledge/links`,
  payload,
  workspaceId,
);

export const removeResearchKnowledgeLink = (linkId: string, workspaceId?: string) =>
  jsonPost<{ link: ResearchKnowledgeLink }>(
    `/api/research/knowledge/links/${encodeURIComponent(linkId)}/remove`,
    {},
    workspaceId,
  );

export const previewResearchPack = (
  researchId: string,
  privacyProfile: string,
  workspaceId?: string,
) => jsonPost<{ preview: ResearchPackPreview }>(
  '/api/research/packs/preview',
  {
    mode: 'project',
    research_ids: [researchId],
    privacy_profile: privacyProfile,
    attribution_mode: 'anonymous',
  },
  workspaceId,
);

export const buildResearchPack = (
  researchId: string,
  payload: {
    privacy_profile: string;
    expected_source_digest: string;
    acknowledge_privacy: boolean;
  },
  workspaceId?: string,
) => jsonPost<{ export: ResearchPackExport }>(
  '/api/research/packs/build',
  {
    mode: 'project',
    research_ids: [researchId],
    ...payload,
    attribution_mode: 'anonymous',
  },
  workspaceId,
);

export const listResearchPacks = (researchId: string, workspaceId?: string) =>
  requestJson<{
    count: number;
    exports: ResearchPackExport[];
    settings: { research_pack_enabled: boolean; research_pack_active: boolean; research_pack_max_bytes: number };
  }>(`/api/research/projects/${encodeURIComponent(researchId)}/packs?${workspaceQuery(workspaceId)}`);

export const previewWorkspaceResearchPack = (
  startedAt: string,
  endedAt: string,
  privacyProfile: string,
  workspaceId?: string,
) => jsonPost<{ preview: ResearchPackPreview }>(
  '/api/research/packs/preview',
  {
    mode: 'workspace_period',
    research_ids: [],
    started_at: startedAt,
    ended_at: endedAt,
    privacy_profile: privacyProfile,
    attribution_mode: 'anonymous',
  },
  workspaceId,
);

export const buildWorkspaceResearchPack = (
  payload: {
    started_at: string;
    ended_at: string;
    privacy_profile: string;
    expected_source_digest: string;
    acknowledge_privacy: boolean;
  },
  workspaceId?: string,
) => jsonPost<{ export: ResearchPackExport }>(
  '/api/research/packs/build',
  {
    mode: 'workspace_period',
    research_ids: [],
    ...payload,
    attribution_mode: 'anonymous',
  },
  workspaceId,
);

export const listWorkspaceResearchPacks = (workspaceId?: string) =>
  requestJson<{
    count: number;
    exports: ResearchPackExport[];
    settings: { research_pack_enabled: boolean; research_pack_active: boolean; research_pack_max_bytes: number };
  }>(`/api/research/packs?${workspaceQuery(workspaceId)}`);

export const researchPackDownloadUrl = (packId: string, workspaceId?: string) =>
  `/api/research/packs/${encodeURIComponent(packId)}/download?${workspaceQuery(workspaceId)}`;

export const inspectResearchPackImport = async (
  file: File,
  workspaceId?: string,
) => {
  const form = new FormData();
  form.append('file', file, file.name);
  return requestJson<{ inspection: ResearchPackImport }>(
    `/api/research/packs/imports/inspect?${workspaceQuery(workspaceId)}`,
    { method: 'POST', body: form },
  );
};

export const importResearchPack = (
  inspectionId: string,
  duplicateAction: 'copy' | 'open_existing' | 'cancel',
  title: string,
  workspaceId?: string,
) => jsonPost<{ import: ResearchPackImport }>(
  '/api/research/packs/imports',
  {
    inspection_id: inspectionId,
    duplicate_action: duplicateAction,
    title,
  },
  workspaceId,
);

export const listResearchPackImports = (workspaceId?: string, hidden = false, offset = 0) =>
  requestJson<{
    count: number;
    next_offset: number | null;
    imports: ResearchPackImport[];
    settings: {
      research_pack_import_enabled: boolean;
      research_pack_import_active: boolean;
      research_pack_import_recovery_enabled?: boolean;
      max_archive_bytes: number;
      inspection_ttl_minutes: number;
    };
  }>(`/api/research/packs/imports?${workspaceQuery(workspaceId)}&hidden=${hidden}&offset=${offset}`);

export const listResearchPackAssets = (workspaceId?: string, archived = false, offset = 0) =>
  requestJson<{ count: number; assets: ResearchPackAsset[]; next_offset: number | null }>(
    `/api/research/pack-assets?${workspaceQuery(workspaceId)}&archived=${archived}&offset=${offset}`,
  );

export const removeResearchPackListing = (id: string, kind: 'asset' | 'import', restore: boolean, workspaceId?: string) =>
  requestJson<{removed: boolean; id: string}>(`/api/research/${kind === 'asset' ? 'pack-assets' : 'packs/imports'}/${encodeURIComponent(id)}${restore ? '/restore' : ''}?${workspaceQuery(workspaceId)}`, {method:restore ? 'POST' : 'DELETE'});

export const getResearchPackAsset = (assetId: string, workspaceId?: string) =>
  requestJson<ResearchPackAssetDetail>(
    `/api/research/pack-assets/${encodeURIComponent(assetId)}?${workspaceQuery(workspaceId)}`,
  );

export const getResearchPackReadOverview = (assetId: string, workspaceId?: string) =>
  requestJson<ResearchPackReadOverview>(
    `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/overview?${workspaceQuery(workspaceId)}`,
  );

export const listResearchPackReadScopes = (assetId: string, workspaceId?: string) =>
  requestJson<{
    schema: string;
    asset_id: string;
    count: number;
    items: ResearchPackSourceScope[];
    conflict_gate: Record<string, any>;
  }>(
    `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/scopes?${workspaceQuery(workspaceId)}`,
  );

export const listResearchPackReadObjects = (
  assetId: string,
  options: {
    object_type?: string;
    source_research_id?: string;
    query?: string;
    limit?: number;
    cursor?: string;
  } = {},
  workspaceId?: string,
) => {
  const params = new URLSearchParams(workspaceQuery(workspaceId));
  Object.entries(options).forEach(([key, value]) => {
    if (value !== undefined && value !== '') params.set(key, String(value));
  });
  return requestJson<ResearchPackReadObjectPage>(
    `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/objects?${params.toString()}`,
  );
};

export const searchResearchPackRead = (
  assetId: string,
  query: string,
  sourceResearchId = '',
  limit = 20,
  workspaceId?: string,
) => jsonPost<ResearchPackSearchResponse>(
  `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/search`,
  {
    query,
    source_research_id: sourceResearchId,
    limit,
  },
  workspaceId,
);

export const getResearchPackReadObject = (
  assetId: string,
  objectType: string,
  objectId: string,
  workspaceId?: string,
) => requestJson<Record<string, any>>(
  `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/objects/`
  + `${encodeURIComponent(objectType)}/${encodeURIComponent(objectId)}?${workspaceQuery(workspaceId)}`,
);

export const getResearchPackIndexStatus = (
  assetId: string,
  workspaceId?: string,
) => requestJson<{ schema: string; asset_id: string; index: ResearchDocumentIndexState }>(
  `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/index?${workspaceQuery(workspaceId)}`,
);

export const rebuildResearchPackIndex = (
  assetId: string,
  forceEmbeddings = false,
  workspaceId?: string,
) => jsonPost<{ schema: string; asset_id: string; index: ResearchDocumentIndexState }>(
  `/api/research/pack-assets/${encodeURIComponent(assetId)}/read/index/rebuild`,
  { force_embeddings: forceEmbeddings },
  workspaceId,
);

export const previewResearchPackAssetOverlay = (
  assetId: string,
  privacyProfile: string,
  workspaceId?: string,
) => jsonPost<{ preview: ResearchPackPreview }>(
  `/api/research/pack-assets/${encodeURIComponent(assetId)}/packs/preview`,
  {
    privacy_profile: privacyProfile,
    attribution_mode: 'anonymous',
  },
  workspaceId,
);

export const buildResearchPackAssetOverlay = (
  assetId: string,
  payload: {
    privacy_profile: string;
    expected_source_digest: string;
    acknowledge_privacy: boolean;
  },
  workspaceId?: string,
) => jsonPost<{ export: ResearchPackExport }>(
  `/api/research/pack-assets/${encodeURIComponent(assetId)}/packs/build`,
  { ...payload, attribution_mode: 'anonymous' },
  workspaceId,
);
