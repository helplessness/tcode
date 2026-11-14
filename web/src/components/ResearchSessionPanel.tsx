import { researchStatusLabel, researchFieldLabel, researchValueText, userFacingTitle, toolDisplayLabel } from '../utils/userFacingLabels';
import { continuationNotice, continuationBudgetExhausted, formatKnownCost } from '../utils/runBudget';
import ResearchRecordDetails from './ResearchRecordDetails';
import { proteinRunArtifactUrl } from '../api/proteinRuns';
import { isStructurePath } from './StructureWorkspace';
import { confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createMemo, createSignal, on } from 'solid-js';
import {
  compareResearchExperiments,
  compareResearchReproduction,
  controlResearchContinuation,
  createResearchExperiment,
  createResearchHypothesis,
  getResearchCheckpoint,
  getResearchContinuation,
  getResearchKnowledge,
  getResearchProjectProvenance,
  generateResearchHypotheses,
  listResearchExperiments,
  listResearchExperimentRuns,
  listResearchFindings,
  listResearchHypotheses,
  reviseResearchHypothesis,
  setResearchHypothesisStatus,
  startResearchContinuation,
  updateResearchExperiment,
  type ResearchCheckpoint,
  type ResearchContinuation,
  type ResearchExperiment,
  type ResearchFinding,
  type ResearchHypothesis,
  type ResearchKnowledgeSnapshot,
  type ResearchProjectProvenance,
} from '../api/research';
import './ResearchSessionPanel.css';
import { researchSourceLabel, researchSourceTypeLabel, researchRelationLabel } from '../utils/researchKnowledgeDisplay';

export type ResearchSessionTab = 'overview' | 'hypotheses' | 'experiments' | 'evidence' | 'findings' | 'continuation';


const SnapshotSummary = (props: { experiment: ResearchExperiment }) => {
  const snapshot = () => props.experiment.run_snapshot || {};
  const artifacts = () => (snapshot().stages || []).reduce((count, stage) => count + Object.keys(stage.artifacts || {}).length, 0);
  const validations = () => (snapshot().stages || []).filter((stage) => Object.keys(stage.validation || {}).length > 0).length;
  return <Show when={snapshot().workflow_run_id}>
    <div class="mt-3 rounded-lg border border-[#2a3136] bg-[#101518] p-3 text-[11px] text-[#8b99a4]">
      <div>计算流程：{toolDisplayLabel(snapshot().template_key)} · 模型：{snapshot().environment?.model || '默认'} · 后端：{researchStatusLabel(snapshot().environment?.backend,'执行位置未说明')}</div>
      <div class="mt-1">代码版本：{snapshot().environment?.git_commit?'已保存执行时的代码版本':'未记录'}<Show when={snapshot().environment?.git_dirty}> · 工作区有未提交修改</Show> · 产物 {artifacts()} · 验证记录 {validations()}</div>
      <details class="mt-2"><summary class="cursor-pointer text-[#8ed5ff]">查看冻结输入</summary><ResearchRecordDetails value={snapshot().input||{}}/></details>
    </div>
  </Show>;
};

export default function ResearchSessionPanel(props: { tab: ResearchSessionTab; researchId: string; workspaceId?: string; readOnly?: boolean; onNavigateTab?: (tab: ResearchSessionTab) => void; onChanged?: () => void; onOpenStructure?: () => void }) {
  const [provenance, setProvenance] = createSignal<ResearchProjectProvenance | null>(null);
  const [checkpoint, setCheckpoint] = createSignal<ResearchCheckpoint | null>(null);
  const [continuation, setContinuation] = createSignal<ResearchContinuation | null>(null);
  const [maxRuns, setMaxRuns] = createSignal(10);
  const [maxMinutes, setMaxMinutes] = createSignal(1440);
  const [maxTokens, setMaxTokens] = createSignal(0);
  const [maxToolCalls, setMaxToolCalls] = createSignal(0);
  const [maxFailures, setMaxFailures] = createSignal(3);
  const [maxCost, setMaxCost] = createSignal(0);
  const [hypotheses, setHypotheses] = createSignal<ResearchHypothesis[]>([]);
  const [experiments, setExperiments] = createSignal<ResearchExperiment[]>([]);
  const [runOptions, setRunOptions] = createSignal<Array<{run_id: string; title: string; status: string}>>([]);
  const [runPage, setRunPage] = createSignal(1), [runPages, setRunPages] = createSignal(1), [runError, setRunError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const talkAboutResearch = (prompt: string) => window.dispatchEvent(new CustomEvent('tcode-continue-research', {detail: {
    researchId: props.researchId, workspaceId: props.workspaceId, prompt: `${prompt}\n当前课题请按平台提供的研究上下文定位。请先查询真实研究目标，再展示操作预览；存在同名记录时请让我确认，不要猜测。仅创建记录不代表启动计算；未经独立确认不要提交计算任务。`,
  }}));
  const loadRuns = async (page = 1) => {
    const key = researchLoadKey(); setRunError('');
    try { const result = await listResearchExperimentRuns(props.researchId, props.workspaceId, page);
      if (key !== researchLoadKey()) return;
      setRunOptions(result.items); setRunPage(result.pagination.page); setRunPages(result.pagination.pages);
    } catch (cause) { if (key === researchLoadKey()) setRunError(cause instanceof Error ? cause.message : '运行列表读取失败，可重试或使用高级 ID 输入'); }
  };
  const [knowledge, setKnowledge] = createSignal<ResearchKnowledgeSnapshot | null>(null);
  const [findings, setFindings] = createSignal<ResearchFinding[]>([]);
  const [loading, setLoading] = createSignal(false);
  const [saving, setSaving] = createSignal(false);
  const [error, setError] = createSignal('');
  const [reloadRevision, setReloadRevision] = createSignal(0);
  const [showHypothesisForm, setShowHypothesisForm] = createSignal(false);
  const [editingHypothesis, setEditingHypothesis] = createSignal<ResearchHypothesis | null>(null);
  const [statement, setStatement] = createSignal('');
  const [rationale, setRationale] = createSignal('');
  const [testPlan, setTestPlan] = createSignal('');
  const [falsification, setFalsification] = createSignal('');
  const [showExperimentForm, setShowExperimentForm] = createSignal(false);
  const [parentExperimentId, setParentExperimentId] = createSignal<string | null>(null);
  const [experimentHypothesisId, setExperimentHypothesisId] = createSignal('');
  const [experimentTitle, setExperimentTitle] = createSignal('');
  const [experimentReason, setExperimentReason] = createSignal('');
  const [workflowRunId, setWorkflowRunId] = createSignal('');
  const [executionMode, setExecutionMode] = createSignal<'shared_workspace' | 'isolated_directory' | 'git_worktree'>('shared_workspace');
  const [compareLeft, setCompareLeft] = createSignal('');
  const [compareRight, setCompareRight] = createSignal('');
  const [comparison, setComparison] = createSignal<{ left: ResearchExperiment; right: ResearchExperiment; differences: Record<string, { left: any; right: any }> } | null>(null);
  const [reproductionComparison, setReproductionComparison] = createSignal<Record<string, any> | null>(null);
  // Session 列表会定期刷新并替换 Session 对象。把三个原始属性压缩为
  // 具有值相等判断的 memo，避免相同研究页被轮询重复加载和重建。
  const researchLoadKey = createMemo(() => JSON.stringify([
    props.researchId,
    props.workspaceId || '',
    props.tab,
    reloadRevision(),
  ]));

  const loadHypotheses = async () => {
    const key = researchLoadKey();
    const result = await listResearchHypotheses(props.researchId, props.workspaceId);
    if (key === researchLoadKey()) setHypotheses(result.hypotheses || []);
  };
  const loadExperiments = async () => {
    const key = researchLoadKey();
    const [hypothesisResult, experimentResult] = await Promise.all([
      listResearchHypotheses(props.researchId, props.workspaceId),
      listResearchExperiments(props.researchId, props.workspaceId),
    ]);
    if (key !== researchLoadKey()) return;
    setHypotheses(hypothesisResult.hypotheses || []);
    setExperiments(experimentResult.experiments || []);
  };

  let loadedScope = '';
  createEffect(on(
    researchLoadKey,
    async () => {
    const researchId = props.researchId;
    const workspaceId = props.workspaceId;
    const tab = props.tab;
    const key = researchLoadKey();
    const scope = JSON.stringify([researchId, workspaceId || '']);
    if (scope !== loadedScope) {
      loadedScope = scope;
      setProvenance(null); setCheckpoint(null); setContinuation(null); setHypotheses([]); setExperiments([]); setKnowledge(null); setFindings([]);
      setShowHypothesisForm(false); setShowExperimentForm(false); setComparison(null); setReproductionComparison(null);
      setRunOptions([]); setRunPage(1); setRunPages(1); setRunError(''); setNotice('');
    }
    if (!researchId) return;
    setLoading(true); setError('');
    try {
      if (tab === 'overview' || tab === 'continuation') {
        const [source, state, continuous] = await Promise.all([getResearchProjectProvenance(researchId, workspaceId), getResearchCheckpoint(researchId, workspaceId), getResearchContinuation(researchId, workspaceId)]);
        if (key !== researchLoadKey()) return;
        setProvenance(source); setCheckpoint(state.checkpoint); setContinuation(continuous.continuation);
      } else if (tab === 'hypotheses') await loadHypotheses();
      else if (tab === 'experiments') await loadExperiments();
      else if (tab === 'evidence') {
        const result = await getResearchKnowledge(researchId, workspaceId);
        if (key === researchLoadKey()) setKnowledge(result);
      } else {
        const result = await listResearchFindings(researchId, workspaceId);
        if (key === researchLoadKey()) setFindings(result.findings || []);
      }
    } catch (cause) { if (key === researchLoadKey()) setError(cause instanceof Error ? cause.message : '研究数据加载失败'); }
    finally { if (key === researchLoadKey()) setLoading(false); }
    },
  ));

  const values = (items: Array<Record<string, any>> | undefined) => (items || []).map((item) => String(item.statement || item.value || item.summary || item.title || '').trim()).filter(Boolean);
  const approvedHypotheses = createMemo(() => hypotheses().filter((item) => ['approved', 'testing'].includes(item.status)));
  const hasExperimentBranches = createMemo(() => experiments().some((item) => !!item.parent_experiment_id));
  const hypothesisById = createMemo(() => new Map(hypotheses().map((item) => [item.hypothesis_id, item])));
  const experimentDepth = (item: ResearchExperiment) => {
    const byId = new Map(experiments().map((entry) => [entry.experiment_id, entry]));
    let depth = 0; let parent = item.parent_experiment_id;
    while (parent && depth < 8) { depth += 1; parent = byId.get(parent)?.parent_experiment_id || null; }
    return depth;
  };

  const openHypothesisForm = (item?: ResearchHypothesis) => {
    setEditingHypothesis(item || null); setStatement(item?.statement || ''); setRationale(item?.rationale || '');
    setTestPlan(item?.test_plan || ''); setFalsification(item?.falsification_condition || ''); setShowHypothesisForm(true);
  };
  const saveHypothesis = async () => {
    if (props.readOnly) return;
    if (!statement().trim()) return;
    setSaving(true); setError('');
    const payload = { statement: statement().trim(), rationale: rationale().trim(), test_plan: testPlan().trim(), falsification_condition: falsification().trim() };
    try {
      if (editingHypothesis()) await reviseResearchHypothesis(editingHypothesis()!.hypothesis_id, payload, props.workspaceId);
      else await createResearchHypothesis(props.researchId, payload, props.workspaceId);
      setShowHypothesisForm(false); await loadHypotheses(); props.onChanged?.();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存假说失败'); }
    finally { setSaving(false); }
  };
  const changeHypothesisStatus = async (item: ResearchHypothesis, status: string) => {
    if (props.readOnly) return;
    setSaving(true); setError('');
    try { await setResearchHypothesisStatus(item.hypothesis_id, status, props.workspaceId); await loadHypotheses(); props.onChanged?.(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '更新假说失败'); }
    finally { setSaving(false); }
  };
  const generateHypotheses = async () => {
    if (props.readOnly) return;
    setSaving(true); setError('');
    try { await generateResearchHypotheses(props.researchId, 3, props.workspaceId); await loadHypotheses(); props.onChanged?.(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '生成候选假说失败'); }
    finally { setSaving(false); }
  };
  const openExperimentForm = (parent?: ResearchExperiment) => {
    setParentExperimentId(parent?.experiment_id || null); setExperimentHypothesisId(parent?.hypothesis_id || approvedHypotheses()[0]?.hypothesis_id || '');
    setExperimentTitle(parent ? `${parent.title} 的后续实验` : ''); setExperimentReason(parent ? '基于上一实验结果继续验证' : ''); setWorkflowRunId(''); setExecutionMode(parent?.execution_mode || 'shared_workspace'); setShowExperimentForm(true);
    setRunOptions([]); void loadRuns();
  };
  const saveExperiment = async () => {
    if (props.readOnly) return;
    if (!experimentTitle().trim() || !experimentHypothesisId()) return;
    setSaving(true); setError('');
    try {
      await createResearchExperiment(props.researchId, { hypothesis_id: experimentHypothesisId(), title: experimentTitle().trim(), reason: experimentReason().trim(), workflow_run_id: workflowRunId().trim(), parent_experiment_id: parentExperimentId(), execution_mode: executionMode() }, props.workspaceId);
      setShowExperimentForm(false); await loadExperiments(); props.onChanged?.();
      setNotice('实验记录已创建；计算尚未由此操作启动。可通过对话另行准备计算并确认资源。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '创建实验失败'); }
    finally { setSaving(false); }
  };
  const finishExperiment = async (item: ResearchExperiment, decision: string) => {
    if (props.readOnly) return;
    if (!await confirmAction(`将“${item.title}”标记完成并冻结，判断为“${researchStatusLabel(decision) || decision}”？后续修改需要新建路线。Git 隔离模式可能提交文件变更；“纳入研究”不执行 Git 合并。此操作不启动计算。`)) return;
    setSaving(true); setError('');
    try {
      await updateResearchExperiment(item.experiment_id, { status: 'completed', decision, result_summary: item.result_summary, workflow_run_id: item.workflow_run_id }, props.workspaceId);
      await loadExperiments();
      props.onChanged?.(); setNotice('实验记录已完成并冻结；没有启动计算。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '更新实验失败'); }
    finally { setSaving(false); }
  };
  const runComparison = async () => {
    if (!compareLeft() || !compareRight() || compareLeft() === compareRight()) return;
    try { setComparison(await compareResearchExperiments(compareLeft(), compareRight(), props.workspaceId)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '实验对比失败'); }
  };
  const runReproductionComparison = async (item: ResearchExperiment) => {
    try { setReproductionComparison(await compareResearchReproduction(item.experiment_id, props.workspaceId)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '复现差异加载失败'); }
  };
  const changeContinuation = async (action: 'start' | 'pause' | 'resume') => {
    if (props.readOnly) return;
    setSaving(true); setError('');
    try {
      const result = action === 'start'
        ? await startResearchContinuation(props.researchId, maxRuns(), maxMinutes(), props.workspaceId, { max_tokens: maxTokens(), max_tool_calls: maxToolCalls(), max_failures: maxFailures(), max_cost_micros: Math.round(maxCost() * 1_000_000) })
        : await controlResearchContinuation(props.researchId, action, props.workspaceId);
      setContinuation(result.continuation); props.onChanged?.();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '持续研究设置失败'); }
    finally { setSaving(false); }
  };

  const inputClass = 'w-full rounded-lg border border-[#30383e] bg-[#0d1215] px-3 py-2 text-sm text-[#d7dee4] outline-none focus:border-[#5ba8d8]';
  const buttonClass = 'rounded-lg border border-[#35434c] px-3 py-1.5 text-xs text-[#b9c6cf] hover:border-[#5ba8d8] hover:text-[#e6f4ff] disabled:opacity-50';

  return <div class="research-session-panel mx-auto w-full max-w-5xl px-1 py-6">
    <Show when={props.readOnly}><p role="status" class="mb-4 text-sm">已归档课题，只供查看；恢复课题后可继续修改。</p></Show>
    <Show when={props.onNavigateTab}><nav aria-label="课题操作" class="mb-4 flex flex-wrap gap-3">
      <button type="button" class={buttonClass} onClick={() => props.onNavigateTab?.('hypotheses')}>提出研究假说 / 查看历史</button>
      <button type="button" class={buttonClass} onClick={() => props.onNavigateTab?.('experiments')}>创建实验 / 查看分支</button>
      <button type="button" class={buttonClass} onClick={() => props.onNavigateTab?.('continuation')}>持续研究设置</button>
    </nav></Show>
    <Show when={loading()}><div class="py-16 text-center text-sm text-[#8b99a4]">正在加载研究数据…</div></Show>
    <Show when={error()}><div role="alert" class="mb-4 rounded-xl border border-[#ef4444]/30 bg-[#ef4444]/8 p-4 text-sm text-[#fca5a5]">{error()} <button type="button" disabled={saving()} onClick={() => setReloadRevision(value => value + 1)}>重试读取</button></div></Show>
    <Show when={!loading()}>
      <Show when={props.tab === 'overview' || props.tab === 'continuation'}><div class="space-y-4">
        <Show when={props.tab === 'overview'}>
        <section class="research-overview-hero rounded-2xl border border-[#2a3136] bg-[#11171b] p-6"><div class="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#8ed5ff]">研究概览</div><h2 class="research-project-title mt-2 text-2xl font-bold text-[#f1f5f9]">{provenance()?.project.title || '未命名研究课题'}</h2><p class="research-project-summary mt-3 text-sm leading-6 text-[#9faeb7]">{checkpoint()?.research_goal || provenance()?.project.description || provenance()?.initial_intent || '尚未形成研究目标摘要。'}</p><div class="mt-5 grid gap-3 sm:grid-cols-3"><div class="research-metric rounded-xl bg-[#171c20] p-4"><div class="text-xs font-medium text-[#71808a]">关联会话</div><div class="mt-1 text-2xl font-semibold text-[#e6edf3]">{provenance()?.counts.sessions || 0}</div></div><div class="research-metric rounded-xl bg-[#171c20] p-4"><div class="text-xs font-medium text-[#71808a]">研究活动</div><div class="mt-1 text-2xl font-semibold text-[#e6edf3]">{provenance()?.counts.activities || 0}</div></div><div class="research-metric rounded-xl bg-[#171c20] p-4"><div class="text-xs font-medium text-[#71808a]">研究产物</div><div class="mt-1 text-2xl font-semibold text-[#e6edf3]">{provenance()?.counts.artifacts || 0}</div></div></div></section>
        <section class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5"><h3 class="text-sm font-semibold text-[#d7dee4]">下一步</h3><Show when={values(checkpoint()?.next_actions).length} fallback={<p class="mt-3 text-sm text-[#71808a]">尚未形成明确的下一步。</p>}><ul class="mt-3 space-y-2"><For each={values(checkpoint()?.next_actions)}>{(item) => <li class="rounded-lg bg-[#171c20] px-3 py-2 text-sm text-[#bdc8d1]">{item}</li>}</For></ul></Show></section>
        </Show>
        <details open={props.tab === 'continuation' || Boolean(continuation())} class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5"><summary class="cursor-pointer text-sm font-semibold">持续研究控制与预算</summary>
          <div class="flex items-center justify-between gap-4"><div><h3 class="text-sm font-semibold text-[#d7dee4]">持续研究</h3><p class="mt-1 text-xs leading-5 text-[#71808a]">设置自动续跑的停止条件。运行次数和持续时间必须填写；可选预算填 0 表示不单独限制。</p></div><span class="research-status-pill shrink-0 text-xs text-[#8ed5ff]">{continuation()?researchStatusLabel(continuation()?.status):'未启用'}</span></div>
          <Show when={continuation()} fallback={
            <div class="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              <label class="research-budget-field"><span>最多运行次数</span><small>达到后不再创建下一轮</small><input class={inputClass} type="number" min="1" value={maxRuns()} onInput={(e) => setMaxRuns(Number(e.currentTarget.value))}/></label>
              <label class="research-budget-field"><span>最长持续时间（分钟）</span><small>到期后不再创建下一轮，暂停也计时</small><input class={inputClass} type="number" min="1" value={maxMinutes()} onInput={(e) => setMaxMinutes(Number(e.currentTarget.value))}/></label>
              <label class="research-budget-field"><span>模型用量上限（令牌数）</span><small>所有续跑累计用量，0 为不限</small><input class={inputClass} type="number" min="0" value={maxTokens()} onInput={(e) => setMaxTokens(Number(e.currentTarget.value))}/></label>
              <label class="research-budget-field"><span>工具调用上限</span><small>累计调用工具次数，0 为不限</small><input class={inputClass} type="number" min="0" value={maxToolCalls()} onInput={(e) => setMaxToolCalls(Number(e.currentTarget.value))}/></label>
              <label class="research-budget-field"><span>已记录费用上限（美元）</span><small>仅依据已上报费用，未知费用无法据此限额；0 为不限</small><input class={inputClass} type="number" min="0" step="0.01" value={maxCost()} onInput={(e) => setMaxCost(Number(e.currentTarget.value))}/></label>
              <label class="research-budget-field"><span>允许失败次数</span><small>累计失败达到次数后不再创建下一轮</small><div class="flex gap-2"><input class={inputClass} type="number" min="0" value={maxFailures()} onInput={(e) => setMaxFailures(Number(e.currentTarget.value))}/><button class={`${buttonClass} shrink-0 px-4`} disabled={saving()} onClick={() => changeContinuation('start')}>启动</button></div></label>
            </div>
          }>{(item) => <div class="mt-3 rounded-xl bg-[#171c20] p-3"><div class="flex flex-wrap items-center justify-between gap-2"><div class="text-xs text-[#9faeb7]">运行 {item().runs_started}/{item().max_runs} · 模型用量 {item().tokens_used}/{item().max_tokens || '∞'} · 工具 {item().tool_calls_used}/{item().max_tool_calls || '∞'} · 失败 {item().failures_used}/{item().max_failures || '∞'} · 费用 {formatKnownCost(item().cost_used_micros / 1_000_000, item().cost_status || 'unknown')} / 上限 {item().max_cost_micros ? `$${(item().max_cost_micros / 1_000_000).toFixed(2)}` : '不限'}<Show when={item().stop_reason}> · {researchStatusLabel(item().stop_reason,'停止原因尚未说明')}</Show></div><Show when={item().status === 'running'} fallback={<button class={buttonClass} disabled={saving() || continuationBudgetExhausted(item())} title={continuationBudgetExhausted(item()) ? '预算已达上限，恢复不会追加预算；请明确调整续跑预算后再继续。' : undefined} onClick={() => changeContinuation('resume')}>恢复续跑</button>}><button class={buttonClass} disabled={saving()} onClick={() => changeContinuation('pause')}>暂停续跑</button></Show></div><p class="mt-2 text-xs text-[#9faeb7]" role="status">{continuationNotice(item())}</p><Show when={item().cost_status !== 'known'}><p class="mt-2 text-xs text-[#9faeb7]">费用信息不完整，当前费用上限无法保证总支出；请同时限制轮次与时间。</p></Show><Show when={item().final_summary}><div class="mt-2 text-xs text-[#9ee6bb]">最佳交付：{item().final_summary}</div></Show></div>}</Show>
        </details>
      </div></Show>

      <Show when={props.tab === 'hypotheses'}><fieldset disabled={props.readOnly} class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5">
        <div class="flex items-center justify-between gap-3"><div><h2 class="text-lg font-semibold text-[#f1f5f9]">研究假说</h2><p class="mt-1 text-xs text-[#71808a]">Agent 可提出草案，只有用户批准的假说才能进入实验。</p></div><div class="flex gap-2"><button class={buttonClass} disabled={saving()} onClick={generateHypotheses}>从检查点提取候选</button><button class={buttonClass} onClick={() => openHypothesisForm()}>新增假说</button></div></div>
        <Show when={showHypothesisForm()}><div class="mt-4 grid gap-3 rounded-xl border border-[#355264] bg-[#121b20] p-4">
          <label>假说陈述（必填）<input class={inputClass} value={statement()} onInput={(e) => setStatement(e.currentTarget.value)} placeholder="可验证的假说陈述"/></label>
          <label>提出依据<textarea class={inputClass} value={rationale()} onInput={(e) => setRationale(e.currentTarget.value)}/></label>
          <label>验证计划<textarea class={inputClass} value={testPlan()} onInput={(e) => setTestPlan(e.currentTarget.value)}/></label>
          <label>证伪条件<input class={inputClass} value={falsification()} onInput={(e) => setFalsification(e.currentTarget.value)} placeholder="什么结果会证伪它？"/></label>
          <div class="flex justify-end gap-2"><button class={buttonClass} onClick={() => setShowHypothesisForm(false)}>取消</button><button class={buttonClass} disabled={saving() || !statement().trim()} onClick={saveHypothesis}>{editingHypothesis() ? '保存修订' : '创建草案'}</button></div></div></Show>
        <Show when={hypotheses().length} fallback={<p class="mt-4 text-sm text-[#71808a]">当前课题还没有结构化假说。</p>}><div class="research-scroll-content mt-4 space-y-3"><For each={hypotheses()}>{(item) => <article class="rounded-xl border border-[#30383e] p-4"><div class="flex items-start justify-between gap-3"><div><div class="text-sm font-medium leading-6 text-[#d7dee4]">{item.statement}</div><div class="mt-1 text-[10px] text-[#71808a]">第 {item.revision} 版 · {researchStatusLabel(item.status)}</div></div><div class="flex shrink-0 gap-2"><button class={buttonClass} onClick={() => openHypothesisForm(item)}>修订</button><Show when={item.status === 'proposed'}><button class={buttonClass} disabled={saving()} onClick={() => changeHypothesisStatus(item, 'approved')}>批准</button></Show><Show when={['approved', 'testing'].includes(item.status)}><button class={buttonClass} disabled={saving()} onClick={() => changeHypothesisStatus(item, 'withdrawn')}>撤回</button></Show></div></div><Show when={item.rationale}><p class="mt-3 text-xs leading-5 text-[#9faeb7]">依据：{item.rationale}</p></Show><Show when={item.test_plan}><p class="mt-1 text-xs leading-5 text-[#9faeb7]">验证：{item.test_plan}</p></Show><Show when={item.falsification_condition}><p class="mt-1 text-xs leading-5 text-[#d2a978]">证伪条件：{item.falsification_condition}</p></Show></article>}</For></div></Show>
      </fieldset></Show>

      <Show when={props.tab === 'hypotheses' || props.tab === 'experiments'}><section class="mb-4 rounded-2xl border border-[#355264] p-4">
        <h2 class="text-sm font-semibold">用对话推进研究</h2><p class="mt-2 text-xs leading-6">先描述想验证什么、这次改变什么。Agent 查询记录并给出确认预览；创建实验只建立记录，启动计算需要另行确认。</p>
        <div class="mt-3 flex flex-wrap gap-2"><button type="button" class={buttonClass} onClick={() => talkAboutResearch('请列出当前假说和实验路线，说明已做过什么、哪些失败、下一步有哪些选择。只读取，不修改。')}>在对话中查看路线</button><button type="button" class={buttonClass} disabled={props.readOnly} onClick={() => talkAboutResearch('我想提出一个研究假说，请先帮我澄清假说、验证计划和证伪条件，再生成待确认的假说创建预览。不要自动批准。')}>在对话中提出假说</button><button type="button" class={buttonClass} disabled={props.readOnly} onClick={() => talkAboutResearch('我想尝试另一条实验路线，请先列出可作为起点的已冻结实验，让我选择并说明改动，然后生成创建记录的确认预览。不要启动计算。')}>在对话中尝试另一条路线</button><button type="button" class={buttonClass} onClick={() => talkAboutResearch('请列出实验让我选择两个，比较本次改变、输入、产物、指标和结果，说明缺少哪些信息；只读取，不修改或运行计算。')}>在对话中比较结果</button></div>
      </section></Show>
      <Show when={notice()}><p role="status" class="mb-3 text-sm">{notice()}</p></Show>
      <Show when={props.tab === 'experiments'}><div class="space-y-4"><section class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5">
        <div class="flex items-center justify-between gap-3"><div><h2 class="text-lg font-semibold text-[#f1f5f9]">{hasExperimentBranches() ? '实验路线与分支' : '实验记录'}</h2><p class="mt-1 text-xs text-[#9faeb7]">先创建记录，再独立准备并启动计算，最后关联运行与结果。完成后冻结，后续尝试保留为新路线。</p></div><button class={buttonClass} disabled={props.readOnly || !approvedHypotheses().length} onClick={() => openExperimentForm()}>新建实验记录</button></div>
        <Show when={showExperimentForm()}><fieldset disabled={props.readOnly} class="mt-4 grid gap-3 rounded-xl border border-[#355264] bg-[#121b20] p-4">
          <label>已批准假说<select class={inputClass} value={experimentHypothesisId()} disabled={!!parentExperimentId()} onChange={(e) => setExperimentHypothesisId(e.currentTarget.value)}><option value="">选择已批准假说</option><For each={approvedHypotheses()}>{item => <option value={item.hypothesis_id}>{item.statement}</option>}</For></select></label>
          <label>实验名称（必填）<input class={inputClass} value={experimentTitle()} onInput={(e) => setExperimentTitle(e.currentTarget.value)}/></label>
          <label>本次改变与验证目的<textarea class={inputClass} value={experimentReason()} onInput={(e) => setExperimentReason(e.currentTarget.value)}/></label>
          <Show when={parentExperimentId()}><p class="text-xs leading-6">基于：{experiments().find(e => e.experiment_id === parentExperimentId())?.title}。沿用父实验输出引用；不会自动复制文件、依赖或计算环境。</p></Show>
          <label>关联已有计算运行（可选，不启动计算）<select class={inputClass} value={workflowRunId()} onChange={e => setWorkflowRunId(e.currentTarget.value)}><option value="">暂不关联，先创建记录</option><For each={runOptions()}>{r => <option value={r.run_id}>{userFacingTitle(r.title,'未命名计算')} · {researchStatusLabel(r.status)}</option>}</For></select></label>
          <div class="flex flex-wrap gap-2"><button type="button" class={buttonClass} disabled={runPage() <= 1} onClick={() => void loadRuns(runPage() - 1)}>上一页运行</button><span class="text-xs">{runPage()} / {runPages()}</span><button type="button" class={buttonClass} disabled={runPage() >= runPages()} onClick={() => void loadRuns(runPage() + 1)}>下一页运行</button><button type="button" class={buttonClass} onClick={() => void loadRuns(runPage())}>刷新运行列表</button></div>
          <Show when={runError()}><p role="alert" class="text-xs">{runError()}</p></Show>

          <label>执行位置<select class={inputClass} value={executionMode()} onChange={(e) => setExecutionMode(e.currentTarget.value as any)}><option value="shared_workspace">共享工作区（不隔离）</option><option value="isolated_directory">独立运行目录（数据/远程任务）</option><option value="git_worktree">Git 隔离工作区（代码实验）</option></select></label>
          <p class="text-xs leading-6">此操作仅创建记录并准备执行位置，不提交计算。Git 模式要求干净且已有提交的仓库。</p>
          <div class="flex justify-end gap-2"><button class={buttonClass} onClick={() => setShowExperimentForm(false)}>取消</button><button class={buttonClass} disabled={saving() || !experimentTitle().trim() || !experimentHypothesisId()} onClick={saveExperiment}>仅创建实验记录</button></div></fieldset></Show>
        <Show when={experiments().length} fallback={<p class="mt-4 text-sm text-[#9faeb7]">当前还没有实验。先批准一个假说，再创建首个实验记录；创建不会启动计算。</p>}><div class="research-scroll-content mt-4 space-y-3"><For each={experiments()}>{(item) => <article class="rounded-xl border border-[#30383e] p-4" style={{ 'margin-left': `${Math.min(experimentDepth(item), 4) * 20}px` }}>
          <div class="flex flex-wrap items-start justify-between gap-3"><div class="min-w-0"><h3 class="text-sm font-semibold">{item.title}</h3><p class="mt-1 text-xs">{researchStatusLabel(item.status)} · 假说第 {item.hypothesis_revision} 版 · {item.frozen_at ? '已冻结，继续需新建路线' : '可整理记录'}<Show when={item.decision}> · {researchStatusLabel(item.decision)}</Show></p></div>
            <div class="flex flex-wrap gap-2"><button type="button" class={buttonClass} disabled={props.readOnly} onClick={() => talkAboutResearch(`请查看实验“${item.title}”。${item.frozen_at ? '我想基于它尝试另一条路线，请询问本次改动，展示新记录的创建预览。' : '请帮我整理结果或关联已有计算运行，先展示对应操作预览，不要直接冻结。'}`)}>{item.frozen_at ? '对话中尝试另一条路线' : '对话中整理这次尝试'}</button>
            <Show when={item.source_experiment_id}><button class={buttonClass} onClick={() => runReproductionComparison(item)}>原始/复现差异</button></Show><Show when={item.frozen_at}><button class={buttonClass} disabled={props.readOnly} onClick={() => openExperimentForm(item)}>手动创建后续记录</button></Show></div></div>
          <p class="mt-3 text-xs leading-6">验证：{hypothesisById().get(item.hypothesis_id)?.statement || '查看对应假说版本'}<br/>本次改变与目的：{item.reason || '尚未记录，可在对话中补充说明'}<br/>基于：{item.parent_experiment_id ? experiments().find(e => e.experiment_id === item.parent_experiment_id)?.title || '历史实验' : '首个尝试 / 独立路线'}<br/>计算：{item.workflow_run_id || item.protein_runs?.length ? '已关联运行；实际执行状态以运行详情为准' : '未关联运行；该记录不能证明计算已启动'}</p>
          <Show when={item.result_summary}><p class="mt-2 rounded-lg bg-[#171c20] p-2 text-xs">结果与理由：{item.result_summary}</p></Show>
          <Show when={item.protein_runs?.length}>
            <details class="mt-3 text-xs"><summary>Boltz 计算轮次（{item.protein_runs?.length}）</summary>
              <p>预测是计算证据，不代表假说或湿实验已验证。</p>
              <button type="button" class={buttonClass} onClick={() => void loadExperiments().catch(() => setNotice('暂时无法刷新实验运行。'))}>刷新运行状态</button>
              <For each={item.protein_runs}>{run => <div class="mt-2 space-y-1">
                <p>{researchStatusLabel(run.lifecycle_status)} · {new Date(run.created_at).toLocaleString()} · 已回传 {run.outputs.length} 个产物{run.source_run_id ? ' · 基于上一轮换输入' : ''}</p>
                <For each={run.outputs}>{file => <Show when={file.artifact_url}>
                  <div class="flex flex-wrap items-center gap-2">
                    <a class="break-all underline" href={proteinRunArtifactUrl(file.artifact_url!, props.workspaceId)}>{file.relative_path}</a>
                    <Show when={isStructurePath(file.relative_path)}>
                      <button type="button" class={buttonClass}
                        aria-label={`三维查看 ${file.relative_path.split('/').pop()}`}
                        onClick={() => { props.onOpenStructure?.(); window.dispatchEvent(new CustomEvent('tcode-open-structure', {
                          detail: { path: file.relative_path, workspaceId: props.workspaceId, proteinRunId: run.run_id },
                        })); }}>三维查看</button>
                    </Show>
                  </div>
                </Show>}</For>
                <Show when={!item.frozen_at && !props.readOnly && run.lifecycle_status === 'completed'}>
                  <button type="button" class={buttonClass} onClick={() => talkAboutResearch(`基于实验“${item.title}”的 Boltz 运行 ${run.run_id} 准备下一轮。请等我上传或指定新主输入文件，仅替换 tools_input；先确认，不要直接提交。`)}>对话中换文件继续</button>
                </Show>
              </div>}</For>
            </details>
          </Show>
          <details class="mt-3 text-xs"><summary>输入、产物、指标与高级操作</summary><p class="mt-2">输入引用 {item.input_artifacts?.length || 0} · 输出产物 {item.output_artifacts?.length || 0}</p><ResearchRecordDetails value={{输入:item.input_artifacts || [],产物:item.output_artifacts || [],指标:item.metrics || {}}}/><SnapshotSummary experiment={item}/>
            <Show when={!item.frozen_at}><p class="mt-3">以下操作均会完成并冻结记录；不会启动计算。</p><div class="mt-2 flex flex-wrap gap-2"><For each={['recommended', 'retained', 'rejected', 'merged']}>{decision => <button type="button" class={buttonClass} disabled={props.readOnly || saving()} onClick={() => finishExperiment(item, decision)}>完成并冻结 · {researchStatusLabel(decision)}</button>}</For></div></Show>
          </details>
        </article>}</For></div></Show>
      </section>
      <Show when={experiments().length > 1}><section class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5"><h3 class="text-sm font-semibold">对比实验</h3><p class="mt-2 text-xs">比较记录与运行快照；没有指标或结果时，不自动判断哪条路线更好。</p><div class="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_auto]"><label>实验 A<select class={inputClass} value={compareLeft()} onChange={(e) => setCompareLeft(e.currentTarget.value)}><option value="">选择实验 A</option><For each={experiments()}>{(item) => <option value={item.experiment_id}>{item.title}</option>}</For></select></label><label>实验 B<select class={inputClass} value={compareRight()} onChange={(e) => setCompareRight(e.currentTarget.value)}><option value="">选择实验 B</option><For each={experiments()}>{(item) => <option value={item.experiment_id}>{item.title}</option>}</For></select></label><button class={buttonClass} disabled={!compareLeft() || !compareRight() || compareLeft() === compareRight()} onClick={runComparison}>对比</button></div><Show when={comparison()}>{(result) => <div class="mt-4 grid gap-3 sm:grid-cols-2"><For each={[result().left, result().right]}>{(item) => <div class="rounded-xl bg-[#171c20] p-4"><div class="text-sm font-medium">{item.title}</div><p class="mt-2 text-xs leading-6">状态：{researchStatusLabel(item.status)}<br/>判断：{researchStatusLabel(item.decision,'判断尚未说明')}<br/>本次改变与目的：{item.reason || '未记录'}<br/>假说版本：第 {item.hypothesis_revision} 版<br/>输入 {item.input_artifacts?.length || 0} · 产物 {item.output_artifacts?.length || 0}</p><p class="mt-2 text-xs leading-6">结果：{item.result_summary || '暂无结果摘要，无法据此判断优劣'}</p><ResearchRecordDetails value={{指标:item.metrics||{}}}/><SnapshotSummary experiment={item}/></div>}</For></div>}</Show></section></Show></div></Show>

      <Show when={props.tab === 'experiments' && reproductionComparison()}>{(result) => <section class="mt-4 rounded-2xl border border-[#c4a7ff]/30 bg-[#11171b] p-5"><h3 class="text-sm font-semibold text-[#d8c8ff]">原始实验与复现实验差异</h3><div class="mt-3 space-y-2"><For each={Object.entries(result().differences || {})}>{([field, values]) => <div class="grid gap-2 rounded-lg bg-[#171c20] p-3 text-xs sm:grid-cols-[120px_1fr_1fr]"><strong class="text-[#9faeb7]">{researchFieldLabel(field)}</strong><span class="text-[#8b99a4]">原始：{researchValueText((values as any).original,field)}</span><span class="text-[#d7dee4]">复现：{researchValueText((values as any).reproduction,field)}</span></div>}</For></div></section>}</Show>
      <Show when={props.tab === 'evidence'}><section class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5"><h2 class="text-lg font-semibold text-[#f1f5f9]">证据</h2><Show when={(knowledge()?.links || []).length} fallback={<p class="mt-4 text-sm text-[#71808a]">当前还没有已关联的证据。</p>}><div class="research-scroll-content mt-4 space-y-3"><For each={knowledge()?.links || []}>{(item) => <article class="rounded-xl border border-[#30383e] p-4"><div class="text-sm text-[#d7dee4]">{researchSourceLabel(item)}</div><div class="mt-1 text-xs text-[#596873]">{researchRelationLabel(item.relation)} · {researchSourceTypeLabel(item.object_type)}</div></article>}</For></div></Show></section></Show>
      <Show when={props.tab === 'findings'}><section class="rounded-2xl border border-[#2a3136] bg-[#11171b] p-5"><h2 class="text-lg font-semibold text-[#f1f5f9]">结论</h2><Show when={findings().length} fallback={<p class="mt-4 text-sm text-[#71808a]">当前还没有经过证据关联的正式结论。</p>}><div class="research-scroll-content mt-4 space-y-3"><For each={findings()}>{(item) => <article class="rounded-xl border border-[#30383e] p-4"><div class="flex justify-between gap-3"><p class="text-sm leading-6 text-[#d7dee4]">{item.statement}</p><span class="text-[10px] text-[#9ee6bb]">{item.status==='active'?'当前有效':researchStatusLabel(item.status)}</span></div><div class="mt-2 text-[10px] text-[#71808a]">证据 {item.evidence_ids.length} 条 · 第 {item.current_revision} 版</div></article>}</For></div></Show></section></Show>
    </Show>
  </div>;
}
