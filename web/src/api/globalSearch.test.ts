import { afterEach, describe, expect, it, vi } from 'vitest';
import { searchMatches, searchWorkspace } from './globalSearch';

afterEach(() => vi.unstubAllGlobals());
describe('workspace search', () => {
  it('matches Chinese and multiple terms without interpreting special syntax', () => {
    expect(searchMatches('蛋白 boltz', '蛋白结构', 'BOLTZ')).toBe(true);
    expect(searchMatches('蛋白 missing', '蛋白')).toBe(false);
    expect(searchMatches('[foo]', '[foo].pdb')).toBe(true);
  });
  it('scopes every request and keeps results when one source fails', async () => {
    const fetcher = vi.fn(async (url: string) => {
      const target = new URL(url, 'http://local');
      expect(target.searchParams.get('workspace_id')).toBe('ws-a');
      if (target.pathname === '/api/paper-wiki/search') return new Response('', {status: 503});
      const fixtures: Record<string, unknown> = {
        '/api/session-search': {items: [{kind: 'message', id: 'm1', session_id: 's1', title: '任务', snippet: '蛋白'}], total: 1},
        '/api/files/search': {items: [{path: 'upload/蛋白.pdb', name: '蛋白.pdb'}]},
        '/api/protein-runs': {runs: [], total: 0},
        '/api/workflow/runs': {runs: [{run_id: 'r1', template_key: '蛋白', status: 'completed', metadata: {workflow_draft_id: 'draft'}}]},
        '/api/paper-library': {items: [], total: 0}, '/api/skills': {skills: []},
        '/api/research/projects': {projects: []}, '/api/scheduled-tasks': {tasks: []}, '/api/symphony/local-issues': {issues: [], total: 0},
        '/api/workflow/templates': {templates: []}, '/api/workflow/drafts': {drafts: []},
      };
      return new Response(JSON.stringify(fixtures[target.pathname]), {status: 200});
    });
    vi.stubGlobal('fetch', fetcher);
    const result = await searchWorkspace('蛋白', 'ws-a', new AbortController().signal, true);
    expect(result.hits.map(hit => hit.kind)).toEqual(['message', 'file', 'workflow']);
    expect(result.hits[2].temporary).toBe(true);
    expect(result.hits[2].id).toBe('r1');
    expect(result.hits[2].title).toBe('工作流运行');
    expect(result.hits[2].snippet).not.toContain('r1');
    expect(result.warnings).toContain('Wiki搜索暂时不可用，请重试');
    expect(fetcher.mock.calls.find(([url]) => url.startsWith('/api/session-search'))?.[0]).toContain('include_archived=true');
    expect(fetcher.mock.calls.find(([url]) => url.startsWith('/api/protein-runs'))?.[0]).toContain('q=');
  });
  it('does not search without a workspace or return aborted results', async () => {
    const fetcher = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', fetcher);
    expect(await searchWorkspace('x', '', new AbortController().signal)).toEqual({hits: [], warnings: []});
    expect(fetcher).not.toHaveBeenCalled();
    const controller = new AbortController(); controller.abort();
    await expect(searchWorkspace('x', 'ws', controller.signal)).rejects.toMatchObject({name: 'AbortError'});
  });
});
