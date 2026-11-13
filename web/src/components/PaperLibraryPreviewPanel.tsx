import { For, Show, createEffect, createSignal, on } from 'solid-js';
import { getPaperLibraryDocument, paperLibraryPdfUrl, type PaperLibraryDocument } from '../api/paperLibrary';

export default function PaperLibraryPreviewPanel(props: {workspaceId: string; docId: string; title: string}) {
  const [detail, setDetail] = createSignal<PaperLibraryDocument>();
  const [error, setError] = createSignal('');
  const [loading, setLoading] = createSignal(true);

  createEffect(on(() => `${props.workspaceId}:${props.docId}`, async () => {
    setLoading(true); setError(''); setDetail();
    try { setDetail((await getPaperLibraryDocument(props.workspaceId, props.docId)).document); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '文献预览加载失败'); }
    finally { setLoading(false); }
  }));

  const useInChat = () => window.dispatchEvent(new CustomEvent('tcode-use-library-item-in-chat', {detail: {
    text: `[[paper-library:${props.docId}]]\n请通过本地文献库 RAG 检索《${props.title}》中与我的问题相关的段落和证据后继续：`,
  }}));

  return <section class="paper-library-preview">
    <header><div><span>本地文献</span><h1 title={props.title}>{props.title}</h1><small>{props.docId}</small></div><button onClick={useInChat}>加入当前聊天</button></header>
    <Show when={loading()}><div class="resource-library-loading">正在读取文献记录…</div></Show>
    <Show when={error()}><div class="resource-library-error" role="alert">{error()}</div></Show>
    <Show when={detail()} keyed>{document=><div class="paper-library-preview-body">
      <Show when={document.pdf_available} fallback={<div class="resource-library-note">该记录没有可预览的本地 PDF，仍可查看结构化内容。</div>}><iframe class="paper-library-preview-pdf" title={`预览 ${document.title}`} src={paperLibraryPdfUrl(props.workspaceId, document.doc_id)}/></Show>
      <div class="resource-library-meta"><span>{document.reference_only?'引用记录':'原文已入库'}</span><span>{document.passages.length} 个段落摘要</span><span>{document.facts.length} 条结构化事实</span></div>
      <Show when={Object.keys(document.metadata?.identifiers || {}).length}><section><h2>文献标识</h2><div class="resource-library-tags"><For each={Object.entries(document.metadata.identifiers)}>{([key,value])=><span>{key.toUpperCase()} · {String(value)}</span>}</For></div></section></Show>
      <Show when={String(document.summary?.summary_text || '')}><section><h2>摘要</h2><p>{String(document.summary.summary_text)}</p></section></Show>
      <Show when={(document.summary?.keywords || []).length}><section><h2>关键词</h2><div class="resource-library-tags"><For each={document.summary.keywords}>{(word:string)=><span>{word}</span>}</For></div></section></Show>
      <Show when={document.passages.length}><section><h2>段落索引</h2><div class="resource-library-passages"><For each={document.passages.slice(0,20)}>{passage=><article><b>{String(passage.section_hint||passage.heading||'正文')}</b><small>{(passage.page_span||[]).length?`第 ${(passage.page_span||[]).join('–')} 页`:''}</small><p>{String(passage.summary_text||passage.text||'')}</p></article>}</For></div></section></Show>
      <Show when={document.facts.length}><section><h2>结构化事实</h2><div class="resource-library-passages"><For each={document.facts.slice(0,40)}>{fact=><article><b>{String(fact.label||fact.fact_type||'事实')}</b><small>{fact.page?`第 ${String(fact.page)} 页`:''}</small><p>{String(fact.value||fact.normalized_value||fact.statement||fact.evidence_text||'')}</p></article>}</For></div></section></Show>
    </div>}</Show>
  </section>;
}
