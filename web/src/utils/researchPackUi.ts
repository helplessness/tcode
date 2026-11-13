import type { ResearchPackAsset, ResearchPackPreview } from '../api/research';

export type ResearchPackPeriodPreset = '7' | '30' | '90' | 'custom';

export type ResearchPackPreviewMetrics = {
  researches: number;
  experiences: number;
  unassigned: number;
  artifacts: number;
  evidence: number;
  supporting: number;
  transientExcluded: number;
  privacyExcluded: number;
  blockers: number;
};

const finiteNumber = (value: unknown) => {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
};

const sumNumbers = (value: unknown) => {
  if (!value || typeof value !== 'object') return 0;
  return Object.values(value as Record<string, unknown>)
    .reduce<number>((total, item) => total + finiteNumber(item), 0);
};

export const researchPackPreviewMetrics = (
  preview: ResearchPackPreview,
): ResearchPackPreviewMetrics => ({
  researches: Math.max(
    finiteNumber(preview.counts?.researches),
    preview.scope?.source_research_ids?.length || 0,
  ),
  experiences: finiteNumber(preview.counts?.experience_items),
  unassigned: finiteNumber(preview.routing_counts?.unassigned),
  artifacts: finiteNumber(preview.counts?.artifacts),
  evidence: finiteNumber(preview.counts?.evidence_records),
  supporting: finiteNumber(preview.durability_counts?.supporting),
  transientExcluded: finiteNumber(preview.exclusions?.transient_activities),
  privacyExcluded: sumNumbers(preview.exclusions),
  blockers: (preview.issues || []).filter((item) => item.severity === 'error').length,
});

export const researchPackPeriodBounds = (
  preset: Exclude<ResearchPackPeriodPreset, 'custom'>,
  now = new Date(),
) => {
  const days = Math.max(1, Number(preset));
  const endedAt = new Date(now);
  const startedAt = new Date(now);
  startedAt.setDate(startedAt.getDate() - days);
  return {
    startedAt: startedAt.toISOString(),
    endedAt: endedAt.toISOString(),
  };
};

export const localDateTimeValue = (value: Date | string) => {
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
};

export const localDateTimeToIso = (value: string) => {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString() : '';
};

export const researchPackAssetGeneration = (asset: ResearchPackAsset) => (
  Math.max(1, finiteNumber(asset.lineage?.generation) || 1)
);

export const researchPackAssetParents = (asset: ResearchPackAsset) => {
  const raw = asset.lineage?.parent_pack_ids;
  if (!Array.isArray(raw)) return [];
  return [...new Set(raw.map((item) => String(item || '').trim()).filter(Boolean))];
};

export const researchPackConflictState = (asset: ResearchPackAsset) => {
  const gate = asset.summary?.conflict_gate;
  const blocked = Boolean(gate && typeof gate === 'object' && gate.continuation_blocked);
  const count = Array.isArray(gate?.open_conflict_ids) ? gate.open_conflict_ids.length : 0;
  return { blocked, count, label: blocked ? `待处理冲突 ${count || ''}`.trim() : '无阻断冲突' };
};

