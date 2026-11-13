"""One approved goal computation uses the existing durable async queue once."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from ..runtime.shared_tracker import SharedTracker
from ..session.manager import SessionManager
from ..tools.base import ToolContext
from .goal_mode import public_goal, owner, change_goal
from .state import ServerState


async def auto_approve_computation(review, session):
    """Explicit per-goal opt-in; reserve its budget before recording approval."""
    from datetime import datetime, timezone
    from ._shared import _delegated_session_lock
    from ..runtime.task_runtime import TaskRuntimeStore
    async with _delegated_session_lock(f'goal-control:{session.id}'):
        session = await SessionManager.get(session.id, **owner(session))
        goal = public_goal(session)
        data = review.get('review_data') or {}
        if (not goal or not goal.get('auto_approve_reviews')
                or data.get('session_id') != session.id
                or goal['status'] not in {'ready', 'running', 'needs_input', 'waiting_external'}
                or time.time() >= goal['deadline'] or goal['rounds_used'] > goal['max_rounds']
                or (data.get('goal_computation') or {}).get('goal_id') != goal['id']
                or data.get('origin_request_id') not in goal.get('run_ids', [])):
            return False
        ids = list(goal.get('auto_approved_review_ids') or [])
        if review['id'] in ids or len(ids) >= goal.get('max_auto_computations', 3):
            return False
        current = SharedTracker.get_review(review_id=review['id'], workdir=session.workdir)
        if not current or current.get('status') != 'pending_review':
            return False
        await change_goal(session.id, owner(session), goal['id'], auto_approved_review_ids=[*ids, review['id']])
        current.update(status='approved', reviewer='goal-auto-policy', reviewed_at=datetime.now(timezone.utc).isoformat(), execution_disposition='execute_original')
        SharedTracker.upsert_review(current, session.workdir)
        TaskRuntimeStore.transition_linked_open([current['id'], current['identifier']], status='completed', phase='auto_approved', source='human_review', session_id=session.id, workdir=session.workdir)
        return True


async def resume_approved_computation(review, *, decision='approved'):
    from ._shared import _delegated_session_lock, _enqueue_approved_proxy_review

    data = review.get('review_data') or {}
    session_id = str(data.get('session_id') or '')
    # Serialize approval replays without blocking Pause/Stop during remote I/O.
    async with _delegated_session_lock(f'goal-computation:{session_id}'):
        session = await SessionManager.get(session_id)
        goal = public_goal(session)
        grant = data.get('goal_computation') or {}
        if not session or not goal or grant.get('goal_id') != goal['id']:
            return {'status': 'skipped', 'reason': 'stale_goal', 'started': False}
        if data.get('origin_request_id') not in goal.get('run_ids', []):
            return {'status': 'skipped', 'reason': 'stale_goal_request', 'started': False}
        # Reload the durable decision, not the possibly stale approval callback.
        saved = SharedTracker.get_review(review_id=review.get('id', ''), workdir=session.workdir)
        if not saved or saved.get('status') != 'approved' or decision != 'approved':
            return {'status': 'skipped', 'reason': 'not_approved', 'started': False}
        saved_data = saved.get('review_data') or {}
        if (saved_data.get('session_id') != session_id
                or (saved_data.get('goal_computation') or {}).get('goal_id') != goal['id']
                or saved_data.get('origin_request_id') not in goal.get('run_ids', [])
                or saved_data.get('proxy_source') != 'job_protocol_submit'
                or (saved_data.get('goal_computation') or {}).get('max_submissions') != 1):
            return {'status': 'skipped', 'reason': 'invalid_goal_grant', 'started': False}
        review = saved
        key = str(review['id'])
        consumed = goal.get('computation_reviews', {}).get(key)
        if consumed:
            return {'status': consumed.get('status', 'claimed'), 'started': False,
                    'identifier': consumed.get('identifier', ''), 'reason': 'approval_already_consumed'}
        if goal['status'] in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
            return {'status': 'skipped', 'reason': 'goal_not_active', 'started': False}
        if time.time() >= goal['deadline'] or goal['rounds_used'] > goal['max_rounds']:
            await change_goal(session_id, owner(session), goal['id'], expected_updated_at=goal['updated_at'], status='budget_exhausted', reason='目标预算耗尽，已批准的计算尚未提交')
            return {'status': 'skipped', 'reason': 'goal_budget_exhausted', 'started': False}
        if ServerState.is_running(session_id):
            return {'status': 'queued', 'reason': 'waiting_for_goal_round', 'started': False}
        if review.get('execution_disposition') == 'revise_before_execute':
            async with _delegated_session_lock(f'goal-control:{session_id}'):
                latest = public_goal(await SessionManager.get(session_id, **owner(session)))
                if not latest or latest['id'] != goal['id'] or latest['status'] in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
                    return {'status': 'skipped', 'reason': 'goal_not_active', 'started': False}
                claims = dict(latest.get('computation_reviews') or {})
                claims[key] = {'status': 'revision_requested', 'max_submissions': 0}
                await change_goal(session_id, owner(session), goal['id'], computation_reviews=claims,
                                  status='ready', reason='按审核反馈修订计算申请，修订后须重新审核',
                                  guidance_note=str(review.get('resume_instruction') or review.get('comments') or ''), guidance_acknowledges_questions=False)
            return {'status': 'skipped', 'reason': 'revision_requested', 'started': False}

        args = review.get('approved_tool_args_override') or review.get('tool_args') or {}
        async def reject(error, reason):
            async with _delegated_session_lock(f'goal-control:{session_id}'):
                latest = public_goal(await SessionManager.get(session_id, **owner(session)))
                if latest and latest['id'] == goal['id'] and latest['status'] not in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
                    claims = dict(latest.get('computation_reviews') or {})
                    claims[key] = {'status': 'failed', 'max_submissions': 1}
                    await change_goal(session_id, owner(session), goal['id'], computation_reviews=claims, status='needs_input', reason=reason)
            return {'status': 'failed', 'error': error, 'started': False}
        agent = ServerState.get_or_create_agent(session_id, hydrate=False)
        if agent._resolve_permission_action('mcp_job-protocol-mcp_submit_job') == 'deny':
            return await reject('permission_denied', '当前工具权限明确拒绝提交，审核不能绕过该拒绝')
        from ..governance.biosafety import evaluate_tool_call
        context = ToolContext(session_id, 'goal-review', session.agent_name or 'build', asyncio.Event(), extra={'workdir': session.workdir, **owner(session)})
        safety = evaluate_tool_call(tool_name=f"mcp_{review['tool_server']}_{review['tool_name']}", arguments=args, context=context)
        if not safety.allowed:
            return await reject(safety.reason, safety.reason)
        from ..mcp.route_permissions import get_agent_allowed_routed_servers
        allowed = get_agent_allowed_routed_servers(context)
        if allowed is not None and str(review['tool_server']).lower() not in allowed:
            return await reject('target_server_not_allowed', '当前 Agent 已无权执行该计算，未提交')
        from ..mcp._proxy import _validate_target_args_contract
        invalid = await _validate_target_args_contract(target_server=review['tool_server'], target_tool=review['tool_name'], tool_args=args, context=context)
        if invalid is not None:
            return await reject(invalid.error or 'invalid_arguments', invalid.error or '获批参数不符合工具契约，未提交')
        latest = public_goal(await SessionManager.get(session_id, **owner(session)))
        if not latest or latest['id'] != goal['id'] or latest['status'] in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
            return {'status': 'skipped', 'reason': 'goal_not_active', 'started': False}
        if time.time() >= latest['deadline'] or latest['rounds_used'] > latest['max_rounds']:
            await change_goal(session_id, owner(session), goal['id'], expected_updated_at=latest['updated_at'], status='budget_exhausted', reason='参数核对期间目标预算耗尽，未提交计算')
            return {'status': 'skipped', 'reason': 'goal_budget_exhausted', 'started': False}
        @asynccontextmanager
        async def admission():
            # Only the durable queue write holds the control lock. Artifact
            # imports and service calls happen before it; refresh happens after.
            async with _delegated_session_lock(f'goal-control:{session_id}'):
                current = public_goal(await SessionManager.get(session_id, **owner(session)))
                if not current or current['id'] != goal['id'] or current['status'] in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
                    raise RuntimeError('goal_not_active')
                if time.time() >= current['deadline'] or current['rounds_used'] > current['max_rounds']:
                    await change_goal(session_id, owner(session), goal['id'], status='budget_exhausted', reason='提交前目标预算耗尽，未提交计算')
                    raise RuntimeError('goal_budget_exhausted')
                claims = dict(current.get('computation_reviews') or {})
                if key in claims:
                    raise RuntimeError('approval_already_consumed')
                # Claim BEFORE the queue write: ambiguous failures never retry.
                claims[key] = {'status': 'claimed', 'max_submissions': 1}
                await change_goal(session_id, owner(session), goal['id'], computation_reviews=claims)
                # Persisting the claim can itself cross the deadline. Do not
                # yield to the synchronous queue write after that wait expires.
                if time.time() >= current['deadline']:
                    await change_goal(session_id, owner(session), goal['id'], status='budget_exhausted', reason='授权写盘期间目标预算耗尽，未提交计算')
                    raise RuntimeError('goal_budget_exhausted')
                yield

        result = await _enqueue_approved_proxy_review(review, session_id=session_id,
                    agent_name=session.agent_name or 'build', workdir=session.workdir, owner=owner(session), admission=admission)
        result = result or {'status': 'failed', 'error': 'invalid_goal_computation_review'}
        accepted = result.get('status') in {'queued', 'deduped'}
        def finish(current):
            latest = public_goal(current)
            if not latest or latest['id'] != goal['id']:
                return
            claims = dict(latest.get('computation_reviews') or {})
            if accepted or key in claims or result.get('error') not in {'goal_not_active', 'goal_budget_exhausted'}:
                claims[key] = {'status': result.get('status', 'failed'), 'identifier': result.get('identifier', ''), 'max_submissions': 1}
            latest['computation_reviews'] = claims
            if latest['status'] not in {'paused', 'stopped', 'completed', 'budget_exhausted'}:
                latest.update(status='waiting_external' if accepted else 'needs_input',
                              reason='已批准的一次计算已受理，等待结果；不会重复提交' if accepted else str(result.get('error') or '计算未受理，请检查后重新申请'))
            latest['updated_at'] = time.time()
            current.metadata['goal_mode'] = latest
        await SessionManager.mutate(session_id, finish, **owner(session))
        return {'status': 'completed' if accepted else 'failed', 'started': accepted,
                'session_id': session_id, 'approved_action': result}


async def dispatch_approved_computations(session):
    """A paused approval is deferred until explicit Resume; ordinary goals bypass."""
    goal = public_goal(session)
    if not goal or goal['status'] not in {'ready', 'waiting_external', 'recovering', 'needs_input'}:
        return
    if goal.get('auto_approve_reviews'):
        for pending in SharedTracker.load_review_store(session.workdir, status='pending_review')['reviews']:
            if (pending.get('review_data') or {}).get('session_id') == session.id:
                await auto_approve_computation(pending, session)
    for review in SharedTracker.load_review_store(session.workdir, status='approved')['reviews']:
        data = review.get('review_data') or {}
        if (data.get('session_id') == session.id
                and (data.get('goal_computation') or {}).get('goal_id') == goal['id']
                and review.get('id') not in goal.get('computation_reviews', {})):
            result = await resume_approved_computation(review)
            current = SharedTracker.get_review(review_id=review['id'], workdir=session.workdir)
            if current:
                current.update(resume_status=result.get('status', 'failed'), resume_result=result)
                SharedTracker.upsert_review(current, session.workdir)
