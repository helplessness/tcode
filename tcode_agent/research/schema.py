"""Research Journal table definitions and backward-compatible SQLite migrations."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 25

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    parent_event_id TEXT NOT NULL DEFAULT '',
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    privacy_state TEXT NOT NULL DEFAULT 'private',
    ingest_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_events_workspace_time
    ON research_events(tenant_id, user_id, workspace_id, occurred_at);
CREATE TABLE IF NOT EXISTS research_routing_outbox (
    event_id TEXT PRIMARY KEY REFERENCES research_events(event_id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_research_events_session_turn
    ON research_events(session_id, turn_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_research_events_trace_time
    ON research_events(trace_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_research_events_source
    ON research_events(source_kind, source_id);
CREATE INDEX IF NOT EXISTS idx_research_events_type_time
    ON research_events(event_type, occurred_at);

CREATE TABLE IF NOT EXISTS research_event_links (
    link_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL DEFAULT '',
    research_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    relevance TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason_code TEXT NOT NULL,
    reason_detail_json TEXT NOT NULL DEFAULT '{}',
    shadow INTEGER NOT NULL DEFAULT 1,
    decision_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correction_id TEXT NOT NULL DEFAULT '',
    semantic_fingerprint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    decided_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(event_id) REFERENCES research_events(event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_event_links_active
    ON research_event_links(event_id)
    WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_event_links_owner_state
    ON research_event_links(
        tenant_id, user_id, workspace_id, state, decided_at
    );
CREATE INDEX IF NOT EXISTS idx_research_event_links_target
    ON research_event_links(candidate_id, research_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_research_event_links_run
    ON research_event_links(run_id, decided_at);

CREATE TABLE IF NOT EXISTS research_attribution_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'shadow',
    trigger_kind TEXT NOT NULL,
    decision_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    scanned_count INTEGER NOT NULL DEFAULT 0,
    decision_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    counts_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_research_attribution_runs_owner_time
    ON research_attribution_runs(
        tenant_id, user_id, workspace_id, started_at
    );

CREATE TABLE IF NOT EXISTS research_attribution_corrections (
    correction_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    candidate_id TEXT NOT NULL DEFAULT '',
    research_id TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    previous_link_id TEXT NOT NULL DEFAULT '',
    shadow_state TEXT NOT NULL DEFAULT '',
    shadow_relevance TEXT NOT NULL DEFAULT '',
    shadow_candidate_id TEXT NOT NULL DEFAULT '',
    shadow_research_id TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(event_id) REFERENCES research_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_research_attribution_corrections_event
    ON research_attribution_corrections(event_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_research_attribution_corrections_owner
    ON research_attribution_corrections(
        tenant_id, user_id, workspace_id, occurred_at
    );

CREATE TABLE IF NOT EXISTS research_topic_candidates (
    candidate_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    suggested_title TEXT NOT NULL,
    title_source TEXT NOT NULL DEFAULT 'rule',
    goal_summary TEXT NOT NULL DEFAULT '',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    supporting_event_count INTEGER NOT NULL DEFAULT 0,
    session_count INTEGER NOT NULL DEFAULT 0,
    artifact_count INTEGER NOT NULL DEFAULT 0,
    tool_count INTEGER NOT NULL DEFAULT 0,
    explicit_goal INTEGER NOT NULL DEFAULT 0,
    stability_score REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'emerging',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    merged_into TEXT NOT NULL DEFAULT '',
    split_from_json TEXT NOT NULL DEFAULT '[]',
    confirmed_research_id TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1,
    decision_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_topic_candidates_owner_state
    ON research_topic_candidates(
        tenant_id, user_id, workspace_id, state, last_seen_at
    );

CREATE TABLE IF NOT EXISTS research_pending_actions (
    action_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    proposal_message_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_digest TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    decision_message_id TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_pending_actions_owner_session
    ON research_pending_actions(
        tenant_id,user_id,workspace_id,session_id,status,created_at
    );

CREATE TABLE IF NOT EXISTS research_candidate_memberships (
    membership_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    state TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason_code TEXT NOT NULL,
    operation_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    semantic_fingerprint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    decided_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(event_id) REFERENCES research_events(event_id),
    FOREIGN KEY(candidate_id) REFERENCES research_topic_candidates(candidate_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_candidate_memberships_active
    ON research_candidate_memberships(event_id)
    WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_candidate_memberships_candidate
    ON research_candidate_memberships(
        tenant_id, user_id, workspace_id, candidate_id, state, decided_at
    );

CREATE TABLE IF NOT EXISTS research_projects (
    research_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    origin_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_projects_owner_status
    ON research_projects(
        tenant_id, user_id, workspace_id, status, updated_at
    );

CREATE TABLE IF NOT EXISTS research_project_deletions (
    deletion_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    private_event_count INTEGER NOT NULL DEFAULT 0,
    local_file_count INTEGER NOT NULL DEFAULT 0,
    records_json TEXT NOT NULL DEFAULT '{}',
    cleanup_errors_json TEXT NOT NULL DEFAULT '[]',
    deleted_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_project_deletions_owner_time
    ON research_project_deletions(
        tenant_id, user_id, workspace_id, deleted_at
    );

CREATE TABLE IF NOT EXISTS research_topic_operations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    source_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
    target_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
    event_ids_json TEXT NOT NULL DEFAULT '[]',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_topic_operations_owner_time
    ON research_topic_operations(
        tenant_id, user_id, workspace_id, occurred_at
    );

CREATE TABLE IF NOT EXISTS research_topic_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    decision_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    event_count INTEGER NOT NULL DEFAULT 0,
    group_count INTEGER NOT NULL DEFAULT 0,
    candidate_created_count INTEGER NOT NULL DEFAULT 0,
    membership_changed_count INTEGER NOT NULL DEFAULT 0,
    merge_suggestion_count INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_research_topic_runs_owner_time
    ON research_topic_runs(
        tenant_id, user_id, workspace_id, started_at
    );

CREATE TABLE IF NOT EXISTS research_topic_merge_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    score REAL NOT NULL,
    reason_code TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_topic_merge_suggestions_owner_state
    ON research_topic_merge_suggestions(
        tenant_id, user_id, workspace_id, state, updated_at
    );

CREATE TABLE IF NOT EXISTS research_episodes (
    episode_id TEXT PRIMARY KEY,
    episode_key TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    goal_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}',
    outcome_status TEXT NOT NULL DEFAULT 'unknown',
    event_ids_json TEXT NOT NULL DEFAULT '[]',
    session_ids_json TEXT NOT NULL DEFAULT '[]',
    trace_ids_json TEXT NOT NULL DEFAULT '[]',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    decisions_json TEXT NOT NULL DEFAULT '[]',
    failed_paths_json TEXT NOT NULL DEFAULT '[]',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    next_actions_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    boundary_reason TEXT NOT NULL,
    derivation_version TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    supersedes_episode_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_episodes_active_key
    ON research_episodes(episode_key)
    WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_research_episodes_owner_project
    ON research_episodes(
        tenant_id, user_id, workspace_id, research_id, state, started_at
    );

CREATE TABLE IF NOT EXISTS research_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    research_goal TEXT NOT NULL DEFAULT '',
    current_state TEXT NOT NULL DEFAULT '',
    confirmed_facts_json TEXT NOT NULL DEFAULT '[]',
    working_hypotheses_json TEXT NOT NULL DEFAULT '[]',
    recent_progress_json TEXT NOT NULL DEFAULT '[]',
    decisions_json TEXT NOT NULL DEFAULT '[]',
    failed_paths_json TEXT NOT NULL DEFAULT '[]',
    known_conflicts_json TEXT NOT NULL DEFAULT '[]',
    important_artifacts_json TEXT NOT NULL DEFAULT '[]',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    next_actions_json TEXT NOT NULL DEFAULT '[]',
    source_episode_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    supersedes_checkpoint_id TEXT NOT NULL DEFAULT '',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    budget_status TEXT NOT NULL DEFAULT 'within_budget',
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_checkpoints_active
    ON research_checkpoints(research_id)
    WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_research_checkpoints_owner_project
    ON research_checkpoints(
        tenant_id, user_id, workspace_id, research_id, revision
    );

CREATE TABLE IF NOT EXISTS research_findings (
    finding_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    episode_id TEXT NOT NULL DEFAULT '',
    statement TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    provenance TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    current_revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE INDEX IF NOT EXISTS idx_research_findings_owner_project
    ON research_findings(
        tenant_id, user_id, workspace_id, research_id, status, updated_at
    );

CREATE TABLE IF NOT EXISTS research_finding_versions (
    version_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    statement TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    provenance TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(finding_id) REFERENCES research_findings(finding_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_finding_versions_revision
    ON research_finding_versions(finding_id, revision);

CREATE TABLE IF NOT EXISTS research_change_events (
    change_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    created_ids_json TEXT NOT NULL DEFAULT '[]',
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_change_events_owner_project
    ON research_change_events(
        tenant_id, user_id, workspace_id, research_id, occurred_at
    );

CREATE TABLE IF NOT EXISTS research_conflicts (
    conflict_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    original_refs_json TEXT NOT NULL DEFAULT '[]',
    challenging_refs_json TEXT NOT NULL DEFAULT '[]',
    original_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    challenging_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL,
    detected_by TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    current_resolution_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE INDEX IF NOT EXISTS idx_research_conflicts_owner_project
    ON research_conflicts(
        tenant_id, user_id, workspace_id, research_id, status, updated_at
    );

CREATE TABLE IF NOT EXISTS research_resolutions (
    resolution_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    conflict_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    result TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    scope_json TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(conflict_id) REFERENCES research_conflicts(conflict_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_resolutions_revision
    ON research_resolutions(conflict_id, revision);

CREATE TABLE IF NOT EXISTS research_derivation_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL DEFAULT '',
    trigger_kind TEXT NOT NULL,
    derivation_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    project_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    episode_created_count INTEGER NOT NULL DEFAULT 0,
    episode_stale_count INTEGER NOT NULL DEFAULT 0,
    checkpoint_created_count INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_research_derivation_runs_owner_time
    ON research_derivation_runs(
        tenant_id, user_id, workspace_id, started_at
    );

CREATE TABLE IF NOT EXISTS research_knowledge_links (
    link_id TEXT PRIMARY KEY,
    link_key TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'neutral',
    state TEXT NOT NULL DEFAULT 'active',
    source_revision INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    discovered_by TEXT NOT NULL DEFAULT 'system',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    supersedes_link_id TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_knowledge_links_active_key
    ON research_knowledge_links(link_key)
    WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_knowledge_links_owner_project
    ON research_knowledge_links(
        tenant_id, user_id, workspace_id, research_id,
        state, object_type, updated_at
    );
CREATE INDEX IF NOT EXISTS idx_research_knowledge_links_subject
    ON research_knowledge_links(subject_type, subject_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_research_knowledge_links_object
    ON research_knowledge_links(object_type, object_id, updated_at);

CREATE TABLE IF NOT EXISTS research_knowledge_lint_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    status TEXT NOT NULL,
    export_ready INTEGER NOT NULL DEFAULT 0,
    checked_link_count INTEGER NOT NULL DEFAULT 0,
    counts_json TEXT NOT NULL DEFAULT '{}',
    issues_json TEXT NOT NULL DEFAULT '[]',
    source_digest TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE INDEX IF NOT EXISTS idx_research_knowledge_lint_owner_project
    ON research_knowledge_lint_runs(
        tenant_id, user_id, workspace_id, research_id, completed_at
    );

CREATE TABLE IF NOT EXISTS research_pack_exports (
    pack_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    status TEXT NOT NULL,
    privacy_profile TEXT NOT NULL,
    attribution_mode TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    privacy_report_json TEXT NOT NULL DEFAULT '{}',
    compatibility_report_json TEXT NOT NULL DEFAULT '{}',
    archive_name TEXT NOT NULL DEFAULT '',
    archive_path TEXT NOT NULL DEFAULT '',
    archive_sha256 TEXT NOT NULL DEFAULT '',
    archive_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(research_id) REFERENCES research_projects(research_id)
);

CREATE INDEX IF NOT EXISTS idx_research_pack_exports_owner_project
    ON research_pack_exports(
        tenant_id, user_id, workspace_id, research_id,
        status, created_at
    );

CREATE TABLE IF NOT EXISTS research_pack_imports (
    import_id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL UNIQUE,
    pack_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    asset_id TEXT NOT NULL DEFAULT '',
    local_research_id TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    duplicate_action TEXT NOT NULL DEFAULT 'cancel',
    archive_name TEXT NOT NULL,
    staged_path TEXT NOT NULL DEFAULT '',
    archive_sha256 TEXT NOT NULL,
    archive_size INTEGER NOT NULL DEFAULT 0,
    source_manifest_json TEXT NOT NULL DEFAULT '{}',
    inspection_report_json TEXT NOT NULL DEFAULT '{}',
    id_mapping_json TEXT NOT NULL DEFAULT '{}',
    compatibility_report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    inspected_at TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_pack_imports_owner_pack
    ON research_pack_imports(
        tenant_id, user_id, workspace_id, pack_id,
        status, created_at
    );
CREATE INDEX IF NOT EXISTS idx_research_pack_imports_local_project
    ON research_pack_imports(local_research_id, status, imported_at);

CREATE TABLE IF NOT EXISTS research_pack_id_mappings (
    mapping_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    local_object_id TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(import_id, object_type, source_object_id),
    FOREIGN KEY(import_id) REFERENCES research_pack_imports(import_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_pack_id_mappings_local
    ON research_pack_id_mappings(
        object_type, local_object_id, import_id
    );

CREATE TABLE IF NOT EXISTS research_activities (
    activity_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    activity_key TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    review_state TEXT NOT NULL DEFAULT 'pending',
    recommendation TEXT NOT NULL DEFAULT 'review',
    confidence REAL NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL DEFAULT '',
    candidate_id TEXT NOT NULL DEFAULT '',
    research_id TEXT NOT NULL DEFAULT '',
    event_ids_json TEXT NOT NULL DEFAULT '[]',
    semantic_event_ids_json TEXT NOT NULL DEFAULT '[]',
    control_event_ids_json TEXT NOT NULL DEFAULT '[]',
    session_ids_json TEXT NOT NULL DEFAULT '[]',
    trace_ids_json TEXT NOT NULL DEFAULT '[]',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    agent_calls_json TEXT NOT NULL DEFAULT '[]',
    workflow_calls_json TEXT NOT NULL DEFAULT '[]',
    agent_runs_json TEXT NOT NULL DEFAULT '[]',
    file_refs_json TEXT NOT NULL DEFAULT '[]',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    artifact_groups_json TEXT NOT NULL DEFAULT '[]',
    category_counts_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL,
    rebuilt_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    UNIQUE(tenant_id, user_id, workspace_id, activity_key)
);

CREATE INDEX IF NOT EXISTS idx_research_activities_owner_review
    ON research_activities(
        tenant_id, user_id, workspace_id, review_state, started_at
    );
CREATE INDEX IF NOT EXISTS idx_research_activities_candidate
    ON research_activities(candidate_id, research_id, started_at);

CREATE TABLE IF NOT EXISTS research_context_audits (
    context_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    research_id TEXT NOT NULL DEFAULT '',
    question_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    relevance_score REAL NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    token_budget INTEGER NOT NULL DEFAULT 0,
    conflict_gate TEXT NOT NULL DEFAULT 'clear',
    matched_terms_json TEXT NOT NULL DEFAULT '[]',
    conflict_ids_json TEXT NOT NULL DEFAULT '[]',
    included_counts_json TEXT NOT NULL DEFAULT '{}',
    omitted_counts_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_context_audits_owner_time
    ON research_context_audits(
        tenant_id, user_id, workspace_id, created_at
    );
CREATE INDEX IF NOT EXISTS idx_research_context_audits_project_time
    ON research_context_audits(research_id, created_at);

CREATE TABLE IF NOT EXISTS research_turn_routes (
    route_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    research_ids_json TEXT NOT NULL DEFAULT '[]',
    imported_scope_refs_json TEXT NOT NULL DEFAULT '[]',
    durability TEXT NOT NULL DEFAULT 'transient',
    confidence REAL NOT NULL DEFAULT 0,
    routing_state TEXT NOT NULL DEFAULT 'unresolved',
    reason_code TEXT NOT NULL DEFAULT '',
    decision_source TEXT NOT NULL DEFAULT 'deterministic',
    source_digest TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_turn_routes_active
    ON research_turn_routes(
        tenant_id, user_id, workspace_id, turn_id
    ) WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_turn_routes_owner_time
    ON research_turn_routes(
        tenant_id, user_id, workspace_id, created_at
    );

CREATE TABLE IF NOT EXISTS research_object_scopes (
    scope_link_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'primary',
    confidence REAL NOT NULL DEFAULT 1,
    source_route_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_object_scopes_active
    ON research_object_scopes(
        tenant_id, user_id, workspace_id,
        object_type, object_id, research_id
    ) WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_object_scopes_project
    ON research_object_scopes(
        tenant_id, user_id, workspace_id,
        research_id, object_type, created_at
    );

CREATE TABLE IF NOT EXISTS research_experience_items (
    experience_id TEXT PRIMARY KEY,
    experience_key TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summary_state TEXT NOT NULL DEFAULT 'complete',
    durability TEXT NOT NULL DEFAULT 'durable',
    outcome_status TEXT NOT NULL DEFAULT 'unknown',
    routing_state TEXT NOT NULL DEFAULT 'unresolved',
    source_research_ids_json TEXT NOT NULL DEFAULT '[]',
    source_activity_ids_json TEXT NOT NULL DEFAULT '[]',
    source_episode_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    origin_pack_ids_json TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL DEFAULT 'observed',
    occurred_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active',
    source_digest TEXT NOT NULL,
    supersedes_experience_id TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_experience_items_active
    ON research_experience_items(
        tenant_id, user_id, workspace_id, experience_key
    ) WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_experience_items_owner_time
    ON research_experience_items(
        tenant_id, user_id, workspace_id,
        durability, occurred_at
    );

CREATE TABLE IF NOT EXISTS research_pack_assets (
    asset_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    pack_id TEXT NOT NULL,
    pack_schema_version TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    scope_mode TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    source_research_ids_json TEXT NOT NULL DEFAULT '[]',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}',
    lineage_json TEXT NOT NULL DEFAULT '{}',
    source_digest TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_research_pack_assets_owner_status
    ON research_pack_assets(
        tenant_id, user_id, workspace_id, status, imported_at
    );
CREATE INDEX IF NOT EXISTS idx_research_pack_assets_owner_pack
    ON research_pack_assets(
        tenant_id, user_id, workspace_id, pack_id, imported_at
    );

CREATE TABLE IF NOT EXISTS research_pack_scopes (
    scope_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'project',
    source_research_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    UNIQUE(asset_id, source_research_id),
    FOREIGN KEY(asset_id) REFERENCES research_pack_assets(asset_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_pack_scopes_owner_asset
    ON research_pack_scopes(
        tenant_id, user_id, workspace_id, asset_id, state
    );

CREATE TABLE IF NOT EXISTS research_pack_overlays (
    overlay_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    parent_pack_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    source_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(asset_id) REFERENCES research_pack_assets(asset_id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_pack_overlays_active
    ON research_pack_overlays(asset_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_research_pack_overlays_owner
    ON research_pack_overlays(
        tenant_id, user_id, workspace_id, updated_at
    );

CREATE TABLE IF NOT EXISTS research_pack_overlay_objects (
    overlay_object_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    overlay_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    local_object_id TEXT NOT NULL,
    imported_scope_refs_json TEXT NOT NULL DEFAULT '[]',
    origin_pack_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active',
    source_digest TEXT NOT NULL,
    supersedes_overlay_object_id TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY(overlay_id) REFERENCES research_pack_overlays(overlay_id)
        ON DELETE CASCADE,
    FOREIGN KEY(asset_id) REFERENCES research_pack_assets(asset_id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_pack_overlay_objects_active
    ON research_pack_overlay_objects(
        overlay_id, object_type, object_key
    ) WHERE superseded_by = '';
CREATE INDEX IF NOT EXISTS idx_research_pack_overlay_objects_owner
    ON research_pack_overlay_objects(
        tenant_id, user_id, workspace_id,
        asset_id, object_type, updated_at
    );

CREATE TABLE IF NOT EXISTS research_pack_object_mappings (
    mapping_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    local_object_id TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    UNIQUE(asset_id, object_type, source_object_id),
    FOREIGN KEY(asset_id) REFERENCES research_pack_assets(asset_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_pack_object_mappings_owner
    ON research_pack_object_mappings(
        tenant_id, user_id, workspace_id,
        object_type, local_object_id
    );

CREATE TABLE IF NOT EXISTS research_pack_objects (
    object_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    source_research_ids_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    immutable INTEGER NOT NULL DEFAULT 1,
    distribution TEXT NOT NULL DEFAULT 'snapshot',
    available_layers_json TEXT NOT NULL DEFAULT '[]',
    missing_layers_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    UNIQUE(asset_id, object_type, source_object_id),
    FOREIGN KEY(asset_id) REFERENCES research_pack_assets(asset_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_pack_objects_owner_asset
    ON research_pack_objects(
        tenant_id, user_id, workspace_id,
        asset_id, object_type, created_at
    );

CREATE TRIGGER IF NOT EXISTS trg_research_pack_objects_immutable
BEFORE UPDATE ON research_pack_objects
BEGIN
    SELECT RAISE(ABORT, 'imported ResearchPack objects are immutable');
END;

CREATE TABLE IF NOT EXISTS research_hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    counter_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    test_plan TEXT NOT NULL DEFAULT '',
    falsification_condition TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_hypotheses_project
    ON research_hypotheses(tenant_id,user_id,workspace_id,research_id,status,updated_at);

CREATE TABLE IF NOT EXISTS research_hypothesis_versions (
    hypothesis_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    statement TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    test_plan TEXT NOT NULL DEFAULT '',
    falsification_condition TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(hypothesis_id, revision),
    FOREIGN KEY(hypothesis_id) REFERENCES research_hypotheses(hypothesis_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS research_experiments (
    experiment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    hypothesis_revision INTEGER NOT NULL DEFAULT 1,
    workflow_run_id TEXT NOT NULL DEFAULT '',
    parent_experiment_id TEXT,
    source_asset_id TEXT NOT NULL DEFAULT '',
    source_experiment_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planned',
    decision TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    run_snapshot_json TEXT NOT NULL DEFAULT '{}',
    execution_mode TEXT NOT NULL DEFAULT 'shared_workspace',
    execution_workdir TEXT NOT NULL DEFAULT '',
    execution_manifest_json TEXT NOT NULL DEFAULT '{}',
    input_artifacts_json TEXT NOT NULL DEFAULT '[]',
    output_artifacts_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    base_commit TEXT NOT NULL DEFAULT '',
    result_commit TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    frozen_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(hypothesis_id) REFERENCES research_hypotheses(hypothesis_id),
    FOREIGN KEY(parent_experiment_id) REFERENCES research_experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_research_experiments_project
    ON research_experiments(tenant_id,user_id,workspace_id,research_id,updated_at);
CREATE INDEX IF NOT EXISTS idx_research_experiments_parent
    ON research_experiments(parent_experiment_id);

CREATE TABLE IF NOT EXISTS research_continuations (
    continuation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paused',
    max_runs INTEGER NOT NULL,
    max_minutes INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL DEFAULT 0,
    max_tool_calls INTEGER NOT NULL DEFAULT 0,
    max_failures INTEGER NOT NULL DEFAULT 0,
    max_cost_micros INTEGER NOT NULL DEFAULT 0,
    runs_started INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    tool_calls_used INTEGER NOT NULL DEFAULT 0,
    failures_used INTEGER NOT NULL DEFAULT 0,
    cost_used_micros INTEGER NOT NULL DEFAULT 0,
    checkpoint_every INTEGER NOT NULL DEFAULT 1,
    selected_hypothesis_id TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    best_experiment_id TEXT NOT NULL DEFAULT '',
    final_summary TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id,user_id,workspace_id,research_id)
);
"""


_SCHEMA_SQL += """
CREATE TABLE IF NOT EXISTS research_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    research_id TEXT NOT NULL REFERENCES research_projects(research_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    document_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    request_id TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE(tenant_id,user_id,workspace_id,research_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_research_snapshots_owner
ON research_snapshots(tenant_id,user_id,workspace_id,research_id,captured_at);
"""

def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create missing tables and upgrade existing workspaces without dropping data."""
    conn.executescript(_SCHEMA_SQL)
    experiment_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(research_experiments)").fetchall()
    }
    if "hypothesis_revision" not in experiment_columns:
        conn.execute(
            "ALTER TABLE research_experiments "
            "ADD COLUMN hypothesis_revision INTEGER NOT NULL DEFAULT 1"
        )
    if "source_asset_id" not in experiment_columns:
        conn.execute("ALTER TABLE research_experiments ADD COLUMN source_asset_id TEXT NOT NULL DEFAULT ''")
    if "source_experiment_id" not in experiment_columns:
        conn.execute("ALTER TABLE research_experiments ADD COLUMN source_experiment_id TEXT NOT NULL DEFAULT ''")
    if "run_snapshot_json" not in experiment_columns:
        conn.execute("ALTER TABLE research_experiments ADD COLUMN run_snapshot_json TEXT NOT NULL DEFAULT '{}'")
    for name, default in (
        ("execution_mode", "shared_workspace"), ("execution_workdir", ""),
        ("execution_manifest_json", "{}"), ("input_artifacts_json", "[]"),
        ("output_artifacts_json", "[]"), ("metrics_json", "{}"),
        ("base_commit", ""), ("result_commit", ""),
    ):
        if name not in experiment_columns:
            conn.execute(f"ALTER TABLE research_experiments ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
    continuation_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(research_continuations)").fetchall()}
    for name in ("max_tokens", "max_tool_calls", "max_failures", "max_cost_micros", "tokens_used", "tool_calls_used", "failures_used", "cost_used_micros"):
        if name not in continuation_columns:
            conn.execute(f"ALTER TABLE research_continuations ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    for name in ("best_experiment_id", "final_summary"):
        if name not in continuation_columns:
            conn.execute(f"ALTER TABLE research_continuations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    # v19 makes the product rule durable: a Workspace can have only one
    # non-archived formal research project. Preserve older rows by archiving
    # every duplicate except the most recently updated project before adding
    # the partial unique index.
    duplicate_owners = conn.execute(
        """
        SELECT tenant_id,user_id,workspace_id
        FROM research_projects
        WHERE status != 'archived'
        GROUP BY tenant_id,user_id,workspace_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for owner in duplicate_owners:
        rows = conn.execute(
            """
            SELECT research_id
            FROM research_projects
            WHERE tenant_id=? AND user_id=? AND workspace_id=?
              AND status != 'archived'
            ORDER BY updated_at DESC,created_at DESC,research_id DESC
            """,
            tuple(owner),
        ).fetchall()
        for row in rows[1:]:
            conn.execute(
                """
                UPDATE research_projects
                SET status='archived',archived_at=CASE
                    WHEN archived_at='' THEN CURRENT_TIMESTAMP ELSE archived_at END
                WHERE research_id=?
                """,
                (row["research_id"],),
            )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_research_projects_one_current
        ON research_projects(tenant_id,user_id,workspace_id)
        WHERE status != 'archived'
        """
    )
    context_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(research_context_audits)"
        ).fetchall()
    }
    if "turn_id" not in context_columns:
        conn.execute(
            "ALTER TABLE research_context_audits "
            "ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
        )
    activity_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(research_activities)"
        ).fetchall()
    }
    if "artifact_groups_json" not in activity_columns:
        conn.execute(
            "ALTER TABLE research_activities "
            "ADD COLUMN artifact_groups_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "agent_calls_json" not in activity_columns:
        conn.execute(
            "ALTER TABLE research_activities "
            "ADD COLUMN agent_calls_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "workflow_calls_json" not in activity_columns:
        conn.execute(
            "ALTER TABLE research_activities "
            "ADD COLUMN workflow_calls_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "agent_runs_json" not in activity_columns:
        conn.execute(
            "ALTER TABLE research_activities "
            "ADD COLUMN agent_runs_json TEXT NOT NULL DEFAULT '[]'"
        )
    import_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(research_pack_imports)"
        ).fetchall()
    }
    if "asset_id" not in import_columns:
        conn.execute(
            "ALTER TABLE research_pack_imports "
            "ADD COLUMN asset_id TEXT NOT NULL DEFAULT ''"
        )
    if "hidden_at" not in import_columns:
        conn.execute("ALTER TABLE research_pack_imports ADD COLUMN hidden_at TEXT NOT NULL DEFAULT ''")
    scope_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(research_pack_scopes)"
        ).fetchall()
    }
    if "scope_type" not in scope_columns:
        conn.execute(
            "ALTER TABLE research_pack_scopes "
            "ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'project'"
        )
    asset_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='research_pack_assets'"
    ).fetchone()
    asset_sql = str(asset_sql_row["sql"] or "") if asset_sql_row else ""
    normalized_asset_sql = "".join(asset_sql.upper().split())
    if "UNIQUE(TENANT_ID,USER_ID,WORKSPACE_ID,PACK_ID)" in normalized_asset_sql:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            CREATE TABLE research_pack_assets_v13 (
                asset_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                pack_id TEXT NOT NULL,
                pack_schema_version TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                scope_mode TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                ended_at TEXT NOT NULL DEFAULT '',
                source_research_ids_json TEXT NOT NULL DEFAULT '[]',
                manifest_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                lineage_json TEXT NOT NULL DEFAULT '{}',
                source_digest TEXT NOT NULL,
                archive_sha256 TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE
            );

            INSERT INTO research_pack_assets_v13
            SELECT * FROM research_pack_assets;

            DROP TABLE research_pack_assets;
            ALTER TABLE research_pack_assets_v13
                RENAME TO research_pack_assets;

            CREATE INDEX idx_research_pack_assets_owner_status
                ON research_pack_assets(
                    tenant_id,user_id,workspace_id,status,imported_at
                );
            CREATE INDEX idx_research_pack_assets_owner_pack
                ON research_pack_assets(
                    tenant_id,user_id,workspace_id,pack_id,imported_at
                );
            """
        )
        conn.execute("PRAGMA foreign_keys=ON")
    export_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(research_pack_exports)"
        ).fetchall()
    }
    if "scope_mode" not in export_columns:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            ALTER TABLE research_pack_exports
                RENAME TO research_pack_exports_v11;

            CREATE TABLE research_pack_exports (
                pack_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                research_id TEXT NOT NULL DEFAULT '',
                scope_mode TEXT NOT NULL DEFAULT 'project',
                scope_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL DEFAULT '',
                ended_at TEXT NOT NULL DEFAULT '',
                source_research_ids_json TEXT NOT NULL DEFAULT '[]',
                schema_version TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                status TEXT NOT NULL,
                privacy_profile TEXT NOT NULL,
                attribution_mode TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                manifest_json TEXT NOT NULL DEFAULT '{}',
                privacy_report_json TEXT NOT NULL DEFAULT '{}',
                compatibility_report_json TEXT NOT NULL DEFAULT '{}',
                archive_name TEXT NOT NULL DEFAULT '',
                archive_path TEXT NOT NULL DEFAULT '',
                archive_sha256 TEXT NOT NULL DEFAULT '',
                archive_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL UNIQUE
            );

            INSERT INTO research_pack_exports(
                pack_id,tenant_id,user_id,workspace_id,research_id,
                scope_mode,scope_json,started_at,ended_at,
                source_research_ids_json,schema_version,generator_version,
                status,privacy_profile,attribution_mode,source_digest,
                manifest_json,privacy_report_json,compatibility_report_json,
                archive_name,archive_path,archive_sha256,archive_size,
                created_at,completed_at,error,idempotency_key
            )
            SELECT
                pack_id,tenant_id,user_id,workspace_id,research_id,
                'project','{}','','',json_array(research_id),
                schema_version,generator_version,status,privacy_profile,
                attribution_mode,source_digest,manifest_json,
                privacy_report_json,compatibility_report_json,archive_name,
                archive_path,archive_sha256,archive_size,created_at,
                completed_at,error,idempotency_key
            FROM research_pack_exports_v11;

            DROP TABLE research_pack_exports_v11;

            CREATE INDEX idx_research_pack_exports_owner_project
                ON research_pack_exports(
                    tenant_id,user_id,workspace_id,research_id,
                    status,created_at
                );
            CREATE INDEX idx_research_pack_exports_owner_scope
                ON research_pack_exports(
                    tenant_id,user_id,workspace_id,scope_mode,
                    started_at,ended_at,status,created_at
                );
            """
        )
        conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO research_schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
