import { createSignal, Show, For, onMount, onCleanup } from 'solid-js';
import { installUploadFeedback } from '../utils/uploadFeedback';
import { sessionStore } from '../stores/session';
import { composerIdentity } from '../stores/composer';

export interface UploadedFile {
  file: File;
  path: string;
  hostPath?: string;
  displayPath?: string;
  containerPath?: string;
  relativePath?: string;
  filename: string;
  sessionId?: string;
  artifactRef?: { kind: 'artifact'; artifact_id: string; service_id?: string };
  bundleRef?: { kind: 'artifact_bundle'; bundle_id: string; service_id?: string };
}

interface FileUploadProps {
  onUpload?: (files: UploadedFile[]) => void;
  disabled?: boolean;
  sessionId?: string;
  allowFolders?: boolean;
  onUploadingChange?: (uploading: boolean) => void;
}

export default function FileUpload(props: FileUploadProps) {
  const [dragOver, setDragOver] = createSignal(false);
  const [uploading, setUploading] = createSignal(false);
  const [failures, setFailures] = createSignal<Array<{ file: File; error: string }>>([]);
  const [progress, setProgress] = createSignal('');
  let uploadingInFlight = false;
  let inputRef: HTMLInputElement | undefined;
  let folderInputRef: HTMLInputElement | undefined;
  let dropTarget!: HTMLDivElement;
  let dragDepth = 0;
  onMount(() => onCleanup(installUploadFeedback(dropTarget, () => !props.disabled && !uploadingInFlight && Boolean(props.sessionId ?? sessionStore.currentSessionId()))));

  const handleFiles = async (files: FileList | File[] | null) => {
    if (!files || files.length === 0 || uploadingInFlight || props.disabled) return;

    const targetSessionId = () => props.sessionId ?? sessionStore.currentSessionId();
    const sessionId = targetSessionId();
    const parentSessionId = sessionStore.currentSessionId();
    const targetIsCurrent = () => targetSessionId() === sessionId && sessionStore.currentSessionId() === parentSessionId;
    const identity = composerIdentity();
    if (!sessionId) {
      console.error('No active session available for upload');
      sessionStore.setStatus('请先创建并选择一个工作目录会话');
      return;
    }
    
    const fileArray = Array.from(files);
    // 去重同一批次内重复文件，避免浏览器重复触发 change 时重复上传。
    const dedupedFiles = fileArray.filter((file, index, arr) => {
      const relPath = String((file as any).webkitRelativePath || '').trim();
      const signature = `${file.name}::${file.size}::${file.lastModified}::${relPath}`;
      return arr.findIndex((other) => {
        const otherRelPath = String((other as any).webkitRelativePath || '').trim();
        return `${other.name}::${other.size}::${other.lastModified}::${otherRelPath}` === signature;
      }) === index;
    });

    uploadingInFlight = true;
    setUploading(true);
    props.onUploadingChange?.(true);
    setFailures([]);
    const failed: Array<{ file: File; error: string }> = [];
    const uploadedFiles: UploadedFile[] = [];
    
    try {
      const folderFiles = dedupedFiles.filter(file => String((file as any).webkitRelativePath || '').trim());
      if (folderFiles.length > 0) {
        try {
          if (folderFiles.some(file => file.size > 100 * 1024 * 1024)) throw new Error('单文件不能超过 100MB');
          const firstRelative = String((folderFiles[0] as any).webkitRelativePath || '').trim();
          const folderName = firstRelative.split('/')[0] || 'folder';
          const createResp = await fetch('/api/artifacts/bundles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: folderName, session_id: sessionId }),
          });
          if (!createResp.ok) throw new Error(`创建文件夹 Bundle 失败: ${await createResp.text()}`);
          const created = await createResp.json();
          const bundleId = String(created.bundle_id || '').trim();
          const serviceId = String(created.ref?.service_id || '').trim();
          for (const file of folderFiles) {
            setProgress(`正在上传 ${file.name}`);
            const relativePath = String((file as any).webkitRelativePath || file.name).trim();
            const formData = new FormData();
            formData.append('file', file);
            const params = new URLSearchParams({ relative_path: relativePath, session_id: sessionId });
            if (serviceId) params.set('service_id', serviceId);
            const response = await fetch(`/api/artifacts/bundles/${encodeURIComponent(bundleId)}/files?${params.toString()}`, {
              method: 'PUT',
              body: formData,
            });
            if (!response.ok) throw new Error(`上传 ${relativePath} 失败: ${await response.text()}`);
          }
          const completeResp = await fetch(`/api/artifacts/bundles/${encodeURIComponent(bundleId)}/complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ expected_files: folderFiles.length, session_id: sessionId, ...(serviceId ? { service_id: serviceId } : {}) }),
          });
          if (!completeResp.ok) throw new Error(`完成文件夹 Bundle 失败: ${await completeResp.text()}`);
          const bundleRef = { kind: 'artifact_bundle' as const, bundle_id: bundleId, ...(serviceId ? { service_id: serviceId } : {}) };
          uploadedFiles.push({
            file: folderFiles[0],
            path: JSON.stringify(bundleRef),
            filename: folderName,
            sessionId,
            bundleRef,
          });
        } catch (error) {
          failed.push(...folderFiles.map(file => ({ file, error: error instanceof Error ? error.message : '文件夹上传失败' })));
        }
      }

      // Keep ordinary chat uploads inside the current Tcode workspace. Remote
      // Artifact transport is deferred until an MCP call actually needs it.
      for (const file of dedupedFiles.filter(file => !String((file as any).webkitRelativePath || '').trim())) {
        try {
          if (!targetIsCurrent()) {
            sessionStore.setSessionStatus(sessionId, '会话已切换，本次上传附件未加入新会话；请重新选择文件。');
            break;
          }
          if (file.size > 100 * 1024 * 1024) throw new Error('单文件不能超过 100MB');
          setProgress(`正在上传 ${file.name}`);
          const formData = new FormData();
          formData.append('file', file);
          const params = new URLSearchParams({ session_id: sessionId });
        
          const resp = await fetch(`/api/files/upload?${params.toString()}`, {
            method: 'POST',
            body: formData,
          });
        
          if (!resp.ok) {
            const error = await resp.json().catch(() => ({}));
            throw new Error(String(error.detail || `上传失败 (${resp.status})`));
          }
        
          const data = await resp.json();
          const responseSessionId: string = String(data.session_id || sessionId || '').trim();
          if (!targetIsCurrent() || responseSessionId !== sessionId) {
            sessionStore.setSessionStatus(sessionId, '文件已保存；会话已切换，请返回原会话重新选择附件。');
            continue;
          }
          const displayPath = String(
            data.display_path || data.container_path || data.relative_path || data.path || '',
          ).trim();
          uploadedFiles.push({
            file,
            path: displayPath,
            displayPath,
            hostPath: String(data.path || '').trim() || undefined,
            containerPath: String(data.container_path || '').trim() || undefined,
            relativePath: String(data.relative_path || '').trim() || undefined,
            filename: data.filename,
            sessionId: responseSessionId,
          });
        } catch (error) {
          failed.push({ file, error: error instanceof Error ? error.message : '网络错误，请重试' });
        }
      }
      
      // Notify parent component with uploaded file info
      if (uploadedFiles.length && composerIdentity() === identity && targetIsCurrent()) {
        props.onUpload?.(uploadedFiles);
      }
      setProgress(uploadedFiles.length ? `已添加 ${uploadedFiles.length} 个附件` : '');
    } catch (e) {
      console.error('Upload error:', e);
    } finally {
      setFailures(failed);
      setUploading(false);
      props.onUploadingChange?.(false);
      uploadingInFlight = false;
    }
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    dragDepth = 0;
    setDragOver(false);
    handleFiles(e.dataTransfer?.files || null);
  };

  const handleDragOver = (e: DragEvent) => {
    if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = props.disabled || uploadingInFlight ? 'none' : 'copy';
    setDragOver(!props.disabled && !uploadingInFlight);
  };

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) setDragOver(false);
  };

  const handleClick = (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    if (!target) return;
    if (target.closest('button')) return;
    if (target.tagName === 'INPUT') return;
    if (uploading()) return;
    if (inputRef) inputRef.click();
  };

  const handleFolderClick = () => {
    if (folderInputRef) folderInputRef.click();
  };

  const handleInput = (e: Event) => {
    const target = e.target as HTMLInputElement;
    handleFiles(target.files);
    // 清空 input，避免重复选择同一文件时行为不一致。
    target.value = '';
  };

  return (
    <div class="flex flex-col gap-2">
      <div
        ref={dropTarget}
        class={`upload-drop-target border-2 border-dashed rounded-lg p-4 text-center cursor-pointer transition-colors ${
          dragOver()
            ? 'border-[#58a6ff] bg-[#21262d]'
            : 'border-[#30363d] hover:border-[#58a6ff]'
        } ${uploading() ? 'opacity-50 pointer-events-none' : ''}`}
        onDrop={handleDrop}
        onDragEnter={e => { if (Array.from(e.dataTransfer?.types || []).includes('Files')) dragDepth++; }}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onClick={handleClick}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          class="hidden"
          onChange={handleInput}
          onClick={(e) => e.stopPropagation()}
          disabled={uploading() || props.disabled}
        />
        <input
          ref={folderInputRef}
          type="file"
          multiple
          class="hidden"
          onChange={handleInput}
          onClick={(e) => e.stopPropagation()}
          disabled={uploading() || props.disabled}
          {...({ webkitdirectory: true } as any)}
        />
        
        <Show
          when={uploading()}
          fallback={
            <>
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class="w-8 h-8 mx-auto mb-2 text-[#8b949e]">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
              </svg>
              <div class="text-sm text-[#8b949e]">
                将文件拖到这里，或 <span class="text-[#58a6ff]">浏览选择</span>
              </div>
              <div class="text-xs text-[#8b949e] mt-1">
                单文件上限 100MB；先保存到当前 Workspace，远端 MCP 按需传输
              </div>
              <Show when={props.allowFolders !== false}><button
                type="button"
                class="btn btn-xs btn-ghost text-[#58a6ff] mt-2"
                onClick={(e) => {
                  e.stopPropagation();
                  handleFolderClick();
                }}
                disabled={uploading() || props.disabled}
              >
                选择文件夹
              </button></Show>
            </>
          }
        >
          <div class="flex items-center justify-center gap-2 text-[#8b949e]">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" class="w-4 h-4 animate-spin">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            <span class="text-sm" role="status">{progress() || '正在添加到 Workspace...'}</span>
          </div>
        </Show>
      </div>
      <Show when={!uploading() && progress()}><p role="status" class="text-sm text-green-300">{progress()}</p></Show>
      <Show when={failures().length > 0}>
        <div role="alert" class="rounded-lg border border-red-500/40 p-3 text-sm text-red-300">
          <For each={failures()}>{item => <p>{item.file.name}：{item.error}</p>}</For>
          <button type="button" disabled={uploading() || props.disabled} class="mt-2 underline"
            onClick={() => void handleFiles(failures().map(item => item.file))}>重试失败文件</button>
        </div>
      </Show>
    </div>
  );
}
