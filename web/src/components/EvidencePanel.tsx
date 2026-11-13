import MarkdownContent from './MarkdownContent';
import { For, Show, createEffect, createMemo, createSignal, createUniqueId, onCleanup } from 'solid-js';
import { openCitation, type CitationRef } from '../utils/citations';
import { evidenceKindLabel, graphEdgeTypeLabel, graphNodeTypeLabel, readableSourceLabel, shortDisplayPath, stringifyEvidenceItem } from '../utils/evidenceDisplay';
import { evidenceImageUrl, evidenceMaintenance, resolveCitation, type CitationRecord, type EvidenceNode, type EvidenceMaintenance } from '../api/evidence';
import ImageLightbox, { type LightboxImageSource } from './ImageLightbox';
import './EvidencePanel.css';

const stateCopy: Record<CitationRecord['state'], string> = {
  available: '当前可用', missing: '来源不存在或不可访问', forbidden: '来源不存在或不可访问',
  withdrawn: '该证据已撤回或失效，不再用于当前取证',
  disabled: '该来源当前未参与取证；以下为历史快照',
  historical: '历史或待重新核对的来源，请核对当前内容及关联依据后再使用',
};
const issueCopy: Record<string, string> = {
  missing_source: '缺少来源', missing_locator: '缺少页码或定位', missing_anchor: '缺少引用锚点',
  missing_search_text: '缺少检索文本', missing_embedding: '未建立向量', unknown_embedding_version: '向量版本未知',
  content_hash_mismatch: '证据内容与保存的哈希不一致', duplicate_content: '发现重复内容',
  orphan_evidence_node: '未关联任何来源', unknown_evidence_id: '证据不存在', unknown_evidence_ref: '引用了不存在的证据',
  evidence_stale: '证据已过期', evidence_retracted: '证据已撤回', evidence_superseded: '证据已被替代',
  storage_corruption: '证据存储完整性检查失败',
  asset_missing: '来源文件已丢失', asset_unreadable: '来源文件无法读取', asset_outside_workspace: '来源文件超出工作区范围',
  asset_hash_mismatch: '来源文件内容与保存的校验值不一致', asset_baseline_missing: '来源文件尚未建立校验基线',
  asset_verification_pending: '来源文件需要重新校验', asset_changed_during_check: '文件在校验过程中发生变化',
  asset_remote_not_checked: '远程来源未执行本地文件校验', asset_checks_truncated: '本次只检查了部分来源文件',
};
const semanticCopy: Record<EvidenceMaintenance['semantic'], string> = {
  ready: '关键词与语义检索可用', updating: '语义索引正在更新，关键词检索可用', pending: '语义索引准备中，关键词检索可用',
  unconfigured: '未配置语义模型，当前使用关键词检索', disabled: '语义检索已关闭，当前使用关键词检索',
  degraded: '语义检索暂不可用，已退回关键词检索', unavailable: '语义索引依赖不可用，当前使用关键词检索',
};
const phaseCopy: Record<string, string> = { lexical: '更新检索索引', semantic: '更新语义索引', assets: '校验来源文件', done: '维护完成' };
const healthCopy = { pass: '基础检查通过', warn: '存在提醒', fail: '检查未通过' };
const tabs = ['原文', '图片', '关联', '检查'] as const;
type Tab = typeof tabs[number];

export default function EvidencePanel(props: {
  request?: { ref: CitationRef; key: number }; workspaceId: string; sessionId?: string; onClose?: () => void; onLoaded?: () => void;
}) {
  const id = createUniqueId();
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [record, setRecord] = createSignal<CitationRecord>();
  const [tab, setTab] = createSignal<Tab>('原文');
  const [retry, setRetry] = createSignal(0);
  const [lightbox, setLightbox] = createSignal<LightboxImageSource | null>(null);
  const [imageLimit, setImageLimit] = createSignal(12);
  const [failedImages, setFailedImages] = createSignal<number[]>([]);
  const [maintenance, setMaintenance] = createSignal<EvidenceMaintenance>();
  const [maintenanceError, setMaintenanceError] = createSignal('');
  const [submitting, setSubmitting] = createSignal(false);
  const [pollRetry, setPollRetry] = createSignal(0);
  let sequence = 0;
  let controller: AbortController | undefined;
  createEffect(() => {
    const request = props.request, workspaceId = props.workspaceId, sessionId = props.sessionId || '';
    retry();
    const current = ++sequence;
    controller?.abort(); setRecord(undefined); setError(''); setLoading(false); setMaintenance(undefined); setMaintenanceError(''); setSubmitting(false);
    setTab('原文'); setLightbox(null); setImageLimit(12); setFailedImages([]);
    if (!request?.ref.id || !workspaceId) return;
    const abort = new AbortController(); controller = abort; setLoading(true);
    void resolveCitation(request.ref, workspaceId, sessionId, abort.signal).then(payload => {
      if (current !== sequence) return;
      setRecord(payload); setMaintenance(payload.maintenance); props.onLoaded?.();
    }).catch(cause => {
      if (current !== sequence || cause?.name === 'AbortError') return;
      setError(readableSourceLabel(cause instanceof Error ? cause.message : '', '读取来源失败，请重试。'));
    }).finally(() => { if (current === sequence) setLoading(false); });
  });
  onCleanup(() => { sequence += 1; controller?.abort(); });
  const busy = () => submitting() || ['queued', 'running'].includes(maintenance()?.status || '');
  const maintain = async (verifyAssets: boolean) => {
    const current = sequence;
    setSubmitting(true); setMaintenanceError('');
    try {
      const result = await evidenceMaintenance(props.workspaceId, controller!.signal, { rebuild: !verifyAssets, verify_assets: verifyAssets });
      if (current === sequence && result.status === 'completed' && props.request) {
        const payload = await resolveCitation(props.request.ref, props.workspaceId, props.sessionId || '', controller!.signal);
        if (current === sequence) setRecord(payload);
      }
      if (current === sequence) setMaintenance(result);
    } catch (cause) {
      if (current === sequence && (cause as Error)?.name !== 'AbortError') setMaintenanceError('维护请求失败，请重试。');
    } finally { if (current === sequence) setSubmitting(false); }
  };
  createEffect(() => {
    const request = props.request, workspaceId = props.workspaceId, sessionId = props.sessionId || '';
    const currentState = maintenance();
    pollRetry();
    if (tab() !== '检查' || !request || !currentState || !['queued', 'running'].includes(currentState.status) && currentState.semantic !== 'pending') return;
    const current = sequence, abort = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const next = await evidenceMaintenance(workspaceId, abort.signal);
        if (current !== sequence || abort.signal.aborted) return;
        if (next.status === 'completed') {
          const payload = await resolveCitation(request.ref, workspaceId, sessionId, abort.signal);
          if (current === sequence && !abort.signal.aborted) setRecord(payload);
        }
        setMaintenanceError(''); setMaintenance(next);
      } catch (cause) { if (current === sequence && (cause as Error)?.name !== 'AbortError') { setMaintenanceError('进度暂时无法读取，正在重试。'); setPollRetry(value => value + 1); } }
    }, maintenanceError() ? 5000 : 1500);
    onCleanup(() => { clearTimeout(timer); abort.abort(); });
  });
  const primaryText = createMemo(() => String(record()?.quote || record()?.content || '').trim());
  const location = createMemo(() => {
    const value = record()?.locator;
    if (!value) return '';
    if (typeof value === 'string') return readableSourceLabel(value, '');
    const item = value as Record<string, unknown>;
    return [item.page ? `第 ${item.page} 页` : '', item.line_start ? `第 ${item.line_start}${item.line_end && item.line_end !== item.line_start ? `至 ${item.line_end}` : ''} 行` : '', item.table_id ? `表格 ${readableSourceLabel(item.table_id, '')}`.trim() : ''].filter(Boolean).join(' · ') || stringifyEvidenceItem(value);
  });
  const nodeMap = createMemo(() => new Map((record()?.graph?.nodes || []).map(node => [node.node_id, node])));
  const graphNodes = createMemo(() => [...(record()?.graph?.nodes || [])].sort((a, b) => Number(b.type === 'evidence') - Number(a.type === 'evidence')).slice(0, 12));
  const positions = createMemo(() => new Map(graphNodes().map((node, i) => [node.node_id, { x: i % 2 ? 255 : 85, y: 32 + Math.floor(i / 2) * 76 }])));
  const graphEdges = createMemo(() => (record()?.graph?.edges || []).filter(edge => positions().has(edge.source) && positions().has(edge.target)));
  const openNode = (node?: EvidenceNode) => {
    if (node?.type === 'evidence') openCitation({ kind: 'evidence', id: node.node_id.replace(/^evidence:/, '') });
  };
  const count = (value: Tab) => value === '图片' ? record()?.images?.length : value === '关联' ? record()?.graph?.nodes?.length : value === '检查' ? record()?.doctor?.issue_count : undefined;
  const selectTab = (event: KeyboardEvent, index: number) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    setTab(tabs[next]); document.getElementById(`${id}-tab-${next}`)?.focus();
  };
  return <section class="evidence-panel" aria-label="来源证据" aria-busy={loading()}>
    <header class="evidence-panel-header"><h2>来源证据</h2><Show when={props.onClose}><button type="button" class="evidence-close" aria-label="关闭来源证据" title="关闭来源证据" onClick={() => props.onClose?.()}>×</button></Show></header>
    <div class="evidence-panel-body">
      <Show when={!loading()} fallback={<p class="evidence-empty" role="status">正在加载来源…</p>}>
        <Show when={!error()} fallback={<div class="evidence-notice" data-state="fail" role="alert">{error()}<button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></div>}>
          <Show when={record()} fallback={<p class="evidence-empty">当前没有打开的来源。</p>}>
            {item => <>
              <div class="evidence-notice" data-state={item().state} role="status">{stateCopy[item().state]}</div>
              <Show when={!['missing', 'forbidden'].includes(item().state)}>
                <h3 class="evidence-title">{readableSourceLabel(item().title, evidenceKindLabel(item().kind) || '证据原文')}</h3>
                <Show when={location()}><p class="evidence-locator">{location()}</p></Show>
                <Show when={item().source_path || item().source_id}><p class="evidence-source">来源：{readableSourceLabel(shortDisplayPath(item().source_path || item().source_id), '已关联的来源资料')}</p></Show>
                <nav class="evidence-tabs" role="tablist" aria-label="证据详情">
                  <For each={tabs}>{(value, index) => <button type="button" role="tab" id={`${id}-tab-${index()}`} aria-selected={tab() === value} aria-controls={`${id}-content`} tabIndex={tab() === value ? 0 : -1} onClick={() => setTab(value)} onKeyDown={event => selectTab(event, index())}>{value}<Show when={count(value) !== undefined}><span>{count(value)}</span></Show></button>}</For>
                </nav>
                <div id={`${id}-content`} role="tabpanel" aria-labelledby={`${id}-tab-${tabs.indexOf(tab())}`} tabIndex={0} class="evidence-tab-content">
                  <Show when={tab() === '原文'}>
                    <Show when={primaryText()} fallback={<p class="evidence-empty">该记录没有可展示的原文摘录。</p>}><div class="evidence-quote"><MarkdownContent content={primaryText()} allowSessionActions={false}/></div></Show>
                    <Show when={(item().citations || []).length}><details class="evidence-details"><summary>更多定位 · {item().citations?.length}</summary><For each={item().citations || []}>{citation => <pre>{stringifyEvidenceItem(citation)}</pre>}</For></details></Show>
                  </Show>
                  <Show when={tab() === '图片'}>
                    <Show when={item().images?.length} fallback={<p class="evidence-empty">该证据没有关联的来源图片。</p>}>
                      <div class="evidence-images"><For each={(item().images || []).slice(0, imageLimit())}>{image => {
                        const label = readableSourceLabel(image.label, '来源图片');
                        const source = () => ({ src: evidenceImageUrl(item().evidence_id || props.request?.ref.id || '', image.index, props.workspaceId), alt: label, downloadName: image.name });
                        return <figure><Show when={!failedImages().includes(image.index)} fallback={<div class="evidence-image-error" role="status">来源图片不存在或不可访问</div>}><button type="button" class="evidence-image-button" title={`查看大图：${label}`} aria-label={`查看大图：${label}`} onClick={() => setLightbox(source())}><img src={source().src} alt={label} loading="lazy" onError={() => setFailedImages(previous => [...previous, image.index])} /></button></Show><figcaption>{label}</figcaption></figure>;
                      }}</For></div>
                      <Show when={(item().images?.length || 0) > imageLimit()}><button type="button" class="evidence-more" onClick={() => setImageLimit(value => value + 12)}>显示更多图片</button></Show>
                    </Show>
                  </Show>
                  <Show when={tab() === '关联'}>
                    <Show when={item().graph?.nodes.length} fallback={<p class="evidence-empty">该来源没有可展示的关联图谱。</p>}>
                      <svg class="evidence-graph" viewBox={`0 0 340 ${Math.max(100, Math.ceil(graphNodes().length / 2) * 76)}`} role="img" aria-label="证据来源关系图">
                        <For each={graphEdges()}>{edge => { const start = () => positions().get(edge.source)!, end = () => positions().get(edge.target)!; return <path d={`M${start().x},${start().y} L${end().x},${end().y}`}><title>{graphEdgeTypeLabel(edge.type)}</title></path>; }}</For>
                        <For each={graphNodes()}>{node => { const point = () => positions().get(node.node_id)!; const label = readableSourceLabel(shortDisplayPath(node.label), graphNodeTypeLabel(node.type).split('（')[0]); return <g transform={`translate(${point().x},${point().y})`} data-evidence={node.type === 'evidence'}><title>{label}</title><rect x="-73" y="-23" width="146" height="46" rx="4"/><text text-anchor="middle" y="-3">{label.length > 11 ? `${label.slice(0, 10)}…` : label}</text><text class="evidence-graph-type" text-anchor="middle" y="13">{graphNodeTypeLabel(node.type).split('（')[0]}</text></g>; }}</For>
                      </svg>
                      <p class="evidence-source">{item().graph?.nodes.length} 个节点 · {item().graph?.edges.length} 条关系<Show when={item().graph?.truncated}> · 部分关联已省略</Show></p>
                      <ul class="evidence-relations"><For each={item().graph?.edges || []}>{edge => <li><span>{graphEdgeTypeLabel(edge.type).split('（')[0]}</span><div><For each={[edge.source, edge.target]}>{nodeId => { const node = () => nodeMap().get(nodeId), label = () => readableSourceLabel(shortDisplayPath(node()?.label), graphNodeTypeLabel(node()?.type).split('（')[0]); return <Show when={node()?.type === 'evidence'} fallback={<p>{label()}</p>}><button type="button" onClick={() => openNode(node())}>{label()}</button></Show>; }}</For></div></li>}</For></ul>
                    </Show>
                  </Show>
                  <Show when={tab() === '检查'}>
                    <Show when={maintenance()}>{job => <div class="evidence-maintenance">
                      <p class="evidence-source">{semanticCopy[job().semantic]}</p>
                      <Show when={job().lexical_ready === false}><p class="evidence-source">历史证据索引准备中，当前检索覆盖尚不完整。</p></Show>
                      <Show when={['queued', 'running', 'completed', 'failed'].includes(job().status)}><p role="status">{job().status === 'failed' ? '维护中断，可继续处理' : job().status === 'queued' ? '维护已排队' : phaseCopy[job().phase] || '维护进行中'} · <strong class="evidence-progress-count">{job().processed}</strong> / {job().total}</p></Show>
                      <div class="evidence-maintenance-actions"><button type="button" disabled={busy()} onClick={() => void maintain(false)}>{job().retryable ? '继续维护' : '更新检索索引'}</button><button type="button" disabled={busy()} onClick={() => void maintain(true)}>校验来源文件</button></div>
                      <Show when={maintenanceError()}><p role="alert">{maintenanceError()}</p></Show>
                    </div>}</Show>
                    <Show when={item().doctor} fallback={<p class="evidence-empty">该来源类型暂不提供证据账本健康检查。</p>}>{doctor => <>
                      <div class="evidence-notice" data-state={doctor().status} role="status">{healthCopy[doctor().status]} · {doctor().issue_count} 个问题</div>
                      <p class="evidence-source">已检查 {doctor().checked_record_count} 条证据<Show when={doctor().truncated}> · 本次为部分检查</Show></p>
                      <Show when={doctor().asset_checks?.file_count}><p class="evidence-source">来源文件：已检查 {doctor().asset_checks?.checked_file_count} 个 · 内容已校验 {doctor().asset_checks?.verified_file_count} 个</p></Show>
                      <Show when={doctor().storage_integrity === 'not_checked'}><p class="evidence-source">本次检查针对引用记录，不包含全库完整性扫描。</p></Show>
                      <ul class="evidence-issues"><For each={doctor().issues}>{issue => <li data-severity={issue.severity}><b>{issue.severity === 'error' ? '错误' : issue.severity === 'warn' ? '提醒' : '信息'}</b><span>{issueCopy[issue.code] || readableSourceLabel(issue.message, '该来源需要进一步核对')}</span></li>}</For></ul>
                    </>}</Show>
                  </Show>
                </div>
              </Show>
            </>}
          </Show>
        </Show>
      </Show>
    </div>
    <ImageLightbox image={lightbox()} onClose={() => setLightbox(null)} />
  </section>;
}
