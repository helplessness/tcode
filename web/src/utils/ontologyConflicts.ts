import type { OntologyGraphEdge } from '../components/OntologyGraph';

export const conflictLabels: Record<string, string> = {
  same_conditions: '同记录条件下相反，待核对', different_conditions: '实验条件不同',
  unknown_conditions: '条件不足，无法比较', source_dispute: '来源争议，待核对', unchecked: '比较达到上限，尚未核对完整',
};
export const conflictText = (edge: Pick<OntologyGraphEdge, 'conflict' | 'conflict_categories'>) =>
  edge.conflict_categories?.length ? edge.conflict_categories.map(key => conflictLabels[key] || key).join('；')
    : edge.conflict ? '待核对' : '本次范围未发现相反主张';
export const relationshipColor = (edge: Pick<OntologyGraphEdge, 'scope' | 'conflict' | 'conflict_categories'>) =>
  edge.conflict ? '#b34e43' : edge.conflict_categories?.some(key => ['unknown_conditions','source_dispute','unchecked'].includes(key))
    ? '#a07b34' : edge.conflict_categories?.includes('different_conditions') ? '#527c87' : edge.scope === 'private' ? '#b38a56' : '#aab0a3';

