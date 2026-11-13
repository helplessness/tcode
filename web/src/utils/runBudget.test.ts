import { expect, it } from 'vitest';
import type { GoalState } from '../api/goalMode';
import type { ResearchContinuation } from '../api/research';
import { formatKnownCost, goalAnswerNotice, goalBudgetExhausted, goalControlNotice, externalRuntimeNotice, continuationNotice, continuationBudgetExhausted, collaborationBudgetNotice } from './runBudget';

const goal = {status:'paused', deadline:200, rounds_used:2, max_rounds:4} as GoalState;

it('explains whether a saved answer will continue or still needs attention', () => {
  expect(goalAnswerNotice({...goal, status:'ready'}, 100)).toContain('将继续处理');
  expect(goalAnswerNotice({...goal, status:'ready', deadline:99}, 100)).toContain('增加预算');
  expect(goalAnswerNotice(goal, 100)).toContain('仍处于暂停');
  expect(goalAnswerNotice({...goal, status:'needs_input', reason:'等待人工审核'}, 100)).toContain('等待人工审核');
  expect(goalAnswerNotice({...goal, status:'needs_input', questions:[{status:'pending'}] as GoalState['questions']}, 100)).toContain('1 个问题待回答');
});

it('distinguishes missing prices, explicit zero and a partial subtotal', () => {
  for (const value of [undefined, null, NaN, Infinity, -1]) expect(formatKnownCost(value)).toContain('未知');
  expect(formatKnownCost(0, 'unknown')).not.toContain('$0');
  expect(formatKnownCost(0, 'known')).toBe('$0.0000');
  expect(formatKnownCost(1.5, 'partial')).toBe('已知部分 $1.5000（总额未知）');
});

it('separates local pause, submission prohibition and remote cancellation', () => {
  expect(goalControlNotice(goal, 100)).toContain('本地目标推进已暂停');
  expect(goalControlNotice(goal, 100)).toContain('暂停不会取消');
  expect(goalControlNotice({...goal, status:'running', deadline:99}, 100)).toContain('禁止该目标新提交');
  expect(goalControlNotice({...goal, status:'running', rounds_used:4}, 100)).toContain('当前轮内已授权');
  expect(goalControlNotice({...goal, status:'waiting_external'}, 100)).toContain('无需重复提交');
  expect(goalControlNotice({...goal, status:'stopped'}, 100)).toContain('单独确认或取消');
});

it('allows resume only with remaining round and time budgets', () => {
  expect(goalBudgetExhausted(goal, 100)).toBe(false);
  expect(goalBudgetExhausted({...goal, deadline:100}, 100)).toBe(true);
  expect(goalBudgetExhausted({...goal, rounds_used:4}, 100)).toBe(true);
  expect(goalBudgetExhausted({...goal, status:'budget_exhausted'}, 100)).toBe(true);
});

it('does not turn absent or stale local records into live remote claims', () => {
  expect(externalRuntimeNotice(undefined)).toContain('远端状态未知');
  expect(externalRuntimeNotice({source:'unavailable'})).toContain('不能据此判断远端已停止');
  expect(externalRuntimeNotice({source:'local_snapshot',active:0,collecting:0,observed_at:null})).toContain('本地暂无作业记录');
  const text = externalRuntimeNotice({source:'local_snapshot',active:2,collecting:1,observed_at:100});
  expect(text).toContain('2 项排队');
  expect(text).toContain('1 项结果收集');
  expect(text).toContain('未经实时远端确认');
});

it('keeps existing workflows independent of continuation limits', () => {
  const item = {status:'stopped', active_runs:1} as ResearchContinuation;
  expect(continuationNotice(item)).toContain('禁止自动创建下一轮');
  expect(continuationNotice(item)).toContain('已有 1 项流程仍未结束');
  expect(continuationNotice({...item, status:'paused', active_runs:null})).toContain('已有运行状态未知');
  expect(continuationNotice({...item, status:'running'})).toContain('允许自动创建下一轮');
});

it('disables budget-exhausted continuation without treating a manual pause as exhaustion', () => {
  for (const reason of ['run','time','token','tool_call','failure','cost'].map(key=>`${key}_limit_reached`)) {
    expect(continuationBudgetExhausted({stop_reason:reason} as ResearchContinuation)).toBe(true);
  }
  expect(continuationBudgetExhausted({status:'paused',stop_reason:''} as ResearchContinuation)).toBe(false);
});

it('shows collaboration usage without claiming its warning cancelled work', () => {
  const text = collaborationBudgetNotice({state:'exhausted', limits:{tokens:100}, usage:{tokens:120}});
  expect(text).toContain('120/100');
  expect(text).toContain('工具调用 未知/未记录');
  expect(text).toContain('不代表本地或远端已中止');
  expect(text).toContain('禁止新的模型和工具调用');
  expect(text).toContain('明确增加额度后可恢复');
});
