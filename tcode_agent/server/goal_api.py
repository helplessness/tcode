from typing import Literal
import time
import uuid
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from ..session.manager import SessionManager
from .workspace_scope import _workspace_context_from_request, _session_owner_kwargs
from .state import ServerState
from .goal_mode import ACTIVE, public_goal, change_goal, goal_service, flush_goal_guidance, record_goal_guidance

router = APIRouter()


class GoalStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    desktop_permission_mode: Literal['ask', 'auto'] | None = None
    workspace_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=4000)
    criteria: str = Field(default='完成用户研究目标中的要求，结论必须有可核验证据；不明确时先询问用户，不得虚构实验结果。', min_length=1, max_length=4000)
    from_chat: bool = False
    max_rounds: int = Field(default=8, ge=1, le=100)
    max_minutes: int = Field(default=30, ge=1, le=1440)
    steps_per_round: int = Field(default=12, ge=1, le=50)
    auto_approve_reviews: bool = False
    max_auto_computations: int = Field(default=3, ge=1, le=100)


class GoalAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    desktop_permission_mode: Literal['ask', 'auto'] | None = None
    workspace_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=100)
    action: Literal['pause', 'resume', 'stop', 'guide', 'answer', 'review_policy']
    auto_approve_reviews: bool = False
    max_auto_computations: int = Field(default=3, ge=1, le=100)
    question_id: str = Field(default='', max_length=100)
    answers: dict[str, str] | None = None
    note: str = Field(default='', max_length=2000)
    extra_rounds: int = Field(default=0, ge=0, le=100)
    extra_minutes: int = Field(default=0, ge=0, le=1440)


async def scoped_session(request, session_id, workspace, *, native=False):
    context = _workspace_context_from_request(request, workspace_id=workspace)
    scope = _session_owner_kwargs(context, include_workspace=True)
    session = await SessionManager.get(session_id, **scope)
    if not session or SessionManager.is_archived_session(session):
        raise HTTPException(404, '会话不存在、已归档或无法访问')
    metadata = session.metadata or {}
    if native and (metadata.get('harness') or {}).get('runtime_id', 'tcode-native') != 'tcode-native':
        raise HTTPException(409, '目标模式目前仅支持 TCode 原生 Agent，请先切换到原生模式')
    return session, scope


def goal_response(session, goal):
    # Observation belongs to every response, never to persisted goal metadata.
    if goal:
        goal = dict(goal)
        # A local observation is not a live remote health check or cancellation receipt.
        from ..runtime.task_runtime import TaskRuntimeStore
        try:
            rows = TaskRuntimeStore.list(session_id=session.id, runtime_kind='mcp_job', workdir=session.workdir)
            goal['external_runtime'] = {
                'source': 'local_snapshot',
                'active': sum(row.get('status') in {'queued', 'running', 'waiting_external'} for row in rows),
                'collecting': sum(row.get('status') == 'collecting' for row in rows),
                'observed_at': max((row.get('updated_at', 0) for row in rows), default=None),
            }
        except (OSError, ValueError, TypeError):
            goal['external_runtime'] = {'source': 'unavailable'}
    return {'goal': goal}


@router.get('/api/sessions/{session_id}/goal')
async def get_goal(session_id: str, request: Request, workspace_id: str = Query(...)):
    session, _ = await scoped_session(request, session_id, workspace_id)
    return goal_response(session, public_goal(session))


@router.post('/api/sessions/{session_id}/goal')
async def start_goal(session_id: str, body: GoalStart, request: Request):
    from ._shared import _delegated_session_lock
    async with _delegated_session_lock(f'goal-control:{session_id}'):
        return await _create_goal(session_id, body, request)


async def _create_goal(session_id, body, request):
    session, scope = await scoped_session(request, session_id, body.workspace_id, native=True)
    from ._shared import _is_chat_session_busy
    if ServerState.is_running(session_id) or _is_chat_session_busy(session_id):
        raise HTTPException(409, '请等待当前运行结束后开启目标模式')
    if not body.objective.strip() or not body.criteria.strip():
        raise HTTPException(422, '请填写目标和完成条件')
    now = time.time()
    goal = dict(schema='tcode.goal-mode.v1', id='goal-' + uuid.uuid4().hex, objective=body.objective.strip(), criteria=body.criteria.strip(),
                max_rounds=body.max_rounds, steps_per_round=body.steps_per_round, rounds_used=0,
                deadline=now + body.max_minutes * 60, created_at=now, updated_at=now,
                status='ready', progress='', next_step='', reason='', run_ids=[],
                auto_approve_reviews=body.auto_approve_reviews, max_auto_computations=body.max_auto_computations)
    if body.desktop_permission_mode is not None:
        goal['desktop_permission_mode'] = body.desktop_permission_mode
    def mutate(current):
        existing = public_goal(current)
        if existing and existing['status'] not in {'completed', 'stopped'}:
            raise HTTPException(409, '当前会话已有未结束目标，请继续或停止它')
        if existing:
            current.metadata['answered_goal_question_calls'] = list(set(current.metadata.get('answered_goal_question_calls') or []) | set(existing.get('answered_question_calls') or []))
        if existing and existing['status'] == 'stopped':
            current.metadata['stopped_goal_run_ids'] = list(dict.fromkeys([
                *current.metadata.get('stopped_goal_run_ids', []), *existing.get('run_ids', []),
            ]))
        current.metadata = {**(current.metadata or {}), 'goal_mode': goal}
        if body.from_chat:
            from ..types import Message
            current.messages.append(Message(role='user', content=goal['objective'], id='msg-' + uuid.uuid4().hex,
                                           metadata={'goal_id': goal['id'], 'purpose': 'goal_request'}))
        return goal
    created = await SessionManager.mutate(session_id, mutate, **scope)
    await goal_service.start()
    return goal_response(session, created)


@router.post('/api/sessions/{session_id}/goal/action')
async def goal_action(session_id: str, body: GoalAction, request: Request):
    from ._shared import _delegated_session_lock
    async with _delegated_session_lock(f'goal-control:{session_id}'):
        return await _apply_goal_action(session_id, body, request)


async def _apply_goal_action(session_id, body, request):
    session, scope = await scoped_session(request, session_id, body.workspace_id, native=body.action in {'resume', 'guide', 'answer'})
    goal = public_goal(session)
    if not goal or goal['id'] != body.goal_id:
        raise HTTPException(409, '目标状态已变化，请刷新后重试')
    if goal['status'] == 'stopped' or (goal['status'] == 'completed' and body.action != 'answer'):
        raise HTTPException(409, '目标已经结束，请创建新目标')
    if body.action == 'review_policy':
        result = await change_goal(session_id, scope, goal['id'],
                                  auto_approve_reviews=body.auto_approve_reviews,
                                  max_auto_computations=body.max_auto_computations)
        return goal_response(session, result)
    if body.action == 'answer':
        from ._shared import _is_chat_session_busy
        from .goal_questions import parse_answer, resume_after_questions
        if body.answers is None and (not body.question_id or not body.note.strip()):
            raise HTTPException(422, '请选择问题并填写回答')
        live = goal_service.tasks.get(session_id)
        owned_running = live and not live.done() and goal['status'] == 'running'
        if (ServerState.is_running(session_id) or _is_chat_session_busy(session_id)) and not owned_running:
            raise HTTPException(409, '请等待当前普通聊天结束后补答，未修改回答')
        def answer(current):
            latest = public_goal(current)
            if not latest or latest['id'] != body.goal_id or latest['status'] == 'stopped':
                raise HTTPException(409, '目标已变化，请刷新后重试')
            answers = body.answers if body.answers is not None else {body.question_id: body.note.strip()}
            questions = {q['id']: q for q in latest.get('questions', [])}
            if not answers or not set(answers).issubset(questions):
                raise HTTPException(404, '问题不存在或不属于当前目标')
            required = {q['id'] for q in questions.values() if q['status'] == 'pending' and q['required']}
            if not required.issubset(answers):
                raise HTTPException(422, '请完成当前问题组的全部必填题，再统一提交')
            parsed = []
            for question_id, raw in answers.items():
                question = questions[question_id]
                if not raw.strip() and not question['required']:
                    if question['status'] == 'pending':
                        parsed.append((question, '', {'type':'custom', 'answer':''}, ''))
                    continue
                try:
                    value, details = parse_answer(question, raw.strip())
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                if question['status'] == 'answered':
                    if question['answer'] != value and question.get('raw_answer', question['answer']) != raw.strip():
                        raise HTTPException(409, '此问题已回答，请通过聊天补充新的指导')
                    continue
                parsed.append((question, value, details, raw.strip()))
            if not parsed:
                return latest
            # All answers are validated before the first persisted change.
            for question, value, details, raw in parsed:
                question.update(status='answered', answer=value, raw_answer=raw,
                                answer_details=details, answered_at=time.time())
            if body.desktop_permission_mode is not None:
                latest['desktop_permission_mode'] = body.desktop_permission_mode
            resume_after_questions(latest, session_id, allow_completed=True)
            if latest['status'] == 'needs_input':
                if latest['deadline'] <= time.time() or latest['rounds_used'] >= latest['max_rounds']:
                    latest['reason'] = '回答已保存；轮次或时间预算已耗尽，请增加预算后继续'
                elif latest.get('waiting_questions') and any(q['status'] == 'pending' for q in latest.get('questions', [])):
                    latest['reason'] = '回答已保存；仍有未回答的问题，全部回答后继续'
            latest['updated_at'] = time.time()
            guidance = '\n'.join(f"用户补答：{q['payload']['question']}\n回答：{value}" for q, value, _, _ in parsed)
            record_goal_guidance(current, latest, guidance + '\n仅调整后续工作，不自动重跑计算；此回答不是工具授权。', acknowledge_questions=False)
            return latest
        result = await SessionManager.mutate(session_id, answer, **scope)
        await goal_service.start()
    elif body.action == 'guide':
        from ._shared import _is_chat_session_busy
        if not body.note.strip():
            raise HTTPException(422, '请填写补充指导')
        live = goal_service.tasks.get(session_id)
        if (ServerState.is_running(session_id) or _is_chat_session_busy(session_id)) and not (live and not live.done() and goal['status'] == 'running'):
            raise HTTPException(409, '请等待当前普通聊天结束后补充目标指导')
        if goal['deadline'] <= time.time() or goal['rounds_used'] >= goal['max_rounds']:
            raise HTTPException(409, '预算已耗尽，请明确增加预算后继续；补充指导不会自动增加预算')
        def guide(current):
            latest = public_goal(current)
            if not latest or latest['id'] != body.goal_id or latest['status'] in {'completed', 'stopped'}:
                raise HTTPException(409, '目标状态已变化，请刷新后重试')
            if latest['status'] in {'paused', 'needs_input'}:
                latest.update(status='ready', reason='')
            latest['updated_at'] = time.time()
            record_goal_guidance(current, latest, body.note.strip())
            if body.desktop_permission_mode is not None:
                latest['desktop_permission_mode'] = body.desktop_permission_mode
            return latest
        result = await SessionManager.mutate(session_id, guide, **scope)
        await goal_service.start()
    elif body.action in {'pause', 'stop'}:
        state = 'paused' if body.action == 'pause' else 'stopped'
        await change_goal(session_id, scope, goal['id'], status=state, reason='用户暂停' if state == 'paused' else '用户停止')
        await goal_service.cancel_round(session_id)
        await SessionManager.mutate(session_id, flush_goal_guidance, **scope)
        result = await change_goal(session_id, scope, goal['id'], status=state)
    else:
        from ._shared import _is_chat_session_busy
        if ServerState.is_running(session_id) or _is_chat_session_busy(session_id):
            raise HTTPException(409, '请等待当前普通聊天运行结束后继续目标')
        if goal['status'] in ACTIVE and goal['status'] != 'recovering':
            raise HTTPException(409, '目标仍在运行或等待已有计算，不需要重复继续')
        rounds = min(100, goal['max_rounds'] + body.extra_rounds)
        deadline = max(time.time(), goal['deadline']) + body.extra_minutes * 60 if body.extra_minutes else goal['deadline']
        if rounds <= goal['rounds_used'] or deadline <= time.time():
            raise HTTPException(409, '预算已耗尽，请明确增加轮次或时间预算后再继续')
        result = await change_goal(session_id, scope, goal['id'], status='ready', reason='', max_rounds=rounds,
                                   deadline=deadline, next_step=body.note.strip() or goal.get('next_step', ''), guidance_note=body.note.strip(),
                                   **({'desktop_permission_mode': body.desktop_permission_mode} if body.desktop_permission_mode is not None else {}))
        await goal_service.start()
    return goal_response(session, result)
