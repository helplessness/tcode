import { describe, expect, it } from 'vitest';

import { bridgeWorkerWarning } from './bridgeWorkerHealth';

describe('bridge worker health hint', () => {
  const now = Date.parse('2026-08-27T10:01:00Z');

  it('warns when a bridge job remains unclaimed for more than 30 seconds', () => {
    expect(bridgeWorkerWarning({
      source_type: 'async',
      status: 'todo',
      execution_channel: 'bridge',
      claim_owner: '',
      created_at: '2026-08-27T10:00:00Z',
    }, now)).toContain('bridge worker');
  });

  it('does not warn for a freshly queued or claimed job', () => {
    expect(bridgeWorkerWarning({
      source_type: 'async',
      status: 'todo',
      execution_channel: 'bridge',
      claim_owner: '',
      created_at: '2026-08-27T10:00:45Z',
    }, now)).toBe('');
    expect(bridgeWorkerWarning({
      source_type: 'async',
      status: 'todo',
      execution_channel: 'bridge',
      claim_owner: 'bridge-worker-1',
      created_at: '2026-08-27T10:00:00Z',
    }, now)).toBe('');
  });
});
