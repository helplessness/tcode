export type OntologyCondition = { key: string; value: string };

// Numeric and boolean values retain their type; units and other text stay literal.
export function ontologyQualifierFilters(conditions: OntologyCondition[]): Record<string, string | number | boolean> {
  const entries: Array<[string, string | number | boolean]> = [];
  const seen = new Set<string>();
  for (const condition of conditions) {
    const key = condition.key.trim(), raw = condition.value.trim();
    if (!key && !raw) continue;
    if (!key || !raw) throw new Error('每个条件都需要名称和值');
    if (seen.has(key)) throw new Error(`条件 ${key} 重复，请合并后查询`);
    let value: unknown;
    try { value = JSON.parse(raw); } catch { value = raw; }
    if (!['string', 'number', 'boolean'].includes(typeof value) || (typeof value === 'number' && !Number.isFinite(value))) {
      throw new Error('条件值只支持文字、数字或 true / false');
    }
    entries.push([key, value as string | number | boolean]); seen.add(key);
  }
  if (entries.length > 20) throw new Error('最多支持 20 个筛选条件');
  return Object.fromEntries(entries);
}

export async function ontologyRequest<T = any>(workspace: string, path: string, body?: unknown): Promise<T> {
  const separator = path.includes('?') ? '&' : '?';
  const response = await fetch(`/api/ontology${path}${separator}workspace_id=${encodeURIComponent(workspace)}`, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({ detail: `服务返回异常 (${response.status})，请重试或检查服务日志` }));
  if (!response.ok) throw new Error(String(result.detail || `请求失败 (${response.status})`));
  return result as T;
}

export async function deleteOntologyResource<T = any>(workspace: string, path: string): Promise<T> {
  const separator = path.includes('?') ? '&' : '?';
  const response = await fetch(`/api/ontology${path}${separator}workspace_id=${encodeURIComponent(workspace)}`, { method: 'DELETE' });
  const result = await response.json().catch(() => ({ detail: `服务返回异常 (${response.status})，请重试或检查服务日志` }));
  if (!response.ok) throw new Error(String(result.detail || `请求失败 (${response.status})`));
  return result as T;
}

export async function uploadOntologySource(workspace: string, file: File): Promise<string> {
  const form = new FormData(); form.append('file', file);
  const response = await fetch(`/api/files/upload?workspace_id=${encodeURIComponent(workspace)}`, { method: 'POST', body: form });
  const result = await response.json().catch(() => ({ detail: `服务返回异常 (${response.status})，请重试或检查服务日志` }));
  if (!response.ok) throw new Error(String(result.detail || '上传失败'));
  return String(result.relative_path);
}
