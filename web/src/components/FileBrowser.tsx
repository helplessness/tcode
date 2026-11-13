import { For, Show, createSignal, createEffect, createMemo, on, onCleanup, onMount } from 'solid-js';
import type { FilePreviewRequest } from '../stores/session';
import { composerIdentity } from '../stores/composer';
import { ZoomableImage } from './ImageLightbox';
import AssetHistory from './AssetHistory';
import { desktop } from '../desktop';
import { planFolderImport } from '../utils/folderImport';
import { installUploadFeedback } from '../utils/uploadFeedback';
import { isHtmlFile, isImageFile, isOfficeFile, isPdfFile } from '../utils/filePreview';
import OfficeFilePreview from './OfficeFilePreview';
import {
  getInlineFileUrl,
  getJobArtifactContent,
  getJobArtifactDownloadUrl,
  listJobArtifacts,
  uploadWorkspaceFile,
} from '../api/client';

interface FileItem {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

interface FileBrowserProps {
  mode?: 'files' | 'changes';
  initialHistory?: boolean;
  workdir?: string;
  workspaceId?: string;
  jobId?: string;
  artifactRoot?: string;
  initialPath?: string;
  refreshTrigger?: number;
  previewRequest?: FilePreviewRequest | null;
  onClose?: () => void;
  onPreview?: (path: string) => void;
  onOpenDetail?: (path: string) => void;
  onQuote?: (text: string) => void;
}

export default function FileBrowser(props: FileBrowserProps) {
  const [showHistory, setShowHistory] = createSignal(props.mode === 'changes' || Boolean(props.initialHistory));
  const [currentPath, setCurrentPath] = createSignal('');
  const [files, setFiles] = createSignal<FileItem[]>([]);
  const [loading, setLoading] = createSignal(false);
  const [listError, setListError] = createSignal('');
  const [selectedFile, setSelectedFile] = createSignal<string | null>(null);
  const [fileContent, setFileContent] = createSignal<string>('');
  const [previewError, setPreviewError] = createSignal<string>('');
  const [previewLoading, setPreviewLoading] = createSignal(false);
  const [leftPaneWidth, setLeftPaneWidth] = createSignal(42);
  const [isPaneResizing, setIsPaneResizing] = createSignal(false);
  let browserContentRef: HTMLDivElement | undefined;
  let latestFileRequest = 0;
  let latestPreviewRequest = 0;
  let uploadInput!: HTMLInputElement;
  let folderInput!: HTMLInputElement;
  const [folderPlan, setFolderPlan] = createSignal<ReturnType<typeof planFolderImport<File>>>();
  let uploadController: AbortController | undefined;
  let uploadVersion = 0;
  let dragDepth = 0;
  const [dragOver, setDragOver] = createSignal(false);
  const [uploading, setUploading] = createSignal(false);
  const [uploadNotice, setUploadNotice] = createSignal('');
  const [uploadErrors, setUploadErrors] = createSignal<string[]>([]);
  const canUpload = () => !props.jobId && Boolean(props.workspaceId?.trim());

  const isArtifactMode = () => Boolean(props.jobId && props.artifactRoot);
  const displayPath = (path: string | null | undefined) => {
    const value = String(path || '').trim();
    if (!value || value.startsWith('/') || isArtifactMode()) return value;
    return `/workspace/${value}`;
  };

  const appendWorkspaceParams = (params: URLSearchParams) => {
    const workspaceId = String(props.workspaceId || '').trim();
    const workdir = String(props.workdir || '').trim();
    if (workspaceId) {
      params.set('workspace_id', workspaceId);
    }
    if (workdir) {
      params.set('workdir', workdir);
    }
  };

  /** 返回当前图片或 PDF 的同源内联地址。 */
  const currentPreviewUrl = () => {
    const path = selectedFile();
    if (!path || (!isImageFile(path) && !isPdfFile(path) && !isHtmlFile(path) && !isOfficeFile(path))) return '';
    if (isArtifactMode() && props.jobId && props.artifactRoot) {
      return getJobArtifactDownloadUrl(props.jobId, {
        path,
        root: props.artifactRoot,
        inline: true,
      });
    }
    if (isHtmlFile(path)) {
      const params = new URLSearchParams({ path });
      appendWorkspaceParams(params);
      return `/api/files/html-preview?${params.toString()}`;
    }
    return getInlineFileUrl(path, props.workdir, props.workspaceId);
  };

  const loadFiles = async (path: string = '') => {
    const requestId = ++latestFileRequest;
    setLoading(true);
    setListError('');
    try {
      if (isArtifactMode() && props.jobId && props.artifactRoot) {
        const data = await listJobArtifacts(props.jobId, {
          path,
          root: props.artifactRoot,
        });
        if (requestId !== latestFileRequest) return;
        setFiles(data.items || []);
      } else {
        const params = new URLSearchParams({ path });
        appendWorkspaceParams(params);
        const resp = await fetch(`/api/files?${params}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(String(data.detail || `读取目录失败 (${resp.status})`));
        if (requestId !== latestFileRequest) return;
        setFiles(data.items || []);
      }
      setCurrentPath(path);
    } catch (e) {
      if (requestId !== latestFileRequest) return;
      setListError(e instanceof Error ? e.message : '读取目录失败，请刷新重试');
      console.error('Failed to load files:', e);
    } finally {
      if (requestId === latestFileRequest) {
        setLoading(false);
      }
    }
  };

  const downloadFile = (path: string) => {
    if (!path) return;
    if (desktop) return;
    const url = isArtifactMode() && props.jobId && props.artifactRoot
      ? getJobArtifactDownloadUrl(props.jobId, { path, root: props.artifactRoot })
      : (() => {
          const params = new URLSearchParams({ path });
          appendWorkspaceParams(params);
          return `/api/files/download?${params.toString()}`;
        })();
    const a = document.createElement('a');
    a.href = url;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  const handleRefresh = () => {
    loadFiles(currentPath());
  };

  const loadFileContent = async (path: string) => {
    const requestId = ++latestPreviewRequest;
    setPreviewLoading(true);
    setPreviewError('');
    setSelectedFile(path);
    setFileContent('');
    try {
      if (isImageFile(path) || isPdfFile(path) || isHtmlFile(path) || isOfficeFile(path)) {
        return;
      }
      if (isArtifactMode() && props.jobId && props.artifactRoot) {
        const data = await getJobArtifactContent(props.jobId, {
          path,
          root: props.artifactRoot,
        });
        if (requestId !== latestPreviewRequest) return;
        setFileContent(data.content || '');
      } else {
        const params = new URLSearchParams({ path });
        appendWorkspaceParams(params);
        const resp = await fetch(`/api/files/content?${params}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(String(data.detail || `文件预览失败 (${resp.status})`));
        }
        if (requestId !== latestPreviewRequest) return;
        setFileContent(data.content || '');
      }
    } catch (e) {
      if (requestId !== latestPreviewRequest) return;
      console.warn('File preview is unavailable:', e);
      const message = e instanceof Error ? e.message : '文件内容加载失败';
      setPreviewError(message || '文件内容加载失败');
      setFileContent('');
    } finally {
      if (requestId === latestPreviewRequest) {
        setPreviewLoading(false);
      }
    }
  };

  const openFileAtPath = async (path: string) => {
    if (!path) return;
    const scope = browserScopeKey();
    const dir = path.split('/').slice(0, -1).join('/');
    if (dir !== currentPath()) {
      await loadFiles(dir);
    }
    if (scope !== browserScopeKey()) return;
    await loadFileContent(path);
  };

  const uploadFiles = async (incoming: File[], preservePaths = false) => {
    if (uploading() || !incoming.length) return;
    if (!canUpload()) {
      setUploadErrors([props.jobId ? '任务产物仅供查看，请切回工作区资产库上传。' : '请先选择工作区。']);
      return;
    }
    const workspace = props.workspaceId!.trim();
    const scope = browserScopeKey();
    const identity = composerIdentity();
    const version = ++uploadVersion;
    const controller = new AbortController();
    uploadController = controller;
    const isCurrent = () => version === uploadVersion && scope === browserScopeKey() && identity === composerIdentity();
    const saved: string[] = [];
    const failed: string[] = [];
    setUploading(true);
    setUploadErrors([]);
    try {
      for (const [index, file] of incoming.entries()) {
        if (!isCurrent()) break;
        setUploadNotice(`正在上传 ${index + 1}/${incoming.length}：${file.name}`);
        try {
          saved.push(await uploadWorkspaceFile(file, workspace, controller.signal, preservePaths ? file.webkitRelativePath : undefined));
        } catch (error) {
          if (!isCurrent() || controller.signal.aborted) return;
          failed.push(`${file.name}：${error instanceof Error ? error.message : '网络错误，请重试'}`);
        }
      }
      if (!isCurrent()) return;
      setUploadErrors(failed);
      setUploadNotice(`已上传 ${saved.length} 个文件到 ${workspace}/upload/${failed.length ? `，${failed.length} 个失败` : ''}`);
      if (saved.length) {
        await loadFiles('upload');
        if (isCurrent()) await loadFileContent(saved[saved.length - 1]);
      }
    } finally {
      if (isCurrent()) { setUploading(false); uploadController = undefined; }
    }
  };

  const isFileDrag = (event: DragEvent) => Array.from(event.dataTransfer?.types || []).includes('Files');
  const handleFileDrop = (event: DragEvent) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepth = 0;
    setDragOver(false);
    if (uploading()) return;
    const items = Array.from(event.dataTransfer?.items || []);
    if (items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) {
      setUploadErrors(['暂不支持拖入文件夹，请选择其中的文件上传。']);
      return;
    }
    void uploadFiles(Array.from(event.dataTransfer?.files || []));
  };

  const handleNavigate = (item: FileItem) => {
    if (item.type === 'directory') {
      loadFiles(item.path);
    } else {
      if (props.onPreview) {
        setSelectedFile(item.path);
        props.onPreview(item.path);
        return;
      }
      void openFileAtPath(item.path).catch(error => setPreviewError(error instanceof Error ? error.message : '无法打开文件'));
    }
  };

  const handleBack = () => {
    const path = currentPath();
    const parentPath = path.split('/').slice(0, -1).join('/');
    loadFiles(parentPath);
  };

  const formatSize = (size?: number) => {
    if (size === undefined) return '';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  };

  const getFileIcon = (type: 'file' | 'directory', name: string) => {
    if (type === 'directory') {
      return (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="w-4 h-4 text-[#58a6ff]">
          <path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/>
        </svg>
      );
    }
    
    const ext = name.split('.').pop()?.toLowerCase();
    const colors: Record<string, string> = {
      'ts': '#3178c6', 'tsx': '#3178c6', 'js': '#f7df1e', 'jsx': '#f7df1e',
      'py': '#3572A5', 'rb': '#701516', 'java': '#b07219',
      'c': '#555555', 'cpp': '#f34b7d', 'h': '#555555', 'hpp': '#f34b7d',
      'go': '#00ADD8', 'rs': '#dea584', 'swift': '#ffac45',
      'md': '#083fa1', 'txt': '#8b949e', 'json': '#cbcb41',
      'yaml': '#cb171e', 'yml': '#cb171e', 'xml': '#e34c26',
      'html': '#e34c26', 'css': '#563d7c', 'scss': '#c6538c',
      'sh': '#89e051', 'bash': '#89e051', 'sql': '#e38c00',
      'gitignore': '#f44d27', 'lock': '#cbcb41',
    };
    const color = colors[ext || ''] || '#8b949e';
    
    return (
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="w-4 h-4" style={{ color }}>
        <path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/>
      </svg>
    );
  };

  // 会话运行态和消息更新会频繁替换对象；使用稳定键避免打开的目录被重置。
  const browserScopeKey = createMemo(() => JSON.stringify([
    String(props.workdir || '').trim(),
    String(props.workspaceId || '').trim(),
    String(props.jobId || '').trim(),
    String(props.artifactRoot || '').trim(),
    String(props.initialPath || ''),
  ]));

  createEffect(on(browserScopeKey, () => {
    uploadVersion += 1;
    uploadController?.abort();
    uploadController = undefined;
    setUploading(false);
    setUploadNotice('');
    setUploadErrors([]);
    dragDepth = 0;
    setFolderPlan(undefined);
    setDragOver(false);
    latestPreviewRequest += 1;
    const initialPath = String(props.initialPath || '');
    setSelectedFile(null);
    setFileContent('');
    setPreviewError('');
    setPreviewLoading(false);
    void loadFiles(initialPath);
  }));

  const stopPaneResize = () => {
    if (!isPaneResizing()) return;
    setIsPaneResizing(false);
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  };

  const resizePane = (event: PointerEvent) => {
    if (!isPaneResizing() || !browserContentRef) return;
    const rect = browserContentRef.getBoundingClientRect();
    if (rect.width <= 0) return;
    const minimumPercent = Math.min(45, (180 / rect.width) * 100);
    const maximumPercent = Math.max(minimumPercent, Math.min(75, ((rect.width - 180) / rect.width) * 100));
    const nextPercent = ((event.clientX - rect.left) / rect.width) * 100;
    setLeftPaneWidth(Math.max(minimumPercent, Math.min(maximumPercent, nextPercent)));
  };

  const startPaneResize = (event: PointerEvent) => {
    event.preventDefault();
    setIsPaneResizing(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  onMount(() => {
    window.addEventListener('pointermove', resizePane);
    window.addEventListener('pointerup', stopPaneResize);
    onCleanup(() => {
      window.removeEventListener('pointermove', resizePane);
      window.removeEventListener('pointerup', stopPaneResize);
      stopPaneResize();
    });
  });

  let uploadTarget!: HTMLDivElement;
  onMount(() => onCleanup(installUploadFeedback(uploadTarget, event => canUpload() && !uploading() && !Array.from(event?.dataTransfer?.items || []).some(item => item.webkitGetAsEntry?.()?.isDirectory))));

  onCleanup(() => {
    uploadVersion += 1;
    uploadController?.abort();
    latestFileRequest += 1;
    latestPreviewRequest += 1;
  });

  // 父组件触发刷新时只更新当前目录，不改变已经选中的文件。
  createEffect(() => {
    const trigger = props.refreshTrigger;
    if (trigger) {
      loadFiles(currentPath());
    }
  });

  // 其他界面请求预览文件时，先切换目录再加载目标文件。
  createEffect(() => {
    const req = props.previewRequest;
    if (req?.path) {
      setShowHistory(false);
      openFileAtPath(req.path);
    }
  });

  const iconButtonClass = 'inline-flex h-8 w-8 items-center justify-center rounded-full border border-[#3e484f] bg-[#171c20] text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]';

  return (
    <div ref={uploadTarget} class="file-browser-panel upload-drop-panel relative flex h-full min-h-0 flex-col bg-[#11171b]" data-testid="asset-browser" data-mode={props.mode || 'files'}
      onDragEnter={event => { if (isFileDrag(event)) { event.preventDefault(); dragDepth += 1; setDragOver(true); } }}
      onDragOver={event => { if (isFileDrag(event)) { event.preventDefault(); event.stopPropagation(); if (event.dataTransfer) event.dataTransfer.dropEffect = canUpload() && !uploading() ? 'copy' : 'none'; } }}
      onDragLeave={event => { if (isFileDrag(event)) { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) setDragOver(false); } }}
      onDrop={handleFileDrop}
    >
      <Show when={dragOver()}>
        <div class="absolute inset-0 z-50 flex items-center justify-center border-2 border-dashed p-6 text-center text-sm" style={{ background: 'var(--tcode-surface, #fffdf7)', color: 'var(--tcode-primary, #586641)', 'border-color': 'currentColor', opacity: '.95' }}>
          {uploading() ? '正在上传，请等待当前批次完成' : canUpload() ? `松开即可上传到 ${props.workspaceId}/upload/` : props.jobId ? '任务产物只读，请切回工作区资产库上传' : '请先选择工作区'}
        </div>
      </Show>
      <div class="file-browser-toolbar flex flex-wrap items-center justify-between gap-2 border-b border-[#2a3136] bg-[#10161a] px-4 py-4">
        <div class="flex min-w-0 items-center gap-2">
          <Show when={props.onClose}>
            <button
              class={iconButtonClass}
              onClick={() => props.onClose?.()}
              title="关闭资产库"
            >
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class="w-4 h-4">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18 18 6M6 6l12 12" />
              </svg>
            </button>
          </Show>
          <Show when={currentPath()}>
            <button
              class={iconButtonClass}
              onClick={handleBack}
              title="返回上级"
            >
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class="w-4 h-4">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
              </svg>
            </button>
          </Show>
          <span class="file-browser-location text-sm font-medium text-[#e6edf3] truncate max-w-[200px]" title={currentPath() || '/'}>
            {desktop && props.mode === 'changes' ? '任务改动' : currentPath() || '/'}
          </span>
        </div>
        <div class="file-browser-actions flex flex-wrap items-center gap-2">
          <Show when={!showHistory() && !props.jobId}>
            <input ref={uploadInput} type="file" multiple hidden aria-label="选择资产文件" disabled={!canUpload() || uploading()}
              onChange={event => { const selected = Array.from(event.currentTarget.files || []); event.currentTarget.value = ''; void uploadFiles(selected); }} />
            <button class="btn btn-sm" disabled={!canUpload() || uploading()} onClick={() => uploadInput.click()}>{uploading() ? '导入中…' : desktop ? '导入本地文件' : '上传文件'}</button>
            <Show when={desktop && props.workspaceId}><button class="btn btn-sm" onClick={()=>void desktop!.workspace(props.workspaceId!, 'finder').catch(error=>setUploadErrors([String(error)]))}>Finder</button><button class="btn btn-sm" onClick={()=>void desktop!.workspace(props.workspaceId!, 'terminal').catch(error=>setUploadErrors([String(error)]))}>终端</button></Show>
            <Show when={desktop}><input ref={element => { folderInput = element; element.setAttribute('webkitdirectory', ''); }} type="file" multiple hidden aria-label="选择本地文件夹"
              onChange={event => {
                const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = '';
                setFolderPlan(undefined); setUploadErrors([]);
                if (!files.length) return;
                try { setFolderPlan(planFolderImport(files)); } catch(error) { setUploadErrors([String(error)]); }
              }} /><button class="btn btn-sm" disabled={!canUpload() || uploading()} onClick={()=>folderInput.click()}>导入文件夹</button></Show>
          </Show>
          <Show when={selectedFile() && /\.(pdb|cif|mmcif|sdf|mol2)$/i.test(selectedFile()!)}>
            <button class="btn btn-sm" onClick={()=>window.dispatchEvent(new CustomEvent('tcode-open-structure',{detail:{path:selectedFile(),workspaceId:props.workspaceId,workdir:props.workdir,jobId:props.jobId,artifactRoot:props.artifactRoot}}))}>三维查看</button>
          </Show>
          <Show when={desktop && props.workspaceId && selectedFile() && !isArtifactMode()}>
            <button class="btn btn-sm" onClick={() => void desktop!.revealFile(props.workspaceId!, selectedFile()!).catch(error => setUploadErrors([String(error)]))}>在文件夹中显示</button>
          </Show>
          <Show when={!desktop && selectedFile()}>
            {(filePath) => (
              <button
                class={`${iconButtonClass} tcode-download-button is-icon`}
                onClick={() => downloadFile(filePath())}
                title="下载文件"
              >
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class="w-4 h-4">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                </svg>
              </button>
            )}
          </Show>
          <button
            class={iconButtonClass}
            onClick={handleRefresh}
            title="刷新"
          >
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class={`w-4 h-4 ${loading() ? 'animate-spin' : ''}`}>
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>

      <div class="shrink-0 border-b border-[#2a3136] px-4 py-2 text-xs text-[#8b949e]">
        <Show when={folderPlan()}>{plan=><div class="mb-2 rounded border p-3" role="region" aria-label="确认文件夹导入"><p>将 {plan().files.length} 个文件（{formatSize(plan().bytes)}）复制到当前本地工作区，保留目录结构。跳过 {plan().skipped} 个隐藏或缓存文件；不会修改源目录，也不会在此步骤上传至远程 MCP。</p><div class="mt-2 flex gap-2"><button class="btn btn-sm" disabled={uploading()} onClick={()=>{const files=plan().files;setFolderPlan(undefined);void uploadFiles(files,true);}}>确认导入</button><button class="btn btn-sm" onClick={()=>setFolderPlan(undefined)}>取消</button></div></div>}</Show>
        <span class="file-browser-upload-hint">{desktop && props.mode === 'changes' ? '选择任务记录，查看公开文件与产物的版本差异。' : props.jobId ? '任务产物只读；上传请使用工作区资产库。' : canUpload() ? `可拖入多个本地文件，保存到 ${props.workspaceId}/upload/（不随浏览目录改变）；单文件最多 100 MB，同名自动改名。` : '选择工作区后可拖入或选择本地文件上传。'}</span>
        <Show when={uploading()}><p class="mt-1">切换工作区会停止剩余上传；已完成的文件保留在原工作区。</p></Show>
        <Show when={uploadNotice()}><p role="status" class="mt-1 break-all">{uploadNotice()}</p></Show>
        <Show when={uploadErrors().length}><ul role="alert" class="mt-1 max-h-24 overflow-auto" style={{ color: 'var(--tcode-danger, #a13e36)' }}><For each={uploadErrors()}>{error => <li class="break-all">{error}</li>}</For></ul></Show>
        <Show when={listError()}><p role="alert" class="mt-1">目录刷新失败：{listError()}</p></Show>
      </div>

      {/* 文件树与预览区域 */}
      <Show when={showHistory() && !props.jobId && props.workspaceId}><AssetHistory workspaceId={props.workspaceId!} onPreview={props.onPreview} onQuote={props.onQuote} /></Show>
      <Show when={!showHistory() || props.jobId || !props.workspaceId}>
      <div ref={browserContentRef} class="flex min-h-0 flex-1">
        <div class="tcode-scrollbar min-w-[180px] overflow-y-auto border-r border-[#2a3136] bg-[#13191d]" style={{ width: `${leftPaneWidth()}%` }}>
          <Show when={loading()}>
            <div class="tcode-loading-state" role="status" aria-live="polite">正在读取文件…</div>
          </Show>

          <Show
            when={!loading() && files().length > 0}
            fallback={
              <Show when={!loading()}>
                <div class="text-center text-[#8b949e] py-4 text-sm">
                  当前目录暂无文件
                </div>
              </Show>
            }
          >
            <div class="file-browser-list divide-y divide-[#2a3136]">
              <For each={files()}>
                {(item) => (
                  <div
                    class={`flex cursor-pointer items-center gap-2 px-3 py-2 transition-colors ${item.type === 'file' && selectedFile() === item.path ? 'bg-[#38bdf8]/12' : 'hover:bg-[#171f24]'}`}
                    role="button"
                    tabIndex={0}
                    aria-label={item.type === 'file' ? `预览文件 ${item.name}` : `打开目录 ${item.name}`}
                    title={displayPath(item.path)}
                    onClick={() => handleNavigate(item)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        handleNavigate(item);
                      }
                    }}
                  >
                    {getFileIcon(item.type, item.name)}
                    <span class="text-sm text-[#e6edf3] flex-1 truncate">{item.name}</span>
                    <Show when={!desktop && item.type === 'file'}>
                      <button
                        class="tcode-download-button is-icon"
                        onClick={(e) => {
                          e.stopPropagation();
                          downloadFile(item.path);
                        }}
                        title="下载"
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class="w-3.5 h-3.5 text-[#8b949e] hover:text-[#58a6ff]">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                      </button>
                    </Show>
                    <Show when={item.type === 'file' && item.size}>
                      <span class="text-xs text-[#8b949e]">{formatSize(item.size)}</span>
                    </Show>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </div>

        <div
          class={`w-2 shrink-0 cursor-col-resize touch-none transition-colors ${isPaneResizing() ? 'bg-[#7bd0ff]' : 'bg-[#20282e] hover:bg-[#38bdf8]/70'}`}
          onPointerDown={startPaneResize}
          title="拖拽调整文件树与预览宽度"
          aria-label="拖拽调整文件树与预览宽度"
        />

        <div class="tcode-scrollbar min-w-0 flex-1 overflow-y-auto bg-[#171c20] p-4">
          <Show when={previewLoading()}>
            <div class="text-sm text-[#8b949e] py-2">正在加载文件预览...</div>
          </Show>

          <Show when={!previewLoading() && selectedFile()} fallback={<div class="text-sm text-[#8b949e] py-2">点击左侧文件进行预览</div>}>
            <div class="mb-2 flex items-center gap-2">
              <div class="min-w-0 flex-1 truncate text-xs text-[#8b949e]" title={displayPath(selectedFile())}>{displayPath(selectedFile())}</div>
              <Show when={props.onOpenDetail}>
                <button class="btn btn-sm shrink-0" onClick={() => props.onOpenDetail?.(selectedFile()!)}>详细查看</button>
              </Show>
            </div>
            <Show
              when={!previewError()}
              fallback={
                <div role="alert" class="rounded-2xl border border-[#f85149]/35 bg-[#f85149]/10 p-4 text-sm text-[#ffb4ae]">
                  文件预览失败：{previewError()}。{desktop ? '请在本地工作区中查看原文件。' : '文件仍可通过上方下载按钮获取。'}
                </div>
              }
            >
              <Show
                when={isImageFile(selectedFile())}
                fallback={
                  <Show
                    when={isPdfFile(selectedFile()) || isHtmlFile(selectedFile())}
                    fallback={isOfficeFile(selectedFile()) ? (
                      <OfficeFilePreview
                        src={currentPreviewUrl()}
                        fileName={selectedFile() || ''}
                        onError={setPreviewError}
                      />
                    ) : (
                      <pre class="overflow-x-auto rounded-2xl border border-[#2a3136] bg-[#0d1216] p-4 font-mono text-xs text-[#e6edf3] whitespace-pre-wrap">
                        {fileContent()}
                      </pre>
                    )}
                  >
                    <div class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0d1216]">
                      <iframe
                        src={currentPreviewUrl()}
                        title={`${isHtmlFile(selectedFile()) ? 'HTML' : 'PDF'} 预览 ${selectedFile() || ''}`}
                        sandbox={isHtmlFile(selectedFile()) ? 'allow-scripts' : undefined}
                        class="h-[72vh] min-h-[520px] w-full border-0 bg-white"
                      />
                      <div class="border-t border-[#2a3136] px-4 py-2 text-xs text-[#8b949e]">
                        {isHtmlFile(selectedFile()) ? 'HTML 在隔离预览中运行；脚本无法访问 TCode 登录态。' : desktop ? '若 PDF 未显示，可在本地工作区中打开原文件。' : '如果浏览器未显示 PDF，请使用上方下载按钮打开原文件。'}
                      </div>
                    </div>
                  </Show>
                }
              >
                <div class="rounded-2xl border border-[#2a3136] bg-[#0d1216] p-4">
                  <ZoomableImage
                    src={currentPreviewUrl()}
                    alt={selectedFile() || '图片预览'}
                    downloadName={selectedFile() || undefined}
                    class="max-w-full h-auto rounded mx-auto"
                  />
                </div>
              </Show>
            </Show>
          </Show>
        </div>
      </div>
      </Show>
    </div>
  );
}
