import { researchStatusLabel, researchValueText, userFacingTitle } from '../utils/userFacingLabels';
import ResearchRecordDetails from './ResearchRecordDetails';
import { For, Show, createEffect, createMemo, createResource, createSignal } from 'solid-js';
import { comparisonRequest, type Comparison, type Evidence, type CandidateDecision, type DecisionRecord, type DecisionHistory } from '../api/researchCompare';
import type { ResearchCompareBlock as Block } from '../utils/contentBlocks';
import { sessionStore } from '../stores/session';
import { composerDrafts, composerKey } from '../stores/composer';
import './CanvasBlock.css';
import './ResearchCompareBlock.css';

export default function ResearchCompareBlock(props: { block: Block; allowSessionActions?: boolean }) {
  const [data, { refetch }] = createResource(() => props.block, b => comparisonRequest<Comparison>(b.run_id, b.session_id, b.result_digest));
  const [query, setQuery] = createSignal('');
  const [wikiOnly, setWikiOnly] = createSignal(false);
  const [selected, setSelected] = createSignal<string[]>([]);
  const [reason, setReason] = createSignal('');
  const [active, setActive] = createSignal('');
  const [evidence, setEvidence] = createSignal<Evidence>();
  const [busy, setBusy] = createSignal(false);
  const [notice, setNotice] = createSignal('');
  const [error, setError] = createSignal('');
  const [question, setQuestion] = createSignal('');
  const [decisions, setDecisions] = createSignal<CandidateDecision[]>([]);
  const [decisionRecord, setDecisionRecord] = createSignal<DecisionRecord | null>(null);
  const [researchId, setResearchId] = createSignal('');
  const [decisionDirty, setDecisionDirty] = createSignal(false);
  const [historyCursor, setHistoryCursor] = createSignal(0);
  const [historyRevision, setHistoryRevision] = createSignal(0);
  const [history, { refetch: reloadHistory }] = createResource(
    () => ({ block: props.block, before: historyCursor() }),
    ({block, before}) => comparisonRequest<DecisionHistory>(block.run_id, block.session_id, block.result_digest, '/decisions/history', undefined, {before_revision: String(before)}),
  );
  const historicalDecision = createMemo(() => history()?.records.find(r => r.revision === historyRevision()));
  createEffect(() => { props.block; setHistoryCursor(0); setHistoryRevision(0); });
  const canAct = () => props.allowSessionActions !== false && sessionStore.currentSessionId() === props.block.session_id && !busy() && !data.error;
  const rows = createMemo(() => (data()?.candidates || []).filter(c => (!wikiOnly() || c.evidence_scope === 'wiki_prior')
    && `${c.enzyme_name || ''} ${c.candidate_id}`.toLowerCase().includes(query().toLowerCase())));
  const candidate = createMemo(() => data()?.candidates.find(c => c.candidate_id === active()));
  createEffect(() => {
    const value = data();
    if (!value) return;
    setSelected(value.selection_status === 'current' ? (value.selection?.items.map(i => i.item_id) || []) : []);
    setReason(value.selection_status === 'current' ? value.selection?.inputs.reason || '' : '');
    const record = value.decision_state.record;
    setDecisionRecord(record);
    setDecisions(value.decision_state.status === 'current' ? record?.decisions || [] : []);
    setResearchId(record?.research_id || '');
    setDecisionDirty(false);
  });
  const decision = (id: string) => decisions().find(d => d.candidate_id === id);
  const changeDecision = (id: string, state: string, reason = decision(id)?.reason || '') => {
    if (!canAct()) return;
    setDecisions(ds => [...ds.filter(d => d.candidate_id !== id), ...(state ? [{ candidate_id: id, state: state as CandidateDecision['state'], reason }] : [])]);
    setDecisionDirty(true);
  };
  const saveDecisions = async () => {
    if (!canAct()) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const record = await comparisonRequest<DecisionRecord>(props.block.run_id, props.block.session_id, props.block.result_digest, '/decisions', {
        result_digest: props.block.result_digest, decisions: decisions(), research_id: researchId(), expected_revision: decisionRecord()?.revision || 0,
      });
      setDecisionRecord(record); setDecisionDirty(false);
      setHistoryCursor(0); setHistoryRevision(0); void reloadHistory();
      setNotice(`研究决策第 ${record.revision} 版已保存到研究经验账本。采用不代表活性验证，也不会启动计算。`);
    } catch (e) { setError(e instanceof Error ? e.message : '决策保存失败'); }
    finally { setBusy(false); }
  };
  const toggle = (id: string) => setSelected(ids => ids.includes(id) ? ids.filter(i => i !== id) : [...ids, id]);
  const showCandidate = (id: string) => { setActive(id); setEvidence(undefined); };
  const save = async () => {
    if (!canAct() || !selected().length) return false;
    setBusy(true); setError(''); setNotice('');
    try {
      await comparisonRequest(props.block.run_id, props.block.session_id, props.block.result_digest, '/selection', {
        result_digest: props.block.result_digest, candidate_ids: selected(), reason: reason(),
      });
      setNotice('选择和理由已保存到会话，尚未代表采用或验证。');
      return true;
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败'); return false; }
    finally { setBusy(false); }
  };
  const ask = async () => {
    const sessionId = props.block.session_id, runId = props.block.run_id, digest = props.block.result_digest;
    const ids = [...selected()], input = question().trim();
    if (decisionDirty() || !input || !await save() || sessionStore.currentSessionId() !== sessionId || props.block.run_id !== runId || props.block.result_digest !== digest) return;
    const key = composerKey(sessionId);
    const text = `请基于我刚保存的候选选择继续研究。运行 ${runId}，结果摘要 ${digest}，候选 ID：${ids.join('、')}。先用 result_item_read 核对这些候选的当前数据，区分原文依据、推断和未知项；如 selected_results 中有当前研究决策，遵守采用、暂缓、排除及其理由，过时决策不得应用。我的要求：${input}`;
    composerDrafts.update(key, { text: [composerDrafts.get(key).text, text].filter(Boolean).join('\n\n') });
    setNotice('选择已保存，要求已加入当前会话输入框；发送后 Agent 继续。');
  };
  const openEvidence = async (reference: string) => {
    const id = active(); setBusy(true); setEvidence(undefined); setError('');
    try {
      const value = await comparisonRequest<Evidence>(props.block.run_id, props.block.session_id, props.block.result_digest,
        '/evidence', undefined, { candidate_id: id, reference });
      if (active() === id) setEvidence(value);
    } catch (e) { setError(e instanceof Error ? e.message : '证据不可用'); }
    finally { setBusy(false); }
  };
  return <section class="intelligent-canvas research-compare" data-style={data()?.display_style || 'clay'} aria-label="研究候选比较台">
    <header class="intelligent-canvas-header"><div><span class="intelligent-canvas-eyebrow">研究结果</span><h3>{props.block.title}</h3></div>
      <button type="button" disabled={busy()} onClick={() => { setError(''); setNotice(''); void refetch(); }}>{decisionDirty() ? '重新读取并放弃未保存决策' : '重新读取'}</button></header>
    <Show when={data.loading}><p role="status">正在读取候选…</p></Show>
    <Show when={data.error}><p role="alert">结果不可用或已改变。请让 Agent 重新打开比较台；旧选择不会用于新的结果。</p></Show>
    <Show when={!data.error && data()}>{value => <div class="research-compare-content">
      <p>本次运行 · {value().candidates.length} 个候选。评分来自模型排序，未知不等于零；Wiki 既往依据不代表目标反应已验证。</p>
      <Show when={value().selection_status === 'stale'}><p role="alert">旧选择已过时，请重新选择并核对理由。</p></Show>
      <Show when={value().decision_state.status === 'stale' && decisionRecord()?.result_digest !== props.block.result_digest}><p role="alert">旧决策对应不同结果，尚未应用。请重新核对并保存。</p></Show>
      <Show when={!value().decision_state.enabled}><p>研究记录功能已关闭，当前仅支持会话选择。</p></Show>
      <div class="research-compare-filters"><label>筛选候选<input value={query()} onInput={e => setQuery(e.currentTarget.value)} placeholder="名称或候选 ID" /></label>
        <label><input type="checkbox" checked={wikiOnly()} onChange={e => setWikiOnly(e.currentTarget.checked)} />只看有 Wiki 既往依据的候选</label></div>
      <div class="research-compare-table"><table><caption>显示 {rows().length} 个候选；已选择 {selected().length} 个（最多20个）</caption>
        <thead><tr><th>选择</th><th>候选</th><th>依据状态</th><th>模型评分</th><th>核实情况</th><th>查看</th><Show when={value().decision_state.enabled}><th>研究决策与理由</th></Show></tr></thead>
        <tbody><For each={rows()}>{c => <tr><td><input type="checkbox" aria-label={`选择 ${userFacingTitle(c.enzyme_name,'未命名候选')}`} checked={selected().includes(c.candidate_id)}
          disabled={!canAct() || (!selected().includes(c.candidate_id) && selected().length >= 20)} onChange={() => toggle(c.candidate_id)} /></td>
          <td>{userFacingTitle(c.enzyme_name,'未命名候选')}</td>
          <td>{c.evidence_scope === 'wiki_prior' ? 'Wiki既往依据' : c.evidence_scope === 'unverified' ? '未核实提议' : c.evidence_scope || '未知'}<Show when={c.hard_conflict}><strong>存在硬冲突</strong></Show></td>
          <td>{c.score ?? '未知'}</td><td>{c.verification_note || '未知'}</td><td><button type="button" disabled={busy()} onClick={() => showCandidate(c.candidate_id)}>查看依据</button></td>
          <Show when={value().decision_state.enabled}><td class="research-compare-decision"><select aria-label={`研究决策 ${userFacingTitle(c.enzyme_name,'未命名候选')}`}
            value={decision(c.candidate_id)?.state || ''} disabled={!canAct() || (!decision(c.candidate_id) && decisions().length >= 20)} onChange={e => changeDecision(c.candidate_id, e.currentTarget.value)}>
            <option value="">未决定</option><option value="adopt">采用</option><option value="defer">暂缓</option><option value="exclude">排除</option></select>
            <Show when={decision(c.candidate_id)}><textarea aria-label={`决策理由 ${userFacingTitle(c.enzyme_name,'未命名候选')}`} maxLength={500}
              value={decision(c.candidate_id)?.reason || ''} disabled={!canAct()} placeholder="填写理由（必填）" onInput={e => changeDecision(c.candidate_id, decision(c.candidate_id)!.state, e.currentTarget.value)} /></Show>
          </td></Show></tr>}</For></tbody>
      </table></div>
      <Show when={candidate()}>{c => <aside class="research-compare-evidence" aria-label="候选证据与缺口"><h4>{userFacingTitle(c().enzyme_name,'未命名候选')}</h4>
        <p>天然反应短引：{c().native_reaction_quote || '未提供可核对短引'}</p>
        <h5>支持与推断</h5><For each={c().supports || []}>{s => <p>{researchStatusLabel(s.kind,'依据类型待说明')} · {s.statement || '未提供'}</p>}</For>
        <h5>反对、限制与未知</h5><For each={c().contradictions || []}>{s => <p>{researchStatusLabel(s.kind,'依据类型待说明')} · {s.statement || '未提供'}</p>}</For>
        <For each={c().knowledge_gaps || []}>{gap => <p>{gap}</p>}</For>
        <h5>核对来源</h5><For each={[...new Set([...(c().sources || []), ...(c().supports || []).flatMap(s => s.source_refs || []), ...(c().contradictions || []).flatMap(s => s.source_refs || [])])]}>{(ref,index) =>
          <button type="button" disabled={busy()} onClick={() => void openEvidence(ref)}>查看来源 {index()+1}</button>}</For>
        <Show when={evidence()}>{ev => <div role="status"><p>{ev().message || '原文片段可读；适用范围仍需核对。'}</p>
          <Show when={ev().claim}>{claim => <><p>{researchStatusLabel(claim().attribution,'归因待说明')} · {researchStatusLabel(claim().source_scope,'适用范围待核对')} · {researchStatusLabel(claim().activity_status)}</p><pre>{claim().statement}</pre><p>{researchValueText(ev().scope,'scope')}</p><ResearchRecordDetails value={claim().sources}/></>}</Show>
        </div>}</Show>
      </aside>}</Show>
      <div class="research-compare-actions"><label>选择理由<textarea value={reason()} maxLength={2000} disabled={!canAct()} onInput={e => setReason(e.currentTarget.value)} /></label>
        <button type="button" disabled={!canAct() || !selected().length} onClick={() => void save()}>保存选择和理由</button>
        <Show when={value().decision_state.enabled}><label>关联课题（可选）<select aria-label="关联课题" value={researchId()} disabled={!canAct()} onChange={e => { setResearchId(e.currentTarget.value); setDecisionDirty(true); }}>
          <option value="">仅记录在本工作区，暂不关联课题</option><For each={value().decision_state.projects}>{p => <option value={p.research_id}>{p.title}</option>}</For>
          <Show when={researchId() && !value().decision_state.projects.some(p => p.research_id === researchId())}><option value={researchId()}>原课题不可用，请重新选择</option></Show>
        </select></label><p>已保存决策版本：{decisionRecord()?.revision || 0} · 本次 {decisions().length} 项。采用只是研究路线决定，不代表实验验证；保存后不会启动计算。</p>
          <button type="button" disabled={!canAct() || !decisionDirty() || decisions().some(d => !d.reason.trim())} onClick={() => void saveDecisions()}>保存研究决策</button>
          <Show when={decisionDirty()}><p role="status">决策尚未保存。保存后再继续对话，或重新读取放弃修改。</p></Show>
        </Show>

        <label>继续研究的要求<textarea value={question()} maxLength={2000} disabled={!canAct()} onInput={e => setQuestion(e.currentTarget.value)} placeholder="例如：比较所选候选的依据和缺失验证" /></label>
        <button type="button" disabled={!canAct() || decisionDirty() || !selected().length || !question().trim()} onClick={() => void ask()}>带选择继续对话</button></div>
    </div>}</Show>
        <details><summary>查看研究决策历史</summary>
          <p>每次只列出 50 个版本。查看历史不会改变当前决策；旧结果的决策只能核对。</p>
          <Show when={history.error}><p role="alert">历史暂时不可用，请重试。</p></Show>
          <label>决策版本<select aria-label="研究决策历史版本" value={historyRevision()} onChange={e => setHistoryRevision(Number(e.currentTarget.value))}>
            <option value="0">选择历史版本</option><For each={history()?.records || []}>{r => <option value={r.revision}>第 {r.revision} 版 · {r.decisions.length} 项决策</option>}</For>
          </select></label>
          <button type="button" disabled={history.loading || !history()?.next_before_revision} onClick={() => {setHistoryRevision(0); setHistoryCursor(history()?.next_before_revision || 0);}}>更早版本</button>
          <button type="button" disabled={history.loading} onClick={() => {setHistoryRevision(0); setHistoryCursor(0); void reloadHistory();}}>最新版本</button>
          <Show when={historicalDecision()}>{r => <div>
            <p>第 {r().revision} 版 · {r().result_digest === props.block.result_digest ? '与当前结果一致' : '对应旧研究结果，不可应用'}</p>
            <For each={r().decisions}>{d => <p>{data()?.candidates.find(c => c.candidate_id === d.candidate_id)?.enzyme_name || '未命名候选'}：{({adopt:'采用',defer:'暂缓',exclude:'排除'})[d.state]} · {d.reason}</p>}</For>
            <Show when={!r().decisions.length}><p>此版本已撤回全部决策。</p></Show>
            <button type="button" disabled={!canAct() || decisionDirty() || r().result_digest !== props.block.result_digest} onClick={() => {setDecisions(r().decisions.map(d => ({...d}))); setResearchId(r().research_id); setDecisionDirty(true);}}>将此版填入决策草稿</button>
          </div>}</Show>
        </details>
    <Show when={notice()}><p class="intelligent-canvas-notice" role="status">{notice()}</p></Show>
    <Show when={error()}><p role="alert">{error()}</p></Show>
  </section>;
}
