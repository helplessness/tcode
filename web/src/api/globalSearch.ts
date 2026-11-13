import type { ResearchProject } from './research';
import { userFacingTitle } from '../utils/userFacingLabels';
export type SearchKind = 'session' | 'message' | 'file' | 'protein-run' | 'workflow' | 'paper' | 'wiki' | 'skill' | 'research' | 'scheduled' | 'job';
export type SearchHit = {
  kind: SearchKind; id: string; title: string; snippet: string;
  sessionId?: string; archived?: boolean; temporary?: boolean;
  project?: ResearchProject;
  templateKey?: string; draftId?: string;
};
export const searchLabels: Record<SearchKind, string> = {
  session: '任务', message: '聊天记录', file: '文件', 'protein-run': '蛋白 Run',
  workflow: '工作流', paper: '文献', wiki: 'Wiki', skill: '技能',
  research: '研究课题', scheduled: '定时任务', job: '远程计算',
};
export function searchMatches(query: string, ...fields: unknown[]): boolean {
  const haystack = fields.map(value => String(value || '')).join(' ').toLocaleLowerCase();
  return query.trim().toLocaleLowerCase().split(/\s+/).every(token => haystack.includes(token));
}

/** Reuse owner/workspace-scoped endpoints; a failed source never hides the others. */
export async function searchWorkspace(query: string, workspaceId: string, signal: AbortSignal,
  includeArchived = false, limit = 30,
  onProgress?: (result: {hits: SearchHit[]; warnings: string[]}) => void): Promise<{hits: SearchHit[]; warnings: string[]}> {
  if (!query.trim() || !workspaceId) return {hits: [], warnings: []};
  const params = new URLSearchParams({workspace_id: workspaceId});
  async function read(path: string, extra: Record<string, string> = {}) {
    const search = new URLSearchParams(params);
    Object.entries(extra).forEach(([key, value]) => search.set(key, value));
    const response = await fetch(`${path}?${search}`, {signal});
    if (!response.ok) throw new Error(`${response.status}`);
    return response.json();
  }
  const sources: {label: string; run: () => Promise<{hits: SearchHit[]; more?: boolean; warning?: string}>}[] = [
    {label: '研究课题', run: async () => {
      const data = await read('/api/research/projects', {limit: '1000', include_archived: String(includeArchived)});
      const rows = data.projects.filter((row: any) => searchMatches(query, row.title, row.description, row.research_id));
      return {hits: rows.slice(0, limit).map((row: any) => ({kind: 'research', id: row.research_id, title: row.title,
        snippet: row.description || row.status, archived: row.status === 'archived', project: row})), more: rows.length > limit || data.projects.length >= 1000};
    }},
    {label: '定时任务', run: async () => {
      const data = await read('/api/scheduled-tasks');
      const rows = data.tasks.filter((row: any) => row.workspace_id === workspaceId && row.status !== 'deleted' && searchMatches(query, row.id, row.name, row.instruction));
      return {hits: rows.slice(0, limit).map((row: any) => ({kind: 'scheduled', id: row.id, title: row.name,
        snippet: `${row.status} · ${row.instruction || ''}`})), more: rows.length > limit};
    }},
    {label: '远程计算', run: async () => {
      const data = await read('/api/symphony/local-issues', {q: query, limit: String(limit)});
      return {hits: data.issues.map((row: any) => ({kind: 'job', id: row.id, title: userFacingTitle(row.title, '计算任务'),
        snippet: '远程计算记录'})), more: data.total > data.issues.length};
    }},
    {label: '任务与聊天', run: async () => {
      const data = await read('/api/session-search', {q: query, limit: String(limit), include_archived: String(includeArchived)});
      return {hits: data.items.map((row: any) => ({kind: row.kind, id: row.id, title: row.title,
        snippet: row.snippet, sessionId: row.session_id, archived: row.archived})), more: data.truncated,
        warning: data.unavailable ? '部分会话暂时无法读取，请重试' : undefined};
    }},
    {label: '文件', run: async () => {
      const data = await read('/api/files/search', {q: query, limit: String(limit)});
      return {hits: data.items.map((row: any) => ({kind: 'file', id: row.path, title: row.name, snippet: row.path})),
        more: data.truncated || data.items.length >= limit};
    }},
    {label: '蛋白 Run', run: async () => {
      const data = await read('/api/protein-runs', {q: query, limit: String(limit)});
      return {hits: data.runs.map((row: any) => ({kind: 'protein-run', id: row.run_id,
        title: userFacingTitle(row.title, '蛋白计算'), snippet: '蛋白计算记录', sessionId: row.session_id})), more: data.total > data.runs.length};
    }},
    {label: '工作流', run: async () => {
      const parts = await Promise.allSettled([read('/api/workflow/runs'), read('/api/workflow/templates'), read('/api/workflow/drafts')]);
      const data = parts[0].status === 'fulfilled' ? parts[0].value : {runs: []};
      const templates = parts[1].status === 'fulfilled' ? parts[1].value.templates : [];
      const drafts = parts[2].status === 'fulfilled' ? parts[2].value.drafts : [];
      const names = new Map(templates.map((row: any) => [row.template_key, row.display_name]));
      const rows = data.runs.filter((row: any) => searchMatches(query, row.run_id, row.template_key, names.get(row.template_key), row.status, row.session_id));
      const hits: SearchHit[] = rows.slice(0, limit).map((row: any) => ({kind: 'workflow', id: row.run_id, title: userFacingTitle(names.get(row.template_key), '工作流运行'),
        snippet: '工作流运行记录', sessionId: row.session_id,
        temporary: Boolean(row.metadata?.workflow_draft_id || row.metadata?.created_from === 'temporary_workflow_draft')}));
      const matchedTemplates = templates.filter((row: any) => searchMatches(query, row.template_key, row.display_name, row.description));
      const matchedDrafts = drafts.filter((row: any) => searchMatches(query, row.draft_id, row.name, row.goal));
      hits.push(...matchedTemplates.slice(0, limit).map((row: any): SearchHit => ({kind: 'workflow', id: `template:${row.template_key}`,
        templateKey: row.template_key, title: row.display_name || row.template_key, snippet: `模板 · ${row.description || ''}`})));
      hits.push(...matchedDrafts.slice(0, limit).map((row: any): SearchHit => ({kind: 'workflow', id: `draft:${row.draft_id}`, draftId: row.draft_id,
        title: userFacingTitle(row.name, '未命名工作流草稿'), snippet: `草稿 · ${row.goal || ''}`, sessionId: row.session_id, temporary: true})));
      return {hits, more: rows.length > limit || matchedTemplates.length > limit || matchedDrafts.length > limit,
        warning: parts.some(part=>part.status==='rejected') ? '部分工作流记录暂时无法读取，请重试' : undefined};
    }},
    {label: '文献', run: async () => {
      const data = await read('/api/paper-library', {q: query, limit: String(limit), offset: '0'});
      return {hits: data.items.map((row: any) => ({kind: 'paper', id: row.doc_id, title: row.title,
        snippet: `${(row.authors || []).join('、')} · ${row.year || ''}`})), more: data.total > data.items.length};
    }},
    {label: 'Wiki', run: async () => {
      const data = await read('/api/paper-wiki/search', {q: query, limit: String(limit), include_archived: String(includeArchived)});
      return {hits: (data.hits || []).map((row: any) => ({kind: 'wiki', id: row.page_id, title: row.title,
        snippet: row.summary || '', archived: Boolean(row.archived_at)})), more: data.total > (data.hits || []).length};
    }},
    {label: '技能', run: async () => {
      const data = await read('/api/skills');
      const rows = data.skills.filter((row: any) => searchMatches(query, row.name, row.description, (row.tags || []).join(' ')));
      return {hits: rows.slice(0, limit).map((row: any) => ({kind: 'skill', id: row.name, title: row.name, snippet: row.description || ''})), more: rows.length > limit};
    }},
  ];
  const hits: SearchHit[] = [], warnings: string[] = [];
  const found = new Map<number, SearchHit[]>();
  await Promise.all(sources.map(async (source, index) => {
    try {
      const result = await source.run();
      found.set(index, result.hits);
      if (result.more) warnings.push(`${source.label}结果较多，当前展示前 ${limit} 项，请细化关键词或进入对应页面`);
      if (result.warning) warnings.push(result.warning);
    } catch { if (!signal.aborted) warnings.push(`${source.label}搜索暂时不可用，请重试`); }
    hits.splice(0, hits.length, ...[...found.entries()].sort(([a], [b]) => a - b).flatMap(([, rows]) => rows));
    if (!signal.aborted) onProgress?.({hits: [...hits], warnings: [...warnings]});
  }));
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  return {hits, warnings};
}
