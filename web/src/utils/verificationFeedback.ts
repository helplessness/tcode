import type { MessageVerification } from '../stores/session';

// Old sessions may contain raw diagnostics. Never show identifiers as explanations.
export function readableVerificationText(value: string): string {
  if (/timeout|timed? ?out|超时/i.test(value)) return '检查等待时间过长，暂时无法确认。';
  if (/not valid json|schema/i.test(value)) return '回答的格式与要求不一致。';
  const clean = value.replace(/\bLOCAL-\d+\b/g, '').replace(/job_id/g, '任务编号')
    .replace(/(?:criterion|verification|outcome|workflow|call|turn|session|contract|bundle|wf)[_-][A-Za-z0-9_-]+/gi, '')
    .replace(/\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_-]+\b/g, '相关项目')
    .replace(/\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[0-9a-f]{24,}\b/gi, '')
    .replace(/(?:\/[^\s，。；]+){2,}|[{}\[\]`<>]/g, '').trim();
  return /[\u4e00-\u9fff]{2}/.test(clean)
    ? clean.slice(0, 160) : '现有信息还不足以确认这一点。';
}

export function verificationFeedback(value?: MessageVerification) {
  if (!value) return [];
  const rows = value.findings?.length ? value.findings : (value.issues || []).map(reason => ({
    status: 'unverified' as const, title: '需要核对的内容', reason,
  }));
  if (!rows.length && (value.verification_status === 'PARTIAL' || value.verification_status === 'UNVERIFIED')) {
    return [{ status: 'unverified', title: '部分内容尚未核实', reason: value.validator === 'semantic_unavailable'
      ? '这次检查未能完成，暂时无法判断。' : '目前没有足够的核对记录，暂时无法判断。' }];
  }
  const result = rows.map(row => ({ ...row, title: readableVerificationText(row.title), reason: readableVerificationText(row.reason) }))
    .map(row => ({...row, reason: row.reason === '现有信息还不足以确认这一点。'
      ? '这份检查记录没有保存具体原因，不能据此认定结果有误。' : row.reason}));
  return result.filter((row, index) => !result.slice(0, index).some(previous =>
    previous.status === row.status && previous.reason === row.reason &&
    (previous.title === row.title || row.reason.includes('没有保存具体原因')))).slice(0, 10);
}

// Progress notices do not replace a delivered answer or make it stale.
export function latestDeliveredAssistant<T extends {role: string; presentation?: string; tool_calls?: unknown[]; verification?: unknown}>(messages: T[]): T | undefined {
  return [...messages].reverse().find(message => message.role === 'assistant'
    && !['async_completion', 'outcome_confirmation', 'commentary', 'workflow_narration'].includes(message.presentation || '')
    && (!message.tool_calls?.length || Boolean(message.verification)));
}
