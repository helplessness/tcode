import type { QuestionRequest } from '../stores/session';

/** Stable field keys keep drafts attached to their request when questions arrive. */
export function questionGroup(requests: QuestionRequest[]) {
  const fields = requests.flatMap(request => {
    const batch = request.question.questions;
    return Array.isArray(batch)
      ? batch.map(item => ({ request, item: item as Record<string, unknown>, batch: true }))
      : [{ request, item: request.question as Record<string, unknown>, batch: false }];
  });
  const key = (field: typeof fields[number]) => JSON.stringify([field.request.request_id, field.batch ? field.item.id : null]);
  return {
    questions: fields.map(field => ({ ...field.item, id: key(field) })),
    answers(raw: string): Record<string, string> {
      const values: Record<string, string> = JSON.parse(raw);
      return Object.fromEntries(requests.map(request => {
        const own = fields.filter(field => field.request.request_id === request.request_id);
        const answer = Array.isArray(request.question.questions)
          ? JSON.stringify(Object.fromEntries(own.map(field => [String(field.item.id), values[key(field)] || ''])))
          : values[key(own[0])] || '';
        return [request.request_id, answer];
      }));
    },
  };
}
