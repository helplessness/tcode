import { describe, expect, it } from 'vitest';
import { restoredSchedulerMode } from './schedulerCapabilities';

describe('restore the persisted scheduler', () => {
  it.each(['supervisor', 'chain', 'swarm', 'auto_swarm_builder'] as const)('restores %s after hydration', mode => {
    expect(restoredSchedulerMode({ scheduler_mode: mode })).toBe(mode);
  });
  it('defaults safely for missing or invalid legacy metadata', () => {
    expect(restoredSchedulerMode()).toBe('supervisor');
    expect(restoredSchedulerMode({ scheduler_mode: 'invalid' })).toBe('supervisor');
  });
});
