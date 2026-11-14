import { researchStatusLabel, userFacingTitle, toolDisplayLabel, researchAgentLabel } from '../utils/userFacingLabels';
import { confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createResource, createSignal, on, onMount, onCleanup } from 'solid-js';
import { dismissDetailsOnOutsideClick } from '../utils/dismissDetails';
import {
  challengeResearchFinding,
  createResearchFinding,
  getResearchCheckpoint,
  getResearchKnowledge,
  listResearchActivities,
  listResearchHypotheses,
  listResearchExperiments,
  listResearchConflicts,
  listResearchEpisodes,
  listResearchFindings,
  previewResearchContinuationContext,
  refreshResearchEpisodes,
  refreshResearchKnowledge,
  removeResearchKnowledgeLink,
  reopenResearchConflict,
  resolveResearchConflict,
  reviewResearchConflict,
  type ResearchCheckpoint,
  type ResearchActivity,
  type ResearchContinuationContext,
  type ResearchConflict,
  type ResearchEpisode,
  type ResearchFinding,
  type ResearchKnowledgeSnapshot,
  type ResearchProject,
} from '../api/research';
import ResearchArtifactGroupCard from './ResearchArtifactGroupCard';
import ResearchMap from './ResearchMap';
import MarkdownContent from './MarkdownContent';
import { readableResearchMarkdown } from '../utils/researchMarkdown';
import ResearchChangesPanel from './ResearchChangesPanel';
import type { ResearchChangeFocus } from '../api/researchChanges';
import ResearchDialog from './ResearchDialog';
import ResearchSessionPanel, { type ResearchSessionTab } from './ResearchSessionPanel';
import { loadResearchNavigation, researchModuleVisible } from '../utils/researchNavigation';
import { sessionStore } from '../stores/session';
import ResearchProjectProvenancePanel from './ResearchProjectProvenancePanel';
import { groupResearchKnowledgeIssues, researchSourceLabel, researchRelationLabel } from '../utils/researchKnowledgeDisplay';

type Props = {
  workspaceId?: string;
  projects: ResearchProject[];
  initialResearchId?: string;
  initialProvenanceOpen?: boolean;
  view?: 'overview' | 'assets' | 'review' | 'advanced';
  onShare?: (project: ResearchProject) => void;
  onEvidenceMap?: (project: ResearchProject) => void;
  onReviewSources?: () => void;
  onProjectChanged?: () => void | Promise<void>;
  changeFocus?: ResearchChangeFocus;
};

const formatTime = (value?: string) => {
  const timestamp = Date.parse(String(value || ''));
  if (!Number.isFinite(timestamp)) return String(value || '');
  return new Date(timestamp).toLocaleString();
};

const outcomeLabels: Record<string, string> = {
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  abandoned: '已放弃',
  unknown: '待判断',
};

const activityStatusLabels: Record<string, string> = {
  completed: '已完成',
  observed: '已记录',
  running: '进行中',
  waiting: '等待用户输入',
  partial: '部分完成',
  incomplete: '需要检查',
  failed: '遇到问题',
  cancelled: '已取消',
};

const activityTypeLabels: Record<string, string> = {
  paper_ingest: '论文入库',
  paper_research: '论文研究',
  file_research: '资料分析',
  subagent_task: '协作研究',
  tool_run: '分析任务',
  research_conversation: '研究讨论',
};

const mechanicalSummaryPattern = /^记录\s*\d+\s*条研究事件，涉及\s*\d+\s*个\s*Session/i;
const technicalOnlyPattern = /^(?:session|trace|event|turn|call|episode|checkpoint|run|task)(?:\s*[:#-]|_[a-z]+\s*[:=])/i;
const inlineTechnicalRefPattern = /(?:^|\s)(?:session|trace|event|turn|call|episode|checkpoint|run)\s*[:#-]\s*[a-z0-9._:-]+/gi;

const readableText = (value: unknown, limit = 520) => {
  const lines = String(value || '')
    .split(/\r?\n/)
    .map((line) => line.trim().replace(/^#{1,6}\s*/, '').replace(/^[-*+]\s+/, ''))
    .filter((line) => line && !technicalOnlyPattern.test(line) && !line.startsWith('# Task Contract'));
  const text = lines.join(' ').replace(inlineTechnicalRefPattern, ' ').replace(/\s+/g, ' ').trim();
  if (!text || mechanicalSummaryPattern.test(text)) return '';
  if (text.length <= limit) return text;
  const head = text.slice(0, limit);
  const boundary = Math.max(...['。', '！', '？', '.', '!', '?', '；'].map((mark) => head.lastIndexOf(mark)));
  return `${boundary > limit / 2 ? head.slice(0, boundary + 1) : head}…`;
};

const provenanceValues = (values: Array<Record<string, any>> | undefined, limit = 4) => (
  (values || [])
    .map((item) => readableText(item?.value, 260))
    .filter(Boolean)
    .slice(0, limit)
);

const activityFallbackSummary = (activity: ResearchActivity) => {
  const file = activity.file_refs.find((item) => item.filename)?.filename;
  if (activity.activity_type === 'paper_ingest') return `已解析并整理${file ? `论文《${file}》` : '一篇论文'}，资料与处理过程已纳入当前课题。`;
  if (activity.activity_type === 'paper_research') return `已围绕${file ? `论文《${file}》` : '相关论文'}完成一轮阅读与分析。`;
  if (activity.activity_type === 'file_research') return `已分析${file ? `资料《${file}》` : '研究资料'}，相关产物已保留。`;
  if (activity.activity_type === 'subagent_task') return '协作 Agent 完成了一项研究任务，过程与结果已汇入当前课题。';
  if (activity.status === 'failed' || activity.status === 'incomplete') return '这次研究动作没有完整结束，需要检查后再决定是否继续。';
  return activity.tool_calls.length > 0
    ? '完成了一轮研究分析，工具过程和结果已保留以便追溯。'
    : '记录了一次与当前课题有关的研究讨论。';
};

const findingLabels: Record<string, string> = {
  active: '当前有效',
  disputed: '存在争议',
  superseded: '已被取代',
  retracted: '已撤回',
  context_limited: '限定条件有效',
};

const knowledgeTypeLabels: Record<string, string> = {
  paper: '论文',
  passage: '原文片段',
  evidence: '证据',
  wiki_page: 'Wiki 页面',
  wiki_claim: 'Wiki 论断',
  finding: '研究结论',
};

export default function ResearchStatePanel(props: Props) {
  let moreMenu:HTMLDetailsElement|undefined;
  onMount(()=>onCleanup(dismissDetailsOnOutsideClick(()=>moreMenu)));
  const [mapView, setMapView] = createSignal(false);
  const [changesView,setChangesView]=createSignal(false);
  createEffect(()=>{if(props.changeFocus){setChangesView(true);setMapView(false);}});
  const [allProgress, setAllProgress] = createSignal(false);
  const [researchId, setResearchId] = createSignal('');
  const [activities, setActivities] = createSignal<ResearchActivity[]>([]);
  const [episodes, setEpisodes] = createSignal<ResearchEpisode[]>([]);
  const [checkpoint, setCheckpoint] = createSignal<ResearchCheckpoint | null>(null);
  const [continuationQuestion, setContinuationQuestion] = createSignal('');
  const [continuationPreview, setContinuationPreview] = createSignal<ResearchContinuationContext | null>(null);
  const [findings, setFindings] = createSignal<ResearchFinding[]>([]);
  const [conflicts, setConflicts] = createSignal<ResearchConflict[]>([]);
  const [knowledge, setKnowledge] = createSignal<ResearchKnowledgeSnapshot | null>(null);
  const [loading, setLoading] = createSignal(false);
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [moduleTab, setModuleTab] = createSignal<ResearchSessionTab>();
  const [conflictHistory, setConflictHistory] = createSignal(false);
  const [knowledgePage, setKnowledgePage] = createSignal(1);
  const moduleKey = createMemo(() => researchId() ? JSON.stringify([researchId(), props.workspaceId || '']) : false);
  const [modules, { refetch: refreshModules }] = createResource(moduleKey, key => {
    const [id, workspace] = JSON.parse(key);
    return loadResearchNavigation(id, workspace);
  });
  const knowledgeIssueGroups = createMemo(() => groupResearchKnowledgeIssues(
    knowledge()?.lint?.issues || [],
    knowledge()?.links || [],
  ));

  const workspaceId = () => String(props.workspaceId || '').trim();
  const selectedProject = createMemo(() => (
    props.projects.find((item) => item.research_id === researchId()) || null
  ));
  const [referenceRecords]=createResource(()=>researchId()?[researchId(),workspaceId()] as const:false,async ([id,ws])=>{
    const [hypotheses,experiments]=await Promise.all([listResearchHypotheses(id,ws),listResearchExperiments(id,ws)]);
    return new Map([...hypotheses.hypotheses.map(h=>[h.hypothesis_id,h.statement] as const),...experiments.experiments.map(e=>[e.experiment_id,e.title] as const)]);
  });
  const progressMarkdown=(value:unknown)=>readableResearchMarkdown(value,new Map([...(referenceRecords.error?[]:referenceRecords()?.entries()||[]),[researchId(),selectedProject()?.title||'当前课题']]));
  const associatedSessions = createMemo(() => sessionStore.sessions().filter(session =>
    String(session.workspace_id || session.workdir_name || '') === workspaceId()
    && String(session.research_id || session.metadata?.research_id || '') === researchId()
    && !session.metadata?.parent_session_id && !session.is_archived));
  const projectActivities = createMemo(() => {
    const target = researchId();
    const originCandidateIds = new Set(selectedProject()?.origin_candidate_ids || []);
    const sourceEventIds = new Set(episodes().flatMap((item) => item.event_ids));
    return activities().filter((item) => (
      item.research_id === target
      || originCandidateIds.has(item.candidate_id)
      || item.semantic_event_ids.some((eventId) => sourceEventIds.has(eventId))
    ));
  });
  const readableGoal = createMemo(() => {
    const project = selectedProject();
    return readableText(project?.description, 360) || readableText(project?.title, 240) || '尚未明确研究目标';
  });
  const readableCurrentState = createMemo(() => {
    const activity = projectActivities()[0];
    const activitySummary = activity && progressMarkdown(activity.summary);
    if (activitySummary) return activitySummary;
    const episodeSummary = progressMarkdown(episodes()[0]?.summary?.value);
    if (episodeSummary) return episodeSummary;
    const title = readableText(selectedProject()?.title, 160) || '当前课题';
    if (projectActivities().length || episodes().length) return `正在围绕“${title}”持续研究，新的过程已经记录，尚未提炼出明确结论。`;
    return '还没有形成阶段进展。开始研究后，这里会整理做过的工作和得到的结果。';
  });
  const readableQuestions = createMemo(() => {
    const values = provenanceValues(checkpoint()?.open_questions).map(progressMarkdown);
    if (values.length) return values;
    const progress = readableCurrentState();
    const inferred = progress.match(/(?:仍需|尚需|还需)[^。！？!?；;]{1,100}(?:验证|确认|核对|补充)/)?.[0];
    return inferred ? [inferred] : [];
  });
  const readableNextActions = createMemo(() => {
    const values = provenanceValues(checkpoint()?.next_actions).map(progressMarkdown);
    if (values.length) return values;
    if (conflicts().some((item) => ['open', 'under_review'].includes(item.status))) {
      return ['先核对仍未解决的冲突，再决定采用哪一项结论。'];
    }
    if (readableQuestions().length) {
      return ['围绕上述未决事项补充证据或验证结果。'];
    }
    if (!findings().some((item) => ['active', 'context_limited'].includes(item.status))) {
      return ['从已有进展中提炼一条可验证的研究结论。'];
    }
    return ['继续补充证据，并在结论发生变化时记录修正或冲突。'];
  });
  const confirmedStatements = createMemo(() => (
    findings()
      .filter((item) => ['active', 'context_limited'].includes(item.status))
      .map((item) => readableText(item.statement, 260))
      .filter(Boolean)
      .slice(0, 4)
  ));
  const episodeForActivity = (activity: ResearchActivity) => {
    const sourceIds = new Set(activity.event_ids);
    return episodes().find((episode) => episode.event_ids.some((eventId) => sourceIds.has(eventId)));
  };

  let loadGeneration = 0;
  let loadedScope = '';
  const load = async (targetId = researchId()) => {
    const generation = ++loadGeneration, targetWorkspace = workspaceId();
    const scope = JSON.stringify([targetWorkspace, targetId]);
    if (scope !== loadedScope) {
      loadedScope = scope;
      setEpisodes([]); setActivities([]); setCheckpoint(null); setFindings([]); setConflicts([]); setKnowledge(null);
    }
    if (!targetId) {
      setEpisodes([]);
      setActivities([]);
      setCheckpoint(null);
      setContinuationPreview(null);
      setFindings([]);
      setConflicts([]);
      setKnowledge(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const [episodeResult, checkpointResult, activityResult, findingResult, conflictResult, knowledgeResult] = await Promise.all([
        listResearchEpisodes(targetId, workspaceId()),
        getResearchCheckpoint(targetId, workspaceId()),
        listResearchActivities(workspaceId(), 'included'),
        listResearchFindings(targetId, workspaceId()),
        listResearchConflicts(targetId, workspaceId()),
        getResearchKnowledge(targetId, workspaceId()),
      ]);
      if (generation !== loadGeneration || workspaceId() !== targetWorkspace || researchId() !== targetId) return;
      setEpisodes(episodeResult.episodes || []);
      setCheckpoint(checkpointResult.checkpoint || null);
      setActivities(activityResult.activities || []);
      setFindings(findingResult.findings || []);
      setConflicts(conflictResult.conflicts || []);
      setKnowledge(knowledgeResult);
      setKnowledgePage(page => Math.min(page, Math.max(1, Math.ceil((knowledgeResult.links || []).length / 10))));
    } catch (e) {
      if (generation === loadGeneration && workspaceId() === targetWorkspace) setError(e instanceof Error ? e.message : '加载研究现场失败');
    } finally {
      if (generation === loadGeneration && workspaceId() === targetWorkspace) setLoading(false);
    }
  };

  createEffect(on(
    () => [props.initialResearchId, props.projects.map((item) => item.research_id).join('|'), workspaceId()] as const,
    ([initial]) => {
      const next = props.projects.some((item) => item.research_id === initial)
        ? String(initial)
        : props.projects.find((item) => item.status !== 'archived')?.research_id
          || props.projects[0]?.research_id
          || '';
      setResearchId(next);
      setModuleTab(undefined); setMapView(false); setAllProgress(false); setConflictHistory(false); setKnowledgePage(1);
      setNotice('');
      setContinuationPreview(null);
      void load(next);
    },
  ));

  const refresh = async () => {
    if (!researchId()) return;
    setBusy('refresh');
    setError('');
    try {
      const result = await refreshResearchEpisodes(researchId(), workspaceId());
      const report = result.report || {};
      let knowledgeNotice = '';
      if (knowledge()?.settings?.knowledge_links_active !== false) {
        const knowledgeResult = await refreshResearchKnowledge(researchId(), workspaceId());
        const knowledgeReport = knowledgeResult.report || {};
        const lint = (knowledgeReport.lint || {}) as Record<string, any>;
        knowledgeNotice = ` 可信链 ${Number(knowledgeReport.link_count || 0)} 条，`
          + `阻断 ${Number((lint.counts || {}).error || 0)} 条。`;
      }
      setNotice(
        `研究进度已保存：整理了 ${Number(report.episode_created_count || 0)} 段新进展，`
        + `更新了 ${Number(report.checkpoint_created_count || 0)} 份研究概览。`
        + knowledgeNotice,
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存研究现场失败');
    } finally {
      setBusy('');
    }
  };

  const previewContinuation = async () => {
    const question = continuationQuestion().trim();
    if (!researchId() || !question) {
      setError('请先输入一条用于测试相关性和上下文预算的问题。');
      return;
    }
    setBusy('continuation-preview');
    setError('');
    try {
      const result = await previewResearchContinuationContext(
        researchId(),
        question,
        workspaceId(),
      );
      setContinuationPreview(result.context);
      setNotice(
        result.context.injected
          ? `该问题会注入约 ${result.context.token_estimate} Token 的受控研究上下文。`
          : result.context.decision === 'block'
            ? '该问题命中课题，但安全门控阻断了注入。'
            : '该问题不会注入研究上下文。',
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : '上下文预览失败');
    } finally {
      setBusy('');
    }
  };

  const refreshKnowledgeChain = async () => {
    if (!researchId()) return;
    setBusy('knowledge');
    setError('');
    try {
      const result = await refreshResearchKnowledge(researchId(), workspaceId());
      const report = result.report || {};
      const lint = (report.lint || {}) as Record<string, any>;
      const errorCount = Number((lint.counts || {}).error || 0);
      const warningCount = Number((lint.counts || {}).warning || 0);
      const retiredCount = Number(report.retired_count || 0);
      setNotice(errorCount === 0
        ? `来源关系已整理完成：${Number(report.link_count || 0)} 条有效关联，已清理 ${retiredCount} 条旧关联。现在可以重新生成导出预览。`
        : `已自动整理来源并清理 ${retiredCount} 条旧关联；仍有 ${errorCount} 项会阻断打包、${warningCount} 项不阻断提醒，请按下方说明处理。`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '刷新知识可信链失败');
    } finally {
      setBusy('');
    }
  };

  const removeKnowledgeLink = async (linkId: string) => {
    if (!await confirmAction('移除此研究关联？历史 Revision 会继续保留。')) return;
    setBusy(`knowledge-link:${linkId}`);
    setError('');
    try {
      await removeResearchKnowledgeLink(linkId, workspaceId());
      setNotice('研究关联已移除，审计历史仍然保留。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '移除研究关联失败');
    } finally {
      setBusy('');
    }
  };

  const recordFinding = (source?: ResearchActivity | ResearchEpisode) => {
    beginEvidenceAction('create', researchId());
    if (source) setEvidenceSource('activity_id' in source ? source.activity_id : source.episode_id);
  };

  const [evidenceAction, setEvidenceAction] = createSignal<{ kind: 'create' | 'challenge' | 'resolve' | 'reopen'; id: string }>();
  const [evidenceSource, setEvidenceSource] = createSignal('');
  const [evidenceText, setEvidenceText] = createSignal('');
  const [resolution, setResolution] = createSignal('inconclusive');
  const sourceRecords = createMemo(() => {
    const records = projectActivities().filter(a => a.semantic_event_ids.length).map(a => ({
      activity_id:a.activity_id, title:a.title, summary:a.summary, ended_at:a.ended_at,
      semantic_event_ids:a.semantic_event_ids, episode_id:episodeForActivity(a)?.episode_id || '',
    }));
    return records.length ? records : episodes().filter(e => e.event_ids.length).map(e => ({
      activity_id:e.episode_id, title:e.title, summary:e.summary.value, ended_at:e.ended_at,
      semantic_event_ids:e.event_ids, episode_id:e.episode_id,
    }));
  });
  const beginEvidenceAction = (kind: 'create' | 'challenge' | 'resolve' | 'reopen', id: string) => {
    setEvidenceSource(''); setEvidenceText(''); setResolution('inconclusive'); setError('');
    setEvidenceAction({kind, id});
  };
  const submitEvidenceAction = async () => {
    const action = evidenceAction(), source = sourceRecords().find(a => a.activity_id === evidenceSource());
    const text = evidenceText().trim();
    if (!action || !source || !text || busy()) return;
    const ids = source.semantic_event_ids, evidenceIds = ids.map(id => `event:${id}`);
    setBusy('evidence-action'); setError('');
    try {
      if (action.kind === 'create') await createResearchFinding(researchId(), {
        statement:text, provenance:'user_stated', source_event_ids:ids, evidence_ids:evidenceIds,
        episode_id:source.episode_id,
      }, workspaceId());
      else if (action.kind === 'challenge') await challengeResearchFinding(action.id, {
        statement:text, provenance:'user_stated', source_event_ids:ids, evidence_ids:evidenceIds,
        episode_id:source.episode_id, conflict_type:'direct_contradiction',
      }, workspaceId());
      else if (action.kind === 'resolve') await resolveResearchConflict(action.id, {result:resolution(), reason:text, evidence_ids:evidenceIds}, workspaceId());
      else await reopenResearchConflict(action.id, text, evidenceIds, workspaceId());
      setEvidenceAction(undefined);
      setNotice('已记录处理结果与所选研究记录。原结论、来源及修正历史仍然保留。');
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : '处理失败，请核对后再试'); }
    finally { setBusy(''); }
  };

  const reviewConflict = async (conflict: ResearchConflict) => {
    setBusy(`conflict:${conflict.conflict_id}`);
    try {
      await reviewResearchConflict(conflict.conflict_id, '用户开始审核冲突', workspaceId());
      setNotice('冲突已进入审核状态。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '更新冲突失败');
    } finally {
      setBusy('');
    }
  };

  return (
    <div class="research-state-panel mx-auto max-w-7xl space-y-4">
      <div class="rounded-xl border border-[#2a3136] bg-[#11171b] p-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-base font-semibold text-[#e6edf3]">{selectedProject()?.title || '研究概览'}</h2>
            <p class="mt-1 text-xs text-[#8b99a4]">查看这项研究做过什么、得到什么、哪里还不确定，以及下一步可以做什么。</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <Show when={props.view === 'advanced'}><select
              aria-label="切换课题" class="h-9 min-w-[260px] rounded-lg border border-[#303539] bg-[#0d1216] px-3 text-xs text-[#d7dee4]"
              value={researchId()}
              onChange={(event) => {
                setResearchId(event.currentTarget.value);
                setContinuationPreview(null);
                          void load(event.currentTarget.value);
              }}
            >
              <For each={props.projects}>
                {(project) => <option value={project.research_id}>{project.title}</option>}
              </For>
            </select></Show>
            <Show when={(!props.view || props.view === 'overview') && selectedProject()?.status !== 'archived'}><button type="button" class="h-9 rounded-lg border px-3 text-xs" onClick={() => window.dispatchEvent(new CustomEvent('tcode-continue-research', { detail: { researchId: researchId(), workspaceId: workspaceId(), title: selectedProject()?.title } }))}>继续研究</button></Show>
            <Show when={props.onShare && selectedProject()}><button type="button" class="h-9 rounded-lg border border-[#303539] px-3 text-xs" onClick={() => props.onShare?.(selectedProject()!)}>分享研究包</button></Show>
            <details ref={moreMenu} class="research-more" onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();event.currentTarget.open=false;event.currentTarget.querySelector('summary')?.focus();}}}><summary>课题更多</summary><div class="research-more-actions" role="group" aria-label="课题更多操作" onClick={event=>{if((event.target as HTMLElement).closest('button:not(:disabled)')){moreMenu!.open=false;moreMenu!.querySelector('summary')?.focus();}}}>
              <button type="button" disabled={selectedProject()?.status === 'archived'} onClick={() => setModuleTab('hypotheses')}>提出研究假说</button>
              <button type="button" disabled={selectedProject()?.status === 'archived'} onClick={() => setModuleTab('experiments')}>创建首个实验 / 查看历史</button>
              <button type="button" onClick={() => setModuleTab('continuation')}>持续研究设置</button>
            <button
              class="h-9 rounded-lg border border-[#38bdf8]/35 bg-[#38bdf8]/10 px-3 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50"
              disabled={!researchId() || selectedProject()?.status === 'archived' || Boolean(busy())}
              onClick={() => void refresh()}
            >
              {busy() === 'refresh' ? '更新中…' : '更新研究摘要与来源'}
            </button>
            </div></details>
          </div>
        </div>
        <Show when={selectedProject()?.status === 'archived'}><p class="mt-3 text-xs">这是已归档课题，只供查看。</p></Show>
        <Show when={error()}><div class="mt-3 rounded-lg border border-[#ef4444]/30 bg-[#ef4444]/10 px-3 py-2 text-xs text-[#fca5a5]">{error()}</div></Show>
        <Show when={notice()}><div class="mt-3 rounded-lg border border-[#38bdf8]/25 bg-[#38bdf8]/8 px-3 py-2 text-xs text-[#8ed5ff]">{notice()}</div></Show>
      </div>

      <Show when={selectedProject()}>{project => <><div class="flex flex-wrap gap-2" aria-label="课题视图"><button type="button" class="rounded-lg border px-3 py-2 text-xs" aria-pressed={!mapView()&&!changesView()} onClick={() => {setMapView(false);setChangesView(false);}}>课题详情</button><button type="button" class="rounded-lg border px-3 py-2 text-xs" aria-pressed={mapView()} onClick={() => {setMapView(true);setChangesView(false);}}>研究地图 · 预览</button><button type="button" class="rounded-lg border px-3 py-2 text-xs" aria-pressed={changesView()} onClick={()=>{setChangesView(true);setMapView(false);}}>研究变化比较</button><button type="button" class="rounded-lg border px-3 py-2 text-xs" onClick={()=>props.onEvidenceMap?.(project())}>假说与证据地图</button></div><Show when={mapView()}><ResearchMap project={project()} workspaceId={workspaceId()} /></Show><Show when={changesView()}><ResearchChangesPanel project={project()} workspaceId={workspaceId()} focus={props.changeFocus} /></Show></>}</Show>
      <div style={{ display: mapView()||changesView() ? 'none' : undefined }}>
      <Show when={loading()}><div class="py-16 text-center text-sm text-[#8b99a4]">正在加载研究现场…</div></Show>
      <Show when={!loading() && !researchId()}>
        <div class="rounded-xl border border-dashed border-[#34404a] p-10 text-center text-sm text-[#71808a]">请先确认研究课题。</div>
      </Show>

      <Show when={!loading() && researchId() && props.view === 'advanced'}>
        <ResearchProjectProvenancePanel
          researchId={researchId()}
          workspaceId={workspaceId()}
          initialOpen={props.initialProvenanceOpen}
          onChanged={props.onProjectChanged}
        />
      </Show>

      <Show when={!loading() && researchId() && props.view === 'assets'}>
        <section class="research-scroll-list space-y-3" aria-label="研究成果与文件列表" tabIndex={0}><h3 class="text-sm font-semibold">研究成果与文件</h3>
          <Show when={!projectActivities().some(a => a.artifact_groups?.length || a.file_refs.length)}><p class="text-xs">还没有纳入课题的成果。</p></Show>
          <For each={projectActivities()}>{activity => <div>
            <For each={activity.artifact_groups || []}>{group => <ResearchArtifactGroupCard group={group} />}</For>
            <For each={activity.file_refs}>{file => <div class="mt-2 text-xs"><Show when={file.path} fallback={<span>{file.filename || '研究文件'} · 未记录可打开路径</span>}><button type="button" class="rounded border px-3 py-2" onClick={() => sessionStore.requestFilePreview(file.path)}>{file.filename || '研究文件'} · 查看文件</button></Show></div>}</For>
          </div>}</For>
        </section>
      </Show>
      <Show when={!loading() && researchId()}>
        <div class="space-y-4">
          <div class="space-y-4">
            <Show when={props.view === 'review'}>
              <Show when={modules()?.hypotheses?.some(item => item.status === 'proposed')}><button type="button" class="rounded-lg border p-3 text-sm" onClick={() => setModuleTab('hypotheses')}>研究假说待批准 · 查看依据与验证计划</button></Show>
              <Show when={knowledge()?.lint?.counts.error || knowledge()?.lint?.counts.warning}><section class="rounded-lg border p-3 text-sm"><p>{Number(knowledge()?.lint?.counts.error || 0)} 项来源阻断打包 · {Number(knowledge()?.lint?.counts.warning || 0)} 项非阻断提醒</p><button type="button" class="mt-2 rounded border p-2" onClick={() => props.onReviewSources?.()}>查看来源与修复说明</button></section></Show>
            </Show>
            <Show when={!props.view || props.view === 'overview'}>
            <section class="research-overview rounded-xl border border-[#2a3136] bg-[#11171b] p-4">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <h3 class="text-sm font-semibold text-[#e6edf3]">当前研究概览</h3>
                  <p class="mt-1 text-[11px] text-[#71808a]">系统根据已纳入课题的研究过程自动整理。</p>
                </div>
                <Show when={checkpoint()} keyed>
                  {(item) => <span class="text-[10px] text-[#71808a]">更新于 {formatTime(item.generated_at)}</span>}
                </Show>
              </div>
              <div class="research-overview-body mt-4 space-y-3 text-xs leading-6 text-[#bdc8d1]">
                <div class="rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                  <div class="text-[10px] font-semibold tracking-wider text-[#7bd0ff]">研究目标</div>
                  <div class="mt-1 text-[#d7dee4]">{readableGoal()}</div>
                </div>
                <div class="rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                  <div class="text-[10px] font-semibold tracking-wider text-[#7bd0ff]">最近进展</div>
                  <div class="research-progress-markdown mt-1"><MarkdownContent content={readableCurrentState()} allowSessionActions={false}/></div>
                </div>
                <Show when={confirmedStatements().length > 0}>
                  <div class="rounded-lg border border-[#38b46a]/20 bg-[#38b46a]/5 p-3">
                    <div class="text-[10px] font-semibold tracking-wider text-[#8bd9aa]">目前可用的结论</div>
                    <For each={confirmedStatements()}>{(value) => <div class="mt-1">• {value}</div>}</For>
                  </div>
                </Show>
                <div class="grid gap-3 md:grid-cols-2">
                  <div class="rounded-lg bg-[#171e23] p-3">
                    <div class="text-[10px] font-semibold tracking-wider text-[#c5a3ff]">仍需弄清</div>
                    <For each={readableQuestions()}>{(value) => <div class="mt-1">• {value}</div>}</For>
                    <Show when={readableQuestions().length === 0}><div class="mt-1 text-[#71808a]">暂时没有明确的未决问题</div></Show>
                  </div>
                  <div class="rounded-lg bg-[#171e23] p-3">
                    <div class="text-[10px] font-semibold tracking-wider text-[#ffc176]">建议下一步</div>
                    <For each={readableNextActions()}>{(value) => <div class="mt-1">• {value}</div>}</For>
                  </div>
                </div>

              </div>
            </section>
            <section class="rounded-xl border border-[#2a3136] p-4 space-y-3" aria-label="研究接续与验证">
              <Show when={modules.loading}><p role="status" class="text-xs">正在读取研究模块…</p></Show>
              <Show when={modules()?.errors.length}><p role="alert" class="text-xs">部分研究模块读取失败，入口仍保留。{modules()?.errors.join('；')} <button type="button" onClick={() => void refreshModules()}>重试</button></p></Show>
              <div class="flex flex-wrap gap-3">
                <Show when={researchModuleVisible(modules()?.hypotheses?.length)}><button type="button" class="rounded-lg border px-3 py-2 text-sm" onClick={() => setModuleTab('hypotheses')}>研究假说{modules()?.hypotheses ? ` · ${modules()!.hypotheses!.length}` : ''}</button></Show>
                <Show when={researchModuleVisible(modules()?.experiments?.length)}><button type="button" class="rounded-lg border px-3 py-2 text-sm" onClick={() => setModuleTab('experiments')}>实验与分支{modules()?.experiments ? ` · ${modules()!.experiments!.length}` : ''}</button></Show>
                <Show when={modules()?.continuation}>{item => <button type="button" class="rounded-lg border px-3 py-2 text-sm" onClick={() => setModuleTab('continuation')}>持续研究 · {researchStatusLabel(item().status)} · 查看控制与预算</button>}</Show>
              </div>
              <details><summary class="cursor-pointer text-sm">关联会话 · 当前列表 {associatedSessions().length} 个</summary>
                <p class="mt-2 text-xs">关联不代表全部消息已进入研究档案。历史会话可在课题管理及会话历史中查看。</p>
                <For each={associatedSessions()}>{session => <button type="button" class="mt-2 block rounded-lg border px-3 py-2 text-sm" onClick={() => window.dispatchEvent(new CustomEvent('tcode-continue-research', {detail:{researchId:researchId(),workspaceId:workspaceId(),title:selectedProject()?.title,sessionId:session.session_id}}))}>{userFacingTitle(session.title,'未命名会话')} · 打开会话</button>}</For>
                <Show when={!associatedSessions().length}><p class="mt-2 text-xs">当前列表没有关联会话。可点击“继续研究”创建并关联一个新会话。</p></Show>
              </details>
            </section>
            </Show>

            <Show when={props.view === 'advanced'}>
              <Show when={checkpoint()}>{item => <p class="text-xs">概览版本 {item().revision} · 约 {item().token_estimate} 个模型令牌 · {researchStatusLabel(item().budget_status,'预算情况未说明')}</p>}</Show>
                <Show when={episodes().length > 0}>
                  <details class="rounded-lg border border-dashed border-[#29333a] p-3 text-[10px] text-[#596873]">
                    <summary class="cursor-pointer select-none">阶段账本与审计信息（{episodes().length} 段）</summary>
                    <div class="mt-2 space-y-2">
                      <For each={episodes()}>
                        {(episode) => (
                          <div class="rounded bg-[#0d1216] px-2 py-1.5">
                            <span>{formatTime(episode.started_at)} — {formatTime(episode.ended_at)}</span>
                            <span class="ml-2 font-mono">r{episode.revision} · {episode.event_ids.length} events · {episode.boundary_reason}</span>
                          </div>
                        )}
                      </For>
                    </div>
                  </details>
                </Show>
            </Show>
            <Show when={props.view === 'advanced'}>
            <details class="rounded-xl border border-[#5b8cff]/20 bg-[#11171b] p-4">
              <summary class="cursor-pointer select-none text-sm font-semibold text-[#a9c1ff]">高级：测试 Agent 是否会使用这项研究</summary>
              <p class="mt-2 text-[11px] leading-5 text-[#8b99a4]">输入一个示例问题，检查系统会不会带上这项研究的历史。此测试不会调用模型，也不会保存问题原文。</p>
              <div class="mt-3 flex justify-end">
                <button
                  class="rounded-lg border border-[#5b8cff]/35 bg-[#5b8cff]/10 px-2.5 py-1.5 text-[11px] font-semibold text-[#b8caff] disabled:opacity-50"
                  disabled={busy() === 'continuation-preview' || !continuationQuestion().trim()}
                  onClick={() => void previewContinuation()}
                >
                  {busy() === 'continuation-preview' ? '判断中…' : '测试是否使用研究历史'}
                </button>
              </div>
              <textarea
                class="mt-3 min-h-20 w-full resize-y rounded-lg border border-[#303b4a] bg-[#0d1216] px-3 py-2 text-xs leading-5 text-[#d7dee4] outline-none focus:border-[#5b8cff]/60"
                value={continuationQuestion()}
                onInput={(event) => {
                  setContinuationQuestion(event.currentTarget.value);
                  setContinuationPreview(null);
                }}
                placeholder="例如：Docking 的 -8 阈值现在可靠吗？或：给我讲个笑话"
              />
              <Show when={continuationPreview()} keyed>
                {(context) => (
                  <div class="mt-3 rounded-lg border border-[#29333a] bg-[#151b20] p-3 text-[11px]">
                    <div class="flex flex-wrap items-center gap-2">
                      <span class={`rounded-full border px-2 py-0.5 font-semibold ${context.injected ? 'border-[#38b46a]/35 text-[#9ee6bb]' : context.decision === 'block' ? 'border-[#ef4444]/35 text-[#fca5a5]' : 'border-[#64748b]/40 text-[#a7b0b8]'}`}>
                        {context.injected ? '会使用研究历史' : context.decision === 'block' ? '因安全检查暂停' : '不会使用研究历史'}
                      </span>
                      <span class="text-[#8b99a4]">相关度 {Math.round(context.relevance_score * 100)}%</span>
                      <span class="text-[#8b99a4]">{context.token_estimate} / {context.token_budget} 个模型令牌</span>
                      <span class={context.conflict_gate === 'required' ? 'text-[#ffc176]' : 'text-[#71808a]'}>
                        冲突检查：{context.conflict_gate === 'required' ? '需要披露' : context.conflict_gate === 'clear' ? '通过' : researchStatusLabel(context.conflict_gate,'尚未核对')}
                      </span>
                    </div>
                    <div class="mt-2 text-[#8b99a4]">决策原因：<span class="font-mono text-[#bdc8d1]">{researchStatusLabel(context.reason,'当前判断尚无中文说明')}</span></div>
                    <Show when={context.matched_terms.length > 0}>
                      <div class="mt-2 flex flex-wrap gap-1.5">
                        <For each={context.matched_terms}>{(term) => <span class="rounded bg-[#202a36] px-2 py-1 text-[10px] text-[#a9c1ff]">{term}</span>}</For>
                      </div>
                    </Show>
                    <div class="mt-2 grid grid-cols-2 gap-2 text-[10px] text-[#71808a] sm:grid-cols-3">
                      <span>冲突 {Number(context.included_counts.conflicts || 0)}</span>
                      <span>结论 {Number(context.included_counts.findings || 0)}</span>
                      <span>阶段进展 {Number(context.included_counts.episodes || 0)}</span>
                    </div>
                    <Show when={progressMarkdown(context.prompt_text)}>
                      <details class="mt-3">
                        <summary class="cursor-pointer text-[#8ed5ff]">查看实际注入内容</summary>
                        <div class="research-progress-markdown"><MarkdownContent content={progressMarkdown(context.prompt_text)} allowSessionActions={false}/></div>
                      </details>
                    </Show>
                  </div>
                )}
              </Show>
            </details>
            </Show>

            <Show when={!props.view || props.view === 'overview'}>
            <section class="rounded-xl border border-[#2a3136] bg-[#11171b] p-4">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <h3 class="text-sm font-semibold text-[#e6edf3]">阶段进展</h3>
                  <p class="mt-1 text-[11px] text-[#71808a]">按一次完整研究动作整理，不再逐条展示系统事件。</p>
                </div>
                <button disabled={selectedProject()?.status === 'archived' || Boolean(busy())} class="rounded-lg border border-[#303539] px-2.5 py-1.5 text-[11px] text-[#bdc8d1]" onClick={() => void recordFinding()}>记录研究结论</button>
              </div>
              <Show when={projectActivities().length === 0 && episodes().length === 0}><p class="mt-4 text-sm text-[#71808a]">还没有可展示的研究进展。</p></Show>
              <Show when={Math.max(projectActivities().length, episodes().length) > 5}><button type="button" class="mt-3 text-xs" onClick={() => setAllProgress(v => !v)}>{allProgress() ? '只看最近五条' : '查看全部进展'}</button></Show>
              <div class="research-scroll-list mt-3 space-y-3" aria-label="研究进展列表" tabIndex={0}>
                <For each={allProgress() ? projectActivities() : projectActivities().slice(0, 5)}>
                  {(activity) => (
                    <article class="rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                      <div class="flex flex-wrap items-center gap-2">
                        <span class="rounded bg-[#20282e] px-2 py-0.5 text-[9px] text-[#8ed5ff]">{activityTypeLabels[activity.activity_type] || '研究进展'}</span>
                        <h4 class="min-w-0 flex-1 text-sm font-semibold text-[#d7dee4]">{progressMarkdown(readableText(activity.title, 180)) || '未命名研究进展'}</h4>
                        <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#9faeb7]">{activityStatusLabels[activity.status] || '已记录'}</span>
                      </div>
                      <div class="research-progress-markdown mt-2"><MarkdownContent content={progressMarkdown(activity.summary)||activityFallbackSummary(activity)} allowSessionActions={false}/></div>

                      <div class="mt-2 flex flex-wrap gap-3 text-[10px] text-[#71808a]">
                        <span>{formatTime(activity.ended_at)}</span>
                        <Show when={activity.file_refs.length > 0}><span>{activity.file_refs.length} 份资料</span></Show>
                        <Show when={activity.artifact_groups?.length > 0}><span>{activity.artifact_groups.length} 组研究产物</span></Show>
                        <Show when={!activity.artifact_groups?.length && activity.artifact_refs.length > 0}><span>{activity.artifact_refs.length} 项底层产物</span></Show>
                      </div>
                      <div class="mt-3 flex flex-wrap items-center gap-2">
                        <For each={activity.file_refs.slice(0, 4)}>{(file) => <span class="max-w-[260px] truncate rounded bg-[#20282e] px-2 py-1 text-[10px] text-[#8ed5ff]">{file.filename || '研究资料'}</span>}</For>
                        <button disabled={selectedProject()?.status === 'archived' || Boolean(busy())} class="rounded border border-[#303539] px-2 py-1 text-[10px] text-[#bdc8d1]" onClick={() => void recordFinding(activity)}>从本次进展记录结论</button>
                      </div>
                      <details class="mt-3 border-t border-[#263038] pt-2 text-[10px] text-[#596873]">
                        <summary class="cursor-pointer select-none">工具与审计详情</summary>
                        <div class="mt-2 flex flex-wrap gap-3">
                          <span>{activity.semantic_event_ids.length} 条有效记录</span>
                          <span>{activity.tool_calls.length} 次工具调用</span>
                          <span>{(activity.agent_calls || []).length} 次协作任务</span>
                          <span>{activity.control_event_ids.length} 条系统记录已折叠</span>
                        </div>
                        <Show when={activity.tool_calls.length > 0}>
                          <div class="mt-2">使用过：{activity.tool_calls.map((item) => toolDisplayLabel(item.name)).join('、')}</div>
                        </Show>
                        <Show when={(activity.agent_calls || []).length > 0}>
                          <div class="mt-2">
                            协作助手：{(activity.agent_calls || []).map((item) => `${researchAgentLabel(item.name)}（${activityStatusLabels[item.state] || researchStatusLabel(item.state)}）`).join('、')}
                          </div>
                        </Show>
                      </details>
                    </article>
                  )}
                </For>
                <Show when={projectActivities().length === 0 && episodes().length > 0}>
                  <For each={allProgress() ? episodes() : episodes().slice(0, 5)}>
                    {(episode) => (
                      <article class="rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                        <div class="flex flex-wrap items-center gap-2">
                          <h4 class="min-w-0 flex-1 text-sm font-semibold text-[#d7dee4]">{progressMarkdown(readableText(episode.title, 180)) || '阶段研究进展'}</h4>
                          <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#9faeb7]">{outcomeLabels[episode.outcome_status] || '已记录'}</span>
                        </div>
                        <div class="research-progress-markdown mt-2"><MarkdownContent content={progressMarkdown(episode.summary.value)||'完成了一段研究，详细过程已保留。'} allowSessionActions={false}/></div>
                        <div class="mt-2 text-[10px] text-[#71808a]">{formatTime(episode.ended_at)}</div>
                        <button disabled={selectedProject()?.status === 'archived' || Boolean(busy())} class="mt-3 rounded border border-[#303539] px-2 py-1 text-[10px] text-[#bdc8d1]" onClick={() => void recordFinding(episode)}>从本次进展记录结论</button>
                      </article>
                    )}
                  </For>
                </Show>

              </div>
            </section>
            </Show>
          </div>

          <div class="space-y-4">
            <Show when={props.view === 'assets' || props.view === 'advanced'}>
            <section class="rounded-xl border border-[#38bdf8]/25 bg-[#11171b] p-4">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div class="flex flex-wrap items-center gap-2">
                    <h3 class="text-sm font-semibold text-[#8ed5ff]">资料与结论来源</h3>
                    <Show when={knowledge()?.lint} keyed>
                      {(lint) => (
                        <span class={`rounded-full border px-2 py-0.5 text-[10px] ${lint.export_ready ? 'border-[#38b46a]/35 text-[#9ee6bb]' : 'border-[#ef4444]/35 text-[#fca5a5]'}`}>
                          {Number(lint.counts.error || 0) > 0
                            ? `${Number(lint.counts.error || 0)} 项阻断打包`
                            : Number(lint.counts.warning || 0) > 0
                              ? `可以打包 · ${Number(lint.counts.warning || 0)} 项提醒`
                              : '来源关系完整'}
                        </span>
                      )}
                    </Show>
                  </div>
                  <p class="mt-1 text-[11px] leading-5 text-[#8b99a4]">查看研究结论引用了哪些论文、证据和 Wiki 内容，方便核对结论从哪里来。</p>
                </div>
                <button
                  class="rounded-lg border border-[#38bdf8]/35 bg-[#38bdf8]/10 px-2.5 py-1.5 text-[11px] font-semibold text-[#8ed5ff] disabled:opacity-50"
                  disabled={busy() === 'knowledge' || knowledge()?.settings?.knowledge_links_active === false}
                  onClick={() => void refreshKnowledgeChain()}
                >
                  {busy() === 'knowledge'
                    ? '正在自动修复…'
                    : knowledge()?.lint?.issues?.length
                      ? '自动修复来源'
                      : knowledge()?.count ? '刷新来源关系' : '整理来源关系'}
                </button>
              </div>

              <Show when={knowledge()?.settings?.knowledge_links_active === false}>
                <div class="mt-3 rounded-lg border border-[#f1a02b]/25 bg-[#f1a02b]/6 px-3 py-2 text-[11px] text-[#ffc176]">研究知识可信链当前未开启。</div>
              </Show>
              <Show when={knowledge()?.count === 0 && knowledge()?.settings?.knowledge_links_active !== false}>
                <p class="mt-4 text-sm text-[#71808a]">还没有整理来源关系。系统会把研究结论追溯到证据、论文原文片段和 Wiki 内容。</p>
              </Show>
              <Show when={(knowledge()?.count || 0) > 0}>
                <div class="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-5">
                  <For each={['paper', 'passage', 'evidence', 'wiki_page', 'wiki_claim']}>
                    {(kind) => (
                      <div class="rounded-lg border border-[#29333a] bg-[#151b20] px-2 py-2 text-center">
                        <div class="text-sm font-semibold text-[#d7dee4]">{Number(knowledge()?.object_counts?.[kind] || 0)}</div>
                        <div class="mt-0.5 text-[9px] text-[#71808a]">{knowledgeTypeLabels[kind]}</div>
                      </div>
                    )}
                  </For>
                </div>

                <Show when={knowledge()?.lint} keyed>
                  {(lint) => (
                    <div class="mt-3 rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                      <div class="flex flex-wrap items-center justify-between gap-2 text-[10px]">
                        <span class="text-[#9faeb7]">来源完整性检查 · 检查 {lint.checked_link_count} 条关联</span>
                        <span class="text-[#71808a]">{formatTime(lint.completed_at)}</span>
                      </div>
                      <div class="mt-2 flex flex-wrap gap-3 text-[10px]">
                        <span class="text-[#fca5a5]">阻断打包 {Number(lint.counts.error || 0)}</span>
                        <span class="text-[#ffc176]">不阻断提醒 {Number(lint.counts.warning || 0)}</span>
                        <span class="text-[#8b99a4]">信息 {Number(lint.counts.info || 0)}</span>
                      </div>
                      <Show when={knowledgeIssueGroups().length > 0}>
                        <div class="mt-3 space-y-2">
                          <For each={knowledgeIssueGroups()}>
                            {(group) => (
                              <article class={`rounded-lg border px-3 py-2.5 ${group.severity === 'error' ? 'border-[#ef4444]/25 bg-[#ef4444]/8' : 'border-[#f1a02b]/20 bg-[#f1a02b]/7'}`}>
                                <div class="flex flex-wrap items-center gap-2">
                                  <span class={`rounded px-1.5 py-0.5 text-[9px] font-semibold ${group.severity === 'error' ? 'bg-[#ef4444]/15 text-[#fca5a5]' : 'bg-[#f1a02b]/12 text-[#ffc176]'}`}>
                                    {group.severity === 'error' ? '需要处理' : '建议复核'}
                                  </span>
                                  <span class="text-[11px] font-semibold text-[#d7dee4]">{group.title}</span>
                                  <Show when={group.count > 1}><span class="text-[9px] text-[#71808a]">{group.count} 项</span></Show>
                                </div>
                                <p class="mt-1.5 text-[10px] leading-4 text-[#9faeb7]">{group.description}</p>
                                <p class="mt-1.5 text-[10px] leading-4 text-[#d7dee4]">怎么处理：{group.action}</p>
                                <Show when={group.sources.length > 0}>
                                  <details class="mt-2 text-[9px] text-[#71808a]">
                                    <summary class="cursor-pointer select-none">查看涉及的来源{group.count > group.sources.length ? `（显示前 ${group.sources.length} 项）` : ''}</summary>
                                    <div class="mt-1.5 space-y-1">
                                      <For each={group.sources}>{(source) => <div class="break-all rounded bg-[#0d1216] px-2 py-1">{source}</div>}</For>
                                    </div>
                                  </details>
                                </Show>
                              </article>
                            )}
                          </For>
                        </div>
                      </Show>
                    </div>
                  )}
                </Show>

                <div class="mt-3 space-y-2">
                  <For each={(knowledge()?.links || []).slice((knowledgePage() - 1) * 10, knowledgePage() * 10)}>
                    {(link) => (
                      <div class="rounded-lg border border-[#29333a] bg-[#151b20] px-3 py-2">
                        <div class="flex min-w-0 items-center gap-2">
                          <span class="shrink-0 rounded bg-[#20282e] px-1.5 py-0.5 text-[9px] text-[#8ed5ff]">{knowledgeTypeLabels[link.object_type] || '来源资料'}</span>
                          <span class="min-w-0 flex-1 truncate text-[11px] text-[#bdc8d1]">{researchSourceLabel(link)}</span>
                          <span class="shrink-0 text-[9px] text-[#71808a]">
                            {researchRelationLabel(link.relation)}{link.side === 'left' ? ' · 一方' : link.side === 'right' ? ' · 另一方' : ''}
                          </span>
                        </div>
                        <div class="mt-1 flex items-center justify-between gap-2 font-mono text-[9px] text-[#596873]">
                          <div class="flex shrink-0 items-center gap-2">
                            <span>版本 {link.revision} · {link.state === 'active' ? '有效' : '待复核'}</span>
                            <Show when={link.discovered_by === 'user'}>
                              <button class="text-[#fca5a5] hover:underline" onClick={() => void removeKnowledgeLink(link.link_id)}>移除</button>
                            </Show>
                          </div>
                        </div>
                      </div>
                    )}
                  </For>
                  <Show when={(knowledge()?.links.length || 0) > 10}><nav class="flex flex-wrap gap-3 text-xs" aria-label="来源关系分页">
                    <button type="button" disabled={knowledgePage() <= 1} onClick={() => setKnowledgePage(value => value - 1)}>上一页来源</button>
                    <span>第 {knowledgePage()} / {Math.ceil((knowledge()?.links.length || 0) / 10)} 页 · 每页 10 条</span>
                    <button type="button" disabled={knowledgePage() * 10 >= (knowledge()?.links.length || 0)} onClick={() => setKnowledgePage(value => value + 1)}>下一页来源</button>
                  </nav></Show>
                </div>
              </Show>
            </section>
            </Show>

            <Show when={props.view === 'review'}>
            <section class="rounded-xl border border-[#f1a02b]/25 bg-[#11171b] p-4">
              <h3 class="text-sm font-semibold text-[#ffc176]">待解决的结论冲突</h3>
              <p class="mt-1 text-[11px] text-[#8b99a4]">新旧结论互相矛盾时，两边都会保留，系统不会擅自替你选择。</p>
              <Show when={conflicts().length === 0}><p class="mt-4 text-sm text-[#71808a]">当前没有研究冲突。</p></Show>
              <div class="research-scroll-list mt-3 space-y-3" aria-label="研究冲突列表" tabIndex={0}>
                <Show when={conflicts().some(item => item.status === 'resolved')}><button type="button" onClick={() => setConflictHistory(value => !value)}>{conflictHistory() ? '收起已解决冲突' : '查看已解决冲突历史'}</button></Show>
                <For each={conflicts().filter(item => conflictHistory() || item.status !== 'resolved')}>
                  {(conflict) => (
                    <article class={`rounded-lg border p-3 ${conflict.status === 'resolved' ? 'border-[#2a3136] bg-[#151b20]' : 'border-[#f1a02b]/30 bg-[#f1a02b]/6'}`}>
                      <div class="flex items-center gap-2">
                        <span class="text-xs font-semibold text-[#e6edf3]">{researchStatusLabel(conflict.conflict_type,'结论或来源存在分歧')}</span>
                        <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#ffc176]">{researchStatusLabel(conflict.status)}</span>
                      </div>
                      <p class="mt-2 text-xs leading-5 text-[#bdc8d1]">{conflict.summary}</p>
                      <div class="mt-3 flex flex-wrap gap-2">
                        <Show when={conflict.status === 'open'}><button class="rounded border border-[#303539] px-2 py-1 text-[10px] text-[#bdc8d1]" onClick={() => void reviewConflict(conflict)}>开始审核</button></Show>
                        <Show when={conflict.status !== 'resolved'}><button class="rounded border border-[#38b46a]/30 px-2 py-1 text-[10px] text-[#9ee6bb]" onClick={() => beginEvidenceAction('resolve', conflict.conflict_id)}>解决</button></Show>
                        <Show when={conflict.status === 'resolved'}><button class="rounded border border-[#f1a02b]/30 px-2 py-1 text-[10px] text-[#ffc176]" onClick={() => beginEvidenceAction('reopen', conflict.conflict_id)}>重新打开</button></Show>
                      </div>
                    </article>
                  )}
                </For>
              </div>
            </section>
            </Show>

            <Show when={!props.view || props.view === 'overview' || props.view === 'review'}>
            <section class="rounded-xl border border-[#2a3136] bg-[#11171b] p-4">
              <h3 class="text-sm font-semibold text-[#e6edf3]">研究结论与修正记录</h3>
              <Show when={findings().length === 0}><p class="mt-4 text-sm text-[#71808a]">还没有正式记录的研究结论。阶段摘要不会自动冒充已确认事实。</p></Show>
              <div class="research-scroll-list mt-3 space-y-3" aria-label="研究结论列表" tabIndex={0}>
                <For each={findings()}>
                  {(finding) => (
                    <article class="rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                      <div class="flex flex-wrap items-center gap-2">
                        <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#9faeb7]">{findingLabels[finding.status] || researchStatusLabel(finding.status)}</span>
                        <details class="text-[10px] text-[#596873]"><summary class="cursor-pointer">来源与版本</summary><div class="mt-1 font-mono">{researchStatusLabel(finding.provenance,'来源类型未说明')} · 第 {finding.current_revision} 版</div></details>
                      </div>
                      <p class="mt-2 text-xs leading-5 text-[#d7dee4]">{finding.statement}</p>
                      <div class="mt-3 flex flex-wrap gap-2">
                        <button disabled={selectedProject()?.status === 'archived'} class="rounded border border-[#ef4444]/25 px-2 py-1 text-[10px] text-[#fca5a5]" onClick={() => beginEvidenceAction('challenge', finding.finding_id)}>用新证据挑战</button>
                      </div>
                    </article>
                  )}
                </For>
              </div>
            </section>
            </Show>
          </div>
        </div>
      </Show>
      </div>
      <Show when={moduleTab()}>{active => <ResearchDialog title={active() === 'hypotheses' ? '研究假说' : active() === 'experiments' ? '实验与分支' : '持续研究设置'} onClose={() => {setModuleTab(undefined); void refreshModules();}}>
        <ResearchSessionPanel tab={active()} researchId={researchId()} workspaceId={workspaceId()} readOnly={selectedProject()?.status === 'archived'} onNavigateTab={setModuleTab} onChanged={() => {void refreshModules();}} onOpenStructure={() => setModuleTab(undefined)} />
      </ResearchDialog>}</Show>
      <Show when={evidenceAction()}>{action => <ResearchDialog title={action().kind === 'create' ? '记录研究结论' : action().kind === 'challenge' ? '用新记录挑战结论' : action().kind === 'resolve' ? '核对并解决冲突' : '重新打开冲突'} busy={Boolean(busy())} onClose={() => setEvidenceAction(undefined)}>
        <p class="text-sm">选择作为依据的研究记录，并说明你的判断。系统会保留准确来源，原有记录不会被覆盖。</p>
        <Show when={!sourceRecords().length}><p class="mt-4" role="status">当前课题没有可选的来源记录，请先将相关研究活动纳入课题。</p></Show>
        <label class="mt-4 block text-sm">依据记录<select class="mt-2 block w-full rounded border p-2" value={evidenceSource()} onChange={e => setEvidenceSource(e.currentTarget.value)}>
          <option value="">请选择研究记录</option><For each={sourceRecords()}>{a => <option value={a.activity_id}>{readableText(a.title, 140) || '研究记录'} · {formatTime(a.ended_at)}</option>}</For>
        </select></label>
        <Show when={evidenceSource()}><p class="mt-2 text-xs">{readableText(sourceRecords().find(a => a.activity_id === evidenceSource())?.summary)}</p></Show>
        <Show when={action().kind === 'resolve'}><label class="mt-4 block text-sm">核对结果<select class="mt-2 block w-full rounded border p-2" value={resolution()} onChange={e => setResolution(e.currentTarget.value)}>
          <option value="inconclusive">现有依据不足，暂不定论</option><option value="original_upheld">原结论获得支持</option><option value="challenger_upheld">新结论获得支持</option><option value="context_dependent">分别在不同条件下有效</option><option value="invalidated">两项结论均失效</option>
        </select></label></Show>
        <label class="mt-4 block text-sm">{action().kind === 'create' ? '研究结论' : action().kind === 'challenge' ? '新结论' : '判断理由'}<textarea class="mt-2 block min-h-32 w-full rounded border p-2" maxLength={2000} value={evidenceText()} onInput={e => setEvidenceText(e.currentTarget.value)} /></label>
        <Show when={error()}><p class="mt-3" role="alert">{error()}</p></Show>
        <button type="button" class="mt-4 rounded border px-4 py-2" disabled={Boolean(busy()) || !evidenceText().trim() || !evidenceSource()} onClick={() => void submitEvidenceAction()}>保存处理结果</button>
      </ResearchDialog>}</Show>
    </div>
  );
}
