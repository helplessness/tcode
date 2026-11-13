import {researchAgentLabel} from '../utils/userFacingLabels';
import MarkdownContent from './MarkdownContent';
import {readableResearchMarkdown} from '../utils/researchMarkdown';
import { promptAction, confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createSignal, on } from 'solid-js';
import {
  excludeResearchEvent,
  getResearchProjectProvenance,
  renameResearchCandidate,
  type ResearchProjectProvenance,
  type ResearchSourceCategory,
} from '../api/research';

type Props = {
  researchId: string;
  workspaceId?: string;
  initialOpen?: boolean;
  onChanged?: () => void | Promise<void>;
};

const formatTime = (value?: string) => {
  const timestamp = Date.parse(String(value || ''));
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : String(value || '');
};

const titleSourceLabels: Record<string, string> = {
  user: '用户命名',
  paper_metadata: '根据论文信息归纳',
  auto_summary: '根据研究意图归纳',
};

const titleSourceLabel = (value?: string) => titleSourceLabels[String(value || '')] || '系统归纳';

const sourceTones: Record<string, string> = {
  message: 'border-[#38bdf8]/25 bg-[#38bdf8]/6 text-[#8ed5ff]',
  tool: 'border-[#f1a02b]/25 bg-[#f1a02b]/6 text-[#ffc176]',
  mcp: 'border-[#f1a02b]/25 bg-[#f1a02b]/6 text-[#ffc176]',
  artifact: 'border-[#c4a7ff]/25 bg-[#c4a7ff]/6 text-[#d8c8ff]',
  workflow: 'border-[#c4a7ff]/25 bg-[#c4a7ff]/6 text-[#d8c8ff]',
  agent: 'border-[#38b46a]/25 bg-[#38b46a]/6 text-[#9ee6bb]',
  file: 'border-[#c4a7ff]/25 bg-[#c4a7ff]/6 text-[#d8c8ff]',
  user_intent: 'border-[#38bdf8]/25 bg-[#38bdf8]/6 text-[#8ed5ff]',
  tool_result: 'border-[#f1a02b]/25 bg-[#f1a02b]/6 text-[#ffc176]',
  agent_result: 'border-[#38b46a]/25 bg-[#38b46a]/6 text-[#9ee6bb]',
};

const sourceTone = (kind: string) => sourceTones[kind] || 'border-[#30383e] bg-[#171d21] text-[#bdc8d1]';

const operationLabels: Record<string, string> = {
  confirm_project: '确认为正式课题',
  rename_candidate: '修改课题名称',
  include_event: '纳入研究记录',
  move_event: '移动研究记录',
  exclude_event: '移出研究记录',
  attribution_exclude: '设为私人内容',
  attribution_include: '恢复为研究内容',
};

const operationLabel = (action: string) => operationLabels[action] || '课题变更（类型尚未说明）';
const KEY_SOURCES_PAGE_SIZE = 10;
type SourceCategoryFilter = ResearchSourceCategory | 'all';
const sourceCategories: Array<{ key: ResearchSourceCategory; label: string }> = [
  { key: 'message', label: '对话' },
  { key: 'tool', label: '工具' },
  { key: 'mcp', label: 'MCP' },
  { key: 'artifact', label: '研究产物' },
  { key: 'workflow', label: '工作流' },
  { key: 'agent', label: '协作助手' },
  { key: 'file', label: '文件' },
  { key: 'other', label: '其他' },
];
const sourceCategoryOrder = new Map(sourceCategories.map((item, index) => [item.key, index]));

const sourceCategoryOf = (
  source: ResearchProjectProvenance['key_sources'][number],
): ResearchSourceCategory => {
  if (source.category) return source.category;
  if (source.kind === 'user_intent' || source.kind === 'agent_result') return 'message';
  if (source.kind === 'file') return 'file';
  if (source.kind === 'tool_result') return 'tool';
  return 'other';
};

export default function ResearchProjectProvenancePanel(props: Props) {
  const [data, setData] = createSignal<ResearchProjectProvenance | null>(null);
  const [opened, setOpened] = createSignal(Boolean(props.initialOpen));
  const [loading, setLoading] = createSignal(false);
  const [busy, setBusy] = createSignal('');
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [sourceCategory, setSourceCategory] = createSignal<SourceCategoryFilter>('all');
  const [keySourcesPage, setKeySourcesPage] = createSignal(1);

  const filteredKeySources = createMemo(() => {
    const selected = sourceCategory();
    return [...(data()?.key_sources || [])]
      .filter((source) => selected === 'all' || sourceCategoryOf(source) === selected)
      .sort((left, right) => {
        const categoryDelta = (sourceCategoryOrder.get(sourceCategoryOf(left)) || 0)
          - (sourceCategoryOrder.get(sourceCategoryOf(right)) || 0);
        if (categoryDelta !== 0) return categoryDelta;
        return Date.parse(right.occurred_at) - Date.parse(left.occurred_at);
      });
  });
  const sourceCounts = createMemo(() => {
    const derived: Partial<Record<ResearchSourceCategory, number>> = {};
    for (const source of data()?.key_sources || []) {
      const category = sourceCategoryOf(source);
      derived[category] = (derived[category] || 0) + 1;
    }
    return {
      ...derived,
      ...(data()?.source_counts || {}),
    };
  });
  const keySourcesPageCount = createMemo(() => Math.max(
    1,
    Math.ceil(filteredKeySources().length / KEY_SOURCES_PAGE_SIZE),
  ));
  const pagedKeySources = createMemo(() => {
    const page = Math.min(Math.max(1, keySourcesPage()), keySourcesPageCount());
    const start = (page - 1) * KEY_SOURCES_PAGE_SIZE;
    return filteredKeySources().slice(start, start + KEY_SOURCES_PAGE_SIZE);
  });

  const load = async () => {
    if (!props.researchId) {
      setData(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      setData(await getResearchProjectProvenance(props.researchId, props.workspaceId));
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : '加载课题来源失败');
    } finally {
      setLoading(false);
    }
  };

  createEffect(() => {
    if (props.initialOpen) setOpened(true);
  });

  createEffect(on(
    () => [props.researchId, sourceCategory()] as const,
    () => setKeySourcesPage(1),
  ));

  createEffect(() => {
    const pageCount = keySourcesPageCount();
    if (keySourcesPage() > pageCount) setKeySourcesPage(pageCount);
  });

  createEffect(on(
    () => [props.researchId, props.workspaceId, opened()] as const,
    ([, , isOpen]) => {
      if (isOpen) void load();
    },
  ));

  const renameProject = async () => {
    const candidate = data()?.origin_candidates[0];
    if (!candidate) return;
    const title = (await promptAction('输入新的课题名称', data()?.project.title || candidate.suggested_title))?.trim();
    if (!title || title === data()?.project.title) return;
    setBusy('rename');
    setError('');
    try {
      await renameResearchCandidate(candidate.candidate_id, title, props.workspaceId);
      setNotice('课题名称已更新，历史名称和修改操作仍然保留。');
      await load();
      await props.onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '修改课题名称失败');
    } finally {
      setBusy('');
    }
  };

  const excludeSource = async (eventId: string) => {
    if (!await confirmAction('将这条记录移出正式课题并保留在私人会话中？原始会话不会删除。')) return;
    setBusy(`exclude:${eventId}`);
    setError('');
    try {
      await excludeResearchEvent(eventId, '用户从正式课题来源面板移出记录', props.workspaceId);
      setNotice('该记录已移出课题；原始会话仍然保留。');
      await load();
      await props.onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : '移出课题来源失败');
    } finally {
      setBusy('');
    }
  };

  return (
    <details
      open={opened()}
      class="rounded-xl border border-[#2a3136] bg-[#11171b] p-4"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary class="cursor-pointer list-none select-none">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div class="flex items-center gap-2">
              <h3 class="text-sm font-semibold text-[#e6edf3]">课题来源与归属</h3>
              <span class="rounded-full border border-[#64748b]/30 px-2 py-0.5 text-[9px] text-[#9faeb7]">按需查看</span>
            </div>
            <p class="mt-1 text-[11px] leading-5 text-[#71808a]">查看这个课题如何形成、依据了哪些资料，并纠正错误归属。</p>
          </div>
          <Show when={data()} keyed>
            {(item) => (
              <div class="flex flex-wrap gap-2 text-[10px] text-[#8b99a4]">
                <span>{item.counts.sessions} 次会话</span>
                <span>{item.counts.files} 份资料</span>
                <span>{item.counts.activities} 项研究活动</span>
              </div>
            )}
          </Show>
        </div>
      </summary>

      <Show when={loading()}><div class="py-8 text-center text-xs text-[#71808a]">正在整理课题来源…</div></Show>
      <Show when={error()}><div class="mt-3 rounded-lg border border-[#ef4444]/25 bg-[#ef4444]/8 px-3 py-2 text-xs text-[#fca5a5]">{error()}</div></Show>
      <Show when={notice()}><div class="mt-3 rounded-lg border border-[#38bdf8]/25 bg-[#38bdf8]/8 px-3 py-2 text-xs text-[#8ed5ff]">{notice()}</div></Show>

      <Show when={!loading() && data()} keyed>
        {(item) => (
          <div class="mt-4 space-y-4 border-t border-[#293238] pt-4">
            <div class="grid gap-3 lg:grid-cols-[1.35fr_0.65fr]">
              <div class="rounded-lg border border-[#29333a] bg-[#151b20] p-3">
                <div class="text-[10px] font-semibold tracking-wider text-[#7bd0ff]">最初研究意图</div>
                <p class="mt-1 text-xs leading-6 text-[#d7dee4]">{item.initial_intent || '没有可读的初始研究意图'}</p>
              </div>
              <div class="rounded-lg border border-[#29333a] bg-[#151b20] p-3 text-xs text-[#9faeb7]">
                <div class="flex items-center justify-between gap-2">
                  <span>形成方式</span>
                  <span class="text-[#d7dee4]">{titleSourceLabel(item.origin_candidates[0]?.title_source)}</span>
                </div>
                <div class="mt-2 flex items-center justify-between gap-2">
                  <span>确认时间</span>
                  <span class="text-right text-[#d7dee4]">{formatTime(item.confirmation?.occurred_at || item.project.created_at)}</span>
                </div>
                <button
                  class="mt-3 rounded border border-[#303539] px-2.5 py-1.5 text-[11px] text-[#bdc8d1] disabled:opacity-50"
                  disabled={!item.origin_candidates.length || busy() === 'rename'}
                  onClick={() => void renameProject()}
                >
                  {busy() === 'rename' ? '修改中…' : '修改课题名称'}
                </button>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
              <For each={[
                ['会话', item.counts.sessions],
                ['活动', item.counts.activities],
                ['文件', item.counts.files],
                ['产物组', item.counts.artifact_groups],
                ['研究产物', item.counts.artifacts],
                ['工具', item.counts.tools],
                ['协作助手', item.counts.agents],
                ['来源记录', item.counts.events],
              ] as Array<[string, number]>}>
                {([label, value]) => (
                  <div class="rounded-lg border border-[#29333a] bg-[#151b20] px-2 py-2 text-center">
                    <div class="text-sm font-semibold text-[#d7dee4]">{value}</div>
                    <div class="mt-0.5 text-[9px] text-[#71808a]">{label}</div>
                  </div>
                )}
              </For>
            </div>

            <Show when={item.origin_candidates.length > 0}>
              <section>
                <div class="text-[11px] font-semibold text-[#9faeb7]">来源候选</div>
                <div class="mt-2 space-y-2">
                  <For each={item.origin_candidates}>
                    {(candidate) => (
                      <div class="rounded-lg border border-[#29333a] bg-[#151b20] px-3 py-2">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <span class="text-xs font-semibold text-[#d7dee4]">{candidate.suggested_title}</span>
                          <span class="rounded-full border border-[#38b46a]/25 px-2 py-0.5 text-[9px] text-[#9ee6bb]">已确认为当前课题</span>
                        </div>
                        <div class="mt-1 text-[10px] text-[#71808a]">
                          {candidate.supporting_event_count} 条记录 · {candidate.session_count} 次会话 · {candidate.tool_count} 次工具执行 · 稳定度 {Math.round(candidate.stability_score * 100)}%
                        </div>
                      </div>
                    )}
                  </For>
                </div>
              </section>
            </Show>

            <Show when={item.key_sources.length > 0}>
              <section>
                <div class="flex items-center justify-between gap-3">
                  <div class="text-[11px] font-semibold text-[#9faeb7]">主要形成依据</div>
                  <div class="text-right text-[10px] text-[#596873]">
                    <div>
                      共 {item.key_sources.length} 条
                      <Show when={sourceCategory() !== 'all'}>{` · 当前筛选 ${filteredKeySources().length} 条`}</Show>
                      {` · 每页 ${KEY_SOURCES_PAGE_SIZE} 条`}
                    </div>
                    <div class="mt-0.5">错误内容可以移出课题，原始会话不会删除</div>
                  </div>
                </div>
                <div class="mt-3 flex flex-wrap gap-2">
                  <For each={[
                    { key: 'all' as const, label: '全部', count: item.key_sources.length },
                    ...sourceCategories.map((category) => ({
                      ...category,
                      count: Number(sourceCounts()[category.key] || 0),
                    })),
                  ]}>
                    {(category) => (
                      <button
                        type="button"
                        class={`rounded-full border px-2.5 py-1 text-[11px] ${sourceCategory() === category.key ? 'border-[#38bdf8]/45 bg-[#38bdf8]/12 text-[#8ed5ff]' : 'border-[#30383e] text-[#8b99a4]'}`}
                        onClick={() => setSourceCategory(category.key)}
                      >
                        {category.label} {category.count}
                      </button>
                    )}
                  </For>
                </div>
                <Show when={filteredKeySources().length === 0}>
                  <div class="mt-3 rounded-lg border border-dashed border-[#34404a] p-6 text-center text-xs text-[#71808a]">
                    当前分类没有主要形成依据。
                  </div>
                </Show>
                <div class="mt-2 space-y-2">
                  <For each={pagedKeySources()}>
                    {(source) => (
                      <div class={`rounded-lg border px-3 py-2 ${sourceTone(sourceCategoryOf(source))}`}>
                        <div class="flex items-start justify-between gap-3">
                          <div class="min-w-0 flex-1">
                            <div class="text-[10px] font-semibold">{source.title}</div>
                            <div class="research-progress-markdown mt-2"><MarkdownContent content={readableResearchMarkdown(source.summary)} allowSessionActions={false}/></div>
                            <div class="mt-1 text-[9px] text-[#71808a]">{formatTime(source.occurred_at)}</div>
                          </div>
                          <Show when={source.can_exclude}>
                            <button
                              class="shrink-0 rounded border border-[#ef4444]/20 px-2 py-1 text-[10px] text-[#fca5a5] disabled:opacity-50"
                              disabled={busy() === `exclude:${source.event_id}`}
                              onClick={() => void excludeSource(source.event_id)}
                            >
                              移出课题
                            </button>
                          </Show>
                        </div>
                      </div>
                    )}
                  </For>
                </div>
                <Show when={keySourcesPageCount() > 1}>
                  <div class="mt-3 flex items-center justify-between gap-3 border-t border-[#29333a] pt-3">
                    <button
                      type="button"
                      class="rounded border border-[#30383e] bg-[#151b20] px-3 py-1.5 text-[10px] text-[#bdc8d1] transition-colors hover:border-[#38bdf8]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-40"
                      disabled={keySourcesPage() <= 1}
                      onClick={() => setKeySourcesPage((page) => Math.max(1, page - 1))}
                    >
                      上一页
                    </button>
                    <span class="text-[10px] text-[#71808a]">
                      第 {keySourcesPage()} / {keySourcesPageCount()} 页
                    </span>
                    <button
                      type="button"
                      class="rounded border border-[#30383e] bg-[#151b20] px-3 py-1.5 text-[10px] text-[#bdc8d1] transition-colors hover:border-[#38bdf8]/35 hover:text-[#8ed5ff] disabled:cursor-not-allowed disabled:opacity-40"
                      disabled={keySourcesPage() >= keySourcesPageCount()}
                      onClick={() => setKeySourcesPage((page) => Math.min(keySourcesPageCount(), page + 1))}
                    >
                      下一页
                    </button>
                  </div>
                </Show>
              </section>
            </Show>

            <Show when={item.activities.length > 0}>
              <section>
                <div class="text-[11px] font-semibold text-[#9faeb7]">形成课题的研究活动</div>
                <div class="mt-2 space-y-2">
                  <For each={item.activities.slice(0, 6)}>
                    {(activity) => (
                      <div class="rounded-lg border border-[#29333a] bg-[#151b20] px-3 py-2">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <span class="text-xs font-medium text-[#d7dee4]">{activity.title || '未命名研究活动'}</span>
                          <span class="text-[9px] text-[#71808a]">{formatTime(activity.ended_at || activity.started_at)}</span>
                        </div>
                        <Show when={activity.summary}><div class="research-progress-markdown mt-2"><MarkdownContent content={readableResearchMarkdown(activity.summary)} allowSessionActions={false}/></div></Show>
                        <Show when={(activity.agent_calls || []).length > 0}>
                          <div class="mt-1 text-[10px] text-[#8ed5ff]">
                            协作助手：{(activity.agent_calls || []).map((agent) => researchAgentLabel(agent.name)).join('、')}
                          </div>
                        </Show>
                      </div>
                    )}
                  </For>
                </div>
              </section>
            </Show>

            <Show when={item.history.length > 0}>
              <details class="rounded-lg border border-dashed border-[#29333a] px-3 py-2">
                <summary class="cursor-pointer text-[11px] font-semibold text-[#8b99a4]">查看归属变更历史（{item.history.length}）</summary>
                <div class="mt-2 space-y-1.5">
                  <For each={item.history}>
                    {(operation) => (
                      <div class="flex flex-wrap items-center justify-between gap-2 rounded bg-[#0d1216] px-2 py-1.5 text-[10px] text-[#71808a]">
                        <span>{operationLabel(operation.action)}{operation.reason ? ` · ${operation.reason}` : ''}</span>
                        <span>{formatTime(operation.occurred_at)}</span>
                      </div>
                    )}
                  </For>
                </div>
              </details>
            </Show>
          </div>
        )}
      </Show>
    </details>
  );
}
