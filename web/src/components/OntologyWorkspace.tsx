import { confirmAction } from './FeedbackDialog';
import { createEffect, createSignal, For, lazy, onCleanup, Show, Suspense } from 'solid-js';
import { deleteOntologyResource, ontologyQualifierFilters, ontologyRequest, uploadOntologySource } from '../api/ontology';
import './OntologyWorkspace.css';
import OntologyEvidenceReport from './OntologyEvidenceReport';
import OntologyConflicts from './OntologyConflicts';
import { conflictLabels } from '../utils/ontologyConflicts';

const OntologyGraph = lazy(() => import('./OntologyGraph'));

type Operation = { id: string; published?: boolean; kind: string; payload: { label?: string; type?: string; subject_id?: string; object_id?: string; predicate?: string; polarity?: string; kind?: string; entity_id?: string; identity_state?: string; suggestions?: string[]; aliases?: string[]; mutations?: string[]; qualifiers?: Record<string, unknown>; quote: string }; span: { locator: { page?: number; line_start: number; line_end: number } } };
type Review = { item_id: string; payload_hash: string; review_revision: number; source_revision: number; decision: string; published: boolean; operations: Operation[]; questions: unknown[] };
type SchemaClass = { id: string; label?: string; description?: string };
type SchemaPredicate = { id: string; label?: string; description?: string; domains: string[]; ranges: string[] };
const REVIEW_PAGE_SIZE = 10;
const SCIENTIFIC_FIELDS: Record<string,string> = {metric:'指标',value:'观测值',unit:'单位',comparison_target:'比较对象',comparison_value:'比较值',comparison_unit:'比较单位',fold_change:'变化倍数',operator:'范围限定',evidence_basis:'证据类别',scale:'规模',duration:'持续时间',time:'时间',condition:'条件',conditions:'条件'};
const PREDICATE_LABELS: Record<string,string> = {has_observation:'研究观察',has_transition_state:'经过渡态',related_to:'相关',catalyzes_reported:'催化（文献报告）',has_participant:'参与者',derived_from:'衍生自',member_of:'属于',requires_cofactor:'需要辅因子',supports_capability:'支持能力'};
const IMPORT_STATES: Record<string,string> = {queued:'等待处理',running:'正在抽取',succeeded:'处理完成',failed:'处理失败',cancelled:'已停止',canceled:'已停止'};

function importStatusText(data: any): string {
  const lines = [`导入任务：${IMPORT_STATES[data.state] || '状态待确认'}${data.cancel_requested ? '（已请求停止）' : ''}`];
  if (data.model_ref) lines.push(`抽取模型：${data.model_ref}`);
  for (const item of data.items || []) {
    const progress = item.progress;
    lines.push(`${item.name || '文档'}：${IMPORT_STATES[item.state] || '状态待确认'}`);
    if (progress) {
      lines.push(`已完成 ${progress.completed_spans ?? 0}/${progress.total_spans ?? 0} 个片段；已生成 ${progress.entities ?? 0} 个实体、${progress.draft_assertions ?? progress.assertions ?? 0} 条关系草稿。`);
      lines.push(progress.reviewable ? '草稿已准备好，可进入审核。' : '草稿尚未准备好，暂不能审核。');
    }
    if (item.error) lines.push(`失败原因：${item.error}`);
    for (const retry of item.gleaning_retries || []) {
      if (retry.state === 'failed' && retry.error) lines.push(`补抽失败：${retry.error}`);
    }
    const warnings = item.report?.warnings || [];
    const limits = [
      warnings.includes('pdf_text_layer_only') && '仅读取 PDF 文本层',
      warnings.includes('figures_and_table_geometry_not_extracted') && '未提取图片和表格布局',
      warnings.includes('supplementary_files_not_included') && '未包含补充材料',
    ].filter(Boolean);
    if (limits.length) lines.push(`解析范围：${limits.join('；')}。`);
  }
  return lines.join('\n');
}

export default function OntologyWorkspace(props: { workspaceId?: string; currentModelRef?: string }) {
  const workspace = () => props.workspaceId || 'workspace-1';
  const [reviews, setReviews] = createSignal<any[]>([]);
  const [jobs, setJobs] = createSignal<any[]>([]);
  const [preview, setPreview] = createSignal<Review>();
  const [reviewFilter, setReviewFilter] = createSignal('');
  const [reviewPage, setReviewPage] = createSignal(0);
  const filteredOperations = () => (preview()?.operations || []).filter(op => JSON.stringify(op.payload).toLowerCase().includes(reviewFilter().toLowerCase()));
  const [selected, setSelected] = createSignal<string[]>([]);
  const [choices, setChoices] = createSignal<Record<string, string>>({});
  const [acknowledged, setAcknowledged] = createSignal(false);
  const [edits, setEdits] = createSignal<Record<string, Record<string, unknown>>>({});
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [query, setQuery] = createSignal('');
  const [entityId, setEntityId] = createSignal('');
  const [queryScope, setQueryScope] = createSignal('both');
  const [predicateFilter, setPredicateFilter] = createSignal('');
  const [evidenceFilter, setEvidenceFilter] = createSignal('');
  const [polarityFilter, setPolarityFilter] = createSignal('');
  const [conditions, setConditions] = createSignal<Array<{key:string;value:string}>>([]);
  let searchSequence = 0;
  const activeFilterCount = () => Number(queryScope()!=='both')+Number(!!predicateFilter())+Number(!!evidenceFilter())+Number(!!polarityFilter())+conditions().filter(row=>row.key.trim()&&row.value.trim()).length;
  const predicateOptions = () => [...new Map([...(schemaData()?.system?.predicates || []), ...(schemaData()?.workspace?.predicates || [])].map((row:any) => [row.id, row])).values()] as SchemaPredicate[];
  const clearSearch = (clearEntity = false) => {
    ++searchSequence; setResult(undefined); setReferences([]);
    if (clearEntity) setEntityId('');
  };
  const [result, setResult] = createSignal<any>();
  const [references, setReferences] = createSignal<string[]>([]);
  const [model, setModel] = createSignal(false);
  const [models, setModels] = createSignal<Array<{id:string;name:string}>>([]);
  const [modelRef, setModelRef] = createSignal('');
  const [defaultModelRef, setDefaultModelRef] = createSignal('');
  const [modelError, setModelError] = createSignal('');
  const effectiveModelRef = () => modelRef() || props.currentModelRef || defaultModelRef();
  const [sourceId, setSourceId] = createSignal('');
  const [baseRevision, setBaseRevision] = createSignal('');
  const [schemaData, setSchemaData] = createSignal<any>();
  const [schemaClasses, setSchemaClasses] = createSignal<SchemaClass[]>([]);
  const [schemaPredicates, setSchemaPredicates] = createSignal<SchemaPredicate[]>([]);
  const [sourcePolicy, setSourcePolicy] = createSignal({public:true,private:true,version:0});
  const [conflictPair, setConflictPair] = createSignal<{left:string;right:string}>();
  createEffect(()=>{workspace();setConflictPair(undefined);});
  const [view, setView] = createSignal<'query' | 'graph' | 'review' | 'schema' | 'conflicts'>('query');
  const [nextReviews, setNextReviews] = createSignal<string | null>(null);
  let generation = 0;
  const button = 'rounded border border-[#45545d] px-3 py-2 text-sm disabled:opacity-40 hover:bg-[#2b363e]';
  const input = 'rounded border border-[#45545d] bg-[#141c22] p-2 text-sm';

  async function refresh(w: string, version: number, after = '') {
    const [r, j, m, p, schema] = await Promise.allSettled([ontologyRequest(w, `/reviews?after=${encodeURIComponent(after)}`), ontologyRequest(w, '/jobs?include_deleted=true'), ontologyRequest(w, '/models'), ontologyRequest(w, '/source-policy'), ontologyRequest(w, '/schema')]);
    if (generation !== version) return;
    if (r.status === 'fulfilled') { setReviews(r.value.items); setNextReviews(r.value.next_cursor); }
    else { setReviews([]); setNextReviews(null); setError('待审草稿暂时无法读取；导入任务仍可单独管理。'); }
    if (j.status === 'fulfilled') setJobs(j.value.items);
    if (m.status === 'fulfilled') { setModels(m.value.items || []); setDefaultModelRef(m.value.default_model_ref || ''); setModelError(m.value.error || ''); }
    else setModelError('模型配置读取失败，请刷新后重试。');
    if (p.status === 'fulfilled') setSourcePolicy(p.value);
    if (schema.status === 'fulfilled') setSchemaData(schema.value);
  }
  async function act(work: (w: string, version: number) => Promise<void>) {
    const w = workspace(), version = generation;
    setBusy(true); setError(''); setNotice('');
    try { await work(w, version); } catch (e) { if (generation === version) setError(String(e)); }
    finally { if (generation === version) setBusy(false); }
  }
  createEffect(() => {
    const w = workspace(), version = ++generation;
    ++searchSequence; setQuery(''); setEntityId(''); setQueryScope('both'); setPredicateFilter(''); setEvidenceFilter(''); setPolarityFilter(''); setConditions([]);
    setPreview(undefined); setSelected([]); setChoices({}); setResult(undefined); setReferences([]); setReviews([]); setJobs([]); setSchemaData(undefined); setError(''); setBusy(false);
    refresh(w, version).catch(e => { if (generation === version) setError(String(e)); });
  });
  createEffect(() => {
    const w = workspace(), version = generation;
    if (!jobs().some(job => ['queued', 'running'].includes(job.state) && !job.deleted_at)) return;
    const timer = setInterval(() => {
      ontologyRequest(w, '/jobs?include_deleted=true').then(data => {
        if (generation === version) setJobs(data.items || []);
      }).catch(() => { if (generation === version) setError('导入状态暂时无法更新，请检查连接后刷新。'); });
    }, 3000);
    onCleanup(() => clearInterval(timer));
  });
  const choose = (id: string, checked: boolean) => setSelected(old => checked ? [...new Set([...old, id])] : old.filter(x => x !== id));
  const reviewPageItems = () => filteredOperations().slice(reviewPage()*REVIEW_PAGE_SIZE,(reviewPage()+1)*REVIEW_PAGE_SIZE);
  const entityName = (id?:string) => preview()?.operations.find(op=>op.kind==='entity'&&op.payload.entity_id===id)?.payload.label || '待命名实体';
  const classLabel = (id?: string) => [...(schemaData()?.system?.classes || []), ...(schemaData()?.workspace?.classes || [])].find((item:any)=>item.id===id)?.label || id || '类型待确认';
  const operationTitle = (op:Operation) => op.kind==='entity'
    ? `${op.payload.label||'待命名实体'}（${classLabel(op.payload.type)}）`
    : `${entityName(op.payload.subject_id)} → ${PREDICATE_LABELS[op.payload.predicate||'']||op.payload.predicate||'关系待确认'} → ${entityName(op.payload.object_id)}`;
  const updateObservation = (op: Operation, key: string, value: string) => {
    try {
      const qualifiers = {...op.payload.qualifiers, ...((edits()[op.id]?.qualifiers as Record<string, unknown>) || {})};
      if (value.trim()) qualifiers[key] = ontologyQualifierFilters([{key, value}])[key];
      else delete qualifiers[key];
      setEdits(old=>({...old,[op.id]:{...old[op.id],qualifiers}}));
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  };
  const selectablePageIds = () => reviewPageItems().filter(op=>!op.published).map(op=>op.id);
  const selectCurrentPage = () => setSelected(old=>[...new Set([...old,...selectablePageIds()])]);
  const updateSchemaClass = (index: number, patch: Partial<SchemaClass>) => setSchemaClasses(old=>old.map((row,i)=>i===index?{...row,...patch}:row));
  const updateSchemaPredicate = (index: number, patch: Partial<SchemaPredicate>) => setSchemaPredicates(old=>old.map((row,i)=>i===index?{...row,...patch}:row));
  const schemaTypeOptions = () => [...(schemaData()?.system?.classes || []),...schemaClasses()].map((row:any)=>row.id).filter(Boolean);
  const setKnowledgeSource = (key:'public'|'private',enabled:boolean) => void act(async(w,v)=>{
    const current=sourcePolicy();
    const saved=await ontologyRequest(w,'/source-policy',{
      public:key==='public'?enabled:current.public,
      private:key==='private'?enabled:current.private,
    });
    if(generation===v){setSourcePolicy(saved);setNotice('Agent 本体知识源设置已保存。');}
  });
  async function openReview(w: string, version: number, id: string) {
    const value = await ontologyRequest<Review>(w, `/reviews/${encodeURIComponent(id)}`);
    if (generation !== version) return;
    setPreview(value); setSelected([]); setReviewFilter(''); setReviewPage(0); setChoices({}); setAcknowledged(false); setEdits({});
  }
  async function search(w: string, version: number, after = '') {
    const sequence = ++searchSequence;
    const params = new URLSearchParams({text:query().trim(), entity_id:entityId(), scope:queryScope(),
      predicate:predicateFilter(), evidence_basis:evidenceFilter(), polarity:polarityFilter(),
      qualifiers:JSON.stringify(ontologyQualifierFilters(conditions())), after});
    const value = await ontologyRequest(w, `/query?${params}`);
    if (generation === version && sequence === searchSequence) { setResult(value); setReferences([]); }
  }

  return <section class="ontology-workspace h-full flex-1 overflow-y-auto p-3 space-y-3">
    <header class="ontology-header"><div><h1 class="text-lg font-semibold">本体知识</h1><p class="text-sm">查询公共知识与当前 Workspace 私有知识，关系探索只读，发布仍需人工审核。</p></div><nav aria-label="本体知识视图"><button classList={{ active:view()==='query' }} onClick={()=>setView('query')}>知识查询</button><button classList={{ active:view()==='graph' }} onClick={()=>setView('graph')}>关系探索</button><button classList={{ active:view()==='review' }} onClick={()=>setView('review')}>知识审核</button><button classList={{ active:view()==='schema' }} onClick={()=>{setView('schema');void act(async(w,v)=>{const data=await ontologyRequest(w,'/schema');if(generation===v){setSchemaData(data);setSchemaClasses(data.workspace.classes || []);setSchemaPredicates(data.workspace.predicates || []);}})}}>实体定义</button><button classList={{active:view()==='conflicts'}} onClick={()=>setView('conflicts')}>冲突处理</button></nav></header>
    <div class="ontology-source-policy"><div><strong>Agent 可用知识源</strong><small>只影响后续 Agent 取证；图谱范围仍可独立查看。</small></div><label><input type="checkbox" checked={sourcePolicy().public} disabled={busy()} onChange={e=>setKnowledgeSource('public',e.currentTarget.checked)}/><span aria-hidden="true"/>公共本体</label><label><input type="checkbox" checked={sourcePolicy().private} disabled={busy()} onChange={e=>setKnowledgeSource('private',e.currentTarget.checked)}/><span aria-hidden="true"/>私有本体</label><Show when={!sourcePolicy().public&&!sourcePolicy().private}><em>当前无可用本体知识源</em></Show></div>
    <Show when={error()}><p role="alert" class="text-red-300">{error()}</p></Show>
    <Show when={jobs().find(job => !job.deleted_at && job.error)}>{job => <p role="alert">导入任务提示：{job().error} 可在“导入任务与失败重试”中查看详情并重试。</p>}</Show>
    <Show when={notice()}><p role="status" class="whitespace-pre-wrap text-emerald-300">{notice()}</p></Show>
    <Show when={view()==='review'}><div class="ontology-review-view space-y-5"><div class="flex flex-wrap gap-3 items-center">
      <label class={button}>上传 PDF / TXT / Markdown
        <input class="block mt-2" type="file" accept=".pdf,.txt,.md" disabled={busy()} onChange={e => {
          const file = e.currentTarget.files?.[0]; if (!file) return;
          void act(async (w,v) => {
            const path = await uploadOntologySource(w,file);
            const job = await ontologyRequest(w,'/imports',{ upload_path:path, extractor:model()?'model':'mentions', model_ref:model()?(effectiveModelRef()||null):null, source_id:sourceId()||null, base_revision:baseRevision()?Number(baseRevision()):null });
            if (generation===v) setNotice(`已提交草稿任务 ${job.job_id}；任务状态会自动更新。`);
            await refresh(w,v);
          });
        }}/>
      </label>
      <label><input type="checkbox" checked={model()} onChange={e=>setModel(e.currentTarget.checked)}/> 使用已配置模型抽取关系</label>
      <Show when={model()}><select class={input} aria-label="本体抽取模型" value={modelRef()} onChange={e=>setModelRef(e.currentTarget.value)}><option value="">{props.currentModelRef ? `当前会话模型（${props.currentModelRef}）` : `用户默认模型（${defaultModelRef() || '未配置'}）`}</option><For each={models()}>{m=><option value={m.id}>{m.name}（{m.id}）</option>}</For></select><span>实际抽取模型：{effectiveModelRef() || '未配置，请先前往模型设置'}</span><Show when={modelError() && !effectiveModelRef()}><span role="alert">{modelError()}</span></Show></Show>
      <input class={input} aria-label="来源标识" placeholder="来源标识（修订时保持相同）" value={sourceId()} onInput={e=>setSourceId(e.currentTarget.value)}/>
      <input class={input} type="number" min="1" aria-label="原修订号" placeholder="原修订号（可选）" value={baseRevision()} onInput={e=>setBaseRevision(e.currentTarget.value)}/>
      <button class={button} disabled={busy()} onClick={()=>void act(refresh)}>刷新</button>
    </div>
    <p class="text-xs text-[#9aa9b3]">未勾选模型时只识别已知实体名称，不生成催化关系。选择云端模型会把本文文本发送到该已配置服务，仅影响本次任务；扫描件需要后续 OCR，引用定位通过不等于科学结论正确。</p>
    <details><summary>导入任务与失败重试</summary><For each={jobs()}>{job=><div class="flex flex-wrap gap-3 p-2"><code>{job.id}</code><span>{job.deleted_at?'已删除':IMPORT_STATES[job.state] || '状态待确认'}</span><Show when={job.model_ref}><span>模型：{job.model_ref}</span></Show><Show when={job.error}><span role="alert">失败原因：{job.error}</span></Show>
      <Show when={!job.deleted_at}><button class={button} disabled={busy()} onClick={()=>void act(async(w,v)=>{ const data=await ontologyRequest(w,`/jobs/${job.id}`); if(generation===v)setNotice(importStatusText(data)); })}>查看状态</button></Show>
      <Show when={!job.deleted_at && ['queued','running'].includes(job.state)}><button class={button} disabled={busy()} onClick={async()=>{if(await confirmAction('停止后才能删除。确认停止这个导入任务？'))void act(async(w,v)=>{await ontologyRequest(w,`/jobs/${job.id}/cancel`,{});setNotice('已请求停止任务；待当前模型调用结束后可删除。');await refresh(w,v);});}}>停止</button></Show>
      <Show when={!job.deleted_at && !['queued','running'].includes(job.state)}><button class={button} disabled={busy()} onClick={()=>void act(async(w,v)=>{await ontologyRequest(w,`/jobs/${job.id}/retry`,{});setNotice('已提交有界重试；失败草稿只保留审计，不可审核或发布。');await refresh(w,v);})}>重试 / 恢复</button><button class={button} disabled={busy()} onClick={async()=>{if(await confirmAction('删除后任务记录会从正常列表隐藏，但可恢复；不会删除已发布知识或来源文件。确认删除？'))void act(async(w,v)=>{await deleteOntologyResource(w,`/jobs/${job.id}`);setNotice('任务记录已软删除，可从本列表恢复；已发布知识与来源文件未受影响。');await refresh(w,v);});}}>删除</button></Show>
      <Show when={!job.deleted_at && job.state==='succeeded'}><button class={button} disabled={busy()} onClick={()=>void act(async(w,v)=>{await ontologyRequest(w,`/jobs/${job.id}/retry-gleaning`,{});setNotice('只补失败的补抽片段；成功结果与历史记录保持不变。');await refresh(w,v);})}>仅补失败补抽</button></Show>
      <Show when={job.deleted_at}><button class={button} disabled={busy()} onClick={()=>void act(async(w,v)=>{await ontologyRequest(w,`/jobs/${job.id}/restore`,{});setNotice('任务记录已恢复。');await refresh(w,v);})}>恢复</button></Show>
    </div>}</For><p class="text-xs">运行中的任务必须先明确停止，任务结束后才能删除。删除仅隐藏任务记录，可恢复；不会删除已发布知识、证据或来源文件。“查看状态”包含已完成/总片段与部分草稿数；只有全部片段验证完成后才进入待审。</p></details>
    <div class="ontology-review-grid">
      <aside class="ontology-review-list space-y-2"><h2 class="font-semibold">待审文档</h2><For each={reviews()}>{r=><div class="space-y-1"><button class={`${button} block w-full text-left`} disabled={busy()||r.needs_rebase} onClick={()=>void act((w,v)=>openReview(w,v,r.item_id))}><span>{r.name}</span><small class="block">{r.needs_rebase?'公共知识版本已更新，需预览重新绑定':r.published?'已发布':r.decision}</small></button><Show when={r.needs_rebase}><button class={button} disabled={busy()} onClick={()=>void act(async(w,v)=>{const check=await ontologyRequest(w,`/reviews/${r.item_id}/rebase`,{apply:false});if(check.blockers?.length){setNotice(`重新校验发现 ${check.blockers.length} 个待处理冲突，未修改草稿。`);return;}if(!check.can_apply){setNotice('旧版本缺少可验证的内容摘要，不能安全自动绑定；请重新抽取。');return;}if(await confirmAction(`已按当前公共本体重新校验 ${check.validated_spans} 个片段，未发现 schema/实体冲突。确认只更新版本绑定？原候选、人工修改和证据保持不变，不会发布。`)){await ontologyRequest(w,`/reviews/${r.item_id}/rebase`,{apply:true});setNotice('草稿已安全绑定到当前公共知识版本；原候选、人工修改和证据均保留，尚未发布。');await refresh(w,v);}})}>预览并重新绑定</button></Show></div>}</For><Show when={nextReviews()}><button class={button} onClick={()=>void act((w,v)=>refresh(w,v,nextReviews()!))}>下一页</button></Show></aside>
      <Show when={preview()}>{review=><div class="ontology-review-content space-y-3">
        <p>来源修订 {review().source_revision}；{review().operations.length} 项候选；{review().questions.length} 个待确认问题。</p>
        <Show when={review().questions.length}><details><summary class="text-amber-300">需核对身份、科学证据、冲突或已隔离关系（{review().questions.length}）</summary><pre class="whitespace-pre-wrap text-xs">{JSON.stringify(review().questions,null,2)}</pre><p class="text-xs">已隔离关系不会进入发布操作；确认问题仅允许发布下方选中的合格候选，不代表修复或发布隔离项。</p></details></Show>
        <div class="flex flex-wrap gap-3"><button class={button} disabled={!selectablePageIds().length} onClick={selectCurrentPage}>全选当前页（最多 10 项）</button><button class={button} onClick={()=>setSelected([])}>清空全部选择</button><span class="self-center text-sm">已选 {selected().length} 项</span></div>
        <input class={input} aria-label="筛选审核候选" placeholder="按实体名称、关系或证据筛选" value={reviewFilter()} onInput={e=>{setReviewFilter(e.currentTarget.value);setReviewPage(0);}}/>
        <div class="flex flex-wrap items-center gap-3"><span>{filteredOperations().length} 项匹配；第 {reviewPage()+1} / {Math.max(1,Math.ceil(filteredOperations().length/REVIEW_PAGE_SIZE))} 页</span><button class={button} disabled={reviewPage()===0} onClick={()=>setReviewPage(p=>p-1)}>上一页候选</button><button class={button} disabled={(reviewPage()+1)*REVIEW_PAGE_SIZE>=filteredOperations().length} onClick={()=>setReviewPage(p=>p+1)}>下一页候选</button></div>
        <For each={reviewPageItems()}>{op=><article class="ontology-review-card rounded border border-[#35434d] p-3 space-y-2">
          <label><input type="checkbox" disabled={op.published} checked={selected().includes(op.id)} onChange={e=>choose(op.id,e.currentTarget.checked)}/> {op.published?'已发布：':''}{operationTitle(op)}</label>
          <Show when={op.payload.aliases?.length}><p class="text-sm break-words">别名：{op.payload.aliases!.join('；')}</p></Show>
          <Show when={op.payload.mutations?.length}><p class="text-sm break-words">突变：{op.payload.mutations!.join(' · ')}</p></Show>
          <Show when={op.kind==='assertion' && op.payload.qualifiers && Object.keys(op.payload.qualifiers).length}><dl class="ontology-query-qualifiers"><For each={Object.entries(op.payload.qualifiers || {})}>{([key,value])=><><dt>{SCIENTIFIC_FIELDS[key] || key}</dt><dd>{typeof value==='string'?value:JSON.stringify(value)}</dd></>}</For></dl></Show>
          <blockquote class="whitespace-pre-wrap text-sm text-[#b9c8d1]">{op.payload.quote}</blockquote>
          <details><summary>高级技术详情</summary><code>{op.kind==='entity'?op.payload.entity_id:`${op.payload.subject_id} → ${op.payload.predicate} → ${op.payload.object_id}`}</code></details>
          <Show when={!op.published && op.kind==='entity'}><div class="flex gap-2"><input class={input} aria-label="实体标签" value={String(edits()[op.id]?.label ?? op.payload.label ?? '')} onInput={e=>setEdits(old=>({...old,[op.id]:{...old[op.id],label:e.currentTarget.value}}))}/><input class={input} aria-label="实体类型" value={String(edits()[op.id]?.type ?? op.payload.type ?? '')} onInput={e=>setEdits(old=>({...old,[op.id]:{...old[op.id],type:e.currentTarget.value}}))}/></div></Show>
          <Show when={!op.published && op.kind==='entity'}><details><summary>修订别名与突变</summary><div class="flex flex-wrap gap-2"><label>别名（分号分隔）<input class={input} aria-label="实体别名" value={((edits()[op.id]?.aliases as string[] | undefined) ?? op.payload.aliases ?? []).join('; ')} onChange={e=>setEdits(old=>({...old,[op.id]:{...old[op.id],aliases:e.currentTarget.value.split(/[;；]/).map(x=>x.trim()).filter(Boolean)}}))}/></label><Show when={op.payload.type==='EnzymeVariant'}><label>突变（空格或逗号分隔）<input class={input} aria-label="变体突变列表" value={((edits()[op.id]?.mutations as string[] | undefined) ?? op.payload.mutations ?? []).join(', ')} onChange={e=>setEdits(old=>({...old,[op.id]:{...old[op.id],mutations:e.currentTarget.value.split(/[,，;；\s]+/).filter(Boolean)}}))}/></label></Show></div><p class="text-xs">别名与突变必须出现在当前实体引文中，保存时重新验证。</p></details></Show>
          <Show when={!op.published && op.kind==='assertion'}><div class="flex gap-2"><input class={input} aria-label="关系谓词" value={String(edits()[op.id]?.predicate ?? op.payload.predicate ?? '')} onInput={e=>setEdits(old=>({...old,[op.id]:{...old[op.id],predicate:e.currentTarget.value}}))}/><select class={input} aria-label="关系极性" value={String(edits()[op.id]?.polarity ?? op.payload.polarity ?? 'supports')} onChange={e=>setEdits(old=>({...old,[op.id]:{...old[op.id],polarity:e.currentTarget.value}}))}><option value="supports">支持</option><option value="opposes">反对</option></select></div></Show>
          <Show when={!op.published && op.payload.predicate==='has_observation'}><details><summary>修订研究观察</summary><div class="grid gap-2 sm:grid-cols-2"><For each={['metric','value','unit','comparison_target','comparison_value','comparison_unit','fold_change','operator']}>{key=><label>{SCIENTIFIC_FIELDS[key] || key}<input class={`${input} block w-full`} aria-label={`观察${SCIENTIFIC_FIELDS[key] || key}`} value={String(((edits()[op.id]?.qualifiers as Record<string, unknown>) ?? op.payload.qualifiers ?? {})[key] ?? '')} onChange={e=>updateObservation(op,key,e.currentTarget.value)}/></label>}</For></div><p class="text-xs">只填写原文支持的值；保存时重新核对证据。不要把相对活性和 kcat/Km 的倍数合并。</p></details></Show>
          <small>第 {op.span.locator.page || '—'} 页；文本行 {op.span.locator.line_start}–{op.span.locator.line_end}</small>
          <Show when={op.payload.identity_state==='needs_review'}><select class={`${input} block`} aria-label="确认实体身份" value={choices()[op.payload.entity_id!]||''} onChange={e=>setChoices(old=>({...old,[op.payload.entity_id!]:e.currentTarget.value}))}><option value="">请选择实体身份</option><option value={op.payload.entity_id}>保留为新的私有实体</option><For each={op.payload.suggestions}>{id=><option value={id}>{id}</option>}</For></select></Show>
        </article>}</For>
        <button class={button} disabled={busy()||review().published||!Object.keys(edits()).length} onClick={()=>void act(async(w,v)=>{
          const revised=await ontologyRequest<Review>(w,`/reviews/${review().item_id}/revisions`,{expected_revision:review().review_revision,payload_hash:review().payload_hash,edits:Object.entries(edits()).map(([operation_id,changes])=>({operation_id,changes}))});
          if(generation===v){setNotice('草稿修订已保存并重新通过 schema 与关系约束校验。');setPreview(revised);setSelected([]);setChoices({});setEdits({});}
          await refresh(w,v);
        })}>保存草稿修订</button>
        <Show when={review().questions.length || Object.keys(choices()).length}><label class="block"><input type="checkbox" checked={acknowledged()} onChange={e=>setAcknowledged(e.currentTarget.checked)}/> 我已核对上述差异，允许选中主张以存在争议的状态保留</label></Show>
        <button class={button} disabled={busy()||review().published||!selected().length} onClick={()=>void act(async(w,v)=>{
          const confirmedIds=selected();
          const decision=await ontologyRequest(w,`/reviews/${review().item_id}/decisions`,{expected_revision:review().review_revision,payload_hash:review().payload_hash,selected_ids:selected(),identity_choices:choices(),accept_conflicts:acknowledged()});
          if(generation===v)setNotice('审核决定已保存，尚未发布。');
          await openReview(w,v,decision.item_id);if(generation===v)setSelected(confirmedIds);await refresh(w,v);
        })}>批量确认选中项</button>
        <button class={button} disabled={busy()||review().published||review().decision!=='accepted'||!selected().length} onClick={async()=>{if(await confirmAction(`确认发布已审核的 ${selected().length} 项？未选候选将继续保持待审。`))void act(async(w,v)=>{
          await ontologyRequest(w,`/changesets/${review().item_id}/publish`,{expected_revision:review().review_revision});
          if(generation===v)setNotice('已发布到当前 Workspace 私有库。');await openReview(w,v,review().item_id);await refresh(w,v);
        });}}>确认发布所选（{selected().length}）</button>
      </div>}</Show>
    </div></div></Show>
    <Show when={view()==='conflicts'}><OntologyConflicts workspaceId={workspace()}/></Show>
    <Show when={view()==='graph'}><Suspense fallback={<p>正在加载关系探索…</p>}><OntologyGraph workspaceId={workspace()}/></Suspense></Show>
    <Show when={view()==='schema'}><div class="ontology-schema-view space-y-4"><div><h2 class="font-semibold">当前 Workspace 的实体与关系定义</h2><p class="text-sm">系统定义只读。这里保存的是版本化 Workspace 快照；旧事实继续绑定创建时的 schema 版本。</p></div>
      <Show when={schemaData()}>{data=><><details><summary>只读系统定义</summary><div class="ontology-table-scroll"><table><thead><tr><th>类型 ID</th><th>显示名称</th><th>说明</th></tr></thead><tbody><For each={data().system.classes}>{(row:any)=><tr><td><code>{row.id}</code></td><td>{row.label||'—'}</td><td>{row.description||'—'}</td></tr>}</For></tbody></table></div></details>
        <section><div class="ontology-schema-heading"><div><h3>Workspace 实体类型</h3><p>ID 为英文标识；名称和说明用于审核与抽取提示。</p></div><button class={button} onClick={()=>setSchemaClasses(old=>[...old,{id:'',label:'',description:''}])}>新增实体类型</button></div><div class="ontology-table-scroll"><table><thead><tr><th>类型 ID（必填）</th><th>显示名称</th><th>说明</th><th></th></tr></thead><tbody><For each={schemaClasses()}>{(row,index)=><tr><td><input aria-label={`实体类型 ${index()+1} ID`} value={row.id} onInput={e=>updateSchemaClass(index(),{id:e.currentTarget.value})}/></td><td><input aria-label={`实体类型 ${index()+1} 名称`} value={row.label||''} onInput={e=>updateSchemaClass(index(),{label:e.currentTarget.value})}/></td><td><input aria-label={`实体类型 ${index()+1} 说明`} value={row.description||''} onInput={e=>updateSchemaClass(index(),{description:e.currentTarget.value})}/></td><td><button class={button} onClick={()=>setSchemaClasses(old=>old.filter((_,i)=>i!==index()))}>移除</button></td></tr>}</For></tbody></table></div></section>
        <section><div class="ontology-schema-heading"><div><h3>Workspace 关系类型</h3><p>关系两端必须从系统或 Workspace 实体类型中选择；可多选。</p></div><button class={button} onClick={()=>setSchemaPredicates(old=>[...old,{id:'',label:'',description:'',domains:[],ranges:[]}])}>新增关系类型</button></div><div class="ontology-table-scroll"><table><thead><tr><th>关系 ID（必填）</th><th>名称</th><th>起点类型（必填）</th><th>终点类型（必填）</th><th>说明</th><th></th></tr></thead><tbody><For each={schemaPredicates()}>{(row,index)=><tr><td><input aria-label={`关系 ${index()+1} ID`} value={row.id} onInput={e=>updateSchemaPredicate(index(),{id:e.currentTarget.value})}/></td><td><input aria-label={`关系 ${index()+1} 名称`} value={row.label||''} onInput={e=>updateSchemaPredicate(index(),{label:e.currentTarget.value})}/></td><td><select multiple aria-label={`关系 ${index()+1} 起点类型`} onChange={e=>updateSchemaPredicate(index(),{domains:Array.from(e.currentTarget.selectedOptions,o=>o.value)})}><For each={schemaTypeOptions()}>{id=><option value={id} selected={row.domains.includes(id)}>{id}</option>}</For></select></td><td><select multiple aria-label={`关系 ${index()+1} 终点类型`} onChange={e=>updateSchemaPredicate(index(),{ranges:Array.from(e.currentTarget.selectedOptions,o=>o.value)})}><For each={schemaTypeOptions()}>{id=><option value={id} selected={row.ranges.includes(id)}>{id}</option>}</For></select></td><td><input aria-label={`关系 ${index()+1} 说明`} value={row.description||''} onInput={e=>updateSchemaPredicate(index(),{description:e.currentTarget.value})}/></td><td><button class={button} onClick={()=>setSchemaPredicates(old=>old.filter((_,i)=>i!==index()))}>移除</button></td></tr>}</For></tbody></table></div></section>
        <details><summary>高级：查看 JSON</summary><p class="text-xs">仅用于复制或排查；请通过上方表格修改。</p><pre class="ontology-schema-json">{JSON.stringify({classes:schemaClasses(),predicates:schemaPredicates()},null,2)}</pre></details>
        <button class={button} disabled={busy()||schemaClasses().some(row=>!row.id.trim())||schemaPredicates().some(row=>!row.id.trim()||!row.domains.length||!row.ranges.length)} onClick={()=>void act(async(w,v)=>{const saved=await ontologyRequest(w,'/schema/revisions',{expected_version:data().workspace_schema_version,classes:schemaClasses(),predicates:schemaPredicates()});if(generation===v){setSchemaData(saved);setSchemaClasses(saved.workspace.classes||[]);setSchemaPredicates(saved.workspace.predicates||[]);setNotice(`Workspace schema v${saved.workspace_schema_version} 已保存。`);}})}>保存新版本</button>
      </>}</Show>
    </div></Show>
    <Show when={view()==='query'}><div class="ontology-query-view space-y-3"><div><h2 class="font-semibold">公共与私有知识查询</h2><p class="mt-1 text-sm">按名称或别名查找实体，筛选有证据的关系，并生成可追溯引用。</p></div>
      <form class="ontology-search-form" onSubmit={e=>{e.preventDefault();void act((w,v)=>search(w,v));}}>
        <label>实体名称或别名<input class={input} aria-label="实体名称" placeholder="例如 TPP，也可输入部分名称" value={query()} onInput={e=>{clearSearch(true);setQuery(e.currentTarget.value);}}/></label>
        <button class={button} type="submit" disabled={busy()||!query().trim()}>{busy()?'查询中…':'查询'}</button>
      </form>
      <details class="ontology-query-filters"><summary>筛选关系与证据<Show when={activeFilterCount()}>（{activeFilterCount()} 项）</Show></summary>
        <div class="ontology-filter-grid">
          <label>知识范围<select aria-label="知识范围" value={queryScope()} onChange={e=>{clearSearch(true);setQueryScope(e.currentTarget.value);}}><option value="both">公共 + 私有</option><option value="public">仅公共</option><option value="private">仅当前 Workspace 私有</option></select></label>
          <label>关系类型<input list="ontology-query-predicates" aria-label="关系类型" placeholder="全部关系；可选择或输入 ID" value={predicateFilter()} onInput={e=>{clearSearch();setPredicateFilter(e.currentTarget.value);}}/><datalist id="ontology-query-predicates"><For each={predicateOptions()}>{row=><option value={row.id}>{row.label||PREDICATE_LABELS[row.id]||row.id}</option>}</For></datalist></label>
          <label>证据类型<select aria-label="证据类型" value={evidenceFilter()} onChange={e=>{clearSearch();setEvidenceFilter(e.currentTarget.value);}}><option value="">全部证据类型</option><option value="experimental">实验观察</option><option value="computational">计算结果</option><option value="literature_report">文献转述</option><option value="hypothesis">未验证假设</option><option value="unspecified">未标明</option></select></label>
          <label>主张方向<select aria-label="主张方向" value={polarityFilter()} onChange={e=>{clearSearch();setPolarityFilter(e.currentTarget.value);}}><option value="">支持与反对</option><option value="supports">支持</option><option value="opposes">反对</option></select></label>
        </div>
        <p>条件按名称与值精确匹配，多个条件同时满足才返回。数字与文字不同；未记录的条件不会被推定为满足。</p>
        <For each={conditions()}>{(condition,index)=><div class="ontology-condition-row"><label>条件名称<input aria-label={`条件 ${index()+1} 名称`} placeholder="例如 pH" value={condition.key} onInput={e=>{clearSearch();setConditions(old=>old.map((item,i)=>i===index()?{...item,key:e.currentTarget.value}:item));}}/></label><label>条件值<input aria-label={`条件 ${index()+1} 值`} placeholder="例如 7 或 37 °C" value={condition.value} onInput={e=>{clearSearch();setConditions(old=>old.map((item,i)=>i===index()?{...item,value:e.currentTarget.value}:item));}}/></label><button class={button} type="button" onClick={()=>{clearSearch();setConditions(old=>old.filter((_,i)=>i!==index()));}}>移除条件 {index()+1}</button></div>}</For>
        <button class={button} disabled={conditions().length>=20} onClick={()=>setConditions(old=>[...old,{key:'',value:''}])}>添加条件</button>
        <button class={button} onClick={()=>{clearSearch(true);setQueryScope('both');setPredicateFilter('');setEvidenceFilter('');setPolarityFilter('');setConditions([]);}}>清除筛选</button>
      </details>
      <Show when={result()}>{data=><>
        <Show when={data().requires_selection}><p role="status">找到多个候选实体，请先选择研究对象，避免混用同名或相似实体。</p><Show when={data().candidates_truncated}><p>候选超过 100 个，请缩小名称范围。</p></Show><div class="ontology-candidate-list"><For each={data().candidates || []}>{candidate=><button class={button} disabled={busy()} onClick={()=>{setEntityId(candidate.id);void act((w,v)=>search(w,v));}}><strong>{candidate.label}</strong><span>{candidate.type} · {candidate.scope==='private'?'私有实体':'公共实体'}</span><small>{candidate.id}</small></button>}</For></div></Show>
        <Show when={!data().requires_selection && !data().public?.length && !data().private?.length}><p role="status">{data().candidates?.length?'已找到实体，但没有符合当前筛选条件的已发布关系；可减少条件后重试。':'未找到匹配实体，请尝试别名、更短的名称或调整知识范围。'}</p></Show>
        <Show when={!data().requires_selection && data().candidates?.length===1}><p role="status">当前对象：{data().candidates[0].label} · {data().candidates[0].type}</p></Show>
        <For each={['public','private'] as const}>{scope=><div><h3>{scope==='public'?'公共事实':'本 Workspace 私有事实'}</h3><For each={data()[scope]}>{row=><article class="border-b border-[#35434d] p-2">
          <label><input type="checkbox" checked={references().includes(row.id)} onChange={e=>setReferences(old=>e.currentTarget.checked?[...old,row.id]:old.filter(x=>x!==row.id))}/> {row.subject_label||row.subject_id} → {PREDICATE_LABELS[row.predicate]||row.predicate} → {row.object_label||row.object_id}（{row.polarity==='supports'?'支持':row.polarity==='opposes'?'反对':row.polarity}）</label>
          <Show when={row.subject_aliases?.length}><p class="text-sm">主体别名：{row.subject_aliases.join('；')}</p></Show>
          <Show when={row.subject_mutations?.length}><p class="text-sm">主体突变：{row.subject_mutations.join(' · ')}</p></Show>
          <p class="ontology-query-meta">证据类型：{({experimental:'实验观察',computational:'计算结果',literature_report:'文献转述',hypothesis:'未验证假设',unspecified:'未标明'} as Record<string,string>)[row.resolved_evidence_basis||row.qualifiers?.evidence_basis||row.evidence_basis||'unspecified']||'未标明'}</p>
          <Show when={Object.keys(row.qualifiers||{}).length}><details><summary>查看观察数值与条件</summary><dl class="ontology-query-qualifiers"><For each={Object.entries(row.qualifiers||{})}>{([key,value])=><><dt>{SCIENTIFIC_FIELDS[key] || key}</dt><dd>{typeof value==='string'?value:JSON.stringify(value)}<Show when={value!==null && ['string','number','boolean'].includes(typeof value)}><button type="button" class="ml-2 text-sm underline" aria-label={`筛选${SCIENTIFIC_FIELDS[key] || key}`} onClick={()=>{setConditions(rows=>[...rows.filter(row=>row.key!==key),{key,value:typeof value==='string'?JSON.stringify(value):String(value)}]);void act(search);}}>按此筛选</button></Show></dd></>}</For></dl></details></Show>
          <p class="whitespace-pre-wrap text-sm">{row.quote || row.evidence?.map((e:any)=>e.quote).join('\n')}</p>
          <Show when={scope==='private'}><button class={button} disabled={busy()} onClick={()=>void act(async(w,v)=>{await ontologyRequest(w,`/assertions/${encodeURIComponent(row.id)}/withdraw`,{expected_revision:row.revision});await search(w,v);})}>撤回此私有主张</button><button class={`${button} ml-2`} disabled={busy()} onClick={()=>void act(async(w,v)=>{await ontologyRequest(w,`/sources/${encodeURIComponent(row.source_id)}/withdraw`,{expected_revision:data().private_revision});await search(w,v);})}>撤回该来源全部已发布主张</button></Show>
        </article>}</For></div>}</For>
        <Show when={data().workspace_conflicts.length}><div class="ontology-conflict-entry"><p>发现 {data().workspace_conflicts.length} 组相反主张，原始证据仍保留。</p><For each={data().workspace_conflicts}>{pair => <button type="button" onClick={()=>setConflictPair({left:pair.left,right:pair.right})}>{conflictLabels[pair.condition_relation] || '需要核对'} · 查看与处理</button>}</For></div></Show>
        <Show when={data().conflicts_truncated}><p class="text-amber-300">差异检查达到单页预算；此页不代表已列出所有冲突。</p></Show>
        <Show when={data().next_cursor}><button class={button} onClick={()=>void act((w,v)=>search(w,v,data().next_cursor))}>下一页关系</button></Show>
      </>}</Show>
      <Show when={conflictPair()}>{pair=><OntologyConflicts workspaceId={workspace()} leftId={pair().left} rightId={pair().right} onClose={()=>setConflictPair(undefined)} onChanged={()=>void act((w,v)=>search(w,v))}/>}</Show>
      <OntologyEvidenceReport workspaceId={workspace()} facts={[...(result()?.public||[]),...(result()?.private||[])].filter(row=>references().includes(row.id))}/>
    </div></Show>
  </section>;
}
