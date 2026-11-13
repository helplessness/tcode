import { promptAction, confirmAction } from './FeedbackDialog';
import { For, Show, createEffect, createSignal, on, onCleanup, onMount } from 'solid-js';
import { listPaperLibrary, type PaperLibraryItem } from '../api/paperLibrary';
import { deleteWorkspaceFile, renameWorkspaceFile, searchWorkspaceFiles, type WorkspaceFileSearchItem } from '../api/files';
import { desktop } from '../desktop';

type Category = 'paper' | 'file';

const formatBytes = (bytes: number) => {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
};

const formatDate = (value: string | number) => {
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN');
};

export default function ResourceLibraryWorkspace(props: {workspaceId?: string; active?: boolean}) {
  const [category, setCategory] = createSignal<Category>();
  const [query, setQuery] = createSignal('');
  const [papers, setPapers] = createSignal<PaperLibraryItem[]>([]);
  const [files, setFiles] = createSignal<WorkspaceFileSearchItem[]>([]);
  const [paperEnabled, setPaperEnabled] = createSignal(true);
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [fileMenu, setFileMenu] = createSignal<{file: WorkspaceFileSearchItem; x: number; y: number}>();

  onMount(() => {
    const closeOnBlur = () => setFileMenu();
    const closeOutside = (event: PointerEvent) => {
      if ((event.target as HTMLElement | null)?.closest('.resource-library-context-menu')) return;
      setFileMenu();
    };
    window.addEventListener('blur', closeOnBlur);
    document.addEventListener('pointerdown', closeOutside);
    onCleanup(() => { window.removeEventListener('blur', closeOnBlur); document.removeEventListener('pointerdown', closeOutside); });
  });

  async function load() {
    const workspaceId = String(props.workspaceId || '').trim();
    if (!workspaceId) return;
    setLoading(true); setError('');
    try {
      const [paperResult, fileResult] = await Promise.allSettled([
        listPaperLibrary(workspaceId, category() === 'paper' ? query() : '', 0, 100),
        searchWorkspaceFiles(category() === 'file' ? query() : '', workspaceId, 100),
      ]);
      if (paperResult.status === 'fulfilled') {
        setPapers(paperResult.value.items); setPaperEnabled(paperResult.value.enabled);
      } else { setPapers([]); setPaperEnabled(false); }
      if (fileResult.status === 'fulfilled') setFiles(fileResult.value);
      else setFiles([]);
      if (paperResult.status === 'rejected' && fileResult.status === 'rejected') throw paperResult.reason;
    } catch (reason) { setError(reason instanceof Error ? reason.message : '资料库加载失败'); }
    finally { setLoading(false); }
  }

  createEffect(on(() => `${props.workspaceId || ''}:${props.active !== false}`, value => {
    if (value.endsWith(':true')) void load();
  }));

  function openPaper(paper: PaperLibraryItem) {
    window.dispatchEvent(new CustomEvent('tcode-preview-library-paper', {detail: {docId: paper.doc_id, title: paper.title}}));
  }

  function openFile(path: string) {
    window.dispatchEvent(new CustomEvent('tcode-preview-library-file', {detail: {path}}));
  }

  function useFileInChat(file: WorkspaceFileSearchItem) {
    window.dispatchEvent(new CustomEvent('tcode-use-library-item-in-chat', {detail: {
      text: `请基于资料库文件“${file.name}”继续：`, path: file.path, name: file.name,
    }}));
  }

  const enter = (next: Category) => { setCategory(next); setQuery(''); void load(); };

  async function renameFile(file: WorkspaceFileSearchItem) {
    const name = (await promptAction('输入新的文件名', file.name))?.trim();
    if (!name || name === file.name) return;
    setError('');
    try { await renameWorkspaceFile(file.path, name, String(props.workspaceId || '')); setNotice(`已重命名为 ${name}`); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '重命名失败'); }
  }

  async function deleteFile(file: WorkspaceFileSearchItem) {
    if (!await confirmAction(`永久删除“${file.name}”？此操作无法撤销。`)) return;
    setError('');
    try { await deleteWorkspaceFile(file.path, String(props.workspaceId || '')); setNotice(`已删除 ${file.name}`); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败'); }
  }

  return <section class="resource-library">
    <header class="resource-library-header">
      <div><span>LOCAL LIBRARY</span><h1>资料库</h1><p>集中查看已入库文献和工作区内的其他资料</p></div>
      <button class="btn btn-sm" onClick={() => void load()} disabled={loading()}>{loading() ? '刷新中…' : '刷新'}</button>
    </header>

    <Show when={category()} fallback={<div class="resource-library-categories">
      <button onClick={() => enter('paper')}><span class="resource-library-category-mark">文</span><div><b>文献</b><p>查看本地文献库、PDF 原文、摘要和结构化证据</p><small>{papers().length} 篇已入库文献</small></div><i>→</i></button>
      <button onClick={() => enter('file')}><span class="resource-library-category-mark">件</span><div><b>其他文件</b><p>查看上传文件、任务产物和其他工作区资料</p><small>{files().length} 个可见文件</small></div><i>→</i></button>
    </div>}>
      <div class="resource-library-breadcrumb"><button onClick={()=>setCategory()}>资料库</button><span>/</span><b>{category()==='paper'?'文献':'其他文件'}</b></div>
      <form class="resource-library-search" onSubmit={event=>{event.preventDefault();void load();}}>
        <input value={query()} onInput={event=>setQuery(event.currentTarget.value)} aria-label="搜索资料" placeholder={category()==='paper'?'搜索标题、作者、DOI 或关键词':'搜索文件名或路径'}/><button type="submit">搜索</button>
      </form>
      <Show when={!paperEnabled() && category()==='paper'}><div class="resource-library-note">Paper 插件当前未启用，无法读取本地文献库。</div></Show>
      <Show when={error()}><div class="resource-library-error" role="alert">{error()}</div></Show>

      <Show when={category()==='paper'}>
        <div class="resource-library-list resource-library-paper-list">
            <Show when={!loading() && papers().length===0}><div class="resource-library-empty">还没有匹配的入库文献。</div></Show>
            <For each={papers()}>{paper=><button class="resource-library-row" onClick={()=>openPaper(paper)}>
              <span class="resource-library-type">PDF</span><span><b>{paper.title}</b><small>{[paper.year, paper.authors.slice(0,3).join('、')].filter(Boolean).join(' · ') || '元数据待完善'}</small><em title={paper.source_path || paper.doc_id}>{paper.source_path || paper.doc_id}</em></span><time>{formatDate(paper.updated_at)}</time>
            </button>}</For>
        </div>
      </Show>

      <Show when={category()==='file'}><div class="resource-library-file-grid">
        <Show when={!loading() && files().length===0}><div class="resource-library-empty">还没有匹配的其他文件。</div></Show>
        <For each={files()}>{file=><article onContextMenu={event=>{event.preventDefault();setFileMenu({file,x:event.clientX,y:event.clientY});}}><span>{file.name.split('.').pop()?.slice(0,4).toUpperCase() || 'FILE'}</span><div><b title={file.name}>{file.name}</b><small title={file.path}>{file.path}</small><em>{file.origin==='uploaded'?'用户上传':'任务生成'} · {formatBytes(file.size)} · {formatDate(file.modified_at)}</em></div><footer><button onClick={()=>openFile(file.path)}>预览</button><button onClick={()=>useFileInChat(file)}>加入聊天</button></footer></article>}</For>
      </div></Show>
    </Show>
    <Show when={fileMenu()} keyed>{menu=><div class="resource-library-context-menu" style={{left:`${Math.min(menu.x,window.innerWidth-190)}px`,top:`${Math.min(menu.y,window.innerHeight-190)}px`}} onPointerDown={event=>event.stopPropagation()}>
      <button onClick={()=>{setFileMenu();openFile(menu.file.path);}}>预览</button>
      <button onClick={()=>{setFileMenu();useFileInChat(menu.file);}}>加入聊天</button>
      <button onClick={()=>{setFileMenu();void renameFile(menu.file);}}>重命名</button>
      <Show when={desktop}><button onClick={()=>{setFileMenu();if(!desktop?.revealFile){setError('桌面原生能力尚未更新，请完全退出并重新启动 TCode Desktop');return;}void desktop.revealFile(String(props.workspaceId||''),menu.file.path).then(()=>setNotice(`已在 Finder 中定位 ${menu.file.name}`)).catch(reason=>setError(reason instanceof Error?reason.message:String(reason)));}}>在 Finder 中显示</button></Show>
      <hr/><button class="danger" onClick={()=>{setFileMenu();void deleteFile(menu.file);}}>删除</button>
    </div>}</Show>
    <Show when={notice()}><div class="resource-library-toast" role="status">{notice()}<button aria-label="关闭提示" onClick={()=>setNotice('')}>×</button></div></Show>
  </section>;
}
