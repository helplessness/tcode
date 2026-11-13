import { describe, expect, it } from 'vitest';

import {
  localDateTimeToIso,
  researchPackAssetGeneration,
  researchPackAssetParents,
  researchPackConflictState,
  researchPackPeriodBounds,
  researchPackPreviewMetrics,
} from './researchPackUi';

describe('research pack UI helpers', () => {
  it('builds stable UTC period bounds for presets', () => {
    const bounds = researchPackPeriodBounds('30', new Date('2026-07-22T12:00:00.000Z'));
    expect(bounds).toEqual({
      startedAt: '2026-06-22T12:00:00.000Z',
      endedAt: '2026-07-22T12:00:00.000Z',
    });
    expect(localDateTimeToIso('2026-07-22T20:00')).toMatch(/^2026-07-22T/);
  });

  it('maps preview fields to the user-visible summary', () => {
    const metrics = researchPackPreviewMetrics({
      counts: { researches: 2, experience_items: 8, artifacts: 3, evidence_records: 4 },
      routing_counts: { unassigned: 2 },
      durability_counts: { supporting: 5 },
      exclusions: { transient_activities: 7, raw_sessions: 10 },
      issues: [{ severity: 'error' }, { severity: 'warning' }],
      scope: { mode: 'workspace_period', source_research_ids: ['a', 'b'], started_at: '', ended_at: '' },
    } as any);
    expect(metrics).toEqual({
      researches: 2,
      experiences: 8,
      unassigned: 2,
      artifacts: 3,
      evidence: 4,
      supporting: 5,
      transientExcluded: 7,
      privacyExcluded: 17,
      blockers: 1,
    });
  });

  it('reads lineage and conflict state without creating local projects', () => {
    const asset = {
      lineage: { generation: 3, parent_pack_ids: ['pack-2', 'pack-2'] },
      summary: { conflict_gate: { continuation_blocked: true, open_conflict_ids: ['c-1'] } },
    } as any;
    expect(researchPackAssetGeneration(asset)).toBe(3);
    expect(researchPackAssetParents(asset)).toEqual(['pack-2']);
    expect(researchPackConflictState(asset)).toEqual({
      blocked: true,
      count: 1,
      label: '待处理冲突 1',
    });
  });
});
