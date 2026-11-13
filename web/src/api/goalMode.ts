import type { QuestionPayload } from '../stores/session';
export type GoalQuestion = {
  id: string; status: 'pending' | 'defaulted' | 'answered'; required: boolean;
  deadline: number | null; default_answer: string; answer: string; payload: QuestionPayload;
};
export function goalQuestionNotice(question: GoalQuestion): string {
  if (question.status === 'answered') return `已回答：${question.answer}`;
  if (question.status === 'defaulted') return `超时暂用默认值：${question.answer}（非用户确认）。仍可补答；不会自动重跑计算。`;
  if (question.required) return '关键输入必须由你确认，不会超时默认。等待时仅处理不依赖答案的已授权工作。';
  return `非关键偏好，将于 ${new Date((question.deadline || 0) * 1000).toLocaleTimeString()} 暂用默认值「${question.default_answer}」，之后仍可补答。`;
}
export type GoalState = {
  id: string; objective: string; criteria: string; status: string; reason: string;
  progress: string; next_step: string; rounds_used: number; max_rounds: number; deadline: number;
  questions?: GoalQuestion[];
  auto_approve_reviews?: boolean; max_auto_computations?: number; auto_approved_review_ids?: string[];
  external_runtime?: { source: 'local_snapshot' | 'unavailable'; active?: number; collecting?: number; observed_at?: number | null };
};
export async function goalRequest(sessionId: string, workspaceId: string, body?: Record<string, unknown>, action = false, signal?: AbortSignal): Promise<GoalState | null> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/goal${action ? '/action' : ''}${body ? '' : `?workspace_id=${encodeURIComponent(workspaceId)}`}`, {
    method: body ? 'POST' : 'GET', signal,
    ...(body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...body, workspace_id: workspaceId})} : {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '无法读取目标状态');
  return data.goal || null;
}
