"""One-shot native-tool approvals in the existing Human Review queue.

Approval resumes the goal, not an unrestricted ordinary Agent turn. The tool
must pass the normal policy again and match the durable approved arguments.
"""
from __future__ import annotations

import json
import time

from ..runtime.shared_tracker import SharedTracker
from ..session.manager import SessionManager
from ..types import ToolResult
from .goal_mode import change_goal, owner, public_goal


def _arguments(review):
    return review.get('approved_tool_args_override') or review.get('tool_args') or {}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _owned(review, session, goal):
    data = review.get('review_data') or {}
    return (review.get('tool_server') == 'tcode-native'
            and data.get('proxy_source') == 'goal_tool_execution'
            and data.get('session_id') == session.id
            and all(data.get(key) == value for key, value in owner(session).items())
            and (data.get('goal_tool_execution') or {}).get('goal_id') == goal['id']
            and (data.get('goal_tool_execution') or {}).get('max_executions') == 1
            and bool(data.get('origin_request_id'))
            and data['origin_request_id'] in goal.get('run_ids', []))


def approved_actions(session, goal):
    """Only unconsumed exact actions are shown to the resumed model."""
    return [row for row in SharedTracker.load_review_store(session.workdir, status='approved')['reviews']
            if _owned(row, session, goal)
            and row.get('execution_disposition') != 'revise_before_execute'
            and row['id'] not in goal.get('tool_review_executions', {})]


def approval_context(session, goal):
    return _json([{'tool': row['tool_name'], 'arguments': _arguments(row)}
                  for row in approved_actions(session, goal)])


async def resume_review(review, *, decision='approved'):
    from ._shared import _delegated_session_lock
    from .state import ServerState
    session_id = str((review.get('review_data') or {}).get('session_id') or '')
    async with _delegated_session_lock(f'goal-control:{session_id}'):
        session = await SessionManager.get(session_id)
        goal = public_goal(session)
        saved = SharedTracker.get_review(review_id=review.get('id', ''), workdir=session.workdir) if session else None
        if not goal or not saved or not _owned(saved, session, goal):
            return {'status': 'skipped', 'reason': 'stale_goal_review', 'started': False}
        if goal['status'] in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
            return {'status': 'skipped', 'reason': 'goal_not_active', 'started': False}
        if time.time() >= goal['deadline'] or goal['rounds_used'] >= goal['max_rounds']:
            await change_goal(session_id, owner(session), goal['id'], status='budget_exhausted', reason='目标预算耗尽，已审核操作尚未执行')
            return {'status': 'skipped', 'reason': 'goal_budget_exhausted', 'started': False}
        if ServerState.is_running(session_id):
            return {'status': 'queued', 'reason': 'waiting_for_goal_round', 'started': False}
        if saved['id'] in goal.get('tool_review_executions', {}):
            return {'status': 'skipped', 'reason': 'approval_already_consumed', 'started': False}
        if decision != 'approved' or saved.get('status') != 'approved':
            await change_goal(session_id, owner(session), goal['id'], status='needs_input', reason='用户未批准此操作，未执行；请调整研究计划')
            return {'status': 'skipped', 'reason': 'not_approved', 'started': False}
        revision = saved.get('execution_disposition') == 'revise_before_execute'
        changes = {}
        if revision:
            changes['tool_review_executions'] = {**goal.get('tool_review_executions', {}), saved['id']: {'status': 'revision_requested'}}
        await change_goal(session_id, owner(session), goal['id'], status='ready',
                          reason='按审核意见调整，变更后的操作须重新审核' if revision else '操作已批准，继续目标并按原参数执行一次',
                          guidance_note=str(saved.get('resume_instruction') or saved.get('comments') or '') if revision else '',
                          guidance_acknowledges_questions=False, **changes)
        return {'status': 'queued', 'reason': 'goal_mode_managed', 'started': False}


async def dispatch_reviews(session):
    goal = public_goal(session)
    if not goal or goal['status'] not in {'needs_input', 'waiting_external', 'recovering'}:
        return
    for review in SharedTracker.load_review_store(session.workdir, status='approved')['reviews']:
        if _owned(review, session, goal) and review['id'] not in goal.get('tool_review_executions', {}):
            await resume_review(review)
            break


async def finish_review(call, result, context):
    """Reflect execution truth without unclaiming failures or reviving a goal."""
    from ._shared import _delegated_session_lock
    from ..runtime.task_runtime import task_runtime_status_for_tool_result
    key = (call.metadata.get('policy') or {}).get('goal_review_id')
    async with _delegated_session_lock(f'goal-control:{context.session_id}'):
        session = await SessionManager.get(context.session_id)
        goal = public_goal(session)
        row = SharedTracker.get_review(review_id=key, workdir=session.workdir) if session else None
        claim = (goal or {}).get('tool_review_executions', {}).get(key, {})
        if not goal or not row or not _owned(row, session, goal) or claim.get('call_id') != call.call_id:
            return
        status = task_runtime_status_for_tool_result(result)
        claims = {**goal['tool_review_executions'], key: {**claim, 'status': status}}
        await change_goal(session.id, owner(session), goal['id'], tool_review_executions=claims)
        row.update(resume_status='failed' if status == 'failed' else 'completed',
                   resume_result={'status': status, 'native_tool_status': status, 'started': True})
        SharedTracker.upsert_review(row, session.workdir)


async def review_tool_call(call, context, policy, *, native_sandbox_tools):
    """Called only for native tools after schema and all hard policy checks."""
    from ._shared import _delegated_session_lock
    from ..mcp._review import enqueue_human_review_issue
    from ..harness.policy import ToolPolicyDecision

    extra = context.extra or {}
    if not callable(extra.get('__goal_checkpoint__')) or not (
            policy.source == 'goal-authorization' or policy.action == 'ask'):
        return policy
    if policy.source == 'goal-authorization' and 'shell' not in set(getattr(policy.capability, 'tags', ())):
        return policy  # Do not extend grants to delegation, Workflow, install or desktop control.
    async with _delegated_session_lock(f'goal-control:{context.session_id}'):
        session = await SessionManager.get(context.session_id)
        goal = public_goal(session)
        if (not goal or goal['status'] != 'running' or context.abort.is_set()
                or not extra.get('request_id') or extra['request_id'] != (goal.get('run_ids') or [None])[-1]
                or time.time() >= goal['deadline'] or goal['rounds_used'] > goal['max_rounds']):
            return policy  # Fail closed; never resurrect a stopped/expired goal.
        for row in approved_actions(session, goal):
            if row.get('tool_name') != call.tool_name or _json(_arguments(row)) != _json(call.arguments):
                continue
            claims = dict(goal.get('tool_review_executions', {}))
            # Claim before entering any tool body. Unknown outcomes cannot replay.
            claims[row['id']] = {'status': 'claimed', 'call_id': call.call_id}
            await change_goal(session.id, owner(session), goal['id'], tool_review_executions=claims)
            if time.time() >= goal['deadline'] or context.abort.is_set():
                return policy
            sandbox = policy.sandbox_decision
            route = bool(sandbox and sandbox.run_in_sandbox and call.tool_name not in native_sandbox_tools)
            return ToolPolicyDecision(action='route_sandbox' if route else 'allow', source='goal-human-review',
                                      capability=policy.capability, sandbox_decision=sandbox,
                                      metadata={**policy.metadata, 'goal_action_confirmation_required': False,
                                                'goal_review_id': row['id']})
        # Reuse a pending request, but never an already consumed approval.
        pending = next((row for row in SharedTracker.load_review_store(session.workdir, status='pending_review')['reviews']
                        if _owned(row, session, goal) and row.get('tool_name') == call.tool_name
                        and _json(row.get('tool_args') or {}) == _json(call.arguments)), None)
    if pending:
        issue = {key: pending[key] for key in ('id', 'identifier', 'status')}
    else:
        issue = await enqueue_human_review_issue(
            server_name='tcode-native', tool_name=call.tool_name, args=call.arguments, context=context,
            review_check={'requires_review': True, 'reason': '目标模式操作申请：仅批准所列工具及参数的一次执行，变更或重试须重新审核。',
                          'trigger': 'goal_tool_execution'},
            review_data_extra={'proxy_source': 'goal_tool_execution', **owner(session),
                               'session_id': session.id, 'origin_request_id': extra['request_id'],
                               'goal_tool_execution': {'goal_id': goal['id'], 'max_executions': 1}})
    return ToolResult(title='等待操作审核', output='操作尚未执行，已加入 Human Review；请在结果审查中批准或拒绝，无需切回普通模式。',
                      metadata={**issue, 'human_review_required': True, 'should_end_turn': True,
                                'execution_environment': {'location': 'not_executed', 'label': 'Not executed', 'sandboxed': False}})
