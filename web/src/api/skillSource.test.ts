import { afterEach, expect, it, vi } from 'vitest';
import { installSkill, resolveSkillSource } from './capabilities';

afterEach(() => vi.unstubAllGlobals());
it('resolves within the workspace, then sends selection and confirmation', async () => {
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({candidates: [], name: 'demo'})));
  vi.stubGlobal('fetch', fetcher);
  const controller = new AbortController();
  await resolveSkillSource('https://skills.example/demo', 'workspace-a', controller.signal);
  expect(fetcher.mock.calls[0][0]).toBe('/api/skills/resolve-source');
  const first = fetcher.mock.calls[0][1]!;
  expect(first.signal).toBe(controller.signal);
  expect(JSON.parse(first.body as string)).toEqual({url: 'https://skills.example/demo', workspace_id: 'workspace-a'});
  await installSkill('https://github.com/owner/repo/tree/pinned/skills/demo', 'workspace-a', {
    introduction_url: 'https://skills.example/demo', path: 'skills/demo', ref: 'pinned', confirmed: true,
  });
  expect(JSON.parse(fetcher.mock.calls[1][1]!.body as string)).toMatchObject({confirmed: true, ref: 'pinned', path: 'skills/demo', scope: 'workspace'});
});
it('preserves the legacy direct install contract', async () => {
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({name: 'demo'})));
  vi.stubGlobal('fetch', fetcher);
  await installSkill('https://github.com/owner/repo/tree/main/skills/demo', 'workspace');
  expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toEqual({
    source_url: 'https://github.com/owner/repo/tree/main/skills/demo', workspace_id: 'workspace', scope: 'workspace', method: 'auto',
  });
});
