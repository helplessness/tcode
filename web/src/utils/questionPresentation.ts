import type { QuestionPayload } from '../stores/session';

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown) => typeof value === 'string' ? value.trim() : '';

export function normalizeQuestionOptions(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map(item => String(item ?? '').trim()).filter(Boolean);
  if (typeof raw !== 'string' || !raw.trim()) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) return normalizeQuestionOptions(parsed);
  } catch { /* Accept the legacy comma-separated representation. */ }
  return raw.split(',').map(item => item.trim()).filter(Boolean);
}

export function questionAnswerValue(options: string[], selected: string[], custom: string, multiple: boolean): string {
  if (custom.trim()) return custom.trim();
  // The backend accepts comma-separated 1-based indices; labels may themselves
  // contain commas and must not be split into unintended selections.
  return multiple ? selected.map(item => options.indexOf(item) + 1).filter(index => index > 0).join(', ')
    : selected[0] || '';
}

/** Present the existing preflight evidence; do not invent a new approval gate. */
export function workflowQuestionSummary(question: QuestionPayload) {
  const metadata = object(question.metadata);
  if (!['workflow_applicability_confirmation', 'workflow_auto_route_confirmation'].includes(text(metadata.kind))) return null;
  const assessment = object(metadata.workflow_applicability || metadata.applicability);
  const verdict = text(assessment.verdict);
  const reason = text(assessment.reason) || text(question.question) || '请确认是否启动所选流程。';
  return {
    title: verdict === 'incompatible' ? '目标与流程不匹配，是否仍要运行？'
      : verdict === 'uncertain' ? '暂时无法确认流程是否适用，是否仍要运行？'
        : '是否启动这个 Workflow？',
    reason,
    purpose: text(assessment.workflow_summary),
    gaps: Array.isArray(assessment.gaps) ? [...new Set(assessment.gaps.map(text).filter(item => item && item !== reason))] : [],
  };
}
