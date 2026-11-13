import { confirmAction } from './FeedbackDialog';
import { installWikiNavigationGuard } from '../utils/wikiNavigationGuard';
import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, onMount } from 'solid-js';
import TabIndicator from './TabIndicator';
import { Portal } from 'solid-js/web';
import {
  approveWikiProposal,
  archiveWikiPage,
  getClaimEvidence,
  getWikiProposal,
  getWikiEvidence,
  getWikiPage,
  getWikiStats,
  listWikiSharedSources,
  listWikiPages,
  listWikiJobs,
  listWikiEntityTypes,
  listWikiProposals,
  purgeWikiPage,
  rebuildWikiVectorIndex,
  reconcileWikiMarkdown,
  regenerateWikiProposal,
  rejectWikiProposal,
  restoreWikiPage,
  rollbackWikiPage,
  runWikiLint,
  searchWikiPages,
  updateWikiPageUserNotes,
  type WikiClaim,
  type WikiEvidenceLink,
  type WikiEvidenceState,
  type WikiJob,
  type WikiPage,
  type WikiProposal,
  type WikiStats,
  type WikiReviewSelection,
  type WikiSharedSource,
  type WikiShareGrant,
} from '../api/paperWiki';
import {
  evidenceKindLabel,
  shortDisplayPath,
  readableSourceLabel,
} from '../utils/evidenceDisplay';
import { fuzzyHighlightSegments, fuzzyMatchesFields } from '../utils/fuzzySearch';
import { dismissDetailsOnOutsideClick } from '../utils/dismissDetails';
import {
  WIKI_CLAIM_EVIDENCE_PREFIX,
  buildWikiAgentDisplayContent,
} from '../utils/wikiEvidenceDisplay';
import MarkdownContent from './MarkdownContent';
import MarkdownEditor from './MarkdownEditor';
import { ZoomableImage } from './ImageLightbox';
import WikiSharePanel from './WikiSharePanel';
import WikiEntityTypes from './WikiEntityTypes';
import { getWikiNoteDraft, saveWikiNoteDraft } from '../api/paperWiki';

type Props = {
  workspaceId?: string;
  active?: boolean;
};

type ViewMode = 'pages' | 'review' | 'types';
type PageStatusFilter = 'active' | 'archived' | 'all';

type EvidencePanelData = {
  claim?: WikiClaim;
  links: WikiEvidenceLink[];
  evidence: Array<Record<string, any>>;
  requested_evidence_ids: string[];
  missing_evidence_ids: string[];
  evidence_state: WikiEvidenceState;
};

const emptyEvidencePanelData = (claim?: WikiClaim): EvidencePanelData => ({
  claim,
  links: [],
  evidence: [],
  requested_evidence_ids: [],
  missing_evidence_ids: [],
  evidence_state: 'none',
});

const CLAIMS_PER_PAGE = 10;

const emptyStats: WikiStats = {
  enabled: true,
  page_count: 0,
  active_page_count: 0,
  archived_page_count: 0,
  claim_count: 0,
  pending_proposal_count: 0,
  active_job_count: 0,
  page_types: {},
  fts_available: false,
};

const statusLabel = (status: string) => ({
  canonical: '正式',
  draft: '草稿',
  pending_review: '待审核',
  stale: '待更新',
  conflicting: '有冲突',
  archived: '已归档',
  rebuilding: '重建中',
  applied: '已应用',
  rejected: '已驳回',
  superseded: '已被替代',
}[status] || status || '未知');

const statusClass = (status: string) => {
  if (status === 'canonical' || status === 'applied') return 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]';
  if (status === 'pending_review' || status === 'stale') return 'border-[#f1a02b]/30 bg-[#f1a02b]/10 text-[#ffc176]';
  if (status === 'conflicting' || status === 'rejected') return 'border-[#da3633]/30 bg-[#da3633]/10 text-[#ffb6b0]';
  return 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]';
};

const operationLabel = (operation: any) => {
  if (operation.type === 'archive_page') return '归档页面';
  if (operation.type === 'restore_page') return '恢复页面';
  if (operation.type === 'purged_page') return '页面已永久删除';
  return operation.before ? '更新页面' : '新增页面';
};

const evidenceLinksFor = (claim: Partial<WikiClaim>) => (
  (claim.evidence_links && claim.evidence_links.length > 0)
    ? claim.evidence_links
    : (claim.evidence_ids || []).map((evidence_id) => ({ evidence_id, relation: 'supports', support_strength: 1 }))
);

const relationLabel = (relation: string) => ({
  supports: '支持',
  contradicts: '反驳',
  context_only: '仅上下文',
}[relation] || relation || '支持');

const relationSummary = (claim: Partial<WikiClaim>) => {
  const counts = evidenceLinksFor(claim).reduce<Record<string, number>>((result, link) => {
    result[link.relation] = (result[link.relation] || 0) + 1;
    return result;
  }, {});
  return [
    counts.supports ? `支持 ${counts.supports}` : '',
    counts.contradicts ? `反驳 ${counts.contradicts}` : '',
    counts.context_only ? `上下文 ${counts.context_only}` : '',
  ].filter(Boolean).join(' · ');
};

const HighlightedText = (props: { text: unknown; query: string; class?: string }) => (
  <span class={props.class}>
    <For each={fuzzyHighlightSegments(props.text, props.query)}>{(segment) => (
      <Show
        when={segment.highlighted}
        fallback={segment.text}
      >
        <mark class="rounded-sm bg-[#f1a02b]/35 px-0.5 text-[#fff1c2]">{segment.text}</mark>
      </Show>
    )}</For>
  </span>
);

export default function PaperWikiWorkspace(props: Props) {
  const desktopSurface = () => document.documentElement.dataset.desktop === 'true';
  const [directoryHidden, setDirectoryHidden] = createSignal(false);
  const [viewMode, setViewMode] = createSignal<ViewMode>('pages');
  const [stats, setStats] = createSignal<WikiStats>(emptyStats);
  const [pages, setPages] = createSignal<WikiPage[]>([]);
  const [proposals, setProposals] = createSignal<WikiProposal[]>([]);
  const [jobs, setJobs] = createSignal<WikiJob[]>([]);
  const [entityTypes, setEntityTypes] = createSignal<Array<{ type_id: string; display_name: string }>>([]);
  const [selectedPage, setSelectedPage] = createSignal<WikiPage | null>(null);
  const [selectedProposal, setSelectedProposal] = createSignal<WikiProposal | null>(null);
  const [query, setQuery] = createSignal('');
  const [pageType, setPageType] = createSignal('');
  const [pageStatusFilter, setPageStatusFilter] = createSignal<PageStatusFilter>('active');
  const [loading, setLoading] = createSignal(false);
  const [saving, setSaving] = createSignal(false);
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [editing, setEditing] = createSignal(false);
  const [editContent, setEditContent] = createSignal('');
  const [draftStatus, setDraftStatus] = createSignal('');
  const [draftRevision, setDraftRevision] = createSignal(0);
  const [draftBase, setDraftBase] = createSignal(0);
  const [draftContent, setDraftContent] = createSignal('');
  let draftQueue: Promise<unknown> = Promise.resolve();
  const dirtyNotes = () => isActive() && viewMode() === 'pages' && editing() && editContent() !== (selectedPage()?.user_notes || '');
  const flushDraft = () => {
    const page = selectedPage(); const content = editContent(); const ws = workspaceId(); const base = draftBase();
    if (!page || !editing() || content === draftContent()) return draftQueue;
    setDraftStatus('正在保存草稿…');
    draftQueue = draftQueue.catch(() => {}).then(async () => {
      const result = await saveWikiNoteDraft(page.page_id, ws, { content, base_revision: base, expected_revision: draftRevision() });
      setDraftRevision(result.draft?.revision || 0); setDraftContent(content); setDraftStatus('草稿已自动保存；尚未发布');
    }).catch(e => { setDraftStatus(`草稿保存失败：${e instanceof Error ? e.message : '请重试'}`); throw e; });
    return draftQueue;
  };
  createEffect(() => {
    if (!editing()) return;
    editContent();
    const timer = window.setTimeout(() => { void flushDraft().catch(() => {}); }, 800);
    onCleanup(() => window.clearTimeout(timer));
  });
  onMount(() => {
    const leave = (e: BeforeUnloadEvent) => { if (dirtyNotes()) { e.preventDefault(); e.returnValue = ''; } };
    const stopNavigationGuard=installWikiNavigationGuard(dirtyNotes,flushDraft);
    window.addEventListener('beforeunload', leave);
    onCleanup(() => { window.removeEventListener('beforeunload', leave); stopNavigationGuard(); });
  });
  const [evidenceOpen, setEvidenceOpen] = createSignal(false);
  const [evidenceLoading, setEvidenceLoading] = createSignal(false);
  const [evidenceData, setEvidenceData] = createSignal<EvidencePanelData>(emptyEvidencePanelData());
  const [rejectReason, setRejectReason] = createSignal('');
  const [reviewComment, setReviewComment] = createSignal('');
  const [lintReport, setLintReport] = createSignal<any>(null);
  const [selectedChangeIds, setSelectedChangeIds] = createSignal<Set<string>>(new Set());
  const [selectedPageFields, setSelectedPageFields] = createSignal<Set<number>>(new Set());
  const [selectedOperations, setSelectedOperations] = createSignal<Set<number>>(new Set());
  const [regenerationInstructions, setRegenerationInstructions] = createSignal('');
  const [searchMode, setSearchMode] = createSignal('');
  const [markdownSyncing, setMarkdownSyncing] = createSignal(false);
  const [sharedSources, setSharedSources] = createSignal<WikiSharedSource[]>([]);
  const [sourceId, setSourceId] = createSignal('');
  const [shareOpen, setShareOpen] = createSignal(false);
  const [shareTarget, setShareTarget] = createSignal<{ type: WikiShareGrant['target_type']; id: string; label?: string } | null>(null);
  const [claimQuery, setClaimQuery] = createSignal('');
  const [claimPage, setClaimPage] = createSignal(1);
  const [purgeTarget, setPurgeTarget] = createSignal<WikiPage | null>(null);
  const [purgeConfirmation, setPurgeConfirmation] = createSignal('');
  const [agentPrompt, setAgentPrompt] = createSignal('');

  const isActive = () => props.active ?? true;
  const workspaceId = () => String(props.workspaceId || '').trim();
  const isReadOnly = createMemo(() => Boolean(sourceId()) || stats().access?.read_only === true);
  const selectedSource = createMemo(() => sharedSources().find((item) => item.source_id === sourceId()));
  const pageTypes = createMemo(() => Object.entries(stats().page_types || {}).sort((a, b) => a[0].localeCompare(b[0])));
  const pendingProposals = createMemo(() => proposals().filter((item) => item.status === 'pending_review'));
  const reviewProposals = createMemo(() => isReadOnly() ? proposals() : pendingProposals());
  const visibleJobs = createMemo(() => jobs().filter((item) => item.status === 'queued' || item.status === 'running' || item.status === 'failed').slice(0, 5));
  const activeJobs = createMemo(() => jobs().some((item) => item.status === 'queued' || item.status === 'running'));
  const vectorReady = createMemo(() => stats().vector?.state?.status === 'ready' && !stats().vector?.dirty);
  const vectorLabel = createMemo(() => {
    if (!stats().vector?.available || stats().vector?.state?.status === 'below_threshold') return '仅 FTS';
    return vectorReady() ? '已就绪' : '待更新';
  });
  const selectedClaims = createMemo(() => selectedPage()?.claims || []);
  const selectedAgentContent = createMemo(() => buildWikiAgentDisplayContent(
    selectedPage()?.content || '',
    selectedClaims(),
  ));
  const filteredClaims = createMemo(() => selectedClaims().filter((claim) => fuzzyMatchesFields([
    claim.statement,
    claim.support_status,
    relationSummary(claim),
    ...(claim.evidence_ids || []),
  ], claimQuery())));
  const claimPageCount = createMemo(() => Math.max(1, Math.ceil(filteredClaims().length / CLAIMS_PER_PAGE)));
  const pagedClaims = createMemo(() => {
    const start = (claimPage() - 1) * CLAIMS_PER_PAGE;
    return filteredClaims().slice(start, start + CLAIMS_PER_PAGE);
  });

  createEffect(() => {
    const lastPage = claimPageCount();
    if (claimPage() > lastPage) setClaimPage(lastPage);
  });

  const evidenceImageUrl = (evidence: any, image: any) => {
    const params = new URLSearchParams();
    if (workspaceId()) params.set('workspace_id', workspaceId());
    if (sourceId()) params.set('source_id', sourceId());
    const query = params.toString();
    return `/api/paper-wiki/evidence/${encodeURIComponent(String(evidence.evidence_id || ''))}/images/${Number(image.index)}${query ? `?${query}` : ''}`;
  };

  const evidenceLocation = (evidence: any) => {
    if (typeof evidence.locator === 'string' && evidence.locator.trim()) return readableSourceLabel(evidence.locator, '');
    const citation = (evidence.citations || []).find((item: any) => item && typeof item === 'object') || {};
    const chunk = evidence.metadata?.paper_chunk || {};
    const anchor = evidence.metadata?.paper_anchor || {};
    const page = citation.page || citation.page_number || chunk.page || anchor.page;
    const section = citation.section || citation.heading || chunk.heading || anchor.heading;
    const table = citation.table || citation.table_id || chunk.table_id || anchor.table_id;
    return [page ? `第 ${page} 页` : '', section ? `章节：${readableSourceLabel(section, '未标注')}` : '', table ? `表格：${readableSourceLabel(table, '未标注')}` : ''].filter(Boolean).join(' · ');
  };

  const evidenceRows = (evidence: any) => [
    ['来源文件', evidence.source_path ? readableSourceLabel(shortDisplayPath(evidence.source_path), '来源文件') : ''],
    ['原文位置', evidenceLocation(evidence)],
  ].filter(([, value]) => String(value || '').trim());

  const loadStats = async () => {
    const result = await getWikiStats(workspaceId(), sourceId());
    setStats(result);
    return result;
  };

  const loadEntityTypes = async () => {
    const result = await listWikiEntityTypes(workspaceId(), sourceId());
    setEntityTypes(result.types || []);
  };

  const loadPages = async (preserveSelection = true) => {
    const statusFilter = pageStatusFilter();
    const includeArchived = statusFilter !== 'active';
    const status = statusFilter === 'archived' ? 'archived' : '';
    const result = query().trim()
      ? await searchWikiPages(query().trim(), workspaceId(), includeArchived, sourceId(), status)
      : await listWikiPages({
          workspaceId: workspaceId(),
          pageType: pageType(),
          status,
          includeArchived,
          limit: 200,
          sourceId: sourceId(),
        });
    const nextPages = 'hits' in result ? result.hits : result.pages;
    setSearchMode('hits' in result ? String(result.search_mode || 'fts') : '');
    setPages(pageType() && query().trim() ? nextPages.filter((item) => item.page_type === pageType()) : nextPages);
    const currentId = preserveSelection ? selectedPage()?.page_id : '';
    if (currentId && nextPages.some((item) => item.page_id === currentId)) {
      await selectPage(currentId);
    } else {
      setSelectedPage(null);
      setEditing(false);
    }
  };

  const loadProposals = async () => {
    const result = await listWikiProposals(workspaceId(), '', sourceId());
    setProposals(result.proposals || []);
    const currentId = selectedProposal()?.proposal_id;
    if (currentId) {
      const current = result.proposals.find((item) => item.proposal_id === currentId);
      if (!current) {
        setSelectedProposal(null);
      } else if (!isReadOnly() && current.status !== 'pending_review') {
        const replacement = result.proposals.find(
          (item) => item.proposal_id === current.superseded_by && item.status === 'pending_review',
        );
        if (replacement) await selectProposal(replacement);
        else setSelectedProposal(null);
      }
    }
  };

  const loadJobs = async () => {
    if (isReadOnly()) {
      setJobs([]);
      return;
    }
    const result = await listWikiJobs(workspaceId());
    setJobs(result.jobs || []);
  };

  const loadSharedSources = async () => {
    const result = await listWikiSharedSources();
    setSharedSources(result.sources || []);
    if (sourceId() && !(result.sources || []).some((item) => item.source_id === sourceId())) {
      setSourceId('');
      setNotice('该共享来源已撤销或不可用，已切回我的 Wiki。');
    }
    return result.sources || [];
  };

  const refreshAll = async () => {
    if (!isActive()) return;
    setLoading(true);
    setError('');
    try {
      await loadSharedSources();
      const currentStats = await loadStats();
      if (!currentStats.enabled) {
        setPages([]);
        setProposals([]);
        setJobs([]);
        setSelectedPage(null);
        setSelectedProposal(null);
        return;
      }
      await Promise.all([loadEntityTypes(), loadPages(false), loadProposals(), loadJobs()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载文献 Wiki 失败');
    } finally {
      setLoading(false);
    }
  };

  async function syncMarkdown(silent = false) {
    if (isReadOnly() || markdownSyncing() || !stats().features?.markdown_sync_enabled) return;
    setMarkdownSyncing(true);
    try {
      const result = await reconcileWikiMarkdown(workspaceId());
      if (result.rejected_count > 0) {
        setNotice(`检测到 ${result.rejected_count} 个机器 Markdown 外部修改，已恢复为当前审核版本；用户笔记不受影响。`);
      } else if (!silent) {
        setNotice(`Markdown 校验完成，扫描 ${result.scanned_count} 个页面，机器内容保持一致。`);
      }
      const unexpectedIssues = (result.issues || []).filter((item) => item.code !== 'machine_markdown_edit_rejected');
      if (!silent && unexpectedIssues.length) {
        setError(`Markdown 校验发现 ${unexpectedIssues.length} 个问题，请查看服务端日志或故障排查文档。`);
      }
    } catch (e) {
      if (!silent) setError(e instanceof Error ? e.message : 'Markdown 校验失败');
    } finally {
      setMarkdownSyncing(false);
    }
  }

  const rebuildVectorIndex = async () => {
    setLoading(true);
    setError('');
    try {
      // force 只要求重新同步；不等于全量重新嵌入。
      const result = await rebuildWikiVectorIndex(workspaceId(), true);
      setNotice(result.job?.job_id
        ? `向量索引增量更新任务已提交：${result.job.job_id}`
        : `未创建向量任务：${result.status === 'disabled' ? '功能已关闭，继续使用 FTS5' : 'FAISS 不可用，继续使用 FTS5'}`);
      await Promise.all([loadJobs(), loadStats()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '提交向量索引重建失败');
    } finally {
      setLoading(false);
    }
  };

  createEffect(on(() => [workspaceId(), sourceId(), isActive()] as const, ([, , active]) => {
    if (active) void refreshAll();
  }));

  createEffect(() => {
    if (!isActive() || isReadOnly() || !stats().features?.markdown_sync_enabled) return;
    const intervalSeconds = Math.max(2, stats().features?.markdown_sync_interval_seconds || 5);
    const timer = window.setInterval(() => void syncMarkdown(true), intervalSeconds * 1000);
    onCleanup(() => window.clearInterval(timer));
  });

  createEffect(() => {
    if (!isActive() || !activeJobs()) return;
    const timer = window.setInterval(async () => {
      try {
        await Promise.all([loadJobs(), loadStats(), loadProposals()]);
      } catch {
        // The normal refresh/error surface will report persistent failures.
      }
    }, 2500);
    onCleanup(() => window.clearInterval(timer));
  });

  const selectPage = async (pageId: string) => {
    setError('');
    try {
      const page = await getWikiPage(pageId, workspaceId(), true, sourceId());
      setSelectedPage(page);
      setClaimQuery('');
      setClaimPage(1);
      setEditing(false);
      setEditContent(page.user_notes || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载 Wiki 页面失败');
    }
  };

  const startEdit = async () => {
    if (isReadOnly()) return;
    const page = selectedPage();
    if (!page) return;
    try {
      await draftQueue.catch(() => {});
      const { draft } = await getWikiNoteDraft(page.page_id, workspaceId());
      const content = draft?.content ?? page.user_notes ?? '';
      setDraftRevision(draft?.revision || 0); setDraftBase(draft?.base_revision ?? page.user_notes_revision ?? 0);
      setDraftContent(content); setEditContent(content); setEditing(true);
      setDraftStatus(draft ? (draft.base_revision !== (page.user_notes_revision || 0) ? '已恢复草稿；正式笔记版本已变化，请核对后重新编辑' : '已恢复自动保存的草稿') : '编辑后自动保存草稿');
    } catch (e) { setError(e instanceof Error ? e.message : '读取草稿失败'); }
  };

  const closeNotes = async () => {
    if (dirtyNotes() && !await confirmAction('笔记尚未发布。保存草稿并关闭编辑器吗？')) return;
    setSaving(true);
    try { await flushDraft(); setEditing(false); }
    catch (e) { setError(e instanceof Error ? e.message : '草稿保存失败，请重试'); }
    finally { setSaving(false); }
  };
  const discardDraft = async () => {
    const page = selectedPage();
    if (!page || !await confirmAction('丢弃草稿并恢复当前正式笔记？')) return;
    setSaving(true);
    try {
      await draftQueue.catch(() => {});
      await saveWikiNoteDraft(page.page_id, workspaceId(), { expected_revision: draftRevision(), delete: true });
      setDraftRevision(0); setDraftBase(page.user_notes_revision || 0);
      setDraftContent(page.user_notes || ''); setEditContent(page.user_notes || '');
      setDraftStatus('草稿已丢弃，已恢复正式笔记');
    } catch (e) { setError(e instanceof Error ? e.message : '丢弃草稿失败'); }
    finally { setSaving(false); }
  };

  const savePage = async () => {
    if (isReadOnly()) return;
    const current = selectedPage();
    if (!current) return;
    setSaving(true);
    setError('');
    try {
      await flushDraft();
      const notes = await updateWikiPageUserNotes({ ...current, user_notes_revision: draftBase() }, editContent(), workspaceId());
      setSelectedPage({ ...current, ...notes });
      setEditing(false);
      setNotice(`用户笔记已保存（笔记版本 ${notes.user_notes_revision}）；Wiki 事实与 Evidence 未被修改。`);
      try { await saveWikiNoteDraft(current.page_id, workspaceId(), { expected_revision: draftRevision(), delete: true }); }
      catch { setNotice('用户笔记已保存；旧草稿清理失败，下次打开时请核对并丢弃旧草稿。'); }
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存用户笔记失败');
    } finally {
      setSaving(false);
    }
  };

  const toggleArchive = async () => {
    if (isReadOnly()) return;
    const page = selectedPage();
    if (!page) return;
    setSaving(true);
    setError('');
    try {
      const updated = page.status === 'archived'
        ? await restoreWikiPage(page, workspaceId())
        : await archiveWikiPage(page, workspaceId());
      setSelectedPage(updated);
      setNotice(updated.status === 'archived' ? '页面已软删除，可以随时恢复。' : '页面已恢复。');
      await Promise.all([loadStats(), loadPages(true)]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '更新页面状态失败');
    } finally {
      setSaving(false);
    }
  };

  const openPermanentDelete = () => {
    if (isReadOnly()) return;
    const page = selectedPage();
    if (!page || page.status !== 'archived') return;
    setPurgeTarget(page);
    setPurgeConfirmation('');
  };

  const permanentlyDeletePage = async () => {
    if (isReadOnly()) return;
    const page = purgeTarget();
    if (!page || page.status !== 'archived') return;
    const confirmation = purgeConfirmation().trim();
    if (confirmation !== 'yes') return;
    setSaving(true);
    setError('');
    try {
      const result = await purgeWikiPage(page, confirmation, workspaceId());
      setSelectedPage(null);
      setEditing(false);
      setEvidenceOpen(false);
      setPurgeTarget(null);
      setPurgeConfirmation('');
      const deletedClaims = Number(result.deleted?.claims || 0);
      const revokedShares = Number(result.share_cleanup?.revoked_direct_shares || 0)
        + Number(result.share_cleanup?.revoked_collection_shares || 0);
      const warning = (result.cleanup_warnings || []).length
        ? `；另有 ${result.cleanup_warnings?.length} 项文件清理警告，请查看服务端日志`
        : '';
      setNotice(`Wiki 页面已永久删除（清理 ${deletedClaims} 条 Claim、撤销 ${revokedShares} 条共享授权）${warning}。论文原文和 EvidenceStore 未删除。`);
      await Promise.all([loadStats(), loadPages(false), loadProposals(), loadJobs()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '永久删除 Wiki 页面失败');
    } finally {
      setSaving(false);
    }
  };

  const rollback = async (revision: number) => {
    if (isReadOnly()) return;
    const page = selectedPage();
    if (!page || revision === page.revision) return;
    if (!await confirmAction(`确认将页面内容恢复到 revision ${revision}？当前版本仍会保留在历史中。`)) return;
    setSaving(true);
    try {
      const updated = await rollbackWikiPage(page.page_id, revision, workspaceId());
      setSelectedPage(updated);
      setNotice(`已基于 revision ${revision} 创建新的回滚版本 revision ${updated.revision}。`);
      await loadPages(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : '回滚失败');
    } finally {
      setSaving(false);
    }
  };

  const openEvidence = async (claim: WikiClaim) => {
    setEvidenceOpen(true);
    setEvidenceLoading(true);
    setEvidenceData(emptyEvidencePanelData(claim));
    try {
      setEvidenceData(await getClaimEvidence(claim.claim_id, workspaceId(), sourceId()));
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载原始证据失败');
    } finally {
      setEvidenceLoading(false);
    }
  };

  const openAgentContentEvidence = (markerId: string) => {
    const normalizedId = String(markerId || '').trim();
    const claim = normalizedId.startsWith(WIKI_CLAIM_EVIDENCE_PREFIX)
      ? selectedClaims().find(
        (item) => item.claim_id === normalizedId.slice(WIKI_CLAIM_EVIDENCE_PREFIX.length),
      )
      : selectedClaims().find((item) => (item.evidence_ids || []).includes(normalizedId));
    if (claim) void openEvidence(claim);
  };

  const openProposalEvidence = async (claim: WikiClaim) => {
    setEvidenceOpen(true);
    setEvidenceLoading(true);
    setEvidenceData(emptyEvidencePanelData(claim));
    try {
      const result = await getWikiEvidence(claim.evidence_ids || [], workspaceId(), sourceId());
      const links = evidenceLinksFor(claim);
      const linkById = new Map(links.map((link) => [link.evidence_id, link]));
      setEvidenceData({
        claim,
        links,
        requested_evidence_ids: result.requested_evidence_ids || result.evidence_ids || [],
        missing_evidence_ids: result.missing_evidence_ids || [],
        evidence_state: result.evidence_state || 'none',
        evidence: (result.evidence || []).map((record) => ({
          ...record,
          wiki_relation: linkById.get(String(record.evidence_id || ''))?.relation || 'supports',
        })),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载 Proposal 原始证据失败');
    } finally {
      setEvidenceLoading(false);
    }
  };

  const selectProposal = async (proposal: WikiProposal) => {
    setError('');
    setReviewComment('');
    setRejectReason('');
    setRegenerationInstructions('');
    try {
      const detail = await getWikiProposal(proposal.proposal_id, workspaceId(), sourceId());
      setSelectedProposal(detail);
      const reviewOperations = detail.review_model?.operations || [];
      setSelectedChangeIds(new Set(
        reviewOperations.flatMap((item) => item.claim_changes.map((change) => change.change_id)),
      ));
      setSelectedPageFields(new Set(
        reviewOperations
          .filter((item) => !item.is_new && item.page_fields_changed)
          .map((item) => item.operation_index),
      ));
      setSelectedOperations(new Set(reviewOperations.map((item) => item.operation_index)));
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载 Wiki Proposal 失败');
    }
  };

  const reviewOperation = (operationIndex: number) => (
    selectedProposal()?.review_model?.operations.find((item) => item.operation_index === operationIndex)
  );

  const toggleSetValue = (
    setter: (value: Set<any> | ((previous: Set<any>) => Set<any>)) => void,
    value: string | number,
    checked: boolean,
  ) => {
    setter((previous: Set<any>) => {
      const next = new Set(previous);
      if (checked) next.add(value);
      else next.delete(value);
      return next;
    });
  };

  const toggleReviewOperation = (operationIndex: number, checked: boolean) => {
    toggleSetValue(setSelectedOperations, operationIndex, checked);
    const review = reviewOperation(operationIndex);
    if (!review?.is_new) return;
    setSelectedChangeIds((previous) => {
      const next = new Set(previous);
      for (const change of review.claim_changes) {
        if (checked) next.add(change.change_id);
        else next.delete(change.change_id);
      }
      return next;
    });
  };

  const currentReviewSelection = (): WikiReviewSelection => ({
    operations: (selectedProposal()?.review_model?.operations || []).map((item) => ({
      operation_index: item.operation_index,
      include_operation: selectedOperations().has(item.operation_index),
      include_page_fields: selectedPageFields().has(item.operation_index),
      accepted_claim_change_ids: item.claim_changes
        .map((change) => change.change_id)
        .filter((changeId) => selectedChangeIds().has(changeId)),
    })),
  });

  const selectedReviewCount = () => (
    selectedChangeIds().size + selectedPageFields().size
    + (selectedProposal()?.review_model?.operations || []).filter(
      (item) => (item.type !== 'upsert_page' || item.is_new) && selectedOperations().has(item.operation_index),
    ).length
  );

  const approveProposal = async () => {
    if (isReadOnly()) return;
    const proposal = selectedProposal();
    if (!proposal) return;
    if (selectedReviewCount() <= 0) {
      setError('请至少勾选一项页面或 Claim 修改');
      return;
    }
    if (!await confirmAction(`确认批准并应用已勾选的 ${selectedReviewCount()} 项 Wiki 修改？未勾选内容不会写入正式页面。`)) return;
    setSaving(true);
    setError('');
    try {
      const applied = await approveWikiProposal(
        proposal,
        workspaceId(),
        reviewComment(),
        currentReviewSelection(),
      );
      setNotice(`已应用选中的 Wiki 修改，approval_hash=${applied.approval_hash || '已记录'}。`);
      setSelectedProposal(null);
      await Promise.all([loadStats(), loadPages(false), loadProposals()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '批准并应用失败');
    } finally {
      setSaving(false);
    }
  };

  const regenerateProposal = async () => {
    if (isReadOnly()) return;
    const proposal = selectedProposal();
    if (!proposal) return;
    if (!await confirmAction('确认让 LLM 基于当前 Evidence 和审核意见重新生成 Proposal？原 Proposal 会在新 Proposal 成功生成后标记为 superseded。')) return;
    setSaving(true);
    setError('');
    try {
      const result = await regenerateWikiProposal(
        proposal,
        regenerationInstructions().trim(),
        workspaceId(),
      );
      setNotice(`LLM 重新生成任务已提交：${result.job?.job_id || '后台处理中'}`);
      await Promise.all([loadJobs(), loadStats()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '提交 LLM 重新生成失败');
    } finally {
      setSaving(false);
    }
  };

  const rejectProposal = async () => {
    if (isReadOnly()) return;
    const proposal = selectedProposal();
    if (!proposal || !rejectReason().trim()) {
      setError('请填写驳回原因');
      return;
    }
    setSaving(true);
    try {
      await rejectWikiProposal(proposal, rejectReason().trim(), workspaceId());
      setNotice('Proposal 已驳回，正式 Wiki 页面没有变化。');
      setSelectedProposal(null);
      await Promise.all([loadStats(), loadProposals()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '驳回失败');
    } finally {
      setSaving(false);
    }
  };

  const runLint = async () => {
    if (isReadOnly()) return;
    setLoading(true);
    try {
      setLintReport(await runWikiLint(workspaceId()));
      setNotice('Wiki 完整性检查已完成。');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Wiki 检查失败');
    } finally {
      setLoading(false);
    }
  };

  const openShare = (target?: { type: WikiShareGrant['target_type']; id: string; label?: string }) => {
    if (isReadOnly()) return;
    setShareTarget(target || null);
    setShareOpen(true);
  };

  const openWikiChat = (prompt?: string) => {
    const text = String(prompt || agentPrompt() || '').trim();
    if (!text) return;
    const page = selectedPage();
    window.dispatchEvent(new CustomEvent('tcode-use-wiki-in-chat', {
      detail: {
        text,
        page_id: String(page?.page_id || '').trim(),
        source_id: sourceId(),
        title: String(page?.title || 'Wiki 页面').trim(),
        page_type: String(page?.page_type || '').trim(),
        status: String(page?.status || '').trim(),
        revision: Number(page?.revision || 0),
        access: isReadOnly() ? 'shared_read_only' : 'owner',
      },
    }));
    setAgentPrompt('');
  };

  let wikiTools!: HTMLDetailsElement;
  onMount(() => {
    onCleanup(dismissDetailsOnOutsideClick(() => wikiTools));
    const focusPaperWiki = (event: Event) => {
      const detail = (event as CustomEvent<{ proposalId?: string; pageId?: string }>).detail || {};
      const proposalId = String(detail.proposalId || '').trim();
      const pageId = String(detail.pageId || '').trim();
      setSourceId('');
      if (proposalId) {
        setViewMode('review');
        setLoading(true);
        void getWikiProposal(proposalId, workspaceId())
          .then((current) => {
            setSelectedProposal(current);
            setSelectedOperations(new Set((current.review_model?.operations || []).map((item) => item.operation_index)));
            setSelectedPageFields(new Set((current.review_model?.operations || []).filter((item) => item.page_fields_changed).map((item) => item.operation_index)));
            setSelectedChangeIds(new Set((current.review_model?.operations || []).flatMap((item) => item.claim_changes || []).map((item) => item.change_id)));
          })
          .catch((reason) => setError(reason instanceof Error ? reason.message : '加载 Wiki Proposal 失败'))
          .finally(() => setLoading(false));
        return;
      }
      if (pageId) {
        setViewMode('pages');
        void selectPage(pageId);
      }
    };
    const refreshWiki = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string }>).detail || {};
      if (String(detail.workspaceId || '').trim() && String(detail.workspaceId || '').trim() !== workspaceId()) return;
      void refreshAll();
    };
    window.addEventListener('tcode-focus-paper-wiki', focusPaperWiki as EventListener);
    window.addEventListener('tcode-wiki-changed', refreshWiki as EventListener);
    onCleanup(() => {
      window.removeEventListener('tcode-focus-paper-wiki', focusPaperWiki as EventListener);
      window.removeEventListener('tcode-wiki-changed', refreshWiki as EventListener);
    });
  });

  return (
    <div class="paper-wiki-workspace flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-[#0f1418]/92 text-[#dee3e8]">
      <header class="shrink-0 border-b border-[#2a3136] bg-[#11171b]/96 px-3 py-2">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="wiki-heading">
            <div class="flex items-center gap-2">
              <h1 class="text-base font-semibold text-[#f7fafc]">文献 Wiki</h1>
              <Show when={isReadOnly()}><span class="rounded-full border border-[#f1a02b]/35 bg-[#f1a02b]/10 px-2 py-0.5 text-[10px] text-[#ffc176]">共享只读</span></Show>
            </div>
            <div class="wiki-source">
              <select aria-label="知识来源" title={selectedSource() ? `来自 ${selectedSource()!.owner.name || selectedSource()!.owner.email || selectedSource()!.owner_user_id}，授权范围 ${selectedSource()!.page_ids.length} 页 + ${selectedSource()!.page_types.length} 类` : '知识来源'} class="max-w-[360px] rounded-lg border border-[#303539] bg-[#0f1418] px-2 py-1 text-[11px] text-[#bdc8d1]" value={sourceId()} onChange={(event) => { setSourceId(event.currentTarget.value); setSelectedPage(null); setSelectedProposal(null); setEditing(false); setViewMode('pages'); }}>
                <option value="">我的 Wiki · {workspaceId() || 'default'}</option>
                <For each={sharedSources()}>{(source) => <option value={source.source_id}>共享 · {source.owner.name || source.owner.email || source.owner_user_id} / {source.workspace_id}</option>}</For>
              </select>
            </div>
          </div>
          <details ref={wikiTools} class="wiki-tools" onKeyDown={(event) => { if (event.key === 'Escape') { wikiTools.open = false; wikiTools.querySelector('summary')?.focus(); } }}>
            <summary>更多操作</summary>
          <div class="wiki-tools-menu" onClick={(event) => { if ((event.target as HTMLElement).closest('button')) wikiTools.open = false; }}>
            <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-0.5 text-[#bdc8d1]">页面 {stats().active_page_count}</span>
            <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-0.5 text-[#bdc8d1]">结论 {stats().claim_count}</span>
            <Show when={stats().features?.vector_search_enabled}><span class={`rounded-full border px-2.5 py-1 ${vectorReady() ? 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]' : 'border-[#f1a02b]/30 bg-[#f1a02b]/10 text-[#ffc176]'}`}>向量 {vectorLabel()}</span></Show>
            <Show when={searchMode()}><span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[#9faeb7]">检索 {searchMode()}</span></Show>
            <Show when={stats().active_job_count > 0}><span class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2.5 py-1 text-[#8ed5ff]">后台任务 {stats().active_job_count}</span></Show>
            <button class={`rounded-full border px-2.5 py-1 ${reviewProposals().length ? 'border-[#f1a02b]/35 bg-[#f1a02b]/12 text-[#ffc176]' : 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]'}`} onClick={() => setViewMode('review')}>
              {isReadOnly() ? 'Proposal 只读' : '待审核'} {reviewProposals().length}
            </button>
            <Show when={!isReadOnly()}><button class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2.5 py-1 text-[#8ed5ff] hover:bg-[#38bdf8]/15" onClick={() => openShare()}>共享管理</button></Show>
            <Show when={!isReadOnly()}><button class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[#9faeb7] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]" onClick={() => void runLint()}>检查 Wiki</button></Show>
            <Show when={!isReadOnly() && stats().features?.markdown_sync_enabled}><button class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[#9faeb7] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]" disabled={markdownSyncing()} onClick={() => void syncMarkdown(false)}>{markdownSyncing() ? '校验中…' : '校验 Markdown'}</button></Show>
            <Show when={!isReadOnly() && stats().features?.vector_search_enabled}><button class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[#9faeb7] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]" onClick={() => void rebuildVectorIndex()}>重建向量</button></Show>
            <button class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[#9faeb7] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]" onClick={() => void refreshAll()}>{loading() ? '刷新中…' : '刷新'}</button>
            <button onClick={() => openWikiChat('当前 Workspace 有几个 Wiki 页面？分别有哪些实体类型？')}>统计 Wiki</button>
            <Show when={selectedPage()}><button onClick={() => openWikiChat('这个 Wiki 有哪些结构化结论和证据支持？')}>询问当前页</button></Show>
            <Show when={!isReadOnly() ? selectedPage() : null}>{(page) => <button onClick={() => openWikiChat(page().status === 'archived' ? '请恢复这个 Wiki 页面。' : '请归档这个 Wiki 页面。')}>{page().status === 'archived' ? '恢复当前页' : '归档当前页'}</button>}</Show>
          </div>
          </details>
        </div>
        <div class="wiki-view-tabs elastic-tabs mt-1.5 flex gap-1">
          <TabIndicator active={viewMode()} />
          <button data-active={viewMode() === 'pages'} class={`rounded-md px-2.5 py-1 text-[11px] font-semibold ${viewMode() === 'pages' ? 'bg-[#38bdf8]/14 text-[#8ed5ff]' : 'text-[#8b949e] hover:text-[#dee3e8]'}`} onClick={() => setViewMode('pages')}>知识页面</button>
          <button data-active={viewMode() === 'review'} class={`rounded-md px-2.5 py-1 text-[11px] font-semibold ${viewMode() === 'review' ? 'bg-[#f1a02b]/14 text-[#ffc176]' : 'text-[#8b949e] hover:text-[#dee3e8]'}`} onClick={() => setViewMode('review')}>变更审核 {reviewProposals().length || ''}</button>
          <Show when={!isReadOnly()}><button data-active={viewMode() === 'types'} class="rounded-md px-3 py-2 text-xs text-[#c4b5fd]" onClick={() => setViewMode('types')}>实体类型管理</button></Show>
          <Show when={desktopSurface() && stats().enabled && viewMode() === 'pages'}>
            <button type="button" class="wiki-directory-toggle" aria-expanded={!directoryHidden() && !editing()} aria-controls="wiki-page-directory" disabled={editing()} onClick={() => setDirectoryHidden(value => !value)}>
              {editing() ? '目录已收起' : directoryHidden() ? '展开目录' : '收起目录'}
            </button>
          </Show>
          <span class="wiki-summary">{stats().active_page_count} 页 · {stats().claim_count} 条结论</span>
        </div>
        <form
          class="mt-1.5 flex flex-col gap-1.5 rounded-lg border border-[#a78bfa]/22 bg-[#a78bfa]/6 p-1.5 lg:flex-row lg:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            openWikiChat();
          }}
        >
          <input
            aria-label="自然语言操作 Wiki"
            class="h-7 min-w-0 flex-1 rounded-md border border-[#303539] bg-[#0f1418] px-2.5 text-[11px] text-[#e6edf3] outline-none placeholder:text-[#59656e] focus:border-[#a78bfa]/50"
            value={agentPrompt()}
            onInput={(event) => setAgentPrompt(event.currentTarget.value)}
            placeholder={selectedPage() ? `询问或操作「${selectedPage()!.title}」` : '例如：当前有几个 Wiki？'}
          />
            <button type="submit" class="rounded-md border border-[#a78bfa]/35 bg-[#a78bfa]/12 px-2.5 py-1 text-[10px] font-semibold text-[#ddd3ff] disabled:opacity-50" disabled={!agentPrompt().trim()}>转到聊天</button>
        </form>
      </header>

      <Show when={isReadOnly()}>
        <div class="border-b border-[#f1a02b]/25 bg-[#f1a02b]/8 px-4 py-2 text-xs leading-5 text-[#ffc176]">当前查看的是共享只读来源。可以检索页面、Claim、Evidence、版本历史和关联 Proposal，但不能编辑、审核、归档、回滚或重建索引；授权撤销后来源会立即隐藏。</div>
      </Show>

      <Show when={error()}>
        <div class="border-b border-[#da3633]/30 bg-[#da3633]/10 px-4 py-2 text-xs text-[#ffb6b0]">{error()}</div>
      </Show>
      <Show when={notice()}>
        <div class="border-b border-[#38b46a]/25 bg-[#38b46a]/8 px-4 py-2 text-xs text-[#9ee6bb]">{notice()}</div>
      </Show>
      <Show when={lintReport()}>
        <div class="border-b border-[#f1a02b]/25 bg-[#f1a02b]/8 px-4 py-2 text-xs text-[#ffc176]">
          Wiki Lint：{String(lintReport()?.status || '').toUpperCase()}，发现 {lintReport()?.issue_count || 0} 个问题。
          <button class="ml-2 underline" onClick={() => setLintReport(null)}>关闭</button>
        </div>
      </Show>
      <Show when={!stats().enabled}>
        <div class="m-6 rounded-2xl border border-[#f1a02b]/30 bg-[#f1a02b]/8 p-6 text-sm leading-6 text-[#ffc176]">
          当前配置未启用 Paper Wiki。请在 paper-research-workflow 配置中设置 <code>library_wiki_enabled=true</code>，然后重启 TCode Server。
        </div>
      </Show>
      <Show when={visibleJobs().length > 0}>
        <div class="border-b border-[#2a3136] bg-[#11171b]/90 px-4 py-2">
          <For each={visibleJobs()}>{(job) => (
            <div class={`flex flex-wrap items-center gap-2 text-[11px] ${job.status === 'failed' ? 'text-[#ffb6b0]' : 'text-[#9faeb7]'}`}>
              <span class={`h-1.5 w-1.5 rounded-full ${job.status === 'failed' ? 'bg-[#da3633]' : 'animate-pulse bg-[#38bdf8]'}`} />
              <span>{job.job_type}</span>
              <span>{job.message || job.status}</span>
              <span class="font-mono text-[9px] text-[#6f7d87]">{Math.round((job.progress || 0) * 100)}%</span>
              <Show when={job.status === 'failed'}><span class="text-[#8b949e]">可检查论文入库/Evidence 日志后重新生成。</span></Show>
            </div>
          )}</For>
        </div>
      </Show>

      <Show when={stats().enabled && viewMode() === 'types' && !isReadOnly()}><WikiEntityTypes workspaceId={workspaceId()} onUpdated={() => { void listWikiEntityTypes(workspaceId()).then(result => setEntityTypes(result.types)); }} /></Show>
      <Show when={stats().enabled && viewMode() === 'pages'}>
        <div class="paper-wiki-body flex min-h-0 flex-1 overflow-hidden" data-directory-hidden={desktopSurface() && (directoryHidden() || editing())} data-editing={editing()}>
          <aside id="wiki-page-directory" hidden={desktopSurface() && (directoryHidden() || editing())} class="flex min-h-0 w-[300px] shrink-0 flex-col border-r border-[#2a3136] bg-[#11171b]/88">
            <div class="space-y-2 border-b border-[#2a3136] p-3">
              <div class="flex gap-2">
                <input class="min-w-0 flex-1 rounded-lg border border-[#303539] bg-[#0f1418] px-3 py-2 text-xs outline-none focus:border-[#7bd0ff]/50" value={query()} onInput={(event) => setQuery(event.currentTarget.value)} onKeyDown={(event) => { if (event.key === 'Enter') void loadPages(false); }} placeholder="搜索主题、实体或结论" />
                <button class="rounded-lg border border-[#303539] bg-[#171c20] px-3 text-xs text-[#8ed5ff]" onClick={() => void loadPages(false)}>搜索</button>
              </div>
              <div class="flex gap-2">
                <select class="min-w-0 flex-1 rounded-lg border border-[#303539] bg-[#0f1418] px-2 py-1.5 text-xs" value={pageType()} onChange={(event) => { setPageType(event.currentTarget.value); void loadPages(false); }}>
                  <option value="">全部类型</option>
                  <For each={pageTypes()}>{([type, count]) => <option value={type}>{type} ({count})</option>}</For>
                </select>
                <select class="rounded-lg border border-[#303539] bg-[#0f1418] px-2 py-1.5 text-[11px] text-[#9faeb7]" value={pageStatusFilter()} onChange={(event) => { setPageStatusFilter(event.currentTarget.value as PageStatusFilter); void loadPages(false); }} aria-label="页面状态筛选">
                  <option value="active">正常页面</option>
                  <option value="archived">仅归档</option>
                  <option value="all">全部页面</option>
                </select>
              </div>
              <Show when={!isReadOnly() && pageType()}><button class="w-full rounded-lg border border-[#a78bfa]/30 bg-[#a78bfa]/10 px-3 py-2 text-xs font-semibold text-[#c4b5fd] hover:bg-[#a78bfa]/15" onClick={() => openShare({ type: 'entity_type', id: pageType(), label: pageType() })}>共享“{pageType()}”实体类型</button></Show>
              <div class="rounded-lg border border-[#303539] bg-[#0f1418]/70 px-3 py-2 text-[10px] leading-4 text-[#6f7d87]">Wiki 页面由 Agent 根据论文与 Evidence 生成；选择页面后可添加自己的笔记。</div>
            </div>
            <div class="tcode-scrollbar min-h-0 flex-1 overflow-y-auto p-2">
              <Show when={pages().length > 0} fallback={<div class="p-4 text-center text-xs leading-5 text-[#6f7d87]">暂无页面。导入论文后系统会生成待审核的 Wiki Proposal，也可以手动新建页面。</div>}>
                <For each={pages()}>{(page) => (
                  <button class={`mb-1 w-full rounded-xl border px-3 py-2.5 text-left transition-colors ${selectedPage()?.page_id === page.page_id ? 'border-[#38bdf8]/32 bg-[#38bdf8]/10' : 'border-transparent hover:border-[#303539] hover:bg-[#171c20]'}`} onClick={() => void selectPage(page.page_id)}>
                    <div class="flex items-start justify-between gap-2">
                      <div class="line-clamp-1 text-sm font-semibold text-[#e6edf3]">{page.title}</div>
                      <span class={`shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] ${statusClass(page.status)}`}>{statusLabel(page.status)}</span>
                    </div>
                    <div class="mt-1 flex items-center gap-2 text-[10px] text-[#6f7d87]"><span>{page.page_type}</span><span>r{page.revision}</span></div>
                    <Show when={page.summary}><div class="mt-1.5 line-clamp-2 text-[11px] leading-4 text-[#8b949e]">{page.summary}</div></Show>
                    <Show when={page.matched_chunks?.[0]}>
                      {(chunk) => (
                        <div class="mt-2 rounded-lg border border-[#38bdf8]/15 bg-[#38bdf8]/5 px-2.5 py-2">
                          <div class="truncate text-[9px] text-[#7bd0ff]">{chunk().heading_path || '正文'} · L{chunk().line_start}-{chunk().line_end}</div>
                          <div class="mt-1 line-clamp-2 text-[10px] leading-4 text-[#8b99a4]">{chunk().content}</div>
                        </div>
                      )}
                    </Show>
                  </button>
                )}</For>
              </Show>
            </div>
          </aside>

          <main class="tcode-scrollbar min-w-0 flex-1 overflow-y-auto bg-[#0f1418]/72">
            <Show when={editing()} fallback={
              <Show when={selectedPage()} fallback={<div class="flex h-full items-center justify-center p-8 text-center text-sm text-[#6f7d87]">从左侧选择页面，或新建一个 Wiki 页面。</div>}>
                {(selected) => (
                  <article class="wiki-reading-page mx-auto max-w-5xl p-5 md:p-8">
                    <div class="flex flex-wrap items-start justify-between gap-3 border-b border-[#2a3136] pb-5">
                      <div class="min-w-0">
                        <div class="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-[#6f7d87]"><span>{selected().page_type}</span><span>revision {selected().revision}</span><span>{selected().updated_by}</span></div>
                        <h2 class="mt-2 text-2xl font-semibold text-[#f7fafc]">{selected().title}</h2>
                        <Show when={selected().summary}><p class="mt-2 max-w-3xl text-sm leading-6 text-[#9faeb7]">{selected().summary}</p></Show>
                      </div>
                      <div class="flex flex-wrap gap-2">
                        <Show when={!isReadOnly()}><button class="rounded-lg border border-[#a78bfa]/30 bg-[#a78bfa]/10 px-3 py-2 text-xs text-[#c4b5fd]" onClick={() => openShare({ type: 'page', id: selected().page_id, label: selected().title })}>共享</button></Show>
                        <Show when={!isReadOnly()}><button class="rounded-lg border border-[#303539] bg-[#171c20] px-3 py-2 text-xs text-[#8ed5ff]" onClick={startEdit}>编辑笔记</button></Show>
                        <Show when={!isReadOnly()}><button class={`rounded-lg border px-3 py-2 text-xs ${selected().status === 'archived' ? 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]' : 'border-[#f1a02b]/30 bg-[#f1a02b]/10 text-[#ffc176]'}`} onClick={() => void toggleArchive()}>{selected().status === 'archived' ? '恢复' : '归档'}</button></Show>
                        <Show when={!isReadOnly() && selected().status === 'archived'}><button class="rounded-lg border border-[#da3633]/35 bg-[#da3633]/10 px-3 py-2 text-xs text-[#ffb6b0] hover:bg-[#da3633]/18 disabled:cursor-not-allowed disabled:opacity-50" disabled={saving()} onClick={openPermanentDelete}>永久删除</button></Show>
                      </div>
                    </div>
                    <section class="mt-6">
                      <div class="mb-2 flex items-center justify-between gap-3"><h3 class="text-sm font-semibold text-[#f7fafc]">Agent 整理内容</h3><span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[9px] text-[#8b949e]">只读 · 来源于论文 Evidence</span></div>
                      <div class="rounded-2xl border border-[#2a3136] bg-[#11171b]/75 p-5"><MarkdownContent content={selectedAgentContent()} role="assistant" renderKey={`${selected().page_id}:${selected().revision}`} onEvidenceClick={openAgentContentEvidence} /></div>
                    </section>
                    <Show when={!isReadOnly()}>
                      <section class="mt-6 rounded-2xl border border-[#a78bfa]/25 bg-[#a78bfa]/7 p-5">
                        <div class="flex items-start justify-between gap-3"><div><h3 class="text-sm font-semibold text-[#eee8ff]">我的笔记</h3><div class="mt-1 text-[10px] text-[#9085ad]">只属于用户编辑区，不会改写 Agent 事实、Claim 或 Evidence。</div></div><button class="shrink-0 rounded-lg border border-[#a78bfa]/30 bg-[#a78bfa]/10 px-3 py-1.5 text-xs text-[#c4b5fd]" onClick={startEdit}>{selected().user_notes ? '编辑笔记' : '添加笔记'}</button></div>
                        <Show when={selected().user_notes} fallback={<div class="mt-4 text-xs text-[#817795]">暂时没有笔记。</div>}><div class="mt-4"><MarkdownContent content={selected().user_notes || ''} role="user" renderKey={`${selected().page_id}:notes:${selected().user_notes_revision || 0}`} /></div></Show>
                      </section>
                    </Show>
                    <Show when={(selected().claims || []).length > 0}>
                      <section class="mt-6">
                        <div class="flex flex-wrap items-end justify-between gap-3">
                          <div>
                            <h3 class="text-sm font-semibold text-[#f7fafc]">结构化结论与证据</h3>
                            <div class="mt-1 text-[10px] text-[#6f7d87]" aria-live="polite">
                              共 {selectedClaims().length} 条
                              <Show when={claimQuery().trim()}> · 找到 {filteredClaims().length} 条</Show>
                            </div>
                          </div>
                          <label class="w-full min-w-0 flex-1 sm:min-w-[240px] md:max-w-sm">
                            <span class="sr-only">模糊搜索结构化结论与证据</span>
                            <input
                              type="search"
                              value={claimQuery()}
                              placeholder="模糊搜索结论内容"
                              class="h-9 w-full rounded-lg border border-[#303539] bg-[#0b1014] px-3 text-xs text-[#e6edf3] outline-none placeholder:text-[#59656e] focus:border-[#7bd0ff]/50"
                              onInput={(event) => {
                                setClaimQuery(event.currentTarget.value);
                                setClaimPage(1);
                              }}
                            />
                          </label>
                        </div>
                        <Show
                          when={filteredClaims().length > 0}
                          fallback={<div class="mt-3 rounded-lg border border-dashed border-[#303539] px-4 py-8 text-center text-xs text-[#6f7d87]">没有找到匹配的结构化结论或证据。</div>}
                        >
                          <div class="mt-3 space-y-2">
                          <For each={pagedClaims()}>{(claim) => (
                            <button class="w-full rounded-xl border border-[#2a3136] bg-[#11171b]/70 p-3 text-left hover:border-[#7bd0ff]/30" onClick={() => void openEvidence(claim)}>
                              <div class="flex flex-wrap items-center gap-2"><span class={`rounded-full border px-2 py-0.5 text-[9px] ${statusClass(claim.support_status === 'supported' ? 'canonical' : 'stale')}`}><HighlightedText text={claim.support_status} query={claimQuery()} /></span><span class="text-[10px] text-[#6f7d87]">置信度 {Math.round((claim.confidence || 0) * 100)}%</span></div>
                              <HighlightedText class="mt-2 block text-sm leading-6 text-[#d8dee4]" text={claim.statement} query={claimQuery()} />
                              <div class="mt-2 text-[10px] text-[#7bd0ff]">{(claim.evidence_ids || []).length > 0 ? `已关联 ${(claim.evidence_ids || []).length} 条论文证据` : '没有关联论文证据'} · 查看来源 →</div>
                            </button>
                          )}</For>
                          </div>
                          <div class="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[#252c31] pt-3">
                            <div class="text-[10px] text-[#6f7d87]">第 {claimPage()} / {claimPageCount()} 页 · 每页 {CLAIMS_PER_PAGE} 条</div>
                            <div class="flex items-center gap-2">
                              <button
                                type="button"
                                class="h-8 w-8 rounded-lg border border-[#303539] bg-[#11171b] text-base text-[#bdc8d1] hover:border-[#7bd0ff]/40 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-35"
                                disabled={claimPage() <= 1}
                                title="上一页"
                                aria-label="上一页"
                                onClick={() => setClaimPage((page) => Math.max(1, page - 1))}
                              >‹</button>
                              <button
                                type="button"
                                class="h-8 w-8 rounded-lg border border-[#303539] bg-[#11171b] text-base text-[#bdc8d1] hover:border-[#7bd0ff]/40 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-35"
                                disabled={claimPage() >= claimPageCount()}
                                title="下一页"
                                aria-label="下一页"
                                onClick={() => setClaimPage((page) => Math.min(claimPageCount(), page + 1))}
                              >›</button>
                            </div>
                          </div>
                        </Show>
                      </section>
                    </Show>
                    <Show when={(selected().history || []).length > 0}>
                      <section class="mt-8 border-t border-[#2a3136] pt-5">
                        <h3 class="text-sm font-semibold text-[#f7fafc]">版本历史</h3>
                        <div class="mt-3 space-y-2">
                          <For each={selected().history || []}>{(revision) => (
                            <div class="flex items-center justify-between gap-3 rounded-lg border border-[#2a3136] bg-[#11171b]/60 px-3 py-2 text-xs">
                              <div><span class="font-semibold text-[#bdc8d1]">r{revision.revision}</span><span class="ml-2 text-[#8b949e]">{revision.reason} · {revision.actor} · {revision.created_at}</span></div>
                              <Show when={!isReadOnly() && revision.revision !== selected().revision}><button class="text-[#8ed5ff] hover:underline" onClick={() => void rollback(revision.revision)}>恢复此版本</button></Show>
                            </div>
                          )}</For>
                        </div>
                      </section>
                    </Show>
                  </article>
                )}
              </Show>
            }>
              <Portal><div ref={element => { const previous = document.activeElement as HTMLElement | null; queueMicrotask(() => element.focus()); onCleanup(() => previous?.focus()); }} class="wiki-notes-modal fixed inset-0 z-[200] flex items-center justify-center bg-black/70 p-3 md:p-6" role="dialog" aria-modal="true" aria-label="编辑 Wiki 用户笔记" tabindex="-1" onKeyDown={event => {
                if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (!saving()) void closeNotes(); }
                if (event.key === 'Tab') {
                  const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input,textarea,[contenteditable="true"],[tabindex="0"]')).filter(item => item.getClientRects().length > 0);
                  const first = controls[0]; const last = controls[controls.length - 1];
                  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
                  else if (!event.shiftKey && (document.activeElement === last || document.activeElement === event.currentTarget)) { event.preventDefault(); first?.focus(); }
                }
              }}>
              <div style={{ height: 'min(860px, 92vh)' }} class="wiki-notes-editor tcode-scrollbar max-h-[92vh] w-full max-w-[1200px] overflow-auto rounded-xl border border-[#43414e] bg-[#11171b] p-4 shadow-2xl md:p-6">
                <div class="flex items-center justify-between border-b border-[#2a3136] pb-2"><div class="min-w-0"><div class="text-[9px] uppercase tracking-[0.12em] text-[#a78bfa]">Personal notes · v{selectedPage()?.user_notes_revision || 0}</div><h2 class="mt-0.5 text-base font-semibold text-[#f7fafc]">{desktopSurface() ? '编辑笔记' : `编辑“${selectedPage()?.title}”的用户笔记`}</h2><Show when={desktopSurface()}><p class="wiki-notes-page-title" title={selectedPage()?.title}>{selectedPage()?.title}</p></Show></div><button disabled={saving()} class="shrink-0 text-[11px] text-[#8b949e] hover:text-[#dee3e8]" onClick={() => void closeNotes()}>关闭编辑</button></div>
                <div class="mt-2 rounded-lg border border-[#a78bfa]/25 bg-[#a78bfa]/7 px-3 py-2 text-[11px] leading-4 text-[#b7aacd]">仅保存个人笔记，不修改页面正文、结论或证据。</div>
                <div class="wiki-notes-editor-field mt-3">
                  <p role="status" class="mb-2 text-xs text-[#c4b5fd]">{draftStatus()}</p>
                  <div class="mb-1 text-[11px] text-[#9faeb7]">用户笔记</div>
                  <Show when={editing()}><MarkdownEditor
                    value={editContent()}
                    disabled={saving()}
                    onChange={setEditContent}
                    minHeight={320}
                    renderKey={`${selectedPage()?.page_id || 'notes'}:notes:${selectedPage()?.user_notes_revision || 0}`}
                  /></Show>
                </div>
                <div class="wiki-notes-editor-actions mt-3 flex flex-wrap justify-end gap-2"><button disabled={saving()} class="min-h-11 rounded-md border border-[#303539] px-3 text-xs" onClick={() => void discardDraft()}>丢弃草稿</button><button disabled={saving()} class="min-h-11 rounded-md border border-[#303539] px-3 text-xs" onClick={() => void closeNotes()}>保存草稿并关闭</button><button class="min-h-11 rounded-md border border-[#a78bfa]/35 bg-[#a78bfa]/12 px-3 text-xs font-semibold text-[#c4b5fd] disabled:opacity-50" disabled={saving()} onClick={() => void savePage()}>{saving() ? '保存中…' : '保存笔记'}</button></div>
              </div>
              </div></Portal>
            </Show>
          </main>
        </div>
      </Show>

      <Show when={stats().enabled && viewMode() === 'review'}>
        <div class="paper-wiki-body flex min-h-0 flex-1 overflow-hidden">
          <aside class="w-[340px] shrink-0 border-r border-[#2a3136] bg-[#11171b]/88 p-3">
            <div class="mb-3 flex items-center justify-between"><div><div class="text-xs font-semibold text-[#f7fafc]">{isReadOnly() ? '关联 Proposal' : '待审核修改'}</div><div class="mt-1 text-[10px] text-[#6f7d87]">{isReadOnly() ? '共享来源，仅供查看，不能代替所有者审核' : 'LLM/编译器不能直接发布'}</div></div><button class="text-[11px] text-[#8ed5ff]" onClick={() => void loadProposals()}>刷新</button></div>
            <div class="tcode-scrollbar h-[calc(100%-48px)] overflow-y-auto space-y-2">
              <Show when={reviewProposals().length > 0} fallback={<div class="rounded-xl border border-dashed border-[#303539] p-6 text-center text-xs leading-5 text-[#6f7d87]">{isReadOnly() ? '该共享范围暂无关联 Proposal。' : '暂无待审核修改。新论文入库或 Agent 提交知识修改后会出现在这里。'}</div>}>
                <For each={reviewProposals()}>{(proposal) => (
                  <button class={`w-full rounded-xl border p-3 text-left ${selectedProposal()?.proposal_id === proposal.proposal_id ? 'border-[#f1a02b]/35 bg-[#f1a02b]/9' : 'border-[#2a3136] bg-[#171c20]/60 hover:border-[#f1a02b]/25'}`} onClick={() => selectProposal(proposal)}>
                    <div class="flex items-center justify-between gap-2"><span class="text-xs font-semibold text-[#e6edf3]">{proposal.trigger_kind}</span><span class={`rounded-full border px-2 py-0.5 text-[9px] ${statusClass(proposal.status)}`}>{statusLabel(proposal.status)}</span></div>
                    <div class="mt-2 text-[11px] text-[#9faeb7]">{proposal.operation_count ?? proposal.operations?.length ?? 0} 项页面修改</div>
                    <div class="mt-1 truncate font-mono text-[9px] text-[#6f7d87]">{proposal.proposal_id}</div>
                  </button>
                )}</For>
              </Show>
            </div>
          </aside>
          <main class="tcode-scrollbar min-w-0 flex-1 overflow-y-auto p-5 md:p-8">
            <Show when={selectedProposal()} fallback={<div class="flex h-full items-center justify-center text-sm text-[#6f7d87]">选择一个 Proposal 查看页面级和 Claim 级差异。</div>}>
              {(selected) => (
                <div class="mx-auto max-w-5xl">
                  <div class="flex flex-wrap items-start justify-between gap-3 border-b border-[#2a3136] pb-5"><div><div class="text-[10px] uppercase tracking-[0.16em] text-[#ffc176]">Human review required</div><h2 class="mt-1 text-xl font-semibold text-[#f7fafc]">Wiki 变更审核</h2><div class="mt-2 font-mono text-[10px] text-[#6f7d87]">{selected().proposal_id}</div></div><div class="text-right text-[11px] text-[#8b949e]"><div>触发：{selected().trigger_kind}</div><div class="mt-1">来源论文：{selected().trigger_doc_ids.join(', ') || '无'}</div></div></div>
                  <Show when={(selected().validation?.warnings || []).length > 0}><div class="mt-4 rounded-xl border border-[#f1a02b]/25 bg-[#f1a02b]/8 p-3 text-xs text-[#ffc176]"><For each={selected().validation.warnings || []}>{(warning) => <div>• {warning}</div>}</For></div></Show>
                  <div class="mt-5 space-y-4">
                    <For each={selected().operations || []}>{(operation, index) => (
                      <section class="rounded-2xl border border-[#2a3136] bg-[#11171b]/70 p-4">
                        <div class="flex items-center justify-between gap-3"><div><span class="rounded-full border border-[#7bd0ff]/25 bg-[#38bdf8]/8 px-2 py-0.5 text-[9px] text-[#8ed5ff]">{operationLabel(operation)}</span><span class="ml-2 text-sm font-semibold text-[#f7fafc]">{operation.page?.title || operation.page_id}</span></div><span class="text-[10px] text-[#6f7d87]">#{index() + 1}</span></div>
                        <Show when={reviewOperation(index())}>{(review) => <div class="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-[#303539] bg-[#0f1418] px-3 py-2 text-[11px] text-[#9faeb7]">
                          <Show when={review().type !== 'upsert_page' || review().is_new}><label class="flex items-center gap-2"><input type="checkbox" disabled={isReadOnly()} checked={selectedOperations().has(index())} onChange={(event) => toggleReviewOperation(index(), event.currentTarget.checked)} /><span>{review().is_new ? '创建这个页面' : '应用这个页面操作'}</span></label></Show>
                          <Show when={review().type === 'upsert_page' && !review().is_new && review().page_fields_changed}><label class="flex items-center gap-2"><input type="checkbox" disabled={isReadOnly()} checked={selectedPageFields().has(index())} onChange={(event) => toggleSetValue(setSelectedPageFields, index(), event.currentTarget.checked)} /><span>应用标题、摘要、正文和页面属性修改</span></label></Show>
                          <span class="text-[#6f7d87]">Claim 可在下方逐条选择</span>
                        </div>}</Show>
                        <Show when={(operation as any).before}><div class="mt-3 grid gap-3 md:grid-cols-2"><div class="rounded-xl border border-[#da3633]/18 bg-[#da3633]/5 p-3"><div class="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#ff9c96]">修改前 · r{(operation as any).before?.revision}</div><div class="mt-2 text-xs leading-5 text-[#9faeb7]">{(operation as any).before?.summary || '无摘要'}</div></div><div class="rounded-xl border border-[#38b46a]/18 bg-[#38b46a]/5 p-3"><div class="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#9ee6bb]">修改后</div><div class="mt-2 text-xs leading-5 text-[#bdc8d1]">{operation.page?.summary || '无摘要'}</div></div></div></Show>
                        <Show when={(reviewOperation(index())?.claim_changes || []).length > 0}>
                          <div class="mt-3 space-y-2">
                            <For each={reviewOperation(index())?.claim_changes || []}>{(change) => {
                              const claim = () => change.after || change.before;
                              return <label class={`flex cursor-pointer gap-3 rounded-xl border p-3 ${selectedChangeIds().has(change.change_id) ? 'border-[#38bdf8]/30 bg-[#38bdf8]/7' : 'border-[#303539] bg-[#0f1418] opacity-65'}`}>
                                <input class="mt-1" type="checkbox" disabled={isReadOnly()} checked={selectedChangeIds().has(change.change_id)} onChange={(event) => toggleSetValue(setSelectedChangeIds, change.change_id, event.currentTarget.checked)} />
                                <div class="min-w-0 flex-1">
                                  <div class="flex flex-wrap items-center gap-2"><span class={`rounded-full border px-2 py-0.5 text-[9px] ${change.kind === 'added' ? 'border-[#38b46a]/30 text-[#9ee6bb]' : change.kind === 'removed' ? 'border-[#da3633]/30 text-[#ffb6b0]' : 'border-[#f1a02b]/30 text-[#ffc176]'}`}>{change.kind === 'added' ? '新增' : change.kind === 'removed' ? '移除' : '修改'}</span><span class="font-mono text-[9px] text-[#6f7d87]">{change.change_id}</span></div>
                                  <Show when={change.kind === 'modified'}><div class="mt-2 rounded-lg border border-[#da3633]/15 bg-[#da3633]/5 px-3 py-2 text-[11px] leading-5 text-[#9faeb7]">修改前：{change.before?.statement}</div></Show>
                                  <div class="mt-2 text-xs leading-5 text-[#d8dee4]">{change.kind === 'removed' ? change.before?.statement : change.after?.statement}</div>
                                  <div class="mt-1 flex flex-wrap items-center justify-between gap-2"><span class="text-[10px] text-[#7bd0ff]">{(claim()?.evidence_ids || []).length > 0 ? `已关联 ${(claim()?.evidence_ids || []).length} 条论文证据` : '没有关联论文证据'}</span><Show when={(claim()?.evidence_ids || []).length > 0}><button type="button" class="text-[10px] text-[#8ed5ff] hover:underline" onClick={(event) => { event.preventDefault(); void openProposalEvidence(claim() as WikiClaim); }}>查看原始证据</button></Show></div>
                                  <Show when={relationSummary(claim() || {})}><div class="mt-1 text-[9px] text-[#8b949e]">关系：{relationSummary(claim() || {})}</div></Show>
                                </div>
                              </label>;
                            }}</For>
                          </div>
                        </Show>
                      </section>
                    )}</For>
                  </div>
                  <Show when={!isReadOnly()} fallback={<div class="mt-6 rounded-xl border border-[#f1a02b]/25 bg-[#f1a02b]/8 p-4 text-xs leading-5 text-[#ffc176]">这是共享范围关联的只读 Proposal。你可以核对差异、状态和 Evidence，但不能批准、驳回或要求 LLM 重新生成；其中未审核内容也不会进入 Agent 的默认 Wiki 检索。</div>}>
                    <div class="mt-6 grid gap-4 rounded-2xl border border-[#2a3136] bg-[#11171b]/80 p-4 lg:grid-cols-3"><label class="text-xs text-[#9faeb7]">批准备注<textarea class="mt-1 h-24 w-full rounded-lg border border-[#303539] bg-[#0f1418] p-3 text-xs text-[#dee3e8] outline-none focus:border-[#38b46a]/45" value={reviewComment()} onInput={(event) => setReviewComment(event.currentTarget.value)} placeholder="可选：记录部分选择的审核判断" /></label><label class="text-xs text-[#9faeb7]">驳回原因<textarea class="mt-1 h-24 w-full rounded-lg border border-[#303539] bg-[#0f1418] p-3 text-xs text-[#dee3e8] outline-none focus:border-[#da3633]/45" value={rejectReason()} onInput={(event) => setRejectReason(event.currentTarget.value)} placeholder="驳回时必填" /></label><label class="text-xs text-[#9faeb7]">LLM 重新生成要求<textarea class="mt-1 h-24 w-full rounded-lg border border-[#303539] bg-[#0f1418] p-3 text-xs text-[#dee3e8] outline-none focus:border-[#7bd0ff]/45" value={regenerationInstructions()} onInput={(event) => setRegenerationInstructions(event.currentTarget.value)} placeholder="例如：删除无 Evidence 的泛化结论，保留底物和温度条件" /></label></div>
                    <div class="mt-4 flex flex-wrap items-center justify-between gap-2"><div class="text-[11px] text-[#8b949e]">已选择 {selectedReviewCount()} 项；未勾选的页面字段和 Claim 不会应用。</div><div class="flex flex-wrap gap-2"><Show when={stats().features?.regeneration_enabled}><button class="rounded-lg border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-4 py-2 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50" disabled={saving()} onClick={() => void regenerateProposal()}>让 LLM 重新生成</button></Show><button class="rounded-lg border border-[#da3633]/30 bg-[#da3633]/10 px-4 py-2 text-xs font-semibold text-[#ffb6b0] disabled:opacity-50" disabled={saving()} onClick={() => void rejectProposal()}>驳回</button><button class="rounded-lg border border-[#38b46a]/30 bg-[#38b46a]/12 px-4 py-2 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50" disabled={saving()} onClick={() => void approveProposal()}>{saving() ? '处理中…' : '批准所选并应用'}</button></div></div>
                  </Show>
                </div>
              )}
            </Show>
          </main>
        </div>
      </Show>

      <Show when={purgeTarget()}>
        {(target) => (
          <div class="fixed inset-0 z-[130] flex items-center justify-center bg-black/70 p-4" onClick={(event) => { if (!saving() && event.target === event.currentTarget) setPurgeTarget(null); }}>
            <section role="dialog" aria-modal="true" aria-labelledby="wiki-purge-title" class="w-full max-w-lg rounded-2xl border border-[#da3633]/35 bg-[#11171b] p-5 shadow-2xl">
              <div class="flex items-start justify-between gap-4 border-b border-[#2a3136] pb-4">
                <div><div class="text-[10px] font-semibold uppercase tracking-[0.16em] text-[#ff8f87]">不可逆操作</div><h2 id="wiki-purge-title" class="mt-1 text-lg font-semibold text-[#f7fafc]">永久删除 Wiki 页面</h2></div>
                <button type="button" class="text-sm text-[#8b949e] hover:text-[#dee3e8] disabled:opacity-40" disabled={saving()} onClick={() => setPurgeTarget(null)}>关闭</button>
              </div>
              <div class="mt-4 rounded-xl border border-[#da3633]/25 bg-[#da3633]/8 p-4 text-xs leading-5 text-[#ffb6b0]">
                <div class="font-semibold">“{target().title}”删除后无法恢复。</div>
                <div class="mt-2">将清除：页面正文、Claim、用户笔记、全部历史版本、Markdown、搜索索引和相关共享授权。</div>
                <div class="mt-1 text-[#c9a29f]">仍会保留：Paper Library 论文原文和独立 EvidenceStore 记录，因为它们可能被其他页面引用。</div>
              </div>
              <label class="mt-4 block text-xs text-[#9faeb7]">
                输入小写 <code class="font-semibold text-[#ffb6b0]">yes</code> 确认
                <input
                  autofocus
                  class="mt-2 w-full rounded-lg border border-[#3a4045] bg-[#0f1418] px-3 py-2 text-sm text-[#f7fafc] outline-none focus:border-[#da3633]/70"
                  value={purgeConfirmation()}
                  onInput={(event) => setPurgeConfirmation(event.currentTarget.value)}
                  onKeyDown={(event) => { if (event.key === 'Enter' && purgeConfirmation().trim() === 'yes' && !saving()) void permanentlyDeletePage(); }}
                  placeholder="请输入 yes"
                />
              </label>
              <div class="mt-5 flex justify-end gap-2">
                <button type="button" class="rounded-lg border border-[#303539] bg-[#171c20] px-4 py-2 text-xs text-[#9faeb7] disabled:opacity-40" disabled={saving()} onClick={() => setPurgeTarget(null)}>取消</button>
                <button type="button" class="rounded-lg border border-[#da3633]/40 bg-[#da3633]/15 px-4 py-2 text-xs font-semibold text-[#ffb6b0] hover:bg-[#da3633]/25 disabled:cursor-not-allowed disabled:opacity-35" disabled={saving() || purgeConfirmation().trim() !== 'yes'} onClick={() => void permanentlyDeletePage()}>{saving() ? '正在永久删除…' : '确认永久删除'}</button>
              </div>
            </section>
          </div>
        )}
      </Show>

      <Show when={evidenceOpen()}>
        <div class="fixed inset-0 z-[120] flex justify-end bg-black/55" onClick={(event) => { if (event.target === event.currentTarget) setEvidenceOpen(false); }}>
          <aside class="tcode-scrollbar h-full w-full max-w-xl overflow-y-auto border-l border-[#2a3136] bg-[#11171b] p-5 shadow-2xl">
            <div class="flex items-start justify-between gap-3 border-b border-[#2a3136] pb-4"><div><div class="text-[10px] text-[#7bd0ff]">原始证据（Original evidence）</div><h2 class="mt-1 text-lg font-semibold text-[#f7fafc]">结论证据（Claim evidence）</h2></div><button class="text-sm text-[#8b949e] hover:text-[#dee3e8]" onClick={() => setEvidenceOpen(false)}>关闭</button></div>
            <div class="mt-4 rounded-xl border border-[#303539] bg-[#0f1418] p-4 text-sm leading-6 text-[#dee3e8]">{evidenceData().claim?.statement}</div>
            <Show when={!evidenceLoading()} fallback={<div class="p-8 text-center text-sm text-[#8b949e]">正在加载论文证据…</div>}>
              <Show when={evidenceData().missing_evidence_ids.length > 0 && evidenceData().evidence.length > 0}>
                <div class="mt-4 rounded-xl border border-[#f1a02b]/25 bg-[#f1a02b]/8 p-4 text-xs leading-5 text-[#ffc176]">
                  <div class="font-semibold">部分证据暂时不可读取，当前结论的证据链不完整。</div>
                  <div class="mt-1">有 {evidenceData().missing_evidence_ids.length} 条来源记录尚未同步或已经失效。</div>
                </div>
              </Show>
              <Show when={evidenceData().evidence.length > 0} fallback={
                <Show
                  when={evidenceData().requested_evidence_ids.length > 0}
                  fallback={<div class="mt-4 rounded-xl border border-[#f1a02b]/25 bg-[#f1a02b]/8 p-4 text-sm leading-6 text-[#ffc176]">该结论没有关联论文 Evidence，应视为用户笔记或未验证内容。</div>}
                >
                  <div class="mt-4 rounded-xl border border-[#da3633]/25 bg-[#da3633]/8 p-4 text-sm leading-6 text-[#ffb6b0]">
                    <div class="font-semibold">该结论存在 Evidence 引用，但对应记录尚未同步或已经失效。</div>
                    <div class="mt-1 text-xs text-[#d7a09c]">这属于证据链断开，不等同于用户笔记。请重新同步来源论文 Evidence。</div>
                    <div class="mt-1 text-xs text-[#d7a09c]">受影响的来源记录：{(evidenceData().missing_evidence_ids.length || evidenceData().requested_evidence_ids.length)} 条。</div>
                  </div>
                </Show>
              }>
                <div class="mt-4 space-y-3">
                  <For each={evidenceData().evidence}>
                    {(evidence) => (
                      <article class="rounded-lg border border-[#2a3136] bg-[#0f1418] p-4">
                        <div class="flex flex-wrap items-center gap-2">
                          <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[9px] text-[#9faeb7]">{evidenceKindLabel(evidence.kind)}</span>
                          <span class={`rounded-full border px-2 py-0.5 text-[9px] ${evidence.wiki_relation === 'contradicts' ? 'border-[#da3633]/30 bg-[#da3633]/10 text-[#ffb6b0]' : evidence.wiki_relation === 'context_only' ? 'border-[#f1a02b]/30 bg-[#f1a02b]/10 text-[#ffc176]' : 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]'}`}>{relationLabel(evidence.wiki_relation || 'supports')}</span>
                          <Show when={evidence.status && evidence.status !== 'active'}><span class="rounded-full border border-[#da3633]/30 bg-[#da3633]/10 px-2 py-0.5 text-[9px] text-[#ffb6b0]">{evidence.status}</span></Show>
                        </div>
                        <h3 class="mt-2 text-sm font-semibold text-[#f7fafc]">{readableSourceLabel(evidence.title, '论文来源证据')}</h3>
                        <Show when={evidenceRows(evidence).length > 0}>
                          <dl class="mt-3 grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-2 text-[11px]">
                            <For each={evidenceRows(evidence)}>{(row) => (
                              <>
                                <dt class="text-[#6f7d87]">{row[0]}</dt>
                                <dd class="min-w-0 break-words text-[#bdc8d1]">{row[1]}</dd>
                              </>
                            )}</For>
                          </dl>
                        </Show>
                        <Show when={(evidence.images || []).length > 0}>
                          <div class="mt-3">
                            <div class="text-[11px] font-semibold text-[#8b949e]">来源图片（Source images）</div>
                            <div class="mt-2 space-y-2">
                              <For each={evidence.images || []}>{(image: any) => (
                                <figure class="overflow-hidden rounded-lg border border-[#252c31] bg-[#0b1014]">
                                  <ZoomableImage src={evidenceImageUrl(evidence, image)} alt={readableSourceLabel(image.label || image.name, '来源图片')} downloadName={String(image.name || '') || undefined} class="max-h-[520px] w-full object-contain" loading="lazy" />
                                  <figcaption class="border-t border-[#252c31] px-3 py-2 text-[11px] text-[#8b949e]">{readableSourceLabel(image.label || image.name, '来源图片')}</figcaption>
                                </figure>
                              )}</For>
                            </div>
                          </div>
                        </Show>
                        <div class="mt-3 text-[11px] font-semibold text-[#8b949e]">证据内容（Content）</div>
                        <pre class="mt-2 whitespace-pre-wrap break-words rounded-lg border border-[#252c31] bg-[#0b1014] p-3 text-xs leading-5 text-[#bdc8d1]">{evidence.content}</pre>
                      </article>
                    )}
                  </For>
                </div>
              </Show>
            </Show>
          </aside>
        </div>
      </Show>
      <WikiSharePanel
        open={shareOpen()}
        workspaceId={workspaceId()}
        entityTypes={entityTypes()}
        initialTarget={shareTarget()}
        onClose={() => setShareOpen(false)}
        onChanged={() => void loadSharedSources()}
      />
    </div>
  );
}
