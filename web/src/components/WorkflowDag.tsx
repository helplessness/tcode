import { createMemo, createSignal, createUniqueId, For, Show } from 'solid-js';
import { passVerticalScrollToParent } from '../utils/scrollBoundary';
import { layoutWorkflowDag, type WorkflowDagStage } from '../utils/workflowDag';

type Props = { stages: WorkflowDagStage[]; label?: string; onStageSelect?: (stageId: string) => void };
const labels: Record<string, string> = {
  queued: '待执行', running: '执行中', committed: '已完成', completed: '已完成',
  review_pending: '待确认', waiting_review: '待确认', blocked: '阻塞', failed: '失败',
  waiting_external_job: '等待计算', collecting: '收集结果', validating: '验证中', skipped: '已跳过',
};

function contractSummary(value?: Record<string, unknown>) {
  if (!value || !Object.keys(value).length) return '未声明';
  const required = Array.isArray(value.required) ? value.required.map(String) : [];
  return required.length ? `必需：${required.join('、')}` : Object.keys(value).join('、');
}

export default function WorkflowDag(props: Props) {
  const arrowId = `workflow-dag-arrow-${createUniqueId()}`;
  const [zoom, setZoom] = createSignal(1);
  const [selectedStageId, setSelectedStageId] = createSignal('');
  const graph = createMemo(() => layoutWorkflowDag(props.stages || []));
  const nodeById = createMemo(() => new Map(graph().nodes.map(node => [node.stage_id, node])));
  const selectedStage = createMemo(() => props.stages.find(stage => stage.stage_id === selectedStageId()) || null);
  const changeZoom = (next: number) => setZoom(Math.min(1.6, Math.max(.6, Math.round(next * 10) / 10)));
  return <div class="workflow-dag-shell">
    <header class="workflow-dag-toolbar">
      <div><span>WORKFLOW CANVAS</span><strong>{props.label || '执行拓扑'}</strong></div>
      <div class="workflow-dag-toolbar-actions">
        <i /> 实时状态 <b>{graph().nodes.length} 节点</b>
        <button type="button" aria-label="缩小 DAG" onClick={() => changeZoom(zoom() - .1)}>−</button>
        <button type="button" class="workflow-dag-zoom-value" aria-label="重置 DAG 缩放" onClick={() => setZoom(1)}>{Math.round(zoom() * 100)}%</button>
        <button type="button" aria-label="放大 DAG" onClick={() => changeZoom(zoom() + .1)}>+</button>
      </div>
    </header>
    <div class="workflow-dag" role="img" aria-label={props.label || 'Workflow DAG'} title="点击节点查看详情；点击空白处关闭" on:wheel={passVerticalScrollToParent} onClick={() => { setSelectedStageId(''); props.onStageSelect?.(''); }}>
    <Show when={graph().nodes.length} fallback={<p>该 Workflow 没有可展示的阶段。</p>}>
      <svg viewBox={`0 0 ${graph().width} ${graph().height}`} style={{ width: `${graph().width * zoom()}px`, height: `${graph().height * zoom()}px` }}>
        <defs><marker id={arrowId} markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" /></marker></defs>
        <For each={graph().edges}>{edge => {
          const from = () => nodeById().get(edge.from)!;
          const to = () => nodeById().get(edge.to)!;
          const middle = () => (from().x + 218 + to().x) / 2;
          return <path class="workflow-dag-edge" data-active={to().status === 'running'} marker-end={`url(#${arrowId})`} d={`M ${from().x + 218} ${from().y + 49} H ${middle()} V ${to().y + 49} H ${to().x - 8}`} />;
        }}</For>
        <For each={graph().nodes}>{node => <g class="workflow-dag-node" classList={{ 'workflow-dag-node--selected': selectedStageId() === node.stage_id }} data-status={node.status || 'queued'} transform={`translate(${node.x} ${node.y})`} role="button" tabindex="0" onClick={(event) => { event.stopPropagation(); setSelectedStageId(node.stage_id); props.onStageSelect?.(node.stage_id); }} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedStageId(node.stage_id); props.onStageSelect?.(node.stage_id); } }}>
          <rect class="workflow-dag-node__surface" width="218" height="98" rx="10" />
          <rect class="workflow-dag-node__bar" width="4" height="98" rx="2" />
          <circle cx="22" cy="24" r="6" />
          <text class="workflow-dag-kicker" x="36" y="27">{String(node.stage_id).toUpperCase().slice(0, 24)}</text>
          <text class="workflow-dag-title" x="18" y="53">{String(node.label || node.stage_id).slice(0, 24)}</text>
          <text class="workflow-dag-meta" x="18" y="75">AGENT · {String(node.owner || 'unassigned').slice(0, 22)}</text>
          <text class="workflow-dag-status" x="18" y="90">{labels[String(node.status || '')] || '待执行'}</text>
          <Show when={node.review_gate}><text class="workflow-dag-gate" x="202" y="90" text-anchor="end">审核点</text></Show>
          <circle class="workflow-dag-port" cx="218" cy="49" r="4" />
          <Show when={(node.depends_on || []).length}><circle class="workflow-dag-port" cx="0" cy="49" r="4" /></Show>
        </g>}</For>
      </svg>
      <Show when={selectedStage()} keyed>{stage => (
        <aside class="workflow-dag-inspector" style={{ zoom: String(zoom()), width: `${100 / zoom()}%` }} onClick={event => event.stopPropagation()}>
          <header><span>节点契约与执行策略</span><small>{stage.owner || '未指定 Agent'}</small></header>
          <div class="workflow-dag-contract">
          <h3>{stage.label || stage.stage_id}</h3>
          <Show when={stage.description}><p>{stage.description}</p></Show>
          <dl>
            <div><dt>前置依赖</dt><dd>{stage.depends_on?.length ? stage.depends_on.join('、') : '无'}</dd></div>
            <div><dt>输入</dt><dd>{contractSummary(stage.input_contract)}</dd></div>
            <div><dt>输出</dt><dd>{contractSummary(stage.output_contract)}</dd></div>
            <div><dt>成功条件</dt><dd>{stage.success_checks?.length ? stage.success_checks.join('；') : '通过节点校验器'}</dd></div>
            <div><dt>重试</dt><dd>{String(stage.retry_policy?.max_attempts || 1)} 次上限</dd></div>
            <div><dt>超时</dt><dd>{stage.timeout_policy?.seconds ? `${String(stage.timeout_policy.seconds)} 秒 / ${String(stage.timeout_policy.on_timeout || 'retry')}` : '使用系统默认值'}</dd></div>
            <div><dt>预期产物</dt><dd>{stage.expected_artifacts?.length ? stage.expected_artifacts.join('、') : '无'}</dd></div>
            <div><dt>人工介入</dt><dd>{stage.review_gate ? '完成后必须确认' : '无需'}</dd></div>
          </dl>
          </div>
        </aside>
      )}</Show>
    </Show>
    </div>
  </div>;
}
