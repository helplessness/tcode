/** Workspace file browsing, search and inline URLs. */

const API_BASE = '';

/** Upload into the explicitly selected workspace's upload/ directory. */
export async function uploadWorkspaceFile(file: File, workspaceId: string, signal?: AbortSignal, relativePath?: string): Promise<string> {
  const workspace = workspaceId.trim();
  if (!workspace) throw new Error('请先选择工作区');
  if (file.size > 100 * 1024 * 1024) throw new Error('单文件不能超过 100 MB');
  const form = new FormData();
  form.append('file', file);
  const params = new URLSearchParams({ workspace_id: workspace });
  if (relativePath) params.set('relative_path', relativePath);
  const response = await fetch(`${API_BASE}/api/files/upload?${params}`, {
    method: 'POST', body: form, signal,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(data.detail || `上传失败 (${response.status})`));
  // Use the returned path: the server may rename a file to avoid overwriting.
  if (typeof data.relative_path !== 'string' || !data.relative_path.startsWith('upload/')) {
    throw new Error('服务器未返回有效的上传路径，请刷新资产库确认文件是否已保存');
  }
  return data.relative_path;
}

export async function getFiles(path = '') {
  try {
    const resp = await fetch(`${API_BASE}/api/files?path=${encodeURIComponent(path)}`);
    const data = await resp.json();
    return data.items || [];
  } catch (e) {
    console.error('Files error:', e);
    return [];
  }
}

export type WorkspaceFileRef = {
  path: string;
  name?: string;
};

export type WorkspaceFileSearchItem = {
  name: string;
  path: string;
  type: 'file';
  size: number;
  modified_at: number;
  origin: 'uploaded' | 'generated';
};

export async function searchWorkspaceFiles(
  query: string,
  workspaceId: string,
  limit = 40,
): Promise<WorkspaceFileSearchItem[]> {
  const params = new URLSearchParams({
    q: String(query || '').trim(),
    workspace_id: String(workspaceId || '').trim(),
    limit: String(Math.max(1, Math.min(100, limit))),
  });
  const resp = await fetch(`${API_BASE}/api/files/search?${params.toString()}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(String(data.detail || `文件搜索失败 (${resp.status})`));
  }
  return Array.isArray(data.items) ? data.items : [];
}

export async function renameWorkspaceFile(path: string, name: string, workspaceId: string) {
  const response = await fetch(`${API_BASE}/api/files/entry`, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path, name, workspace_id: workspaceId}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(data.detail || `重命名失败 (${response.status})`));
  return data as {path: string; name: string};
}

export async function deleteWorkspaceFile(path: string, workspaceId: string) {
  const params = new URLSearchParams({path, workspace_id: workspaceId});
  const response = await fetch(`${API_BASE}/api/files/entry?${params}`, {method: 'DELETE'});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(data.detail || `删除失败 (${response.status})`));
  return data as {deleted: boolean; path: string};
}

export async function getFileContent(path: string) {
  try {
    const resp = await fetch(`${API_BASE}/api/files/content?path=${encodeURIComponent(path)}`);
    const data = await resp.json();
    return data.content || '';
  } catch (e) {
    console.error('File content error:', e);
    return '';
  }
}

export function getInlineFileUrl(path: string, workdir?: string, workspaceId?: string): string {
  const search = new URLSearchParams({ path });
  if (workspaceId) {
    search.set('workspace_id', workspaceId);
  }
  if (workdir) {
    search.set('workdir', workdir);
  }
  return `${API_BASE}/api/files/inline?${search.toString()}`;
}
