"""Opt-in, durable goal supervision over the existing native execution entry.

No second tool executor or workflow scheduler: each bounded round uses the same
native harness, workflow admission and runtime records as an ordinary turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

from ..runtime.task_runtime import TaskRuntimeStore
from ..session.manager import SessionManager
from .state import ServerState

ACTIVE = {'ready', 'running', 'waiting_external', 'recovering'}


def public_goal(session):
    raw = (session.metadata or {}).get('goal_mode') if session else None
    if not isinstance(raw, dict) or not all(key in raw for key in ('id','status','objective','criteria','deadline','updated_at','rounds_used','max_rounds','steps_per_round')):
        return None
    if not isinstance(raw['id'], str) or not raw['id'] or not isinstance(raw['status'], str):
        return None
    if any(not isinstance(raw[key], (int, float)) or not math.isfinite(raw[key]) for key in ('deadline','updated_at','rounds_used','max_rounds','steps_per_round')):
        return None
    return deepcopy(raw)


def goal_message_verifications(session, messages):
    """Project the durable goal verdict onto its final reply, without rewriting history."""
    goal = public_goal(session)
    if not goal or goal['status'] not in {'completed', 'needs_input'} or not isinstance(goal.get('validation'), dict) or not goal['validation']:
        return {}
    bound_id = goal.get('validation_message_id')
    # Historical completed goals predate this binding; never infer an old
    # verdict for a failed/interrupted newer round.
    if not bound_id and goal['status'] != 'completed':
        return {}
    final = None
    in_round = False
    for message in messages:
        metadata = message.metadata or {}
        if message.role == 'user':
            if metadata.get('goal_id') == goal['id'] and metadata.get('purpose') == 'goal_continuation':
                in_round = True
                final = None
            else:
                in_round = False
        elif (in_round and message.role == 'assistant' and message.id and message.content
              and not message.tool_calls and not metadata.get('internal') and message.presentation != 'commentary'):
            final = message
    if final is None or (bound_id and bound_id != final.id) or (final.verification or {}).get('superseded'):
        return {}
    return {final.id: deepcopy(goal['validation'])}


def owns_async_result(session, issue):
    from ._shared import _extract_async_issue_resume_data
    origin = _extract_async_issue_resume_data(issue).get('origin_request_id')
    if str(origin or '').strip().lower() in {'', 'none', 'null', 'undefined'}:
        origin = ''
    if not origin and isinstance(issue.get('review_data'), dict):
        origin = str(issue['review_data'].get('origin_request_id') or '').strip()
        if origin.lower() in {'none', 'null', 'undefined'}:
            origin = ''
    # Keep stopped goals fenced even after their visible card is replaced.
    metadata = (session.metadata or {}) if session else {}
    if origin and origin in metadata.get('stopped_goal_run_ids', []):
        return True
    goal = public_goal(session)
    if not goal or goal['status'] == 'completed':
        return False
    if origin:
        return origin in goal.get('run_ids', [])
    if goal['status'] in ACTIVE:
        return True
    # Old computations may finish after Stop, but must not restart the old goal.
    # New ordinary-mode jobs retain the original automatic resume behavior.
    timestamp = issue.get('created_at') or issue.get('createdAt')
    created = TaskRuntimeStore._timestamp_seconds(timestamp)
    if not created and isinstance(timestamp, str):
        try:
            created = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return created <= goal['updated_at']


async def claim_ordinary_turn(session_id, scope, request_id, task):
    """Serialize goal controls and ordinary turn admission, not execution."""
    from ._shared import _delegated_session_lock
    async with _delegated_session_lock(f'goal-control:{session_id}'):
        goal = public_goal(await SessionManager.get(session_id, **scope))
        if goal and goal['status'] in ACTIVE:
            return 'goal_mode_active'
        return ServerState.claim_chat_turn(session_id, request_id, task)


def owner(session):
    return {key: getattr(session, key) for key in ('tenant_id', 'user_id', 'workspace_id')}


def flush_goal_guidance(session):
    from ..types import Message
    goal = (session.metadata or {}).get('goal_mode') or {}
    ids = {message.id for message in session.messages}
    for item in goal.get('guidance_messages') or []:
        if item['id'] not in ids:
            session.messages.append(Message(role='user', content=item['content'], id=item['id'],
                                           metadata={'goal_id': goal['id'], 'purpose': 'goal_guidance'}))
    goal['guidance_messages'] = []


def acknowledge_goal_questions(session, goal):
    """Only chat clarification calls belonging to this goal can be answered by guidance."""
    calls = set()
    in_goal = False
    for message in session.messages:
        if message.role == 'user':
            in_goal = (message.metadata or {}).get('goal_id') == goal['id']
        if in_goal:
            calls.update(call.call_id for call in (message.tool_calls or []) if call.tool_name == 'question')
    goal['answered_question_calls'] = list(set(goal.get('answered_question_calls') or []) | calls)
    session.metadata['answered_goal_question_calls'] = list(set(session.metadata.get('answered_goal_question_calls') or []) | set(goal['answered_question_calls']))


def record_goal_guidance(session, goal, note, *, acknowledge_questions=True):
    goal['guidance'] = [*(goal.get('guidance') or []), note][-8:]
    goal['guidance_messages'] = [*(goal.get('guidance_messages') or []),
                                 {'id': 'msg-' + uuid.uuid4().hex, 'content': note}]
    goal['guidance_version'] = goal.get('guidance_version', 0) + 1
    if acknowledge_questions:
        acknowledge_goal_questions(session, goal)
    session.metadata['goal_mode'] = goal
    if goal['status'] != 'running':
        flush_goal_guidance(session)


async def change_goal(session_id, scope, goal_id, *, expected_updated_at=None, expected_status=None, consumed_guidance_version=None, resume_on_guidance=False, guidance_note='', guidance_acknowledges_questions=True, **changes):
    def mutate(session):
        goal = (session.metadata or {}).get('goal_mode')
        if not goal or goal['id'] != goal_id:
            return None
        if expected_updated_at is not None and goal.get('updated_at') != expected_updated_at:
            return None
        if expected_status is not None and goal.get('status') != expected_status:
            return None
        if ((changes.get('status') == 'completed' or (changes.get('status') == 'needs_input' and resume_on_guidance)) and consumed_guidance_version is not None
                and goal.get('guidance_version', 0) != consumed_guidance_version):
            changes.update(status='ready', validation=None, reason='收到新指导，将在下一轮处理后重新验收')
            acknowledge_goal_questions(session, goal)
        goal.update(changes, updated_at=time.time())
        session.metadata['goal_mode'] = goal
        if guidance_note:
            record_goal_guidance(session, goal, guidance_note, acknowledge_questions=guidance_acknowledges_questions)
        return deepcopy(goal)
    return await SessionManager.mutate(session_id, mutate, **scope)


def waiting_state(session, *, ignore_goal_questions=False):
    records = TaskRuntimeStore.list(session_id=session.id, workdir=session.workdir)
    # Compatibility with persisted query records written before terminal async
    # results took precedence over the historical accepted_async flag.
    from ..runtime.task_runtime import task_runtime_status_for_tool_result
    from ..types import ToolResult
    for row in records:
        result = (row.get('metadata') or {}).get('result_metadata') or {}
        if row.get('runtime_kind') == 'agent_tool' and row.get('phase') == 'finalize' and row.get('status') == 'waiting_external' and result.get('status') in {'completed', 'failed', 'cancelled', 'canceled', 'terminated'}:
            row['status'] = task_runtime_status_for_tool_result(ToolResult(title='', output='', metadata=result, error=(row.get('metadata') or {}).get('error_preview') or None))
    metadata = session.metadata or {}
    answered = set(metadata.get('answered_goal_question_calls') or []) | set((metadata.get('goal_mode') or {}).get('answered_question_calls') or [])
    questions = (metadata.get('goal_mode') or {}).get('questions') or []
    answered.update(q['call_id'] for q in questions if q.get('call_id')
                    and q.get('status') in {'answered', 'defaulted'})
    # A batch can share one tool call: an unanswered sibling still blocks it.
    if ignore_goal_questions:
        answered.update(q['call_id'] for q in questions if q.get('call_id'))
    else:
        answered.difference_update(q['call_id'] for q in questions if q.get('call_id')
                                  and q.get('status') == 'pending')
    statuses = {row.get('status') for row in records if row.get('runtime_kind') != 'tool'
                and not (row.get('status') == 'waiting_user' and row.get('tool_name') == 'question' and row.get('external_id') in answered)}
    if statuses & {'waiting_user', 'waiting_review'}:
        return 'needs_input'
    if statuses & {'queued', 'running', 'waiting_external', 'collecting'}:
        return 'waiting_external'
    return ''


class GoalModeService:
    def __init__(self):
        self.loop = None
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(2)

    async def start(self):
        if self.loop and not self.loop.done():
            return
        # Recover persisted goals; ambiguous interrupted actions require human inspection.
        for session in await SessionManager.list(include_archived=False):
            goal = public_goal(session)
            if goal and goal['status'] == 'running':
                await change_goal(session.id, owner(session), goal['id'], expected_updated_at=goal['updated_at'], status='recovering', reason='服务重启，正在检查既有运行记录')
        self.loop = asyncio.create_task(self._poll(), name='goal-mode-supervisor')

    async def stop(self):
        tasks = list(self.tasks.values())
        if self.loop:
            self.loop.cancel()
            tasks.append(self.loop)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.loop = None

    async def _poll(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # A malformed/deleted session must not kill supervision of other goals.
                import logging
                logging.getLogger(__name__).exception('Goal supervision tick failed')
            await asyncio.sleep(2)

    async def tick(self):
        for session in await SessionManager.list(include_archived=False):
            goal = public_goal(session)
            if goal:
                from .goal_tool_review import dispatch_reviews
                await dispatch_reviews(session)
                from .goal_computation import dispatch_approved_computations
                await dispatch_approved_computations(session)
                session = await SessionManager.get(session.id, **owner(session))
                goal = public_goal(session)
            if goal and goal.get('questions'):
                from .goal_questions import expire_questions
                await expire_questions(session, goal)
                session = await SessionManager.get(session.id, **owner(session))
                goal = public_goal(session)
            if not goal or goal['status'] not in ACTIVE or session.id in self.tasks:
                continue
            if time.time() >= goal['deadline'] or goal['rounds_used'] >= goal['max_rounds']:
                await change_goal(session.id, owner(session), goal['id'], expected_updated_at=goal['updated_at'], status='budget_exhausted', reason='目标模式预算已耗尽')
                continue
            waiting = waiting_state(session)
            if waiting:
                await change_goal(session.id, owner(session), goal['id'], expected_updated_at=goal['updated_at'], status=waiting, reason='等待已有计算或用户审批，不重复提交')
                continue
            if goal['status'] == 'recovering':
                open_tools = [row for row in TaskRuntimeStore.list(session_id=session.id, workdir=session.workdir)
                              if row.get('runtime_kind') == 'tool' and row.get('status') in {'running', 'queued'}]
                if open_tools or (session.metadata or {}).get('active_turn'):
                    await change_goal(session.id, owner(session), goal['id'], expected_updated_at=goal['updated_at'], status='needs_input', reason='上轮工具结果不明确，请检查运行记录后再继续，避免重复执行')
                    continue
            if ServerState.is_running(session.id):
                continue
            task = asyncio.create_task(self._round(session, goal), name=f'goal-round-{session.id}')
            self.tasks[session.id] = task
            task.add_done_callback(lambda _, sid=session.id: self.tasks.pop(sid, None))

    async def cancel_round(self, session_id):
        task = self.tasks.get(session_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _round(self, session, goal):
        scope = owner(session)
        run_id = 'goal-round-' + uuid.uuid4().hex
        task = asyncio.current_task()
        async with self.semaphore:
            fresh = await SessionManager.get(session.id, **scope)
            latest = public_goal(fresh)
            if not latest or SessionManager.is_archived_session(fresh) or latest['id'] != goal['id'] or latest['status'] not in ACTIVE:
                return
            goal = latest
            if ((fresh.metadata or {}).get('harness') or {}).get('runtime_id', 'tcode-native') != 'tcode-native':
                await change_goal(session.id, scope, goal['id'], expected_updated_at=goal['updated_at'], status='needs_input', reason='运行时已切换，请切回 TCode 原生模式后继续目标')
                return
            remaining = goal['deadline'] - time.time()
            if remaining <= 0 or goal['rounds_used'] >= goal['max_rounds']:
                await change_goal(session.id, scope, goal['id'], expected_updated_at=goal['updated_at'], status='budget_exhausted', reason='目标模式预算已耗尽')
                return
            if ServerState.claim_chat_turn(session.id, run_id, task) != 'accepted':
                return
            agent = None
            previous = None
            checkpoint_token = None
            checkpoint = {}
            blocked = []
            permission_blocked = []
            authorization_blocked = []
            try:
                started = await change_goal(session.id, scope, goal['id'], expected_updated_at=goal['updated_at'], status='running', rounds_used=goal['rounds_used'] + 1,
                                  run_ids=(goal.get('run_ids', []) + [run_id])[-100:])
                if not started:
                    return
                async with asyncio.timeout(remaining):
                    from ..runtime.agent_backend import NormalizedRunInput, NormalizedMessage, RunContext
                    from .native_execution import open_native_agent, run_native_turn
                    from ..tools.builtin.goal_checkpoint import goal_checkpoint_active
                    from .goal_tool_review import approval_context
                    from .goal_questions import defer_question, pending_questions, question_context, PREFERENCE_QUESTIONS
                    checkpoint_token = goal_checkpoint_active.set(True)
                    prompt = '\n'.join([
                        '这是用户显式开启的目标模式。仅推进当前目标，不自行扩大权限或降低验收标准。',
                        f"目标：{goal['objective']}", f"完成条件：{goal['criteria']}",
                        f"人工审核设置：{'工具 review 自动通过，不要重复请求人工确认' if goal.get('auto_approve_reviews') else '逐项人工审核，待审时等待用户处理'}；自动计算提交上限 {goal.get('max_auto_computations', 3)}，已自动批准 {len(goal.get('auto_approved_review_ids') or [])} 项。硬性安全拒绝与预算限制始终有效。",
                        f"已有进展：{goal.get('progress', '')}", f"下一步：{goal.get('next_step', '')}",
                        'Human Review 已批准且尚未执行的本地操作（只允许原工具和原参数一次；需要时按原参数调用，变更或重试须重新审核）：' + approval_context(fresh, goal),
                        f"已批准计算的现有队列编号（仅查询和复用，不得重提）：{[entry.get('identifier') for entry in (goal.get('computation_reviews') or {}).values() if entry.get('identifier')]}",
                        f"上次目标校验待处理事项：{str((goal.get('validation') or {}).get('failed_criteria') or '')[:2000]}",
                        '用户补充指导（按顺序）：\n' + '\n'.join(goal.get('guidance') or []),
                        '保留的问题及回答（默认值不是用户确认）：\n' + question_context(goal),
                        f'允许超时默认的宿主报告展示问题模板（question 必须原样匹配相应 preference 的一种）：{PREFERENCE_QUESTIONS}。自由措辞、输入文件格式或科学事实即使标成 preference 也不能默认。',
                        '问题会持久保留，提问后可继续做不依赖答案且已授权的工作。未知关键事实及计算授权必须等待用户；不得用偏好问题传递或默认批准它们。仅语言、篇幅、展示格式可用 question(required=false, preference=language/verbosity/format, default_answer=明确展示偏好, timeout_seconds=60) 等待后暂用默认值；关键输入用 required=true。不重复询问已保留的问题；仅等待这些问题且无独立工作时报告 wait；另有能力、证据或授权阻塞时报告 blocked。迟到回答只调整后续工作，不自动重跑计算。',
                        '像聊天一样解释当前进展；目标或完成条件不明确时先提出具体问题。仅等待已保留的问题时报告 wait；另有阻塞时报告 blocked。补充指导不授予新权限，也不自动改变目标或降低验收条件；实质变更先确认。',
                        '主动判断关键输入是否齐全，不依赖用户要求你提问。先检查已有聊天、工作区和结果，不重复询问已知信息。缺少会影响研究方向、安全或计算参数的信息（如起始序列、目标功能、目标温度或实验数据）时，在执行相关计算前集中提出必要问题，不猜造输入。语言、篇幅等非关键偏好采用用户上下文中的合理默认值，不为此阻塞任务。',
                        '先区分任务是解释已有结果、分析已有文件还是请求新计算。按任务核对起始蛋白（序列或结构及链）、配体或复合物、研究问题、评价指标和必要实验条件；并非每个任务都要所有输入。只问会改变研究结论、方向、安全或计算参数的缺口，说明已知信息与每个问题的用途。关键事实用 question（required=true）集中询问，不用默认答案或推荐值代替未知序列、配体、条件、阈值或实验数据。',
                        '模型轮次和时间预算不是计算次数、费用或重跑授权。优先复用已有运行；确需新计算时先说明目的、输入、方法、完整参数及资源成本（未知必须说明），只能通过 job-protocol submit_job 发起人工审核申请，每项只批准一次提交。审核前不会执行；重跑或参数变化须新审核。申请后等待，不自行轮询或重提；结果返回后继续目标。不能用 Shell、直连工具、Workflow 或子代理绕过审核。',
                        '本地执行或读取若需审核，会加入现有 Human Review；等待用户在结果审查中处理，无需切回普通模式。恢复后仅可按获批工具的精确参数执行一次，不能据此启动其他计算。',
                        '提交入口使用扁平的 input_config：target_server、target_tool 和工具参数同层，例如 {"input_config":{"target_server":"simple-demo-mcp","target_tool":"echo_text","text":"hello"}}，不要再包 arguments 或附加 workspace_id。参数不确定先 describe_target_tool，fields 指定真实参数名；契约不通过先纠正，不重复申请无效参数。',
                        '能力缺失时先用 capability_catalog 查找现有能力；仍不可用则停止并报告缺少什么、已完成什么和可行下一步，不安装、不改服务、不猜造结果。产物缺失、授权被拒、证据不足或实验未做时，保留已有证据并报告 blocked，不能将计算成功当成研究目标达成；只依据现有证据明确局限的解释任务不必凭空要求新实验。',
                        '优先复用已有 Run、Workflow 和已返回结果；不得重复提交已有计算。',
                        '每轮结束前发现并调用 goal_checkpoint，报告 progress、next_step 和 state。',
                        '需要审批、无法取得实验数据或其他独立阻塞时报告 blocked；仅等待已保留问题的回答或已有异步任务时报告 wait。',
                        '只有所有完成条件已有证据时才报告 complete；预测不等于实验验证。',
                        '本轮完成后给用户简短汇报。不要创建定时任务或另一个持续目标。',
                    ])
                    context = RunContext(**scope, workdir=session.workdir, session_id=session.id, run_id=run_id,
                                         request_id=run_id, agent_id=session.agent_name or 'build', runtime_id='tcode-native')
                    selected_model = str((fresh.metadata.get('harness') or {}).get('model_id') or fresh.metadata.get('model') or '')
                    snapshot_id = fresh.metadata.get('config_snapshot_id')
                    if selected_model and snapshot_id:
                        from ..config.task_snapshot import TaskConfigSnapshotStore
                        snapshot = TaskConfigSnapshotStore.get(snapshot_id, session.workdir)
                        if snapshot.runtime_config.get('model') != selected_model:
                            snapshot = TaskConfigSnapshotStore.capture(session.workdir, task_id=session.id, force=True,
                                                                       runtime_overrides={'model': selected_model})
                            def bind_snapshot(current):
                                current.metadata['config_snapshot_id'] = snapshot.snapshot_id
                            await SessionManager.mutate(session.id, bind_snapshot, **scope)
                    run_input = NormalizedRunInput(thread_id=session.id, run_id=run_id, agent_id=context.agent_id,
                                                   model_id=selected_model,
                                                   messages=[NormalizedMessage(id=run_id, role='user', content=prompt)])
                    await SessionManager.mutate(session.id, flush_goal_guidance, **scope)
                    agent, _ = await open_native_agent(run_input, context)
                    previous = (agent.agent_info, getattr(agent, '_permission_handler', None), getattr(agent, '_question_handler', None),
                                getattr(agent, '_agent_event_emitter', None),
                                deepcopy(getattr(agent, '_next_user_message_metadata', None)),
                                getattr(agent, '_next_user_message_id', ''),
                                getattr(agent, '_goal_tool_routing_prompt', ''),
                                getattr(agent, '_goal_authorization_blocked_callback', None),
                                getattr(agent, '_goal_question_callback', None),
                                getattr(agent, '_review_read_permissions', False),
                                getattr(agent, '_goal_risk_reviewer', None))
                    from .goal_biosafety import goal_risk_reviewer
                    agent._goal_risk_reviewer = goal_risk_reviewer(session=session, goal=goal, provider=agent.provider, owner=scope)
                    if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
                        agent._review_read_permissions = goal.get('desktop_permission_mode', 'ask') == 'ask'
                    permission = dict(agent.agent_info.permission or {})
                    permission['scheduled_task'] = 'deny'
                    steps = min(agent.agent_info.steps or goal['steps_per_round'], goal['steps_per_round'])
                    agent.agent_info = replace(agent.agent_info, permission=permission, steps=steps)
                    agent._goal_checkpoint_callback = lambda payload: checkpoint.update(payload)
                    # Latch outside the transcript: compaction must not erase a denial.
                    agent._goal_authorization_blocked_callback = lambda: authorization_blocked.append(True)
                    async def goal_question(payload, call_id):
                        return await defer_question(session.id, scope, goal['id'], payload, call_id)
                    agent._goal_question_callback = goal_question
                    agent._goal_tool_routing_prompt = prompt
                    async def permission_handler(name, arguments):
                        permission_blocked.append(True)
                        blocked.append('当前操作需要用户审批，请切回普通模式处理后再继续目标')
                        return False
                    async def question_handler(payload):
                        blocked.append(str(payload.get('question') or '需要用户补充输入'))
                        return ''
                    async def discard(_): pass
                    agent.set_permission_handler(permission_handler)
                    agent.set_question_handler(question_handler)
                    agent.set_agent_event_emitter(None)
                    agent.set_next_user_message_context(metadata={'internal': True, 'purpose': 'goal_continuation', 'goal_id': goal['id']})
                    round_message_start = len(agent.messages)
                    await run_native_turn(agent=agent, run_input=run_input, context=context,
                                          question_handler=question_handler, emit_message=discard, emit_state=discard,
                                          discover_workflows=goal['rounds_used'] == 0)
                    # Preserve transcript boundaries without exposing the internal control prompt.
                    def hide_prompt(persisted):
                        for message in persisted.messages:
                            if message.role == 'user' and str(message.content or '') == prompt:
                                message.presentation = 'async_hidden_prompt'
                    await SessionManager.mutate(session.id, hide_prompt, **scope)
                    current = await SessionManager.get(session.id, **scope)
                    state = waiting_state(current)
                    validation = None
                    validation_message_id = ''
                    validation_failed = False
                    validation_reason = '目标完成条件尚未通过校验，请检查结果或补充证据后继续'
                    confirmation_required = bool(authorization_blocked) or any(
                        (call.metadata or {}).get('goal_action_confirmation_required')
                        for message in agent.messages[round_message_start:]
                        for call in (message.tool_calls or [])
                    )
                    if confirmation_required:
                        permission_blocked.append(True)
                        blocked.append('新增计算或启动执行需要单独确认；请切回普通模式审批执行，再继续目标。未提交本次计算，轮次／时间预算和补充指导不代表计算授权。')
                    if checkpoint.get('state') == 'blocked':
                        blocked.append(str(checkpoint.get('next_step') or '').strip()
                                       or str(checkpoint.get('progress') or '').strip()
                                       or '需要补充关键输入、能力或证据后继续')
                    questions = pending_questions(public_goal(current) or {})
                    waiting_questions = bool(questions) and not blocked and not waiting_state(current, ignore_goal_questions=True)
                    if questions:
                        state = 'waiting_external' if state == 'waiting_external' and not blocked else 'needs_input'
                    elif blocked:
                        state = 'needs_input'
                    elif not state and checkpoint.get('state') == 'complete':
                        from ..guard.outcome_guard import validate_outcome
                        from ..runtime.evidence_ledger import EvidenceLedger
                        from ..task.outcome_contract import OutcomeContract
                        criteria = [goal['criteria']]
                        if goal.get('guidance'):
                            criteria.append('同时核对以下用户补充指导中与原目标一致的细化要求；不得据此取消原完成条件、降低证据标准或扩大权限。冲突或实质目标变更须等待用户确认，不能直接验收通过：\n' + '\n'.join(goal['guidance']))
                        contract = OutcomeContract.compile(goal['objective'], supplied={
                            'goal': goal['objective'], 'acceptance_criteria': criteria,
                            'schema_version': 'tcode.outcome/v2', 'semantic_validation': 'always',
                            'max_repair_attempts': 0,
                        })
                        # Validate the actual multi-round deliverable, not just the final checkpoint summary.
                        outputs = []
                        goal_messages = []
                        in_goal = False
                        for message in agent.messages:
                            if message.role == 'user':
                                in_goal = (message.metadata or {}).get('goal_id') == goal['id']
                            if in_goal:
                                goal_messages.append(message)
                            if in_goal and message.role == 'assistant' and message.content:
                                outputs.append(str(message.content))
                        candidate = '\n\n'.join(outputs)
                        from .auth import session_result_verification_settings
                        verification_settings = await session_result_verification_settings(session)
                        result = await validate_outcome(candidate, contract, EvidenceLedger.from_messages(goal_messages),
                                                        provider=agent.provider, workspace_root=session.workdir,
                                                        verification_enabled=verification_settings["enabled"],
                                                        verification_mode=verification_settings["mode"], usage_session=session)
                        validation = result.to_dict()
                        validation_message_id = next((message.id for message in reversed(agent.messages[round_message_start:])
                            if message.role == 'assistant' and message.content and not message.tool_calls
                            and message.presentation != 'commentary'), '')
                        if validation.get('validator') == 'semantic_unavailable':
                            validation_reason = '验收服务未返回有效结论，目标尚未验收；无需重复提交计算，请检查模型服务后再继续'
                            if any('finish_reason=length' in str(warning) for warning in validation.get('warnings', [])):
                                validation_reason = '验收模型达到单次输出上限，未返回有效结论；这不是目标轮次预算耗尽，也不代表结果不合格。请检查验收模型配置后再继续'
                        state = 'completed' if getattr(result, 'advisory', False) or (result.verdict == 'pass' and result.goal_status == 'SATISFIED') else 'needs_input'
                        validation_failed = state != 'completed'
                    elif not state:
                        routed_workflow = any('workflow' in str(row.get('runtime_kind')) for row in TaskRuntimeStore.list(session_id=session.id, workdir=session.workdir))
                        state = 'ready' if checkpoint.get('state') == 'continue' or (not checkpoint and routed_workflow) else 'needs_input'
                    progress = str(checkpoint.get('progress') or '')
                    next_step = str(checkpoint.get('next_step') or '')
                    fingerprint = hashlib.sha256((progress + '\n' + next_step).encode()).hexdigest()
                    repeated = goal.get('no_progress_rounds', 0) + 1 if fingerprint == goal.get('progress_fingerprint') else 0
                    if state == 'ready' and repeated >= 2:
                        state = 'needs_input'
                        blocked.append('连续多轮没有新进展，已停止自动重试')
                    await change_goal(session.id, scope, goal['id'], expected_status='running', consumed_guidance_version=goal.get('guidance_version', 0), resume_on_guidance=not permission_blocked and not questions, status=state, progress=progress, next_step=next_step, waiting_questions=waiting_questions,
                                      progress_fingerprint=fingerprint, no_progress_rounds=repeated, validation=validation, validation_message_id=validation_message_id,
                                      reason=blocked[0] if blocked else ('问题已保留，等待回答或非关键偏好超时；关键输入不会自动确认' if questions else (validation_reason if validation_failed else ('请检查结果或补充信息后继续' if state == 'needs_input' else ''))))
            except TimeoutError:
                exhausted = time.time() >= goal['deadline']
                await change_goal(session.id, scope, goal['id'], expected_status='running', status='budget_exhausted' if exhausted else 'needs_input',
                                  reason='目标模式时间预算已耗尽' if exhausted else '本轮调用超时，请检查已有结果后继续；时间预算未清零')
            except asyncio.CancelledError:
                latest = public_goal(await SessionManager.get(session.id, **scope))
                if latest and latest['id'] == goal['id'] and latest['status'] == 'running':
                    await change_goal(session.id, scope, goal['id'], expected_status='running', status='recovering', reason='运行被中断，恢复前将检查已有记录')
                raise
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Goal round failed')
                await change_goal(session.id, scope, goal['id'], expected_status='running', status='needs_input', reason='本轮未完成，请查看聊天与运行记录后继续')
            finally:
                if agent and previous:
                    agent.agent_info = previous[0]
                    agent.set_permission_handler(previous[1])
                    agent.set_question_handler(previous[2])
                    agent.set_agent_event_emitter(previous[3])
                    agent._next_user_message_metadata = previous[4]
                    agent._next_user_message_id = previous[5]
                    agent._goal_tool_routing_prompt = previous[6]
                    agent._goal_checkpoint_callback = None
                    agent._goal_authorization_blocked_callback = previous[7]
                    agent._goal_question_callback = previous[8]
                    agent._review_read_permissions = previous[9]
                    agent._goal_risk_reviewer = previous[10]
                    # A later ordinary async resume must not inherit this goal's origin.
                    if getattr(agent, '_active_client_request_id', '') == run_id:
                        agent._active_client_request_id = ''
                if checkpoint_token is not None:
                    goal_checkpoint_active.reset(checkpoint_token)
                ServerState.finish_chat_turn(session.id, task)


goal_service = GoalModeService()
