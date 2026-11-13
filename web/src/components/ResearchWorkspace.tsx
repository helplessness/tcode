import { researchStatusLabel, researchFieldLabel, researchValueText, userFacingTitle, toolDisplayLabel, researchAgentLabel } from '../utils/userFacingLabels';
import { promptAction } from './FeedbackDialog';
import { confirmAction, showFeedback } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, onMount } from 'solid-js';
import { dismissDetailsOnOutsideClick } from '../utils/dismissDetails';
import { openCardDetail } from '../utils/cardTransition';
import TabIndicator from './TabIndicator';
import {
  confirmResearchCandidate,
  createResearchReproduction,
  deleteResearchProject,
  removeResearchPackListing,
  dismissResearchCandidate,
  excludeResearchActivity,
  excludeResearchEvent,
  getResearchCandidate,
  getResearchCenter,
  getResearchHealth,
  getResearchProjectDeletionPreview,
  importResearchPack,
  includeResearchActivity,
  includeResearchEvent,
  inspectResearchPackImport,
  listResearchCandidates,
  listResearchActivities,
  listResearchPackAssets,
  listResearchPackImports,
  mergeResearchCandidates,
  moveResearchEvent,
  renameResearchCandidate,
  runTopicDiscovery,
  setResearchProjectStatus,
  type ResearchCandidate,
  type ResearchActivity,
  type ResearchEvent,
  type ResearchHealth,
  type ResearchPackImport,
  type ResearchPackAsset,
  type ResearchProject,
} from '../api/research';
import MarkdownContent from './MarkdownContent';
import {readableResearchMarkdown} from '../utils/researchMarkdown';
import ResearchStatePanel from './ResearchStatePanel';
import ResearchChangesPanel from './ResearchChangesPanel';
import HypothesisEvidenceMap from './HypothesisEvidenceMap';
import type {HypothesisFocus} from '../api/hypothesisMap';
import type { ResearchChangeFocus } from '../api/researchChanges';
import ResearchDialog from './ResearchDialog';
import ResearchArtifactGroupCard from './ResearchArtifactGroupCard';
import ImportedResearchPackCard from './ImportedResearchPackCard';
import ResearchPackShareDialog, { type ResearchPackShareTarget } from './ResearchPackShareDialog';
import { buildExpandablePreview } from '../utils/expandablePreview';
import { sessionStore } from '../stores/session';

type Props = {
  workspaceId?: string;
  active?: boolean;
  focusRequest?: {id: string; key: number; project?: ResearchProject; comparison?:ResearchChangeFocus;hypothesis?:HypothesisFocus};
};

type CandidateDetail = {
  candidate: ResearchCandidate;
  events: ResearchEvent[];
  event_count: number;
};

type EventCategory = 'message' | 'tool' | 'mcp' | 'artifact' | 'workflow' | 'agent' | 'file' | 'other';
type EventCategoryFilter = EventCategory | 'all';
const PAGE_SIZE = 10;
const eventCategories: Array<{ key: EventCategory; label: string }> = [
  { key: 'message', label: '对话' },
  { key: 'tool', label: '工具' },
  { key: 'mcp', label: 'MCP' },
  { key: 'artifact', label: '研究产物' },
  { key: 'workflow', label: '工作流' },
  { key: 'agent', label: '子 Agent' },
  { key: 'file', label: '文件' },
  { key: 'other', label: '其他' },
];

const eventCategory = (event: ResearchEvent): EventCategory => {
  const type = String(event.event_type || '').toLowerCase();
  const kind = String(event.source_kind || '').toLowerCase();
  if (type.startsWith('message.') || kind === 'session_message') return 'message';
  if (type.startsWith('mcp.') || kind === 'mcp_tool') return 'mcp';
  if (type.startsWith('workflow.') || kind === 'workflow') return 'workflow';
  if (type.startsWith('agent.') || kind === 'subagent') return 'agent';
  if (
    type.startsWith('artifact.')
    || type.startsWith('document.')
    || ['artifact', 'tool_artifact', 'document'].includes(kind)
  ) return 'artifact';
  if (type.startsWith('file.') || ['file_upload', 'file_change'].includes(kind)) return 'file';
  if (type.startsWith('tool.') || kind === 'tool_call') return 'tool';
  return 'other';
};

const categoryLabel = (category: EventCategory) => (
  eventCategories.find((item) => item.key === category)?.label || '其他'
);

const readablePayloadText = (event: ResearchEvent) => {
  const payload = event.payload || {};
  const value = payload.content_preview
    || payload.output_preview
    || payload.error_preview
    || payload.paper_title
    || payload.document_title
    || payload.artifact_title
    || payload.display_name
    || payload.summary_text
    || payload.summary
    || payload.statement
    || payload.description
    || payload.task
    || payload.filename;
  return String(value || '').trim();
};

const isTechnicalAuditEvent = (event: ResearchEvent) => {
  const type = String(event.event_type || '').toLowerCase();
  const category = eventCategory(event);
  const readable = readablePayloadText(event);
  if (
    ['session.created', 'session.archived', 'session.resumed', 'turn.started', 'turn.completed', 'turn.failed', 'turn.cancelled', 'message.compacted'].includes(type)
    || type.endsWith('.started')
    || type === 'agent.spawned'
  ) return true;
  if (type === 'message.created' && readable.startsWith('# Task Contract')) return true;
  if (type === 'message.created' && !readable) return true;
  if (category === 'artifact') {
    const title = String(event.payload?.title || '').trim().toLowerCase();
    const hasReadableTitle = Boolean(title && !['artifact', 'file', 'output', 'result', 'task', 'tool artifact'].includes(title));
    return !readable && !hasReadableTitle;
  }
  if (['tool', 'mcp', 'workflow', 'agent'].includes(category)) return !readable;
  return false;
};

const eventDisplayTitle = (event: ResearchEvent) => {
  const category = eventCategory(event);
  const type = String(event.event_type || '').toLowerCase();
  if (category === 'message') return event.actor_type === 'user' ? '用户研究请求' : 'Agent 研究记录';
  if (category === 'file') return type === 'file.uploaded' ? '上传研究文件' : '更新研究文件';
  if (category === 'artifact') return '生成研究产物';
  if (category === 'agent') return '子 Agent 研究过程';
  if (category === 'workflow') return '研究工作流结果';
  if (category === 'mcp') return type.endsWith('failed') ? 'MCP 执行失败' : 'MCP 执行结果';
  if (category === 'tool') return type.endsWith('failed') ? '工具执行失败' : '工具执行结果';
  return '研究记录';
};

const actorLabel = (actor: string) => ({
  user: '用户',
  agent: 'Agent',
  tool: '工具',
  system: '系统',
}[String(actor || '').toLowerCase()] || '来源类型未说明');

const categoryCounts = (events: ResearchEvent[]) => {
  const counts: Partial<Record<EventCategory, number>> = {};
  for (const event of events) {
    const category = eventCategory(event);
    counts[category] = (counts[category] || 0) + 1;
  }
  return counts;
};

const sortByCategory = <T extends { event: ResearchEvent }>(rows: T[]) => {
  const order = new Map(eventCategories.map((item, index) => [item.key, index]));
  return [...rows].sort((left, right) => {
    const categoryOrder = (order.get(eventCategory(left.event)) || 0) - (order.get(eventCategory(right.event)) || 0);
    if (categoryOrder !== 0) return categoryOrder;
    return Date.parse(right.event.occurred_at) - Date.parse(left.event.occurred_at);
  });
};

const groupedRows = <T extends { event: ResearchEvent }>(rows: T[]) => eventCategories
  .map((category) => ({
    ...category,
    rows: rows.filter((row) => eventCategory(row.event) === category.key),
  }))
  .filter((group) => group.rows.length > 0);

const activityCategoryCounts = (activities: ResearchActivity[]) => {
  const counts: Partial<Record<EventCategory, number>> = {};
  for (const activity of activities) {
    for (const category of eventCategories) {
      if (Number(activity.category_counts?.[category.key] || 0) > 0) {
        counts[category.key] = (counts[category.key] || 0) + 1;
      }
    }
  }
  return counts;
};

const activityStatusLabels: Record<string, string> = {
  observed: '已记录',
  running: '进行中',
  waiting: '等待用户输入',
  completed: '已完成',
  partial: '部分完成',
  failed: '执行失败',
  incomplete: '记录不完整',
  cancelled: '已取消',
};

const activityTypeLabels: Record<string, string> = {
  paper_ingest: '论文解析与入库',
  paper_research: '论文研究',
  file_research: '文件研究',
  subagent_task: '子 Agent 研究',
  tool_run: '工具研究',
  research_conversation: '研究讨论',
};

const recommendationText = (activity: ResearchActivity) => {
  if (activity.reason_code === 'incomplete_tool_record') {
    return '工具开始后没有收到完成记录，请先核对执行结果。';
  }
  if (activity.reason_code === 'single_existing_research_target') {
    return '这组动作只指向一个现有课题，建议整体纳入。';
  }
  if (activity.reason_code === 'user_file_research_activity') {
    return '用户请求和文件来自同一次研究动作，建议整体纳入。';
  }
  return '系统无法唯一判断归属，请核对后选择课题。';
};

type ActivityAgentRun = ResearchActivity['agent_runs'][number];

const executionStateClass = (state: string) => (
  state === 'failed'
    ? 'text-[#fca5a5]'
    : ['running', 'partial', 'waiting'].includes(state)
      ? 'text-[#ffc176]'
      : 'text-[#9ee6bb]'
);

function AgentRunNode(props: {
  activity: ResearchActivity;
  run: ActivityAgentRun;
  depth?: number;
}) {
  const childRuns = () => (props.activity.agent_runs || []).filter(
    (item) => item.parent_agent_run_id === props.run.agent_run_id,
  );
  const tools = () => (props.activity.tool_calls || []).filter(
    (item) => item.producer_agent_run_id === props.run.agent_run_id,
  );
  const workflows = () => (props.activity.workflow_calls || []).filter(
    (item) => item.producer_agent_run_id === props.run.agent_run_id,
  );
  const files = () => (props.activity.file_refs || []).filter(
    (item) => (item.producer_agent_run_ids || []).includes(props.run.agent_run_id),
  );
  const artifacts = () => (props.activity.artifact_refs || []).filter(
    (item) => item.producer_agent_run_id === props.run.agent_run_id,
  );
  const producedCount = () => files().length + artifacts().length;

  return (
    <div class={`${(props.depth || 0) > 0 ? 'ml-3 border-l border-[#33404a] pl-3' : ''}`}>
      <div class="rounded-lg border border-[#2b363e] bg-[#11171b] px-3 py-2.5">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex min-w-0 items-center gap-2">
            <span class={`rounded px-1.5 py-0.5 text-[9px] font-semibold ${props.run.is_root ? 'bg-[#38bdf8]/12 text-[#8ed5ff]' : 'bg-[#a78bfa]/12 text-[#c4b5fd]'}`}>
              {props.run.is_root ? '主研究助手' : '协作助手'}
            </span>
            <span class="truncate text-[11px] font-medium text-[#d7dee4]">
              {researchAgentLabel(props.run.name)}
            </span>
          </div>
          <span class={`text-[10px] ${executionStateClass(props.run.state)}`}>
            {activityStatusLabels[props.run.state] || researchStatusLabel(props.run.state)}
          </span>
        </div>
        <Show when={props.run.task}>
          <div class="mt-1.5 line-clamp-3 text-[11px] leading-5 text-[#8b99a4]">{props.run.task}</div>
        </Show>
        <Show when={props.run.result_summary}>
          <div class="mt-1.5 line-clamp-3 rounded border border-[#38b46a]/15 bg-[#38b46a]/5 px-2 py-1.5 text-[10px] leading-4 text-[#9ee6bb]">
            交接结果 · {props.run.result_summary}
          </div>
        </Show>
        <div class="mt-2 flex flex-wrap gap-1.5 text-[9px] text-[#71808a]">
          <Show when={tools().length > 0}><span>工具 {tools().length}</span></Show>
          <Show when={workflows().length > 0}><span>工作流 {workflows().length}</span></Show>
          <Show when={producedCount() > 0}><span>文件/产物 {producedCount()}</span></Show>
          <Show when={childRuns().length > 0}><span>委派 {childRuns().length}</span></Show>
          <Show when={(props.run.completed_work || []).length > 0}><span>完成步骤 {props.run.completed_work.length}</span></Show>
          <Show when={(props.run.failed_work || []).length > 0}><span class="text-[#fca5a5]">失败步骤 {props.run.failed_work.length}</span></Show>
          <Show when={props.run.recovery_required}><span class="text-[#ffc176]">需要接管/恢复</span></Show>
        </div>
        <Show when={props.run.error}>
          <div class="mt-2 rounded border border-[#ef4444]/20 bg-[#ef4444]/5 px-2 py-1.5 text-[10px] text-[#fca5a5]">
            {props.run.error}
          </div>
        </Show>
        <Show when={tools().length > 0 || workflows().length > 0 || producedCount() > 0}>
          <details class="mt-2 border-t border-[#252d32] pt-2">
            <summary class="cursor-pointer text-[10px] text-[#8ed5ff]">查看该 Agent 的具体工作</summary>
            <div class="mt-2 space-y-1.5">
              <For each={tools()}>
                {(tool) => (
                  <div class="flex items-center justify-between gap-2 text-[10px]">
                    <span class="truncate text-[#bdc8d1]">操作 · {toolDisplayLabel(tool.name)}</span>
                    <span class={executionStateClass(tool.state)}>{activityStatusLabels[tool.state] || researchStatusLabel(tool.state)}</span>
                  </div>
                )}
              </For>
              <For each={workflows()}>
                {(workflow) => (
                  <div class="rounded border border-[#2b363e] px-2 py-1.5 text-[10px]">
                    <div class="flex items-center justify-between gap-2">
                      <span class="truncate text-[#c4b5fd]">工作流运行</span>
                      <span class={executionStateClass(workflow.state)}>{activityStatusLabels[workflow.state] || researchStatusLabel(workflow.state)}</span>
                    </div>
                    <Show when={workflow.stages?.length > 0}>
                      <div class="mt-1 text-[#71808a]">
                        {workflow.stages.map((stage) => userFacingTitle(stage.name,'未命名步骤')).join(' → ')}
                      </div>
                    </Show>
                  </div>
                )}
              </For>
              <For each={files()}>
                {(file) => <div class="truncate text-[10px] text-[#8b99a4]">文件 · {file.filename || file.path}</div>}
              </For>
              <For each={artifacts()}>
                {(artifact) => <div class="truncate text-[10px] text-[#8b99a4]">产物 · {artifact.ref.split('/').pop() || artifact.ref}</div>}
              </For>
            </div>
          </details>
        </Show>
      </div>
      <Show when={childRuns().length > 0}>
        <div class="mt-2 space-y-2">
          <For each={childRuns()}>
            {(child) => <AgentRunNode activity={props.activity} run={child} depth={(props.depth || 0) + 1} />}
          </For>
        </div>
      </Show>
    </div>
  );
}

function CategoryFilters(props: {
  active: EventCategoryFilter;
  counts: Partial<Record<EventCategory, number>>;
  total: number;
  onSelect: (category: EventCategoryFilter) => void;
}) {
  return (
    <div class="flex flex-wrap gap-2">
      <For each={[{ key: 'all' as const, label: '全部', count: props.total }, ...eventCategories.map((item) => ({
        ...item,
        count: props.counts[item.key] || 0,
      }))]}>
        {(item) => (
          <button
            class={`rounded-full border px-2.5 py-1 text-[11px] ${props.active === item.key ? 'border-[#38bdf8]/45 bg-[#38bdf8]/12 text-[#8ed5ff]' : 'border-[#30383e] text-[#8b99a4]'}`}
            onClick={() => props.onSelect(item.key)}
          >
            {item.label} {item.count}
          </button>
        )}
      </For>
    </div>
  );
}

function PaginationControls(props: {
  page: number;
  pages: number;
  total: number;
  onPage: (page: number) => void;
}) {
  return (
    <Show when={props.total > PAGE_SIZE}>
      <div class="flex items-center justify-between gap-3 border-t border-[#252d32] pt-3 text-xs text-[#71808a]">
        <span>共 {props.total} 条 · 每页 {PAGE_SIZE} 条</span>
        <div class="flex items-center gap-2">
          <button
            class="rounded-lg border border-[#30383e] px-2.5 py-1 text-[#bdc8d1] disabled:opacity-35"
            disabled={props.page <= 1}
            onClick={() => props.onPage(props.page - 1)}
          >
            上一页
          </button>
          <span>{props.page} / {props.pages}</span>
          <button
            class="rounded-lg border border-[#30383e] px-2.5 py-1 text-[#bdc8d1] disabled:opacity-35"
            disabled={props.page >= props.pages}
            onClick={() => props.onPage(props.page + 1)}
          >
            下一页
          </button>
        </div>
      </div>
    </Show>
  );
}

const stateLabels: Record<string, string> = {
  emerging: '形成中',
  stable: '较稳定',
  confirmed: '已确认',
  merged: '已合并',
  split: '已拆分',
  dismissed: '已忽略',
};

const eventContent = (event: ResearchEvent) => {
  const payload = event.payload || {};
  if (eventCategory(event) === 'agent') {
    const name = researchAgentLabel(payload.subagent_name || event.actor_id);
    const state = String(payload.status || event.event_type.split('.').pop() || '').trim();
    const task = String(payload.task || payload.error_preview || payload.blocker || '').trim();
    return `${name}${state ? ` · ${activityStatusLabels[state] || researchStatusLabel(state)}` : ''}${task ? `：${task}` : ''}`;
  }
  const value = readablePayloadText(event)
    || payload.title
    || (eventCategory(event) === 'file' ? event.source_ref : '')
    || eventDisplayTitle(event);
  return String(value || '').trim();
};

const formatTime = (value?: string) => {
  const timestamp = Date.parse(String(value || ''));
  if (!Number.isFinite(timestamp)) return String(value || '');
  return new Date(timestamp).toLocaleString();
};

const formatBytes = (value?: number) => {
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

export default function ResearchWorkspace(props: Props) {
  let moreMenu:HTMLDetailsElement|undefined;
  onMount(()=>onCleanup(dismissDetailsOnOutsideClick(()=>moreMenu)));
  const [tab, setTab] = createSignal<'candidates' | 'pending' | 'projects' | 'import' | 'upload' | 'state' | 'review' | 'advanced' | 'history' | 'changes' | 'evidence'>('projects');
  const [returnTab, setReturnTab] = createSignal<'projects' | 'import' | 'review'>('projects');
  createEffect(on(tab, (next, previous) => {
    if (previous && ['projects', 'import', 'review'].includes(previous) && !['projects', 'import', 'review'].includes(next)) {
      setReturnTab(previous as 'projects' | 'import' | 'review');
    }
  }));
  const [stateView, setStateView] = createSignal<'overview' | 'assets' | 'review' | 'advanced'>('advanced');
  const [openedAsset, setOpenedAsset] = createSignal<ResearchPackAsset | null>(null);
  const [health, setHealth] = createSignal<ResearchHealth | null>(null);
  const [candidates, setCandidates] = createSignal<ResearchCandidate[]>([]);
  const [projects, setProjects] = createSignal<ResearchProject[]>([]);
  const [pendingItems, setPendingItems] = createSignal<ResearchActivity[]>([]);
  const [allActivities, setAllActivities] = createSignal<ResearchActivity[]>([]);
  const [packImports, setPackImports] = createSignal<ResearchPackImport[]>([]);
  const [packAssets, setPackAssets] = createSignal<ResearchPackAsset[]>([]);
  const [removedAssets, setRemovedAssets] = createSignal<ResearchPackAsset[]>([]);
  const [removedImports, setRemovedImports] = createSignal<ResearchPackImport[]>([]);
  type ListingKey = 'asset' | 'import' | 'removed-asset' | 'removed-import';
  const [listingOffsets, setListingOffsets] = createSignal<Record<ListingKey, number>>({'asset':0,'import':0,'removed-asset':0,'removed-import':0});
  const [listingNext, setListingNext] = createSignal<Partial<Record<ListingKey, number | null>>>({});
  const pageListing = async (kind: 'asset' | 'import', removed: boolean, offset: number) => {
    if (busy()) return;
    const target = workspaceId(), key: ListingKey = removed ? `removed-${kind}` : kind;
    setBusy('listing-page'); setError('');
    try {
      if (kind === 'asset') {
        const result = await listResearchPackAssets(target, removed, offset);
        if (target !== workspaceId()) return;
        (removed ? setRemovedAssets : setPackAssets)(result.assets);
        setListingNext(previous => ({...previous, [key]:result.next_offset}));
      } else {
        const result = await listResearchPackImports(target, removed, offset);
        if (target !== workspaceId()) return;
        (removed ? setRemovedImports : setPackImports)(result.imports);
        setListingNext(previous => ({...previous, [key]:result.next_offset}));
      }
      setListingOffsets(previous => ({...previous, [key]:offset}));
    } catch (e) {if (target === workspaceId()) setError(e instanceof Error ? e.message : '读取列表失败');}
    finally {setBusy('');}
  };
  const listingPager = (kind: 'asset' | 'import', removed = false) => {
    const key: ListingKey = removed ? `removed-${kind}` : kind;
    return <div class="mt-3 flex items-center gap-3 text-xs">
      <button type="button" disabled={Boolean(busy()) || !listingOffsets()[key]} onClick={() => void pageListing(kind, removed, Math.max(0, listingOffsets()[key] - 50))}>上一页</button>
      <span>第 {Math.floor(listingOffsets()[key] / 50) + 1} 页 · 每页最多 50 条</span>
      <button type="button" disabled={Boolean(busy()) || listingNext()[key] == null} onClick={() => void pageListing(kind, removed, listingNext()[key]!)}>下一页</button>
    </div>;
  };
  const loadRemoved = async () => {
    const target = workspaceId();
    try {
      const [assets, imports] = await Promise.all([listResearchPackAssets(target, true, listingOffsets()['removed-asset']), listResearchPackImports(target, true, listingOffsets()['removed-import'])]);
      if (target === workspaceId()) {setRemovedAssets(assets.assets); setRemovedImports(imports.imports); setListingNext(previous => ({...previous, 'removed-asset':assets.next_offset,'removed-import':imports.next_offset}));}
    } catch (e) {if (target === workspaceId()) setError(e instanceof Error ? e.message : '读取已删除记录失败');}
  };
  const removeListing = async (id: string, kind: 'asset' | 'import', restore = false) => {
    if (busy()) return;
    if (!restore && !await confirmAction(kind === 'asset'
      ? '移除这份已导入资料？资料会移到“更多 → 高级诊断 → 已移除资料与记录”，可恢复。已生成的本地页面、画布和后续研究记录会保留。'
      : '移除这条导入记录？记录可在“更多 → 高级诊断”恢复。已接收的研究资料不会被删除。')) return;
    const target = workspaceId();
    setBusy('pack-listing'); setError('');
    try {
      await removeResearchPackListing(id, kind, restore, target);
      if (target !== workspaceId()) return;
      if (kind === 'import' && packInspection()?.import_id === id) setPackInspection(null);
      setOpenedAsset(null); setNotice(restore ? '已恢复。' : '已移除，可在更多菜单的高级诊断中恢复。');
      setListingOffsets({'asset':0,'import':0,'removed-asset':0,'removed-import':0});
      await load(); await loadRemoved();
    } catch (e) {if (target === workspaceId()) setError(e instanceof Error ? e.message : '操作失败');}
    finally {setBusy('');}
  };
  const [packInspection, setPackInspection] = createSignal<ResearchPackImport | null>(null);
  const [packImportEnabled, setPackImportEnabled] = createSignal(false);
  const [packImportTitle, setPackImportTitle] = createSignal('');
  const [selectedCandidateId, setSelectedCandidateId] = createSignal('');
  const [selectedProjectId, setSelectedProjectId] = createSignal('');
  let appliedSearchKey: number | undefined;
  let pendingSearchProject: ResearchProject | undefined;
  createEffect(() => {
    const request = props.focusRequest;
    if (!request) {pendingSearchProject = undefined;return;}
    if (props.active === false || request.key === appliedSearchKey) return;
    pendingSearchProject = request.project;
    if (request.project) setProjects(items => items.some(item=>item.research_id===request.id) ? items : [...items, request.project!]);
    setSelectedProjectId(request.id); setStateView('overview'); setTab(request.hypothesis?'evidence':request.comparison?'changes':'state');
    appliedSearchKey = request.key;
  });
  const [provenanceProjectId, setProvenanceProjectId] = createSignal('');
  const [candidateDetail, setCandidateDetail] = createSignal<CandidateDetail | null>(null);
  const [mergeSelected, setMergeSelected] = createSignal<Set<string>>(new Set());
  const [eventTargets, setEventTargets] = createSignal<Record<string, string>>({});
  const [loading, setLoading] = createSignal(false);
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [sourceCategory, setSourceCategory] = createSignal<EventCategoryFilter>('all');
  const [sourcePage, setSourcePage] = createSignal(1);
  const [pendingCategory, setPendingCategory] = createSignal<EventCategoryFilter>('all');
  const [pendingPage, setPendingPage] = createSignal(1);
  const [shareTarget, setShareTarget] = createSignal<ResearchPackShareTarget | null>(null);
  const [copiedEventId, setCopiedEventId] = createSignal('');
  let copiedResetTimer: ReturnType<typeof setTimeout> | undefined;

  onCleanup(() => {
    if (copiedResetTimer) clearTimeout(copiedResetTimer);
  });

  const workspaceId = () => String(props.workspaceId || '').trim();
  const candidateLimit = createMemo(() => Number(health()?.settings?.topic_max_candidates || 3));
  const projectLimit = createMemo(() => 1);
  const activeProjects = createMemo(() => projects().filter((project) => project.status !== 'archived'));
  const archivedProjects = createMemo(() => projects().filter((project) => project.status === 'archived'));
  const candidatePool = createMemo(() => candidates()
    .filter((candidate) => ['emerging', 'stable'].includes(candidate.state))
    .slice(0, candidateLimit()));
  const historicalCandidates = createMemo(() => candidates().filter(
    (candidate) => ['merged', 'split', 'dismissed'].includes(candidate.state),
  ));
  const assignableCandidates = createMemo(() => candidates().filter(
    (candidate) => ['emerging', 'stable', 'confirmed'].includes(candidate.state),
  ));
  const topicsEnabled = createMemo(() => Boolean(health()?.settings?.topics_active));
  const readableSourceEvents = createMemo(() => (
    (candidateDetail()?.events || []).filter((event) => !isTechnicalAuditEvent(event))
  ));
  const technicalSourceEvents = createMemo(() => (
    (candidateDetail()?.events || []).filter((event) => isTechnicalAuditEvent(event))
  ));
  const candidateArtifactGroups = createMemo(() => {
    const candidateId = selectedCandidateId();
    const researchId = candidateDetail()?.candidate.confirmed_research_id || '';
    const groups = allActivities()
      .filter((activity) => (
        activity.candidate_id === candidateId
        || Boolean(researchId && activity.research_id === researchId)
      ))
      .flatMap((activity) => activity.artifact_groups || []);
    const unique = new Map(groups.map((group) => [group.group_id, group]));
    return [...unique.values()].sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at));
  });
  const candidateAgentCount = (candidateId: string) => {
    const identities = new Set<string>();
    for (const activity of allActivities()) {
      if (activity.candidate_id !== candidateId) continue;
      for (const call of activity.agent_calls || []) {
        identities.add(String(call.session_id || call.assignment_id || call.name || '').trim());
      }
    }
    identities.delete('');
    return identities.size;
  };
  const sourceCounts = createMemo(() => {
    const counts = categoryCounts(readableSourceEvents());
    if (candidateArtifactGroups().length) counts.artifact = candidateArtifactGroups().length;
    return counts;
  });
  const technicalSourceCounts = createMemo(() => categoryCounts(technicalSourceEvents()));
  const technicalTraceIds = createMemo(() => [...new Set(
    technicalSourceEvents().map((event) => String(event.trace_id || '').trim()).filter(Boolean),
  )]);
  const sourceRows = createMemo(() => sortByCategory(
    readableSourceEvents()
      .map((event) => ({ event }))
      .filter((row) => sourceCategory() === 'all' || eventCategory(row.event) === sourceCategory()),
  ));
  const sourcePages = createMemo(() => Math.max(1, Math.ceil(sourceRows().length / PAGE_SIZE)));
  const safeSourcePage = createMemo(() => Math.min(sourcePage(), sourcePages()));
  const visibleSourceGroups = createMemo(() => groupedRows(sourceRows().slice(
    (safeSourcePage() - 1) * PAGE_SIZE,
    safeSourcePage() * PAGE_SIZE,
  )));
  const pendingCounts = createMemo(() => activityCategoryCounts(pendingItems()));
  const pendingRows = createMemo(() => [...pendingItems()].filter(
    (item) => pendingCategory() === 'all'
      || Number(item.category_counts?.[pendingCategory() as EventCategory] || 0) > 0,
  ).sort((left, right) => Date.parse(right.started_at) - Date.parse(left.started_at)));
  const pendingPages = createMemo(() => Math.max(1, Math.ceil(pendingRows().length / PAGE_SIZE)));
  const safePendingPage = createMemo(() => Math.min(pendingPage(), pendingPages()));
  const visiblePendingRows = createMemo(() => pendingRows().slice(
    (safePendingPage() - 1) * PAGE_SIZE,
    safePendingPage() * PAGE_SIZE,
  ));

  const loadDetail = async (candidateId: string) => {
    if (!candidateId) {
      setCandidateDetail(null);
      return;
    }
    try {
      const target = workspaceId();
      const detail = await getResearchCandidate(candidateId, target);
      if (target !== workspaceId() || selectedCandidateId() !== candidateId) return;
      if (detail.candidate.state === 'confirmed') {
        setCandidateDetail(null);
        return;
      }
      setCandidateDetail(detail);
    } catch (e) {
      setCandidateDetail(null);
      setError(e instanceof Error ? e.message : '加载候选详情失败');
    }
  };

  let loadGeneration = 0;
  const load = async (preserveSelection = true) => {
    const generation = ++loadGeneration, targetWorkspace = workspaceId();
    setLoading(true);
    setError('');
    try {
      const [healthResult, centerResult, candidateResult, activityResult, importResult, assetResult] = await Promise.all([
        getResearchHealth(workspaceId()),
        getResearchCenter(workspaceId()),
        listResearchCandidates(workspaceId(), true),
        listResearchActivities(workspaceId(), ''),
        listResearchPackImports(workspaceId(), false, listingOffsets().import),
        listResearchPackAssets(workspaceId(), false, listingOffsets().asset),
      ]);
      if (generation !== loadGeneration || workspaceId() !== targetWorkspace) return;
      setHealth(healthResult);
      setCandidates(candidateResult.candidates || []);
      const centerProjects = [
        ...(centerResult.current_project ? [centerResult.current_project] : []),
        ...(centerResult.history || []),
      ];
      if (pendingSearchProject && !centerProjects.some(item=>item.research_id===pendingSearchProject?.research_id)) centerProjects.push(pendingSearchProject);
      pendingSearchProject = undefined;
      setProjects(centerProjects);
      setAllActivities(activityResult.activities || []);
      setPendingItems((activityResult.activities || []).filter((item) => item.review_state === 'pending'));
      setPackImports(importResult.imports || []);
      setPackAssets(assetResult.assets || []);
      setListingNext(previous => ({...previous, asset:assetResult.next_offset, import:importResult.next_offset}));
      setPackImportEnabled(Boolean(importResult.settings?.research_pack_import_active));
      const pendingInspection = (importResult.imports || []).find((item) => (
        item.status === 'inspected' && Date.parse(item.expires_at) > Date.now()
      ));
      if (pendingInspection && packInspection()?.inspection_id !== pendingInspection.inspection_id) {
        setPackInspection(pendingInspection);
        setPackImportTitle(String(pendingInspection.inspection_report?.researches?.[0]?.title || ''));
      } else if (!pendingInspection) {
        setPackInspection(null);
      }
      const existing = preserveSelection ? selectedCandidateId() : '';
      const nextId = candidateResult.candidates.some((item) => (
        item.candidate_id === existing && item.state !== 'confirmed'
      ))
        ? existing
        : candidateResult.candidates.find((item) => ['emerging', 'stable'].includes(item.state))?.candidate_id || '';
      setSelectedCandidateId(nextId);
      if (!centerProjects.some((item) => item.research_id === selectedProjectId())) {
        setSelectedProjectId(
          centerResult.current_project?.research_id
          || centerProjects[0]?.research_id
          || '',
        );
      }
      await loadDetail(nextId);
    } catch (e) {
      if (generation === loadGeneration && workspaceId() === targetWorkspace) setError(e instanceof Error ? e.message : '加载研究中心失败');
    } finally {
      if (generation === loadGeneration && workspaceId() === targetWorkspace) setLoading(false);
    }
  };

  createEffect(on(
    () => [Boolean(props.active), workspaceId()] as const,
    ([active, workspace], previous) => {
      if (!previous || workspace !== previous[1]) {
        setListingOffsets({'asset':0,'import':0,'removed-asset':0,'removed-import':0}); setListingNext({});
      }
      if (!active) return;
      setNotice('');
      void load(false);
    },
  ));

  createEffect(on(
    () => [selectedCandidateId(), sourceCategory()] as const,
    () => setSourcePage(1),
  ));

  createEffect(on(
    () => pendingCategory(),
    () => setPendingPage(1),
  ));

  const selectCandidate = (candidateId: string) => {
    setSelectedCandidateId(candidateId);
    void loadDetail(candidateId);
  };

  const runDiscovery = async () => {
    setBusy('discover');
    setError('');
    setNotice('');
    try {
      const result = await runTopicDiscovery(workspaceId());
      const report = result.report || {};
      setNotice(
        `候选扫描完成：新增 ${Number(report.candidate_created_count || 0)} 个候选，更新 ${Number(report.membership_changed_count || 0)} 条归属。`,
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '候选扫描失败');
    } finally {
      setBusy('');
    }
  };

  const renameCandidate = async (candidate: ResearchCandidate) => {
    const title = (await promptAction('输入新的课题候选名称', candidate.suggested_title))?.trim();
    if (!title || title === candidate.suggested_title) return;
    setBusy(`rename:${candidate.candidate_id}`);
    try {
      await renameResearchCandidate(candidate.candidate_id, title, workspaceId());
      setNotice('候选名称已更新，并保留了重命名审计记录。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '重命名失败');
    } finally {
      setBusy('');
    }
  };

  const confirmCandidate = async (candidate: ResearchCandidate) => {
    if (!await confirmAction(`确认将“${candidate.suggested_title}”设为正式课题？来源会话将按现有规则关联到课题。`)) return;
    setBusy(`confirm:${candidate.candidate_id}`);
    try {
      const result = await confirmResearchCandidate(candidate.candidate_id, workspaceId());
      const boundSessionIds = new Set(result.bound_session_ids || []);
      sessionStore.setSessions((previous) => previous.map((session) => (
        boundSessionIds.has(session.session_id)
          ? {
              ...session,
              mode: 'research',
              research_id: result.project.research_id,
              mode_source: 'candidate_confirmed',
              metadata: {
                ...(session.metadata || {}),
                mode: 'research',
                research_id: result.project.research_id,
                mode_source: 'candidate_confirmed',
              },
            }
          : session
      )));
      setSelectedProjectId(result.project.research_id);
      setProvenanceProjectId(result.project.research_id);
      setSelectedCandidateId('');
      setCandidateDetail(null);
      setMergeSelected((previous) => {
        const next = new Set(previous);
        next.delete(candidate.candidate_id);
        return next;
      });
      setNotice(boundSessionIds.size
        ? `候选已确认为正式课题，已关联 ${boundSessionIds.size} 个来源会话。关联不代表全部消息都已纳入档案。`
        : '候选已确认为正式课题，但没有可关联的来源会话；可在目标会话菜单选择“关联到课题”。');
      await load(false);
      setStateView('overview'); setTab('projects');
    } catch (e) {
      setError(e instanceof Error ? e.message : '确认课题失败');
    } finally {
      setBusy('');
    }
  };

  const dismissCandidate = async (candidate: ResearchCandidate) => {
    if (!await confirmAction(`忽略“${candidate.suggested_title}”？事件会回到待整理状态，不会删除原会话。`)) return;
    setBusy(`dismiss:${candidate.candidate_id}`);
    try {
      await dismissResearchCandidate(candidate.candidate_id, '用户在研究中心忽略候选', workspaceId());
      setNotice('候选已忽略；原始 Session 与事件仍然保留。');
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '忽略候选失败');
    } finally {
      setBusy('');
    }
  };

  const toggleMergeCandidate = (candidateId: string, checked: boolean) => {
    setMergeSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(candidateId);
      else next.delete(candidateId);
      return next;
    });
  };

  const mergeCandidates = async () => {
    const selected = [...mergeSelected()];
    if (selected.length < 2) {
      setError('请至少选择两个候选再合并');
      return;
    }
    const defaultTitle = candidates().find((item) => item.candidate_id === selected[0])?.suggested_title || '';
    const title = (await promptAction('输入合并后的候选名称', defaultTitle))?.trim();
    if (!title) return;
    setBusy('merge');
    try {
      const result = await mergeResearchCandidates(selected, title, workspaceId());
      setMergeSelected(new Set<string>());
      setSelectedCandidateId(result.candidate.candidate_id);
      setNotice('候选已合并；旧候选保留为审计状态。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '候选合并失败');
    } finally {
      setBusy('');
    }
  };

  const targetForEvent = (eventId: string) => (
    eventTargets()[eventId]
    || selectedCandidateId()
    || assignableCandidates()[0]?.candidate_id
    || ''
  );

  const setTargetForEvent = (eventId: string, candidateId: string) => {
    setEventTargets((previous) => ({ ...previous, [eventId]: candidateId }));
  };

  const includeEvent = async (eventId: string, move = false) => {
    const candidateId = targetForEvent(eventId);
    if (!candidateId) {
      setError('当前没有可接收事件的候选');
      return;
    }
    setBusy(`event:${eventId}`);
    try {
      if (move) await moveResearchEvent(eventId, candidateId, workspaceId());
      else await includeResearchEvent(eventId, candidateId, workspaceId());
      setNotice(move ? '事件已移动，旧关系保留审计。' : '事件已纳入候选。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '整理事件失败');
    } finally {
      setBusy('');
    }
  };

  const excludeEvent = async (eventId: string) => {
    if (!await confirmAction('确认将这条内容排除出研究派生层？原始 Session 不会被删除。')) return;
    setBusy(`event:${eventId}`);
    try {
      await excludeResearchEvent(eventId, '用户在研究中心排除', workspaceId());
      setNotice('事件已排除，只保留在私人 Session。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '排除事件失败');
    } finally {
      setBusy('');
    }
  };

  const includeActivity = async (activity: ResearchActivity) => {
    const candidateId = targetForEvent(activity.activity_id);
    if (!candidateId) {
      setError('当前没有可接收研究活动的候选');
      return;
    }
    setBusy(`activity:${activity.activity_id}`);
    setError('');
    try {
      const result = await includeResearchActivity(
        activity.activity_id,
        candidateId,
        workspaceId(),
      );
      const processed = Number(result.processed_event_ids?.length || 0);
      const failed = Number(result.errors?.length || 0);
      setNotice(
        failed > 0
          ? `研究活动已部分整理：纳入 ${processed} 条语义事实，${failed} 条失败。`
          : `研究活动已整体纳入，共处理 ${processed} 条语义事实；折叠的控制事件无需单独操作。`,
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '整理研究活动失败');
    } finally {
      setBusy('');
    }
  };

  const excludeActivity = async (activity: ResearchActivity) => {
    if (!await confirmAction(
      `确认排除“${activity.title}”？原始会话和底层研究记录 仍会保留，但不会进入研究档案、能力资产和共享内容。`,
    )) return;
    setBusy(`activity:${activity.activity_id}`);
    setError('');
    try {
      const result = await excludeResearchActivity(
        activity.activity_id,
        '用户在研究中心排除整组研究活动',
        workspaceId(),
      );
      const processed = Number(result.processed_event_ids?.length || 0);
      setNotice(`研究活动已排除，共处理 ${processed} 条语义事实；原始记录仍保留。`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '排除研究活动失败');
    } finally {
      setBusy('');
    }
  };

  const changeProjectStatus = async (project: ResearchProject) => {
    const action = project.status === 'archived' ? 'activate' : 'archive';
    setBusy(`project:${project.research_id}`);
    try {
      const result = await setResearchProjectStatus(project.research_id, action, workspaceId());
      const affected = new Set(result.archived_session_ids || result.restored_session_ids || []);
      sessionStore.setSessions((previous) => previous.map((session) => (
        affected.has(session.session_id)
          ? {
              ...session,
              is_archived: action === 'archive',
              archive_cause: action === 'archive' ? 'project_cascade' : '',
              archive_locked: false,
              metadata: {
                ...(session.metadata || {}),
                archive_cause: action === 'archive' ? 'project_cascade' : '',
                archive_locked: false,
              },
            }
          : session
      )));
      setNotice(action === 'archive' ? '课题及其关联会话已归档。' : '课题及其级联归档会话已恢复。');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '更新 Project 状态失败');
    } finally {
      setBusy('');
    }
  };

  const permanentlyDeleteProject = async (project: ResearchProject) => {
    setBusy(`project-delete:${project.research_id}`);
    setError('');
    let preview;
    try {
      preview = await getResearchProjectDeletionPreview(project.research_id, workspaceId());
    } catch (e) {
      setBusy('');
      setError(e instanceof Error ? e.message : '无法生成删除影响预览');
      return;
    }
    if (!await confirmAction(
      `永久删除“${project.title}”？\n\n将同时删除 ${preview.research_session_count} 个关联会话及其消息，并删除研究档案、研究包、导入产物和明确归属于本课题的受管本地文件。该操作不可撤销。`,
      {title:'永久删除课题？',tone:'danger',confirmLabel:'永久删除'},
    )) { setBusy(''); return; }
    while (true) {
      const confirmation = await promptAction(
        '请输入 yes 确认永久删除；如果不删除，请点击“取消”：',
        '',
      );
      if (confirmation === null) {
        setBusy('');
        setNotice('已取消永久删除。');
        return;
      }
      if (confirmation.trim().toLowerCase() === 'yes') break;
      await showFeedback('需要输入 yes 才能永久删除。如果不删除，请点击“取消”。',{title:'需要再次确认',tone:'warning'});
    }
    setNotice('');
    try {
      const result = await deleteResearchProject(
        project.research_id,
        'yes',
        preview.preview_token,
        workspaceId(),
      );
      if (selectedProjectId() === project.research_id) setSelectedProjectId('');
      if (provenanceProjectId() === project.research_id) setProvenanceProjectId('');
      const deletion = result.deletion;
      const deletedSessionIds = new Set(result.deleted_session_ids || []);
      if (deletedSessionIds.has(String(sessionStore.currentSessionId() || ''))) {
        sessionStore.setCurrentSessionId(null);
        sessionStore.setMessages([]);
      }
      sessionStore.setSessions((previous) => previous.filter(
        (session) => !deletedSessionIds.has(session.session_id),
      ));
      const removedFiles = Number(deletion.local_files_removed || 0)
        + Number(deletion.imported_papers_removed || 0)
        + Number(deletion.imported_wiki_pages_removed || 0);
      setNotice(
        deletion.cleanup_error_count > 0
          ? `课题已删除，已清理 ${removedFiles} 项本地文件或产物；另有 ${deletion.cleanup_error_count} 项清理警告已写入审计。`
          : `课题已永久删除，同时删除 ${deletedSessionIds.size} 个关联会话，并清理 ${removedFiles} 项本地文件或产物。`,
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : '永久删除课题失败');
    } finally {
      setBusy('');
    }
  };

  const inspectPack = async (file?: File) => {
    if (!file) return;
    setBusy('pack-inspect');
    setError('');
    setNotice('');
    setPackInspection(null);
    try {
      const result = await inspectResearchPackImport(file, workspaceId());
      setPackInspection(result.inspection);
      setPackImportTitle(String(result.inspection.inspection_report?.researches?.[0]?.title || ''));
      setNotice('ResearchPack 已通过结构、Checksum、内容安全和兼容性检查。请核对后再导入。');
      const imports = await listResearchPackImports(workspaceId());
      setPackImports(imports.imports || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : '检查 ResearchPack 失败');
    } finally {
      setBusy('');
    }
  };

  const commitPackImport = async (action: 'copy' | 'open_existing' | 'cancel') => {
    const inspection = packInspection();
    if (!inspection) return;
    if (action === 'copy' && !await confirmAction(
      '确认把这个研究包导入当前研究空间？它不会占用正式课题名额。来源内容保持只读，后续研究记录保存在本地。',
    )) return;
    setBusy('pack-import');
    setError('');
    try {
      const result = await importResearchPack(
        inspection.inspection_id,
        action,
        packImportTitle().trim(),
        workspaceId(),
      );
      setPackInspection(null);
      if (action === 'cancel') {
        setNotice('已取消本次导入，暂存文件已清理。');
        await load();
        return;
      }
      setNotice(action === 'open_existing' ? '已打开此前导入的研究资料。' : 'ResearchPack 已导入为资料资产；Agent 可在聊天中渐进读取并继续记录。');
      await load(false);
      if (result.import.asset_id) setTab('import');
    } catch (e) {
      setError(e instanceof Error ? e.message : '导入 ResearchPack 失败');
    } finally {
      setBusy('');
    }
  };

  const usePackInChat = (asset: ResearchPackAsset) => {
    window.dispatchEvent(new CustomEvent('tcode-use-research-pack-in-chat', {
      detail: {
        assetId: asset.asset_id,
        packId: asset.pack_id,
        title: asset.title,
      },
    }));
  };
  const reproducePackExperiment = async (asset: ResearchPackAsset, experimentId: string) => {
    const project = activeProjects()[0];
    if (!project) throw new Error('请先确认当前 Workspace 的正式课题');
    const result = await createResearchReproduction(project.research_id, asset.asset_id, experimentId, workspaceId(), String(sessionStore.currentSessionId() || ''));
    setNotice(result.reproduction_ready
      ? '已创建并启动独立复现实验；结果会冻结后用于原始/复现对比。'
      : `已创建复现实验，但兼容性检查阻止自动运行：${result.compatibility_issues.join('、')}`);
  };

  const copyEventContent = async (eventId: string, content: string) => {
    try {
      await navigator.clipboard.writeText(content);
      setCopiedEventId(eventId);
      if (copiedResetTimer) clearTimeout(copiedResetTimer);
      copiedResetTimer = setTimeout(() => setCopiedEventId(''), 2000);
    } catch {
      setError('复制失败，请手动选择完整内容进行复制。');
    }
  };

  const eventCard = (event: ResearchEvent, included: boolean) => {
    const content = buildExpandablePreview(readableResearchMarkdown(eventContent(event),new Map(projects().map(p=>[p.research_id,p.title]))));
    return (
      <article class="rounded-xl border border-[#2a3136] bg-[#11171b] p-3">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2 text-[10px] text-[#71808a]">
              <span class="rounded-full bg-[#38bdf8]/10 px-2 py-0.5 font-semibold text-[#8ed5ff]">{categoryLabel(eventCategory(event))}</span>
              <span>{actorLabel(event.actor_type)}</span>
            </div>
            <h3 class="mt-1.5 text-sm font-semibold text-[#e6edf3]">{eventDisplayTitle(event)}</h3>
          </div>
          <span class="shrink-0 text-[10px] text-[#71808a]">{formatTime(event.occurred_at)}</span>
        </div>
        <div class="research-progress-markdown mt-2"><MarkdownContent content={content.fullText} allowSessionActions={false}/></div>
        <Show when={content.truncated}>
          <details class="group mt-2 rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2 text-xs">
            <summary class="cursor-pointer select-none font-medium text-[#8ed5ff]">
              <span class="group-open:hidden">展开完整内容</span>
              <span class="hidden group-open:inline">收起完整内容</span>
            </summary>
            <div class="mt-2 flex items-center justify-end">
              <button
                type="button"
                class="rounded-md border border-[#38bdf8]/25 bg-[#38bdf8]/8 px-2.5 py-1 text-[11px] font-medium text-[#8ed5ff] hover:bg-[#38bdf8]/15"
                onClick={() => void copyEventContent(event.event_id, content.fullText)}
              >
                {copiedEventId() === event.event_id ? '已复制' : '复制全文'}
              </button>
            </div>
            <div
              class="mt-2 max-h-80 overflow-y-auto overscroll-contain whitespace-pre-wrap break-words rounded-md border border-[#252d32] bg-[#0a0f12] p-3 text-sm leading-6 text-[#d7dee4]"
              tabIndex={0}
              aria-label="完整事件内容"
            >
              <MarkdownContent content={content.fullText} allowSessionActions={false}/>
            </div>
          </details>
        </Show>
        <details class="mt-2 text-[10px] text-[#596873]">
          <summary class="cursor-pointer select-none">技术详情</summary>
          <div class="mt-1">{categoryLabel(eventCategory(event))} · {actorLabel(event.actor_type)} · {formatTime(event.occurred_at)}</div>
        </details>
        <div class="mt-3 flex flex-wrap items-center gap-2">
          <select
            class="h-8 min-w-[180px] rounded-lg border border-[#303539] bg-[#0d1216] px-2 text-xs text-[#bdc8d1] outline-none focus:border-[#7bd0ff]/50"
            value={targetForEvent(event.event_id)}
            onChange={(target) => setTargetForEvent(event.event_id, target.currentTarget.value)}
          >
            <For each={assignableCandidates()}>
              {(candidate) => <option value={candidate.candidate_id}>{candidate.suggested_title}</option>}
            </For>
          </select>
          <button
            class="h-8 rounded-lg border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-3 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50"
            disabled={busy() === `event:${event.event_id}` || assignableCandidates().length === 0}
            onClick={() => void includeEvent(event.event_id, included)}
          >
            {included ? '移动到候选' : '纳入候选'}
          </button>
          <button
            class="h-8 rounded-lg border border-[#ef4444]/25 bg-[#ef4444]/8 px-3 text-xs font-semibold text-[#fca5a5] disabled:opacity-50"
            disabled={busy() === `event:${event.event_id}`}
            onClick={() => void excludeEvent(event.event_id)}
          >
            排除
          </button>
        </div>
      </article>
    );
  };

  const activityCard = (activity: ResearchActivity) => (
    <article class="rounded-xl border border-[#2f383e] bg-[#11171b] p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2 text-[10px]">
            <span class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2 py-0.5 font-semibold text-[#8ed5ff]">
              {activityTypeLabels[activity.activity_type] || '研究活动'}
            </span>
            <span class={`rounded-full border px-2 py-0.5 ${activity.status === 'failed' || activity.status === 'incomplete' ? 'border-[#ef4444]/30 text-[#fca5a5]' : activity.status === 'running' ? 'border-[#f1a02b]/30 text-[#ffc176]' : 'border-[#38b46a]/30 text-[#9ee6bb]'}`}>
              {activityStatusLabels[activity.status] || researchStatusLabel(activity.status)}
            </span>
            <span class="text-[#71808a]">{formatTime(activity.started_at)}</span>
          </div>
          <h3 class="mt-2 break-words text-base font-semibold text-[#e6edf3]">{readableResearchMarkdown(activity.title)}</h3>
          <Show when={activity.summary}>
            <p class="mt-2 line-clamp-3 whitespace-pre-wrap break-words text-xs leading-5 text-[#9faeb7]">{activity.summary}</p>
          </Show>
        </div>
        <div class="rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2 text-right">
          <div class="text-[10px] uppercase tracking-wider text-[#596873]">系统建议</div>
          <div class="mt-1 text-xs font-semibold text-[#ffc176]">
            {activity.recommendation === 'include' ? '建议纳入' : '需要核对'} · {Math.round(activity.confidence * 100)}%
          </div>
        </div>
      </div>

      <div class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216]/70 px-3 py-2 text-xs leading-5 text-[#8b99a4]">
        {recommendationText(activity)}
      </div>

      <div class="mt-3 flex flex-wrap gap-2 text-[10px] text-[#9faeb7]">
        <For each={eventCategories.filter((item) => Number(activity.category_counts?.[item.key] || 0) > 0)}>
          {(item) => (
            <span class="rounded border border-[#30383e] bg-[#171d21] px-2 py-1">
              {item.label} {Number(activity.category_counts?.[item.key] || 0)}
            </span>
          )}
        </For>
        <span class="rounded border border-[#30383e] bg-[#171d21] px-2 py-1">
          需要整理的内容 {activity.semantic_event_ids.length}
        </span>
        <Show when={activity.control_event_ids.length > 0}>
          <span class="rounded border border-[#30383e] bg-[#171d21] px-2 py-1 text-[#71808a]">
            技术记录已折叠 {activity.control_event_ids.length}
          </span>
        </Show>
      </div>

      <Show when={activity.artifact_groups?.length > 0}>
        <div class="mt-3 space-y-2">
          <For each={activity.artifact_groups}>
            {(group) => <ResearchArtifactGroupCard group={group} />}
          </For>
        </div>
      </Show>
      <Show when={!activity.artifact_groups?.length && activity.artifact_refs.length > 0}>
        <div class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216]/55 px-3 py-2 text-xs leading-5 text-[#8b99a4]">
          本次活动保留了 {activity.artifact_refs.length} 项底层执行产物，已归入技术审计详情。
        </div>
      </Show>

      <Show when={activity.file_refs.length > 0}>
        <div class="mt-3 space-y-1.5">
          <For each={activity.file_refs}>
            {(file) => (
              <div class="flex min-w-0 items-center justify-between gap-3 rounded-lg border border-[#293238] px-3 py-2 text-xs">
                <div class="min-w-0">
                  <div class="truncate font-medium text-[#bdc8d1]">{file.filename || file.path || '未命名文件'}</div>
                  <div class="mt-0.5 truncate font-mono text-[10px] text-[#596873]">{researchValueText(file.path,'source_path')}</div>
                </div>
                <Show when={file.size > 0}>
                  <span class="shrink-0 text-[10px] text-[#71808a]">{formatBytes(file.size)}</span>
                </Show>
              </div>
            )}
          </For>
        </div>
      </Show>

      <Show when={activity.tool_calls.length > 0 || (activity.agent_calls || []).length > 0 || (activity.agent_runs || []).length > 0 || (activity.workflow_calls || []).length > 0}>
        <details class="mt-3 rounded-lg border border-[#293238] bg-[#0d1216]/55 px-3 py-2">
          <summary class="cursor-pointer text-xs text-[#9faeb7]">查看 Agent 执行树与产物归属</summary>
          <div class="mt-2 space-y-2">
            <Show
              when={(activity.agent_runs || []).length > 0}
              fallback={(
                <>
                  <For each={activity.agent_calls || []}>
                    {(agent) => (
                      <div class="rounded border border-[#263038] bg-[#11171b] px-2.5 py-2 text-[11px]">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <span class="font-medium text-[#d7dee4]">子 Agent · {researchAgentLabel(agent.name)}</span>
                          <span class={executionStateClass(agent.state)}>{activityStatusLabels[agent.state] || researchStatusLabel(agent.state)}</span>
                        </div>
                        <Show when={agent.task}><div class="mt-1 line-clamp-2 text-[#8b99a4]">{agent.task}</div></Show>
                      </div>
                    )}
                  </For>
                  <For each={activity.tool_calls}>
                    {(tool) => (
                      <div class="flex flex-wrap items-center justify-between gap-2 text-[11px]">
                        <span class="font-mono text-[#bdc8d1]">{toolDisplayLabel(tool.name)}</span>
                        <span class={executionStateClass(tool.state)}>{activityStatusLabels[tool.state] || researchStatusLabel(tool.state)}</span>
                      </div>
                    )}
                  </For>
                </>
              )}
            >
              <For each={(activity.agent_runs || []).filter((run) => (
                run.is_root
                || !run.parent_agent_run_id
                || !(activity.agent_runs || []).some((item) => item.agent_run_id === run.parent_agent_run_id)
              ))}>
                {(run) => <AgentRunNode activity={activity} run={run} />}
              </For>
            </Show>
            <div class="border-t border-[#252d32] pt-2 font-mono text-[10px] text-[#596873]">
              已关联 {activity.agent_runs?.length || 0} 个 Agent 执行节点 · 原始事件 {activity.event_ids.length} 条
            </div>
          </div>
        </details>
      </Show>

      <div class="mt-4 flex flex-wrap items-center gap-2">
        <select
          class="h-9 min-w-[220px] flex-1 rounded-lg border border-[#303539] bg-[#0d1216] px-2 text-xs text-[#bdc8d1] outline-none focus:border-[#7bd0ff]/50"
          value={targetForEvent(activity.activity_id)}
          onChange={(target) => setTargetForEvent(activity.activity_id, target.currentTarget.value)}
        >
          <For each={assignableCandidates()}>
            {(candidate) => <option value={candidate.candidate_id}>{candidate.suggested_title}</option>}
          </For>
        </select>
        <button
          class="h-9 rounded-lg border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-4 text-xs font-semibold text-[#8ed5ff] disabled:opacity-50"
          disabled={busy() === `activity:${activity.activity_id}` || assignableCandidates().length === 0}
          onClick={() => void includeActivity(activity)}
        >
          整体纳入
        </button>
        <button
          class="h-9 rounded-lg border border-[#ef4444]/25 bg-[#ef4444]/8 px-4 text-xs font-semibold text-[#fca5a5] disabled:opacity-50"
          disabled={busy() === `activity:${activity.activity_id}`}
          onClick={() => void excludeActivity(activity)}
        >
          整体排除
        </button>
      </div>
    </article>
  );

  const viewTitles: Record<string, string> = { candidates:'研究候选与依据', pending:'整理研究记录', projects:'研究概览', import:'资料与成果', upload:'检查并导入研究资料', state:'课题详情', review:'需要处理', advanced:'研究管理与诊断', history:'切换与管理课题' };
  createEffect(on(workspaceId, () => {
    setOpenedAsset(null); setRemovedAssets([]); setRemovedImports([]);
    setTab('projects');
  }, {defer:true}));
  const renderContent = () => (
      <div class="research-content min-h-0 flex-1 overflow-auto p-4 md:p-5">
        <Show when={loading()}>
          <div class="py-16 text-center text-sm text-[#8b99a4]">正在加载研究投影…</div>
        </Show>

        <Show when={!loading() && tab() === 'projects'}>
          <Show when={activeProjects()[0]} fallback={<section class="space-y-3"><h2 class="text-base font-semibold">课题可以随着研究逐步形成</h2><p class="text-sm">可以继续提问、查看资料，或选择候选核对依据。不必先确认课题才能开始研究。</p>
            <div class="flex flex-wrap gap-3"><button type="button" class="rounded-lg border p-3 text-sm" onClick={() => setTab('import')}>查看资料与成果</button><button type="button" class="rounded-lg border p-3 text-sm" onClick={() => setShareTarget({mode:'workspace_period',title:'当前工作区的研究经历'})}>分享一段研究经历</button><Show when={archivedProjects().length}><button type="button" class="rounded-lg border p-3 text-sm" onClick={() => setTab('history')}>查看历史课题</button></Show></div>
            <Show when={!candidatePool().length}><p class="text-xs">暂时没有待确认候选。可以开始研究，或在高级菜单重新扫描已有记录。</p></Show>
            <For each={candidatePool()}>{candidate => <button type="button" class="block w-full rounded-xl border border-[#30383e] p-4 text-left" onClick={() => {selectCandidate(candidate.candidate_id); setTab('candidates');}}><strong>{candidate.suggested_title}</strong><p class="mt-2 text-xs">{candidate.goal_summary || '查看记录并确认研究范围'}</p></button>}</For>
          </section>}>{project => <ResearchStatePanel onEvidenceMap={p=>{setSelectedProjectId(p.research_id);setTab('evidence');}} workspaceId={workspaceId()} projects={projects()} initialResearchId={project().research_id} view="overview"
            onShare={p => setShareTarget({mode:'project', researchId:p.research_id, title:p.title})} onProjectChanged={() => load()} />}</Show>
        </Show>
        <Show when={!loading() && tab() === 'review'}>
          <section class="space-y-4"><h2 class="text-base font-semibold">需要你判断的事项</h2>
            <p class="text-sm">这些记录尚未明确归属，或结论之间存在冲突。处理前先核对内容和依据。</p>
            <Show when={candidatePool().length > 0}><button type="button" class="block rounded-lg border p-3 text-sm" onClick={() => setTab('candidates')}>核对 {candidatePool().length} 个研究候选：确认它们是否属于要开展的课题</button></Show>
            <Show when={pendingItems().length > 0}><button type="button" class="block rounded-lg border p-3 text-sm" onClick={() => setTab('pending')}>可选整理 · {pendingItems().length} 组记录尚未归属，可以稍后处理</button></Show>
            <Show when={!candidatePool().length && !pendingItems().length}><p class="text-xs">没有需要确认的课题或待归属记录。</p></Show>
            <Show when={activeProjects()[0]}>{project => <ResearchStatePanel onEvidenceMap={p=>{setSelectedProjectId(p.research_id);setTab('evidence');}} workspaceId={workspaceId()} projects={projects()} initialResearchId={project().research_id} view="review" onReviewSources={() => {setSelectedProjectId(project().research_id);setStateView('assets');setTab('state');}} onProjectChanged={() => load()} />}</Show>
          </section>
        </Show>
        <Show when={!loading() && tab() === 'advanced'}>
          <section class="space-y-4"><h2 class="text-base font-semibold">研究管理与诊断</h2>
            <p class="text-xs">已记录 {health()?.event_count || 0} 条研究事件，正式课题 {activeProjects().length} 项。</p>
            <button type="button" class="rounded-lg border p-3 text-sm" disabled={!topicsEnabled() || Boolean(busy())} onClick={() => void runDiscovery()}>重新扫描研究候选</button>
            <button type="button" class="ml-3 rounded-lg border p-3 text-sm" onClick={() => setTab('history')}>切换与管理历史课题</button>
            <button type="button" class="ml-3 rounded-lg border p-3 text-sm" onClick={() => setShareTarget({mode:'workspace_period', title:'当前工作区的研究经历'})}>分享一段研究经历</button>
            <details onToggle={event => {if (event.currentTarget.open) void loadRemoved();}}><summary>已移除资料与记录（可恢复）</summary>
              <p class="mt-2 text-xs">删除仅移出使用列表，审计及来源保留。可在这里恢复；本地生成副本不会被删除。</p>
              <h3 class="mt-3 text-sm">已删除资料</h3>
              <div class="research-scroll-list mt-2 space-y-2" aria-label="已删除资料列表" tabIndex={0}>
                <For each={removedAssets()}>{asset => <div class="flex items-center justify-between gap-3"><span>{asset.title}</span><button type="button" disabled={Boolean(busy())} onClick={() => void removeListing(asset.asset_id, 'asset', true)}>恢复资料</button></div>}</For>
                <Show when={!removedAssets().length}><p>没有已删除的资料。</p></Show>
              </div>{listingPager('asset', true)}
              <h3 class="mt-3 text-sm">已删除导入记录</h3>
              <div class="research-scroll-list mt-2 space-y-2" aria-label="已删除导入记录列表" tabIndex={0}>
                <For each={removedImports()}>{item => <div class="flex items-center justify-between gap-3"><span>{item.archive_name}</span><button type="button" disabled={Boolean(busy())} onClick={() => void removeListing(item.import_id, 'import', true)}>恢复记录</button></div>}</For>
                <Show when={!removedImports().length}><p>没有已删除的导入记录。</p></Show>
              </div>{listingPager('import', true)}
            </details>
            <Show when={activeProjects()[0]}>{project => <ResearchStatePanel onEvidenceMap={p=>{setSelectedProjectId(p.research_id);setTab('evidence');}} workspaceId={workspaceId()} projects={projects()} initialResearchId={project().research_id} view="advanced" onProjectChanged={() => load()} />}</Show>
          </section>
        </Show>
        <Show when={!loading() && tab() === 'candidates'}>
          <div class="grid min-h-full gap-4 xl:grid-cols-[minmax(320px,0.85fr)_minmax(420px,1.4fr)]">
            <div class="space-y-3">
              <div class="flex items-center justify-between gap-3">
                <div class="text-xs text-[#8b99a4]">
                  系统自动归纳课题名，最多保留 {candidateLimit()} 个候选；你可以重命名、忽略或合并。
                </div>
                <button
                  class="h-8 shrink-0 rounded-lg border border-[#f1a02b]/30 bg-[#f1a02b]/10 px-3 text-xs font-semibold text-[#ffc176] disabled:opacity-40"
                  disabled={mergeSelected().size < 2 || busy() === 'merge'}
                  onClick={() => void mergeCandidates()}
                >
                  合并所选
                </button>
              </div>
              <Show when={candidatePool().length === 0}>
                <div class="rounded-xl border border-dashed border-[#34404a] p-8 text-center text-sm text-[#71808a]">
                  {activeProjects().length > 0
                    ? '当前没有新的稳定候选。你可以在研究现场继续已有课题，或让新的研究活动作为待归属研究记录保留。'
                    : '还没有达到门槛的课题候选。继续研究即可，系统会在后台逐步积累。'}
                </div>
              </Show>
              <For each={candidatePool()}>
                {(candidate) => (
                  <article
                    class={`cursor-pointer rounded-xl border p-4 transition-colors ${selectedCandidateId() === candidate.candidate_id ? 'border-[#38bdf8]/45 bg-[#38bdf8]/8' : 'border-[#2a3136] bg-[#11171b] hover:border-[#3b4a54]'}`}
                    onClick={() => selectCandidate(candidate.candidate_id)}
                  >
                    <div class="flex items-start gap-3">
                      <input
                        type="checkbox"
                        disabled={candidate.state === 'confirmed'}
                        checked={mergeSelected().has(candidate.candidate_id)}
                        onClick={(event) => event.stopPropagation()}
                        onChange={(event) => toggleMergeCandidate(candidate.candidate_id, event.currentTarget.checked)}
                      />
                      <div class="min-w-0 flex-1">
                        <div class="flex flex-wrap items-center gap-2">
                          <div class="min-w-0 flex-1">
                            <div class="text-[9px] font-semibold uppercase tracking-wider text-[#6f8797]">
                              {candidate.title_source === 'user'
                                ? '用户命名'
                                : candidate.title_source === 'paper_metadata'
                                  ? '根据论文信息归纳'
                                  : '系统归纳课题名'}
                            </div>
                            <h2 class="mt-0.5 truncate text-sm font-semibold text-[#e6edf3]">{candidate.suggested_title}</h2>
                          </div>
                          <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#bdc8d1]">{stateLabels[candidate.state] || researchStatusLabel(candidate.state)}</span>
                        </div>
                        <Show when={candidate.goal_summary && candidate.goal_summary !== candidate.suggested_title}>
                          <p class="mt-2 line-clamp-2 text-xs leading-5 text-[#8b99a4]">研究意图：{candidate.goal_summary}</p>
                        </Show>
                        <div class="mt-2 flex flex-wrap gap-3 text-[11px] text-[#71808a]">
                          <span>{candidate.supporting_event_count} 条关联记录</span>
                          <span>{candidate.session_count} 次会话</span>
                          <span>{candidate.tool_count} 次工具执行</span>
                          <Show when={candidateAgentCount(candidate.candidate_id) > 0}>
                            <span>{candidateAgentCount(candidate.candidate_id)} 次协作任务</span>
                          </Show>
                          <span>{candidate.artifact_count} 项研究产物</span>
                          <span>稳定度 {Math.round(candidate.stability_score * 100)}%</span>
                        </div>
                        <div class="mt-3 flex flex-wrap gap-1.5">
                          <For each={candidate.keywords.slice(0, 6)}>
                            {(keyword) => <span class="rounded bg-[#20282e] px-2 py-1 text-[10px] text-[#9faeb7]">{keyword}</span>}
                          </For>
                        </div>
                        <div class="mt-3 flex flex-wrap gap-2">
                          <button
                            class="rounded-lg border border-[#303539] px-2.5 py-1.5 text-[11px] text-[#bdc8d1]"
                            onClick={(event) => { event.stopPropagation(); void renameCandidate(candidate); }}
                          >
                            重命名
                          </button>
                          <Show when={candidate.state !== 'confirmed'}>
                            <button
                              class="rounded-lg border border-[#38b46a]/30 bg-[#38b46a]/8 px-2.5 py-1.5 text-[11px] text-[#9ee6bb] disabled:cursor-not-allowed disabled:opacity-40"
                              disabled={activeProjects().length >= projectLimit()}
                              title={activeProjects().length >= projectLimit() ? `当前 Workspace 已达到 ${projectLimit()} 个正式课题上限；新的研究可保留为待归属研究记录` : ''}
                              onClick={(event) => { event.stopPropagation(); void confirmCandidate(candidate); }}
                            >
                              {activeProjects().length >= projectLimit() ? '保留为待归属研究记录' : '确认为正式课题'}
                            </button>
                          </Show>
                          <Show when={candidate.state !== 'confirmed'}>
                            <button
                              class="rounded-lg border border-[#ef4444]/20 px-2.5 py-1.5 text-[11px] text-[#fca5a5]"
                              onClick={(event) => { event.stopPropagation(); void dismissCandidate(candidate); }}
                            >
                              忽略
                            </button>
                          </Show>
                        </div>
                      </div>
                    </div>
                  </article>
                )}
              </For>
              <Show when={historicalCandidates().length > 0}>
                <details class="rounded-xl border border-[#2a3136] bg-[#11171b]/70 p-3">
                  <summary class="cursor-pointer select-none text-xs font-semibold text-[#8b99a4]">
                    历史候选 · {historicalCandidates().length} 个
                  </summary>
                  <p class="mt-2 text-[11px] leading-5 text-[#71808a]">这里保留已忽略、已合并或已拆分的候选，供追溯形成过程；已转为正式课题的内容请在研究现场查看。</p>
                  <div class="mt-2 space-y-2">
                    <For each={historicalCandidates()}>
                      {(candidate) => (
                        <button
                          class={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${selectedCandidateId() === candidate.candidate_id ? 'border-[#38bdf8]/40 bg-[#38bdf8]/8' : 'border-[#293238] bg-[#151b20] hover:border-[#3b4a54]'}`}
                          onClick={() => selectCandidate(candidate.candidate_id)}
                        >
                          <div class="flex items-center justify-between gap-2">
                            <span class="min-w-0 truncate text-xs font-medium text-[#bdc8d1]">{candidate.suggested_title}</span>
                            <span class="shrink-0 rounded-full border border-[#3e484f] px-2 py-0.5 text-[9px] text-[#8b99a4]">{stateLabels[candidate.state] || researchStatusLabel(candidate.state)}</span>
                          </div>
                          <div class="mt-1 text-[10px] text-[#596873]">{candidate.supporting_event_count} 条来源记录 · {formatTime(candidate.last_seen_at)}</div>
                        </button>
                      )}
                    </For>
                  </div>
                </details>
              </Show>
            </div>

            <div class="min-w-0 rounded-xl border border-[#2a3136] bg-[#11171b]/70 p-4">
              <Show when={!candidateDetail()}>
                <div class="py-16 text-center text-sm text-[#71808a]">选择一个候选查看来源事件。</div>
              </Show>
              <Show when={candidateDetail()} keyed>
                {(detail) => (
                  <>
                    <div class="border-b border-[#2a3136] pb-3">
                      <div class="text-[10px] font-semibold uppercase tracking-wider text-[#71808a]">系统为什么归纳为这个课题</div>
                      <h2 class="mt-1 text-base font-semibold text-[#e6edf3]">{detail.candidate.suggested_title}</h2>
                      <p class="mt-1 text-xs text-[#8b99a4]">
                        {readableSourceEvents().length + candidateArtifactGroups().length} 条可读依据
                        <Show when={technicalSourceEvents().length > 0}>{` · ${technicalSourceEvents().length} 条技术记录已折叠`}</Show>
                        {` · 首次 ${formatTime(detail.candidate.first_seen_at)}`}
                      </p>
                    </div>
                    <div class="mt-3">
                      <CategoryFilters
                        active={sourceCategory()}
                        counts={sourceCounts()}
                        total={readableSourceEvents().length + candidateArtifactGroups().length}
                        onSelect={setSourceCategory}
                      />
                    </div>
                    <div class="mt-4 space-y-5">
                      <Show when={(sourceCategory() === 'all' || sourceCategory() === 'artifact') && candidateArtifactGroups().length > 0}>
                        <section>
                          <div class="mb-2 flex items-center gap-2 text-[11px] font-semibold text-[#9faeb7]">
                            <span class="h-1.5 w-1.5 rounded-full bg-[#38bdf8]" />
                            研究产物
                            <span class="font-normal text-[#596873]">{candidateArtifactGroups().length} 组</span>
                          </div>
                          <div class="space-y-2">
                            <For each={candidateArtifactGroups()}>
                              {(group) => <ResearchArtifactGroupCard group={group} />}
                            </For>
                          </div>
                        </section>
                      </Show>
                      <Show when={sourceRows().length === 0 && !((sourceCategory() === 'all' || sourceCategory() === 'artifact') && candidateArtifactGroups().length > 0)}>
                        <div class="rounded-lg border border-dashed border-[#34404a] p-8 text-center text-sm text-[#71808a]">当前分类没有需要用户判断的课题依据。</div>
                      </Show>
                      <For each={visibleSourceGroups()}>
                        {(group) => (
                          <section>
                            <div class="mb-2 flex items-center gap-2 text-[11px] font-semibold text-[#9faeb7]">
                              <span class="h-1.5 w-1.5 rounded-full bg-[#38bdf8]" />
                              {group.label}
                              <span class="font-normal text-[#596873]">本页 {group.rows.length} 条</span>
                            </div>
                            <div class="space-y-3">
                              <For each={group.rows}>{(row) => eventCard(row.event, true)}</For>
                            </div>
                          </section>
                        )}
                      </For>
                      <PaginationControls
                        page={safeSourcePage()}
                        pages={sourcePages()}
                        total={sourceRows().length}
                        onPage={setSourcePage}
                      />
                      <Show when={technicalSourceEvents().length > 0}>
                        <details class="rounded-xl border border-[#293238] bg-[#0d1216]/55 px-3 py-3">
                          <summary class="cursor-pointer text-xs font-semibold text-[#8b99a4]">
                            技术与审计详情 · 已折叠 {technicalSourceEvents().length} 条
                          </summary>
                          <p class="mt-2 text-xs leading-5 text-[#71808a]">
                            这些记录用于恢复执行链和排查故障，已经并入完整研究动作，不需要单独纳入或排除。
                          </p>
                          <div class="mt-2 flex flex-wrap gap-2 text-[10px] text-[#71808a]">
                            <For each={eventCategories.filter((item) => Number(technicalSourceCounts()?.[item.key] || 0) > 0)}>
                              {(item) => (
                                <span class="rounded border border-[#30383e] px-2 py-1">
                                  {item.label} {Number(technicalSourceCounts()?.[item.key] || 0)}
                                </span>
                              )}
                            </For>
                            <Show when={technicalTraceIds().length > 0}>
                              <span class="rounded border border-[#30383e] px-2 py-1">关联调用链 {technicalTraceIds().length}</span>
                            </Show>
                          </div>
                          <Show when={technicalTraceIds().length > 0}>
                            <details class="mt-2 border-t border-[#252d32] pt-2 text-[10px] text-[#596873]">
                              <summary class="cursor-pointer">查看诊断标识</summary>
                              <div class="mt-1">已保留 {technicalTraceIds().length} 组来源追踪记录。</div>
                            </details>
                          </Show>
                        </details>
                      </Show>
                    </div>
                  </>
                )}
              </Show>
            </div>
          </div>
        </Show>

        <Show when={!loading() && tab() === 'pending'}>
          <div class="mx-auto max-w-5xl space-y-4">
            <div class="rounded-xl border border-[#2a3136] bg-[#11171b]/70 p-4">
              <div class="flex flex-wrap items-end justify-between gap-3">
                <div>
                  <div class="text-[10px] font-semibold uppercase tracking-wider text-[#71808a]">待整理研究活动</div>
                  <p class="mt-1 max-w-3xl text-xs leading-5 text-[#8b99a4]">
                    系统把同一次用户请求、文件、工具和子 Agent 动作聚合为一张卡片；底层 Event 完整保留，但无需逐条判断技术事件。
                  </p>
                </div>
                <div class="text-xs text-[#71808a]">每页 {PAGE_SIZE} 条</div>
              </div>
              <div class="mt-3">
                <CategoryFilters
                  active={pendingCategory()}
                  counts={pendingCounts()}
                  total={pendingItems().length}
                  onSelect={setPendingCategory}
                />
              </div>
            </div>
            <Show when={pendingItems().length === 0}>
              <div class="rounded-xl border border-dashed border-[#34404a] p-10 text-center text-sm text-[#71808a]">
                当前没有待整理的研究活动。
              </div>
            </Show>
            <Show when={pendingItems().length > 0 && pendingRows().length === 0}>
              <div class="rounded-xl border border-dashed border-[#34404a] p-10 text-center text-sm text-[#71808a]">当前分类没有待整理活动。</div>
            </Show>
            <div class="space-y-3">
              <For each={visiblePendingRows()}>{(activity) => activityCard(activity)}</For>
            </div>
            <PaginationControls
              page={safePendingPage()}
              pages={pendingPages()}
              total={pendingRows().length}
              onPage={setPendingPage}
            />
          </div>
        </Show>

        <Show when={!loading() && tab() === 'history'}>
          <div class="mx-auto max-w-5xl space-y-6">
            <section>
              <div class="mb-3 flex items-center justify-between gap-3">
                <div>
                  <div class="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#9ee6bb]">正在进行</div>
                  <h2 class="mt-1 text-base font-semibold text-[#edf2f7]">当前课题</h2>
                </div>
                <span class="rounded-full border border-[#30383e] px-2.5 py-1 text-[11px] text-[#8b99a4]">最多 1 个</span>
              </div>
              <Show when={activeProjects().length === 0}>
                <div class="rounded-xl border border-dashed border-[#34404a] bg-[#11171b]/45 p-7 text-center text-sm text-[#71808a]">
                  当前没有正式课题。请从下方研究候选中选择并确认一个课题。
                </div>
              </Show>
              <div class="research-scroll-list grid gap-3 md:grid-cols-2" aria-label="历史课题列表" tabIndex={0}>
              <For each={activeProjects()}>
              {(project) => (
                <article class="rounded-xl border border-[#38b46a]/25 bg-[#11171b] p-4">
                  <div class="flex items-start justify-between gap-3">
                    <div>
                      <h2 class="text-sm font-semibold text-[#e6edf3]">{project.title}</h2>

                    </div>
                    <span class="rounded-full border border-[#38b46a]/30 px-2 py-0.5 text-[10px] text-[#9ee6bb]">当前课题</span>
                  </div>
                  <Show when={project.description}>
                    <p class="mt-3 text-xs leading-5 text-[#9faeb7]">{project.description}</p>
                  </Show>
                  <div class="mt-3 text-[11px] text-[#71808a]">更新于 {formatTime(project.updated_at)}</div>
                  <button
                    class="mt-4 rounded-lg border border-[#303539] px-3 py-1.5 text-xs text-[#bdc8d1] disabled:opacity-50"
                    disabled={busy() === `project:${project.research_id}`}
                    onClick={() => void changeProjectStatus(project)}
                  >
                    归档课题
                  </button>
                  <button
                    class="ml-2 mt-4 rounded-lg border border-[#38bdf8]/30 bg-[#38bdf8]/8 px-3 py-1.5 text-xs text-[#8ed5ff]"
                    onClick={(event) => openCardDetail(event.currentTarget.closest('article') as HTMLElement, '.research-project-detail', () => {
                      setSelectedProjectId(project.research_id);
                      setStateView('overview'); setTab('state');
                    })}
                  >
                    打开研究现场
                  </button>
                  <button
                    class="ml-2 mt-4 rounded-lg border border-[#c4a7ff]/25 bg-[#c4a7ff]/6 px-3 py-1.5 text-xs text-[#d8c8ff]"
                    onClick={(event) => openCardDetail(event.currentTarget.closest('article') as HTMLElement, '.research-project-detail', () => {
                      setSelectedProjectId(project.research_id);
                      setProvenanceProjectId(project.research_id);
                      setStateView('advanced'); setTab('state');
                    })}
                  >
                    查看课题来源
                  </button>
                  <button
                    class="ml-2 mt-4 rounded-lg border border-[#38b46a]/35 bg-[#38b46a]/8 px-3 py-1.5 text-xs font-semibold text-[#9ee6bb]"
                    onClick={() => setShareTarget({
                      mode: 'project',
                      researchId: project.research_id,
                      title: project.title,
                    })}
                  >
                    分享这项研究
                  </button>
                  <button
                    class="ml-2 mt-4 rounded-lg border border-[#f85149]/30 px-3 py-1.5 text-xs text-[#ff9b96] disabled:opacity-50"
                    disabled={busy() === `project-delete:${project.research_id}`}
                    onClick={() => void permanentlyDeleteProject(project)}
                  >永久删除课题</button>
                </article>
              )}
              </For>
              </div>
            </section>

            <section>
              <div class="mb-3 flex items-center justify-between gap-3">
                <div>
                  <div class="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#8b99a4]">历史记录</div>
                  <h2 class="mt-1 text-base font-semibold text-[#edf2f7]">历史课题</h2>
                </div>
                <span class="text-xs text-[#71808a]">{archivedProjects().length} 个</span>
              </div>
              <Show when={archivedProjects().length === 0}>
                <div class="rounded-xl border border-dashed border-[#34404a] p-7 text-center text-sm text-[#71808a]">还没有归档的历史课题。</div>
              </Show>
              <div class="research-scroll-list grid gap-3 md:grid-cols-2" aria-label="历史课题列表" tabIndex={0}>
                <For each={archivedProjects()}>
                  {(project) => (
                    <article class="rounded-xl border border-[#2a3136] bg-[#11171b]/70 p-4">
                      <div class="flex items-start justify-between gap-3">
                        <h3 class="text-sm font-semibold text-[#d7dee4]">{project.title}</h3>
                        <span class="rounded-full border border-[#3e484f] px-2 py-0.5 text-[10px] text-[#71808a]">已归档</span>
                      </div>
                      <Show when={project.description}><p class="mt-2 line-clamp-3 text-xs leading-5 text-[#8b99a4]">{project.description}</p></Show>
                      <div class="mt-3 text-[10px] text-[#596873]">归档于 {formatTime(project.archived_at || project.updated_at)}</div>
                      <div class="mt-3 flex flex-wrap gap-2">
                        <button
                          class="rounded-lg border border-[#303539] px-2.5 py-1.5 text-[11px] text-[#bdc8d1] disabled:opacity-40"
                          disabled={activeProjects().length >= 1 || busy() === `project:${project.research_id}`}
                          title={activeProjects().length >= 1 ? '请先归档当前课题，再恢复历史课题' : ''}
                          onClick={() => void changeProjectStatus(project)}
                        >恢复课题</button>
                        <button class="rounded-lg border border-[#38bdf8]/25 px-2.5 py-1.5 text-[11px] text-[#8ed5ff]" onClick={() => { setSelectedProjectId(project.research_id); setStateView('overview'); setTab('state'); }}>只读查看</button>
                        <button class="rounded-lg border border-[#38b46a]/25 px-2.5 py-1.5 text-[11px] text-[#9ee6bb]" onClick={() => setShareTarget({ mode: 'project', researchId: project.research_id, title: project.title })}>导出分享</button>
                        <button class="rounded-lg border border-[#f85149]/30 px-2.5 py-1.5 text-[11px] text-[#ff9b96] disabled:opacity-50" disabled={busy() === `project-delete:${project.research_id}`} onClick={() => void permanentlyDeleteProject(project)}>永久删除</button>
                      </div>
                    </article>
                  )}
                </For>
              </div>
            </section>
          </div>
        </Show>

        <Show when={!loading() && (tab() === 'import' || tab() === 'upload')}>

          <div class="mx-auto max-w-5xl space-y-4">
            <Show when={tab() === 'import'}><Show when={activeProjects()[0]}>{project => <ResearchStatePanel onEvidenceMap={p=>{setSelectedProjectId(p.research_id);setTab('evidence');}} workspaceId={workspaceId()} projects={projects()} initialResearchId={project().research_id} view="assets" onShare={p => setShareTarget({mode:'project',researchId:p.research_id,title:p.title})} onProjectChanged={() => load()} />}</Show><section class="rounded-xl border border-[#2a3136] bg-[#11171b]/70 p-4">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div class="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#c4a7ff]">收到的研究资料</div>
                  <h2 class="mt-1 text-base font-semibold text-[#edf2f7]">已导入的研究资料</h2>
                  <p class="mt-1 max-w-3xl text-xs leading-5 text-[#8b99a4]">
                    来源项目只是资料包内部视图。Agent 会按问题渐进读取；相关后续研究自动记录在本地接力记录中。
                  </p>
                </div>
                <button type="button" class="rounded-lg border border-[#30383e] px-3 py-2 text-xs" onClick={() => setTab('upload')}>检查并导入研究资料</button>
              </div>
              <Show when={packAssets().length === 0}>
                <div class="mt-4 rounded-xl border border-dashed border-[#34404a] p-8 text-center text-sm text-[#71808a]">
                  还没有导入研究资料。点击“检查并导入研究资料”选择研究包，先核对内容再接收。
                </div>
              </Show>
              <div class="research-scroll-list mt-4 grid gap-3 lg:grid-cols-2" aria-label="已导入研究资料列表" tabIndex={0}>
                <For each={packAssets()}>
                  {(asset) => (
                    <div><ImportedResearchPackCard
                      asset={asset}
                      onOpen={setOpenedAsset}
                      workspaceId={workspaceId()}
                      onUseInChat={usePackInChat}
                      onReproduce={reproducePackExperiment}
                      onReexport={(item) => setShareTarget({
                        mode: 'asset',
                        assetId: item.asset_id,
                        title: item.title,
                      })}
                    /><button type="button" class="mt-2 rounded border px-3 py-2 text-xs" disabled={Boolean(busy())} onClick={() => void removeListing(asset.asset_id, 'asset')}>移除资料（可恢复）</button></div>
                  )}
                </For>
              </div>
              {listingPager('asset')}
            </section></Show>

            <Show when={tab() === 'upload'}><section class="rounded-xl border border-[#2a3136] bg-[#11171b] p-4">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div class="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#c4a7ff]">ResearchPack Importer</div>
                  <h2 class="mt-1 text-base font-semibold text-[#edf2f7]">检查并导入研究资料</h2>
                  <p class="mt-1 max-w-3xl text-xs leading-5 text-[#8b99a4]">
                    上传只会触发静态检查；包内 SKILL、脚本、依赖、MCP 和配置不会执行或注册。导入后创建资料包资产和来源项目视图，不创建本地正式课题，也不占用三个课题名额。
                  </p>
                </div>
                <span class="rounded-full border border-[#c4a7ff]/30 bg-[#c4a7ff]/8 px-2.5 py-1 text-[11px] text-[#d8c8ff]">只读来源资料 · 本地接力记录</span>
              </div>
              <Show when={!packImportEnabled()}>
                <div class="mt-3 rounded-lg border border-[#f1a02b]/30 bg-[#f1a02b]/10 px-3 py-2 text-xs text-[#ffc176]">
                  当前 Workspace 未开启 ResearchPack Importer。
                </div>
              </Show>
              <label class="mt-4 flex cursor-pointer items-center justify-center rounded-xl border border-dashed border-[#45515a] bg-[#0d1216] px-4 py-8 text-center hover:border-[#c4a7ff]/50">
                <div>
                  <div class="text-sm font-semibold text-[#d7dee4]">选择 .researchpack.zip</div>
                  <div class="mt-1 text-xs text-[#71808a]">先检查文件安全、压缩大小、内容完整性、格式兼容性、冲突记录和访问权限</div>
                </div>
                <input
                  class="hidden"
                  type="file"
                  accept=".zip,.researchpack.zip,application/zip"
                  disabled={!packImportEnabled() || busy() === 'pack-inspect' || busy() === 'pack-import'}
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    void inspectPack(file);
                    event.currentTarget.value = '';
                  }}
                />
              </label>
              <Show when={busy() === 'pack-inspect'}>
                <div class="mt-3 text-center text-xs text-[#d8c8ff]">正在进行安全检查，不会执行包内内容…</div>
              </Show>
            </section>

            <Show when={packInspection()} keyed>
              {(inspection) => {
                const report = inspection.inspection_report || {};
                const duplicate = report.duplicate;
                const counts = report.import_preview || report.contents || {};
                return (
                  <section class="rounded-xl border border-[#38b46a]/30 bg-[#11171b] p-4">
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div class="text-[10px] font-semibold uppercase tracking-wider text-[#78d89d]">检查通过</div>
                        <h3 class="mt-1 text-base font-semibold text-[#edf8f1]">{String(report.researches?.[0]?.title || '未命名研究包')}</h3>
                      </div>
                      <div class="flex flex-wrap gap-2 text-[10px]">
                        <span class="rounded-full border border-[#38b46a]/30 px-2 py-1 text-[#9ee6bb]">Checksum 已验证</span>
                        <span class="rounded-full border border-[#38b46a]/30 px-2 py-1 text-[#9ee6bb]">无自动执行权限</span>
                        <span class="rounded-full border border-[#30383e] px-2 py-1 text-[#9faeb7]">资料格式第 {inspection.schema_version} 版</span>
                      </div>
                    </div>
                    <div class="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                      <For each={Object.entries(counts)}>
                        {([key, value]) => (
                          <div class="rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
                            <div class="text-[10px] uppercase text-[#596873]">{researchFieldLabel(key,'相关记录数量')}</div>
                            <div class="mt-1 text-sm font-semibold text-[#d7dee4]">{Number(value || 0)}</div>
                          </div>
                        )}
                      </For>
                    </div>
                    <div class="mt-3 grid gap-2 sm:grid-cols-3 text-xs text-[#8b99a4]">
                      <div>压缩包：{formatBytes(report.archive_bytes)}</div>
                      <div>解压后：{formatBytes(report.uncompressed_bytes)}</div>
                      <div>文件：{Number(report.file_count || 0)}</div>
                    </div>
                    <Show when={duplicate?.already_imported}>
                      <div class="mt-3 rounded-lg border border-[#38bdf8]/25 bg-[#38bdf8]/8 px-3 py-2 text-xs text-[#8ed5ff]">
                        这个研究包已导入过。可以打开已有资料，或明确创建另一个独立接力分支。
                      </div>
                    </Show>
                    <label class="mt-4 block text-xs text-[#9faeb7]">
                      资料包显示名称
                      <input
                        class="mt-1 h-9 w-full rounded-lg border border-[#303539] bg-[#0d1216] px-3 text-sm text-[#e6edf3] outline-none focus:border-[#c4a7ff]/50"
                        value={packImportTitle()}
                        maxlength={160}
                        onInput={(event) => setPackImportTitle(event.currentTarget.value)}
                      />
                    </label>
                    <div class="mt-4 flex flex-wrap gap-2">
                      <button
                        class="rounded-lg border border-[#38b46a]/35 bg-[#38b46a]/10 px-3 py-2 text-xs font-semibold text-[#9ee6bb] disabled:opacity-40"
                        disabled={busy() === 'pack-import'}
                        onClick={() => void commitPackImport('copy')}
                      >
                        {busy() === 'pack-import' ? '导入中…' : '导入为研究资料'}
                      </button>
                      <Show when={duplicate?.already_imported}>
                        <button
                          class="rounded-lg border border-[#38bdf8]/30 bg-[#38bdf8]/8 px-3 py-2 text-xs font-semibold text-[#8ed5ff] disabled:opacity-40"
                          disabled={busy() === 'pack-import'}
                          onClick={() => void commitPackImport('open_existing')}
                        >
                          打开已有资料
                        </button>
                      </Show>
                      <button
                        class="rounded-lg border border-[#30383e] px-3 py-2 text-xs text-[#9faeb7] disabled:opacity-40"
                        disabled={busy() === 'pack-import'}
                        onClick={() => void commitPackImport('cancel')}
                      >
                        取消并清理暂存文件
                      </button>
                    </div>
                  </section>
                );
              }}
            </Show>

            <section class="rounded-xl border border-[#2a3136] bg-[#11171b] p-4">
              <h3 class="text-sm font-semibold text-[#d7dee4]">最近导入记录</h3>
              <Show when={packImports().length === 0}>
                <div class="mt-3 rounded-lg border border-dashed border-[#34404a] p-6 text-center text-xs text-[#71808a]">还没有 ResearchPack 导入记录。</div>
              </Show>
              <div class="research-scroll-list mt-3 space-y-2" aria-label="导入记录列表" tabIndex={0}>
                <For each={packImports()}>
                  {(item) => (
                    <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#293238] bg-[#0d1216] px-3 py-2">
                      <div class="min-w-0">
                        <div class="truncate text-xs font-semibold text-[#bdc8d1]">{userFacingTitle(item.source_manifest?.research?.title, '研究资料包')}</div>
                      </div>
                      <div class="text-right text-[10px] text-[#71808a]">
                        <div class={item.status === 'completed' ? 'text-[#9ee6bb]' : item.status === 'failed' ? 'text-[#fca5a5]' : 'text-[#ffc176]'}>{researchStatusLabel(item.status)}</div>
                        <div>{formatTime(item.imported_at || item.inspected_at)}</div>
                      </div>
                      <button type="button" disabled={Boolean(busy()) || item.status === 'importing'} onClick={() => void removeListing(item.import_id, 'import')}>移除记录（可恢复）</button>
                    </div>
                  )}
                </For>
              </div>
              {listingPager('import')}
            </section></Show>
          </div>
        </Show>

        <Show when={!loading() && tab() === 'state'}>
          <div class="research-project-detail">
          <ResearchStatePanel
            onEvidenceMap={p=>{setSelectedProjectId(p.research_id);setTab('evidence');}}
            workspaceId={workspaceId()}
            projects={projects()}
            view={stateView()}
            initialResearchId={selectedProjectId()}
            changeFocus={props.focusRequest?.comparison}
            initialProvenanceOpen={provenanceProjectId() === selectedProjectId()}
            onShare={p => setShareTarget({mode:'project',researchId:p.research_id,title:p.title})}
            onProjectChanged={() => load()}
          />
          </div>
        </Show>
      </div>
  );

  return (
    <section class="research-workspace flex min-w-0 flex-1 flex-col overflow-hidden bg-[#0f1418]/76">
      <header class="border-b border-[#2a3136] bg-[#0d1216]/90 px-5 py-4">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 class="mt-1 text-xl font-semibold text-[#f7fafc]">研究中心</h1>
          </div>
          <div class="flex flex-wrap gap-2">
            <button type="button" class="h-9 rounded-lg border px-3 text-xs" onClick={() => setTab('history')}>当前课题与历史</button>
            <details ref={moreMenu} class="research-more" onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();event.currentTarget.open=false;event.currentTarget.querySelector('summary')?.focus();}}}><summary>更多</summary><div class="research-more-actions" role="group" aria-label="研究中心更多操作" onClick={event=>{if((event.target as HTMLElement).closest('button')){moreMenu!.open=false;moreMenu!.querySelector('summary')?.focus();}}}>
              <button type="button" onClick={() => setTab('candidates')}><span>课题候选与依据</span><small>查看候选课题及发现依据</small></button>
              <button type="button" onClick={() => setTab('pending')}><span>可选整理</span><small>查看待整理的研究内容</small></button>
              <button type="button" onClick={() => setShareTarget({mode:'workspace_period',title:'当前工作区的研究经历'})}><span>分享一段研究经历</span><small>选择并分享工作区的研究经历</small></button>
              <button type="button" onClick={() => setTab('advanced')}><span>高级诊断与移除恢复</span><small>检查记录或恢复已移除资料</small></button>
            </div></details>
            <button
              class="h-9 rounded-lg border border-[#303539] bg-[#171c20] px-3 text-xs font-semibold text-[#bdc8d1] disabled:opacity-50"
              disabled={loading()}
              onClick={() => void load()}
            >
              刷新
            </button>
          </div>
        </div>

        <Show when={Boolean(health()) && !topicsEnabled()}>
          <div class="mt-3 rounded-lg border border-[#f1a02b]/30 bg-[#f1a02b]/10 px-3 py-2 text-xs text-[#ffc176]">
            当前 Workspace 未开启 Topic Candidate Preview。已记录的 Session 不受影响。
          </div>
        </Show>
        <Show when={error()}>
          <div class="mt-3 rounded-lg border border-[#ef4444]/30 bg-[#ef4444]/10 px-3 py-2 text-xs text-[#fca5a5]">{error()}</div>
        </Show>
        <Show when={notice()}>
          <div class="mt-3 rounded-lg border border-[#38bdf8]/25 bg-[#38bdf8]/8 px-3 py-2 text-xs text-[#8ed5ff]">{notice()}</div>
        </Show>
        <nav aria-label="研究中心导航" class="elastic-tabs mt-4 flex gap-2 overflow-x-auto">
          <TabIndicator active={['projects','import','review'].includes(tab()) ? tab() : returnTab()} />
          <For each={[
            { key: 'projects' as const, label: '研究概览' },
            { key: 'import' as const, label: '资料与成果' },
            { key: 'review' as const, label: '需要处理' },
          ]}>
            {(item) => (
              <button
                aria-pressed={( ['projects','import','review'].includes(tab()) ? tab() : returnTab()) === item.key}
                data-active={( ['projects','import','review'].includes(tab()) ? tab() : returnTab()) === item.key}
                class={`rounded-full border px-3 py-1.5 text-xs font-semibold ${tab() === item.key ? 'border-[#38bdf8]/35 bg-[#38bdf8]/12 text-[#8ed5ff]' : 'border-[#303539] bg-[#171c20] text-[#8b949e]'}`}
                onClick={() => setTab(item.key)}
              >
                {item.label}
              </button>
            )}
          </For>
        </nav>
      </header>

      <Show when={['projects','import','review'].includes(tab())}>{renderContent()}</Show>
      <Show when={tab()==='evidence'}><div class="research-changes-page"><header><button type="button" onClick={()=>setTab('projects')}>返回研究概览</button><div><small>假说与证据地图</small><h2>{projects().find(p=>p.research_id===selectedProjectId())?.title}</h2></div></header><Show when={projects().find(p=>p.research_id===selectedProjectId())}>{project=><HypothesisEvidenceMap project={project()} workspaceId={workspaceId()} focus={props.focusRequest?.hypothesis}/>}</Show></div></Show>
      <Show when={tab()==='changes'}><div class="research-changes-page"><header><button type="button" onClick={()=>setTab('projects')}>返回研究概览</button><div><small>研究变化比较</small><h2>{projects().find(p=>p.research_id===selectedProjectId())?.title}</h2></div></header><Show when={projects().find(p=>p.research_id===selectedProjectId())}>{project=><ResearchChangesPanel project={project()} workspaceId={workspaceId()} focus={props.focusRequest?.comparison}/>}</Show></div></Show>
      <Show when={!['projects','import','review','changes','evidence'].includes(tab())}>
        <ResearchDialog title={viewTitles[tab()]} onClose={() => setTab(returnTab())} busy={Boolean(busy())}>
          <Show when={error()}><p role="alert">{error()}</p></Show>
          <Show when={notice()}><p role="status">{notice()}</p></Show>
          {renderContent()}
        </ResearchDialog>
      </Show>
      <Show when={openedAsset()} keyed>{asset => <ResearchDialog title={`研究资料 · ${asset.title}`} onClose={() => setOpenedAsset(null)}>
        <ImportedResearchPackCard asset={asset} workspaceId={workspaceId()} onUseInChat={item => { usePackInChat(item); setOpenedAsset(null); setTab('projects'); }}
          onReproduce={reproducePackExperiment} onReexport={item => setShareTarget({mode:'asset', assetId:item.asset_id, title:item.title})} />
      </ResearchDialog>}</Show>
      <Show when={shareTarget()} keyed>
        {(target) => (
          <ResearchPackShareDialog
            target={target}
            workspaceId={workspaceId()}
            onClose={() => setShareTarget(null)}
            onBuilt={() => {
              setNotice('研究资料包已生成，可以下载或继续留在当前工作区。');
              void load();
            }}
          />
        )}
      </Show>
    </section>
  );
}
