import type { GoalState } from '../api/goalMode';
import type { ResearchContinuation } from '../api/research';
import type { SubagentItem } from '../api/subagents';

export function formatKnownCost(value: number | null | undefined, status?: string): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || status === 'unknown') return '未知（尚未计价）';
  const amount = `$${value.toFixed(4)}`;
  return status === 'partial' ? `已知部分 ${amount}（总额未知）` : amount;
}

export function goalBudgetExhausted(goal: GoalState, now = Date.now() / 1000): boolean {
  return goal.status === 'budget_exhausted' || goal.deadline <= now || goal.rounds_used >= goal.max_rounds;
}

export function goalAnswerNotice(goal: GoalState, now = Date.now() / 1000): string {
  const prefix = '回答已保存。';
  if (goal.status === 'paused') return prefix + '目标仍处于暂停状态，点击继续后处理。';
  if (['completed', 'stopped'].includes(goal.status)) return prefix + '目标已结束，未自动继续。';
  if (goal.status !== 'running' && goalBudgetExhausted(goal, now)) return prefix + '轮次或时间预算已耗尽，请增加预算后继续。';
  const pending = goal.questions?.filter(question => question.status === 'pending').length || 0;
  if (pending) return prefix + `仍有 ${pending} 个问题待回答。`;
  if (goal.status === 'ready') return prefix + '将继续处理后续步骤，计算提交仍按当前审核设置执行。';
  if (goal.status === 'running') return prefix + '将在当前轮次结束后接收此回答。';
  if (goal.status === 'waiting_external') return prefix + '正在等待已有计算结果，无需重新提交。';
  return prefix + (goal.reason || '目标仍有其他阻塞，请查看目标详情后继续。');
}

export function goalControlNotice(goal: GoalState, now = Date.now() / 1000): string {
  if (goal.status === 'paused') return '本地目标推进已暂停，禁止该目标新提交计算；暂停不会取消已提交的远端作业。';
  if (['stopped', 'completed'].includes(goal.status)) return '本地目标已结束，禁止该目标新提交计算；已提交的远端作业需要单独确认或取消。';
  if (goal.status === 'budget_exhausted' || goal.deadline <= now || goal.rounds_used > goal.max_rounds) return '目标预算已耗尽，禁止该目标新提交计算；增加预算后才可继续。远端已有作业不会因此取消。';
  if (goal.rounds_used >= goal.max_rounds) return '轮次已用满，不再启动下一轮；当前轮内已授权的计算仍受时间、权限与提交上限检查。';
  if (goal.status === 'waiting_external') return '本地正在等待已有计算结果；无需重复提交。远端状态以作业记录为准。';
  return '本地可按授权推进；每次计算提交仍需通过权限、预算和一次性授权检查。';
}

export function externalRuntimeNotice(runtime: GoalState['external_runtime']): string {
  if (!runtime || runtime.source !== 'local_snapshot') return '远端状态未知：没有可用的本地作业记录，不能据此判断远端已停止。';
  if (runtime.observed_at == null) return '远端状态未知：本地暂无作业记录，不能据此判断远端已停止。';
  const stamp = runtime.observed_at ? `，最近记录 ${new Date(runtime.observed_at * 1000).toLocaleString()}` : '';
  return `本会话作业最近记录：${runtime.active ?? 0} 项排队／运行／等待远端，${runtime.collecting ?? 0} 项结果收集中${stamp}。这是本地快照，未经实时远端确认。`;
}

export function continuationBudgetExhausted(item: ResearchContinuation): boolean {
  return Boolean(item.stop_reason?.endsWith('_limit_reached'));
}

export function continuationNotice(item: ResearchContinuation): string {
  const control = item.status === 'running' ? '允许自动创建下一轮（仍受预算检查）' : '禁止自动创建下一轮';
  const existing = item.active_runs == null ? '已有运行状态未知' : `已有 ${item.active_runs} 项流程仍未结束`;
  const limit = continuationBudgetExhausted(item) ? '预算已达上限，恢复操作不会追加预算。' : '';
  return `${control}；${existing}。${limit}此处暂停或预算停止只限制续跑，不暂停当前流程，也不取消远端作业。`;
}

export function collaborationBudgetNotice(budget: SubagentItem['budget']): string {
  if (!budget) return '';
  const values = (['tokens', 'tool_calls', 'retries'] as const).map((key, i) => {
    const used = budget.usage?.[key];
    const limit = budget.limits?.[key];
    return `${['模型令牌', '工具调用', '重试'][i]} ${used == null ? '未知' : used}/${limit == null ? '未记录' : limit > 0 ? limit : '不限'}`;
  });
  const state = budget.state === 'exhausted' ? '预算用量已达上限' : budget.state === 'warning' ? '预算用量接近上限' : '预算用量';
  return `${state}：${values.join(' · ')}。按已收到的模型用量更新，未回报部分未知；耗尽后禁止新的模型和工具调用，明确增加额度后可恢复。已发出的请求可返回；不代表本地或远端已中止。`;
}
