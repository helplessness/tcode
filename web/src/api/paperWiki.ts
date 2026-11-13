export type WikiEvidenceLink = {
  claim_id?: string;
  evidence_id: string;
  relation: 'supports' | 'contradicts' | 'context_only' | string;
  support_strength?: number;
  note?: string;
  source_hash?: string;
  verified_at?: string;
};

export type WikiEntityType = { type_id: string; display_name: string; status: string; schema: { requested_by?: string; reviewed_by?: string; review_reason?: string } };
export type WikiNoteDraft = { content: string; base_revision: number; revision: number; updated_at: string };
export const getWikiNoteDraft = (pageId: string, workspaceId?: string) => requestJson<{ draft: WikiNoteDraft | null }>(`/api/paper-wiki/pages/${encodeURIComponent(pageId)}/note-draft?${workspaceQuery(workspaceId)}`);
export const saveWikiNoteDraft = (pageId: string, workspaceId: string, draft: Partial<WikiNoteDraft> & { expected_revision: number; delete?: boolean }) => requestJson<{ draft: WikiNoteDraft | null }>(`/api/paper-wiki/pages/${encodeURIComponent(pageId)}/note-draft`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...draft, workspace_id: workspaceId }) });
export const manageWikiEntityTypes = (workspaceId: string) => requestJson<{ types: WikiEntityType[] }>(`/api/paper-wiki/entity-types?${workspaceQuery(workspaceId)}&include_all=true`);
export const requestWikiEntityType = (workspaceId: string, type_id: string, display_name: string) => requestJson<{ types: WikiEntityType[] }>('/api/paper-wiki/entity-types', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace_id: workspaceId, type_id, display_name }) });
export const reviewWikiEntityType = (workspaceId: string, item: WikiEntityType, status: string, reason: string) => requestJson<{ types: WikiEntityType[] }>(`/api/paper-wiki/entity-types/${encodeURIComponent(item.type_id)}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace_id: workspaceId, expected_status: item.status, status, reason }) });

export type WikiEvidenceState = 'none' | 'complete' | 'partial' | 'missing';

export type WikiEvidenceResult = {
  claim?: WikiClaim;
  links?: WikiEvidenceLink[];
  evidence_ids?: string[];
  requested_evidence_ids: string[];
  missing_evidence_ids: string[];
  evidence_state: WikiEvidenceState;
  evidence: Array<Record<string, any>>;
};

export type WikiClaim = {
  claim_id: string;
  page_id?: string;
  statement: string;
  scope?: Record<string, unknown>;
  evidence_ids: string[];
  evidence_links?: WikiEvidenceLink[];
  support_status: string;
  confidence: number;
  origin: string;
};

export type WikiPage = {
  page_id: string;
  canonical_key?: string;
  page_type: string;
  title: string;
  slug?: string;
  summary: string;
  content: string;
  user_notes?: string;
  user_notes_revision?: number;
  user_notes_updated_by?: string;
  user_notes_updated_at?: string;
  status: string;
  revision: number;
  content_hash?: string;
  aliases: string[];
  source_doc_ids: string[];
  evidence_ids: string[];
  origin?: string;
  updated_by?: string;
  updated_at?: string;
  archived_at?: string | null;
  markdown_path?: string;
  claims?: WikiClaim[];
  matched_claims?: WikiClaim[];
  matched_chunks?: Array<{
    chunk_id: string;
    heading_path?: string;
    content: string;
    line_start: number;
    line_end: number;
    byte_start: number;
    byte_end: number;
    score: number;
  }>;
  history?: WikiRevision[];
};

export type WikiPageUserNotes = {
  page_id: string;
  user_notes: string;
  user_notes_revision: number;
  user_notes_updated_by: string;
  user_notes_updated_at: string;
};

export type WikiRevision = {
  revision_id: string;
  revision: number;
  parent_revision?: number;
  actor: string;
  reason: string;
  origin: string;
  created_at: string;
};

export type WikiOperation = {
  type: 'upsert_page' | 'archive_page' | 'restore_page' | string;
  page_id?: string;
  page?: Partial<WikiPage> & { claims?: WikiClaim[] };
  before?: Partial<WikiPage> & { claims?: WikiClaim[] };
};

export type WikiClaimChange = {
  change_id: string;
  claim_key: string;
  kind: 'added' | 'modified' | 'removed';
  before?: WikiClaim;
  after?: WikiClaim;
};

export type WikiReviewOperation = {
  operation_index: number;
  type: string;
  page_id: string;
  title: string;
  is_new: boolean;
  page_fields_changed: boolean;
  page_field_changes: Array<{ field: string; before: unknown; after: unknown }>;
  claim_changes: WikiClaimChange[];
};

export type WikiReviewSelection = {
  operations: Array<{
    operation_index: number;
    include_operation: boolean;
    include_page_fields: boolean;
    accepted_claim_change_ids: string[];
  }>;
};

export type WikiProposal = {
  proposal_id: string;
  status: string;
  trigger_kind: string;
  trigger_doc_ids: string[];
  base_revisions: Record<string, number>;
  operations?: WikiOperation[];
  operation_count?: number;
  validation: { valid?: boolean; errors?: string[]; warnings?: string[] };
  proposal_hash: string;
  compiler_version?: string;
  created_by?: string;
  reviewed_by?: string;
  review_comment?: string;
  created_at?: string;
  updated_at?: string;
  applied_at?: string;
  review_model?: { operations: WikiReviewOperation[]; change_count: number };
  review_selection?: WikiReviewSelection;
  applied_operations?: WikiOperation[];
  approval_hash?: string;
  parent_proposal_id?: string;
  superseded_by?: string;
};

export type WikiStats = {
  enabled: boolean;
  page_count: number;
  active_page_count: number;
  archived_page_count: number;
  claim_count: number;
  pending_proposal_count: number;
  active_job_count: number;
  page_types: Record<string, number>;
  fts_available: boolean;
  vector?: {
    enabled: boolean;
    available: boolean;
    dirty: boolean;
    min_pages: number;
    weight: number;
    state?: Record<string, any>;
  };
  features?: {
    regeneration_enabled: boolean;
    markdown_sync_enabled: boolean;
    markdown_sync_interval_seconds: number;
    vector_search_enabled: boolean;
  };
  access?: {
    mode: 'owner' | 'shared_read_only' | string;
    read_only: boolean;
    source_id?: string;
    owner_user_id?: string;
    workspace_id?: string;
  };
};

export type WikiSharedSource = {
  source_id: string;
  tenant_id: string;
  owner_user_id: string;
  workspace_id: string;
  permission: 'wiki_read' | string;
  read_only: true;
  page_ids: string[];
  page_types: string[];
  share_ids: string[];
  grant_count: number;
  owner: { user_id: string; name: string; email: string };
};

export type WikiShareMember = {
  user_id: string;
  email: string;
  name: string;
  role: string;
};

export type WikiShareCollection = {
  collection_id: string;
  tenant_id: string;
  owner_user_id: string;
  workspace_id: string;
  name: string;
  description: string;
  page_ids: string[];
  created_at: number;
  updated_at: number;
};

export type WikiShareGrant = {
  share_id: string;
  source_id: string;
  target_type: 'page' | 'entity_type' | 'collection';
  target_id: string;
  grantee_user_id: string;
  status: string;
  updated_at: number;
};

export type WikiShareContext = {
  tenant_id: string;
  owner_user_id: string;
  workspace_id: string;
  members: WikiShareMember[];
  collections: WikiShareCollection[];
  shares: WikiShareGrant[];
};

export type WikiJob = {
  job_id: string;
  job_type: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | string;
  progress: number;
  message: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

const workspaceQuery = (workspaceId?: string) => {
  const params = new URLSearchParams();
  if (String(workspaceId || '').trim()) params.set('workspace_id', String(workspaceId).trim());
  return params;
};

const wikiReadQuery = (workspaceId?: string, sourceId?: string) => {
  const params = workspaceQuery(workspaceId);
  if (String(sourceId || '').trim()) params.set('source_id', String(sourceId).trim());
  return params;
};

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(String(payload.detail || payload.message || `请求失败 (${response.status})`));
  }
  return payload as T;
}

export const getWikiStats = (workspaceId?: string, sourceId?: string) =>
  requestJson<WikiStats>(`/api/paper-wiki/stats?${wikiReadQuery(workspaceId, sourceId)}`);

export const listWikiEntityTypes = (workspaceId?: string, sourceId?: string) =>
  requestJson<{ count: number; types: Array<{ type_id: string; display_name: string }> }>(
    `/api/paper-wiki/entity-types?${wikiReadQuery(workspaceId, sourceId)}`,
  );

export async function listWikiPages(options: {
  workspaceId?: string;
  pageType?: string;
  status?: string;
  includeArchived?: boolean;
  limit?: number;
  offset?: number;
  sourceId?: string;
} = {}) {
  const params = wikiReadQuery(options.workspaceId, options.sourceId);
  if (options.pageType) params.set('page_type', options.pageType);
  if (options.status) params.set('status', options.status);
  if (options.includeArchived) params.set('include_archived', 'true');
  params.set('limit', String(options.limit || 100));
  params.set('offset', String(options.offset || 0));
  return requestJson<{ total: number; pages: WikiPage[] }>(`/api/paper-wiki/pages?${params}`);
}

export async function searchWikiPages(query: string, workspaceId?: string, includeArchived = false, sourceId?: string, status = '') {
  const params = wikiReadQuery(workspaceId, sourceId);
  params.set('q', query);
  params.set('limit', '50');
  if (status) params.set('status', status);
  if (includeArchived) params.set('include_archived', 'true');
  return requestJson<{
    query: string;
    total: number;
    hits: WikiPage[];
    search_mode?: 'hybrid' | 'fts' | 'recent' | string;
    vector?: Record<string, any>;
  }>(`/api/paper-wiki/search?${params}`);
}

export async function getWikiPage(pageId: string, workspaceId?: string, includeHistory = true, sourceId?: string) {
  const params = wikiReadQuery(workspaceId, sourceId);
  if (includeHistory) params.set('include_history', 'true');
  return requestJson<WikiPage>(`/api/paper-wiki/pages/${encodeURIComponent(pageId)}?${params}`);
}

export async function updateWikiPageUserNotes(
  page: WikiPage,
  userNotes: string,
  workspaceId?: string,
) {
  return requestJson<WikiPageUserNotes>(`/api/paper-wiki/pages/${encodeURIComponent(page.page_id)}/user-notes`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_notes: userNotes,
      expected_notes_revision: page.user_notes_revision || 0,
      workspace_id: workspaceId || '',
    }),
  });
}

async function pageAction(
  page: WikiPage,
  action: 'archive' | 'restore',
  workspaceId?: string,
) {
  return requestJson<WikiPage>(
    `/api/paper-wiki/pages/${encodeURIComponent(page.page_id)}/${action}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: page.revision, workspace_id: workspaceId || '' }),
    },
  );
}

export const archiveWikiPage = (page: WikiPage, workspaceId?: string) =>
  pageAction(page, 'archive', workspaceId);
export const restoreWikiPage = (page: WikiPage, workspaceId?: string) =>
  pageAction(page, 'restore', workspaceId);

export type WikiPagePurgeResult = {
  status: 'purged' | string;
  page_id: string;
  deleted: {
    claims?: number;
    evidence_links?: number;
    user_notes?: number;
    revisions?: number;
    dependencies?: number;
    redacted_proposals?: number;
  };
  removed_file_groups?: number;
  cleanup_warnings?: string[];
  source_records_preserved?: boolean;
  share_cleanup?: {
    revoked_direct_shares?: number;
    revoked_collection_shares?: number;
    updated_collections?: number;
    deleted_empty_collections?: number;
  };
  vector_job_id?: string;
};

export async function purgeWikiPage(
  page: WikiPage,
  confirmation: string,
  workspaceId?: string,
) {
  return requestJson<WikiPagePurgeResult>(
    `/api/paper-wiki/pages/${encodeURIComponent(page.page_id)}`,
    {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision: page.revision,
        confirmation,
        workspace_id: workspaceId || '',
      }),
    },
  );
}

export async function rollbackWikiPage(pageId: string, revision: number, workspaceId?: string) {
  return requestJson<WikiPage>(`/api/paper-wiki/pages/${encodeURIComponent(pageId)}/rollback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision, workspace_id: workspaceId || '' }),
  });
}

export async function getClaimEvidence(claimId: string, workspaceId?: string, sourceId?: string) {
  const params = wikiReadQuery(workspaceId, sourceId);
  return requestJson<WikiEvidenceResult & { claim: WikiClaim; links: WikiEvidenceLink[] }>(
    `/api/paper-wiki/claims/${encodeURIComponent(claimId)}/evidence?${params}`,
  );
}

export async function getWikiEvidence(evidenceIds: string[], workspaceId?: string, sourceId?: string) {
  return requestJson<WikiEvidenceResult & { evidence_ids: string[] }>(
    '/api/paper-wiki/evidence',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ evidence_ids: evidenceIds, workspace_id: workspaceId || '', source_id: sourceId || '' }),
    },
  );
}

export async function listWikiProposals(workspaceId?: string, status = '', sourceId?: string) {
  const params = wikiReadQuery(workspaceId, sourceId);
  if (status) params.set('status', status);
  params.set('limit', '100');
  return requestJson<{ count: number; proposals: WikiProposal[] }>(`/api/paper-wiki/proposals?${params}`);
}

export async function getWikiProposal(proposalId: string, workspaceId?: string, sourceId?: string) {
  const params = wikiReadQuery(workspaceId, sourceId);
  return requestJson<WikiProposal>(
    `/api/paper-wiki/proposals/${encodeURIComponent(proposalId)}?${params}`,
  );
}

export async function listWikiJobs(workspaceId?: string, status = '') {
  const params = workspaceQuery(workspaceId);
  if (status) params.set('status', status);
  params.set('limit', '30');
  return requestJson<{ count: number; jobs: WikiJob[] }>(`/api/paper-wiki/jobs?${params}`);
}

export async function approveWikiProposal(
  proposal: WikiProposal,
  workspaceId?: string,
  comment = '',
  selection?: WikiReviewSelection,
) {
  return requestJson<WikiProposal>(
    `/api/paper-wiki/proposals/${encodeURIComponent(proposal.proposal_id)}/approve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        proposal_hash: proposal.proposal_hash,
        comment,
        selection,
        workspace_id: workspaceId || '',
      }),
    },
  );
}

export async function regenerateWikiProposal(
  proposal: WikiProposal,
  instructions: string,
  workspaceId?: string,
) {
  return requestJson<{ status: string; job: WikiJob; proposal_id: string }>(
    `/api/paper-wiki/proposals/${encodeURIComponent(proposal.proposal_id)}/regenerate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions, workspace_id: workspaceId || '' }),
    },
  );
}

export async function reconcileWikiMarkdown(
  workspaceId?: string,
  options: { force?: boolean; dryRun?: boolean; maxPages?: number } = {},
) {
  return requestJson<{
    status: string;
    changed_count: number;
    changed_pages: Array<Record<string, any>>;
    rejected_count: number;
    rejected_pages: Array<Record<string, any>>;
    issues: Array<Record<string, any>>;
    scanned_count: number;
  }>('/api/paper-wiki/reconcile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workspace_id: workspaceId || '',
      force: Boolean(options.force),
      dry_run: Boolean(options.dryRun),
      max_pages: options.maxPages || 2000,
    }),
  });
}

export async function rebuildWikiVectorIndex(workspaceId?: string, force = false) {
  return requestJson<{ status: string; job: Partial<WikiJob> & { status?: string } }>('/api/paper-wiki/vector/rebuild', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workspace_id: workspaceId || '', force }),
  });
}

export async function rejectWikiProposal(proposal: WikiProposal, reason: string, workspaceId?: string) {
  return requestJson<WikiProposal>(
    `/api/paper-wiki/proposals/${encodeURIComponent(proposal.proposal_id)}/reject`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason, workspace_id: workspaceId || '' }),
    },
  );
}

export async function runWikiLint(workspaceId?: string) {
  return requestJson<{ status: string; issue_count: number; issues: Array<Record<string, any>> }>(
    '/api/paper-wiki/lint',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workspace_id: workspaceId || '' }),
    },
  );
}

export const listWikiSharedSources = () =>
  requestJson<{ sources: WikiSharedSource[] }>('/api/paper-wiki/shared-sources');

export const getWikiShareContext = (workspaceId?: string) =>
  requestJson<WikiShareContext>(`/api/paper-wiki/share-context?${workspaceQuery(workspaceId)}`);

export async function grantWikiShare(options: {
  workspaceId?: string;
  targetType: WikiShareGrant['target_type'];
  targetId: string;
  granteeUserIds: string[];
}) {
  return requestJson<{ shares: WikiShareGrant[]; count: number }>('/api/paper-wiki/shares', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workspace_id: options.workspaceId || '',
      target_type: options.targetType,
      target_id: options.targetId,
      grantee_user_ids: options.granteeUserIds,
    }),
  });
}

export const revokeWikiShare = (shareId: string, workspaceId?: string) =>
  requestJson<{ status: string }>(
    `/api/paper-wiki/shares/${encodeURIComponent(shareId)}?${workspaceQuery(workspaceId)}`,
    { method: 'DELETE' },
  );

export async function createWikiShareCollection(options: {
  workspaceId?: string;
  name: string;
  description?: string;
  pageIds: string[];
}) {
  return requestJson<{ collection: WikiShareCollection }>('/api/paper-wiki/share-collections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workspace_id: options.workspaceId || '',
      name: options.name,
      description: options.description || '',
      page_ids: options.pageIds,
    }),
  });
}

export const deleteWikiShareCollection = (collectionId: string, workspaceId?: string) =>
  requestJson<{ status: string }>(
    `/api/paper-wiki/share-collections/${encodeURIComponent(collectionId)}?${workspaceQuery(workspaceId)}`,
    { method: 'DELETE' },
  );
