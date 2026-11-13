import { describe, expect, it } from 'vitest';
import { localJobResultRoot } from './jobResultLocation';

describe('materialized job result location', () => {
  it('prefers materialized output over an obsolete remote job reference', () => {
    expect(localJobResultRoot({ job_id: 'remote-job', output_abs_path: '/workspace/job/output',
      artifact_source: { browse_api: 'job_artifacts', root: '/remote/output' } })).toBe('/workspace/job/output');
  });
  it('leaves remote-only results on the existing artifact browser', () => {
    expect(localJobResultRoot({ job_id: 'remote-job', output_root: '/remote/output' })).toBe('');
    expect(localJobResultRoot({ transfer_state: 'ready', output_root: 'https://server/result' })).toBe('');
  });
});
