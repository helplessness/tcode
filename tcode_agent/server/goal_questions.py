"""Durable, goal-owned clarification; never a substitute for tool approval."""
import json
import time
import uuid
from copy import deepcopy

from ..session.manager import SessionManager
from ..types import ToolResult


# Only presentation preferences can expire. required=False alone grants nothing.
PREFERENCE_DEFAULTS = {
    'language': {'中文', '英文', 'Chinese', 'English', 'zh', 'en'},
    'verbosity': {'简洁', '标准', '详细', 'concise', 'standard', 'detailed'},
    'format': {'Markdown', 'markdown', '纯文本', 'text', '表格', 'table'},
}
PREFERENCE_QUESTIONS = {
    'language': ('报告使用哪种语言？', 'Which language should the report use?'),
    'verbosity': ('报告需要多详细？', 'How detailed should the report be?'),
    'format': ('报告采用哪种展示格式？', 'Which presentation format should the report use?'),
}


def can_default(question):
    """Only host-defined report presentation questions, not model-labelled facts."""
    payload = question['payload']
    preference = payload.get('preference')
    default = question.get('default_answer', '')
    return (question.get('required') is False
            and payload.get('type') != 'batch' and not payload.get('questions')
            and payload.get('question') in PREFERENCE_QUESTIONS.get(preference, ())
            and default in PREFERENCE_DEFAULTS.get(preference, set())
            and not payload.get('confirmation_option')
            and not (payload.get('metadata') or {}).get('kind')
            and (payload.get('allow_custom') is not False or default in (payload.get('options') or [])))


def parse_answer(question, answer):
    """Keep the existing Question choice semantics, including numbered selections."""
    from ..tools.builtin.question import BatchQuestionItem, QuestionTool
    payload = question['payload']
    options = payload.get('options') or []
    tool = QuestionTool()
    if not answer and payload.get('required') is False:
        return '', tool._process_custom_answer('')
    if payload.get('type') == 'batch':
        items = [BatchQuestionItem(**item) for item in payload.get('questions') or []]
        if not items:
            raise ValueError('问题组内容缺失，请让 Agent 重新提出完整问题组')
        result = tool._process_batch_answer(answer, items)
        value = result['answer']
    elif payload.get('type') == 'single' and options:
        result = tool._process_single_choice(answer, options, payload.get('allow_custom') is not False)
        value = result['selected_option']
    elif payload.get('type') == 'multiple' and options:
        if payload.get('allow_custom') is False:
            # The shared parser is intentionally lenient; a critical goal reply
            # must not silently discard even one invalid/empty selection.
            parts = [part.strip() for part in answer.split(',')]
            if not all(part and (
                    1 <= int(part) <= len(options) if part.isdigit()
                    else part.lower() in {option.strip().lower() for option in options}
            ) for part in parts):
                raise ValueError('回答包含无效或空的选项，请检查全部选择后重试')
        result = tool._process_multiple_choice(answer, options, payload.get('allow_custom') is not False)
        value = json.dumps(result['selected_options'], ensure_ascii=False) if result['selected_options'] else None
    else:
        result = tool._process_custom_answer(answer)
        value = result['answer']
    if not value:
        raise ValueError('回答不匹配可选项，请选择有效选项后重试')
    return value, result


def resume_after_questions(goal, session_id, *, allow_completed=False):
    from .state import ServerState
    from ._shared import _is_chat_session_busy
    if (goal['deadline'] > time.time() and goal['rounds_used'] < goal['max_rounds']
            and not pending_questions(goal)
            and ((goal['status'] == 'needs_input' and goal.get('waiting_questions'))
                 or (allow_completed and goal['status'] == 'completed'))
            and not ServerState.is_running(session_id) and not _is_chat_session_busy(session_id)):
        goal.update(status='ready', reason='', waiting_questions=False, validation=None)
        return True
    return False


def pending_questions(goal):
    return [q for q in goal.get('questions', []) if q['status'] == 'pending']


def question_context(goal):
    return '\n'.join(
        f"问题：{q['payload']['question']}；状态：{q['status']}；"
        f"选项：{json.dumps(q['payload'].get('options') or [], ensure_ascii=False)}；"
        + (f"独立问题：{json.dumps(q['payload']['questions'], ensure_ascii=False)}；"
           if q['payload'].get('questions') else '')
        + f"{'用户回答' if q['status'] == 'answered' else '待用户回答' if q['status'] == 'pending' else '暂用默认值'}：{q.get('answer', '')}"
        for q in goal.get('questions', [])
    )


async def defer_question(session_id, scope, goal_id, payload, call_id):
    from .goal_mode import public_goal
    default = str(payload.get('default_answer') or '').strip()
    preference = payload.get('preference')
    optional = can_default({'payload':payload, 'default_answer':default, 'required':payload.get('required')})
    # The host, not the model, bounds the waiting window.
    timeout = max(30, min(300, int(payload.get('timeout_seconds') or 60)))

    def mutate(session):
        goal = public_goal(session)
        if not goal or goal['id'] != goal_id or goal['status'] != 'running':
            raise ValueError('目标已暂停、停止或替换，未创建问题')
        questions = goal.setdefault('questions', [])
        existing = next((q for q in questions if q['payload']['question'] == payload['question']
                         and q['payload'].get('preference') == preference
                         and q['required'] == (not optional)
                         and (q['payload'].get('type') or 'custom') == (payload.get('type') or 'custom')
                         and (q['payload'].get('options') or []) == (payload.get('options') or [])
                         and (q['payload'].get('questions') or []) == (payload.get('questions') or [])
                         and (q['payload'].get('allow_custom') is not False) == (payload.get('allow_custom') is not False)
                         and (q['payload'].get('known_context') or '') == (payload.get('known_context') or '')
                         and (not optional or q.get('default_answer') == default)), None)
        if existing:
            return existing
        if len(questions) >= 20:
            raise ValueError('本目标的问题数量已达上限，请先整理现有问题')
        item = dict(id='question-' + uuid.uuid4().hex, status='pending', required=not optional,
                    created_at=time.time(), deadline=time.time() + timeout if optional else None,
                    default_answer=default if optional else '', answer='', call_id=call_id,
                    payload={key: deepcopy(payload.get(key)) for key in (
                        'question', 'type', 'options', 'allow_custom', 'known_context', 'recommendation', 'preference',
                        'questions') if key != 'questions' or payload.get('questions')})
        questions.append(item)
        session.metadata['goal_mode'] = goal
        return item

    item = await SessionManager.mutate(session_id, mutate, **scope)
    if not item:
        raise ValueError('当前目标不可访问，未创建问题')
    resolved = item['status'] in {'answered', 'defaulted'}
    return ToolResult(title='目标问题已保留', output=(
        f"{'用户回答' if item['status'] == 'answered' else '暂用默认值（非用户确认）'}：{item['answer']}"
        if resolved else '问题已保留，用户可稍后回答。只推进不依赖此答案且已授权的工作；'
        '不得猜测关键输入、视为计算批准或重复提交计算。仅等待这些问题且无独立工作时报告 wait；'
        '另有授权、能力或证据阻塞时才报告 blocked。'),
        metadata={'goal_question_id':item['id'], 'goal_question_pending':not resolved,
                  **(deepcopy(item.get('answer_details') or {}) if item['payload'].get('type') == 'batch' and resolved else {}),
                  'answer':item['answer'], 'answer_source':item['status'], 'should_end_turn':False})


async def expire_questions(session, goal):
    """Called by the existing supervisor, including while awaiting a preference."""
    from .goal_mode import public_goal, owner, record_goal_guidance
    if goal['status'] not in {'ready', 'running', 'needs_input', 'waiting_external', 'recovering'}:
        return
    now = time.time()
    if now >= goal['deadline'] or goal['rounds_used'] >= goal['max_rounds']:
        return
    recovery_due = goal['status'] == 'needs_input' and goal.get('waiting_questions') and not pending_questions(goal)
    if not recovery_due and not any(q['status'] in {'pending', 'defaulted'} and (
            (not q['required'] and not can_default(q))
            or (q['status'] == 'pending' and can_default(q) and q.get('deadline') and q['deadline'] <= now))
            for q in goal.get('questions', [])):
        return
    def mutate(current):
        latest = public_goal(current)
        if not latest or latest['id'] != goal['id'] or latest['status'] not in {'ready', 'running', 'needs_input', 'waiting_external', 'recovering'}:
            return
        if now >= latest['deadline'] or latest['rounds_used'] >= latest['max_rounds']:
            return
        expired = []
        hardened = False
        for question in latest.get('questions', []):
            if question['status'] in {'pending', 'defaulted'} and not question['required'] and not can_default(question):
                if question['status'] == 'defaulted':
                    question['previous_default'] = question.get('answer', '')
                question.update(required=True, deadline=None, default_answer='', answer='', status='pending')
                hardened = True
        for question in pending_questions(latest):
            if can_default(question) and question.get('deadline') and question['deadline'] <= now:
                question.update(status='defaulted', answer=question['default_answer'])
                expired.append(f"{question['payload']['question']}：超时暂用默认值 {question['answer']}（非用户确认，用户仍可补答）")
        resumed = resume_after_questions(latest, session.id)
        if not expired and not hardened and not resumed:
            return
        latest['updated_at'] = now
        if expired:
            record_goal_guidance(current, latest, '\n'.join(expired), acknowledge_questions=False)
        else:
            current.metadata['goal_mode'] = latest
    await SessionManager.mutate(session.id, mutate, **owner(session))
