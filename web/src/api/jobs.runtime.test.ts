import { afterEach, expect, it, vi } from 'vitest';
import { getRuntimeJob, recoverJobArtifacts } from './jobs';
import { readFileSync } from 'node:fs';

afterEach(() => vi.unstubAllGlobals());

function respond(status: number, data: unknown) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify(data), { status })));
}

it('allows tracker-only jobs to omit the optional runtime projection', async () => {
  respond(200, { job: null, projection_state: 'tracker_only' });
  expect(await getRuntimeJob('remote', 'session', 'workspace', { trackerIdentifier: 'LOCAL-1' })).toBeNull();
  respond(404, { detail: 'job not found' });
  await expect(getRuntimeJob('remote', 'session', 'workspace')).rejects.toThrow('job not found');
});

it('does not suppress session ownership, service or network errors', async () => {
  for (const [status, detail] of [[404, 'job not found'], [404, 'session or job not found'], [403, 'Forbidden'], [500, 'unavailable']] as const) {
    respond(status, { detail });
    await expect(getRuntimeJob('remote', 'session', 'workspace', { trackerIdentifier: 'LOCAL-1' })).rejects.toThrow(detail);
  }
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network unavailable')));
  await expect(getRuntimeJob('remote', 'session', 'workspace', { trackerIdentifier: 'LOCAL-1' })).rejects.toThrow('network unavailable');
});

it('preserves existing runtime records and owner query parameters', async () => {
  respond(200, { job: { job_id: 'native', cursor: 3 } });
  expect(await getRuntimeJob('native', 'session', 'workspace')).toEqual({ job_id: 'native', cursor: 3 });
  expect(fetch).toHaveBeenCalledWith('/api/runtime/jobs/native?session_id=session&workspace_id=workspace');
});

it('rejects incomplete successful responses rather than treating them as missing projections', async () => {
  respond(200, { job: null });
  await expect(getRuntimeJob('remote', 'session', 'workspace', { trackerIdentifier: 'LOCAL-1' })).rejects.toThrow();
  respond(200, { job: null, projection_state: 'tracker_only' });
  await expect(getRuntimeJob('remote', 'session', 'workspace')).rejects.toThrow();
});

it('retrieves existing artifacts with owner scope, never invokes retry or resume', async () => {
  const payload = {session_id:'s', workspace_id:'w', tracker_identifier:'LOCAL-1', bundle_id:'bundle_existing'};
  respond(200, {success:true, recovery:{total_files:21,original_status:'failed'}, workspace:{transfer_state:'ready'}});
  const result = await recoverJobArtifacts('job/one', payload);
  expect(result.recovery.original_status).toBe('failed');
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch).toHaveBeenCalledWith('/api/runtime/jobs/job%2Fone/recover-artifacts', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload),
  });
  respond(409, {detail:'文件归属不符'});
  await expect(recoverJobArtifacts('one',payload)).rejects.toThrow('文件归属不符');
  respond(200, {success:true});
  await expect(recoverJobArtifacts('one',payload)).rejects.toThrow();
});

it('keeps recovery progress and failures explicit in the existing task panel', () => {
  const panel = readFileSync(new URL('../components/JobWorkspacePanel.tsx', import.meta.url),'utf8');
  expect(panel).toContain('取回文件，不重新运行');
  expect(panel).toContain('正在下载并校验');
  expect(panel).toContain('disabled={recovering()');
  expect(panel).toContain('role="alert" class="text-sm">{recoveryError()}');
});
