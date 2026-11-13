"""Feishu decisions reuse owner-scoped Human Review and result-only resumes."""
import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime


def fingerprint(review):
    payload = {k: review.get(k) for k in
               ('id', 'identifier', 'tool_server', 'tool_name', 'tool_args', 'review_data', 'reason')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def reviews(backend, conversation):
    from .human_review_api import list_human_review_queue
    result = await list_human_review_queue(backend.request(conversation), limit=500,
                                         session_id=conversation['session_id'])
    return result['reviews']


async def decide(backend, conversation, review_id, expected, decision):
    from .human_review_api import approve_human_review, reject_human_review
    from .schemas import HumanReviewApproveRequest, HumanReviewRejectRequest
    current = next((r for r in await reviews(backend, conversation) if r['id'] == review_id), None)
    if not current or current['status'] != 'pending_review' or fingerprint(current) != expected:
        return '审批单已处理、已变更或不可访问，请查看最新审批状态。'
    # Both API handlers validate access and persist the decision before their first
    # await; desktop and Feishu therefore share the same pending-state gate.
    request = backend.request(conversation)
    if decision == 'approve':
        await approve_human_review(HumanReviewApproveRequest(issue_id=review_id), request)
        return '已批准原审批单，执行状态请用 /status 查询。'
    await reject_human_review(HumanReviewRejectRequest(issue_id=review_id,
        reason='用户在飞书拒绝本次操作', resume_after_rejection=False), request)
    return '已拒绝本次操作。'


class Interactions:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.store.execute('''CREATE TABLE IF NOT EXISTS review_cards (
            token TEXT PRIMARY KEY, conversation TEXT NOT NULL, review_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, expires REAL NOT NULL, message_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending', rendered TEXT NOT NULL DEFAULT '',
            UNIQUE(conversation,review_id))''')

    async def command(self, conversation, text):
        parts = text.split()
        if len(parts) != 2 or parts[0] not in {'/approve', '/reject'}:
            return '请使用审批卡中的完整 /approve 或 /reject 命令。'
        return await self.act(parts[1], parts[0][1:], conversation['sender'], conversation['chat'],
                              conversation_key=conversation['key'])

    async def act(self, token, decision, operator, chat, *, message_id='', conversation_key=''):
        rows = self.store.rows('SELECT * FROM review_cards WHERE token=?', (token,))
        if not rows or decision not in {'approve', 'reject'}:
            return '审批操作无效。'
        card = rows[0]
        rows = self.store.rows('SELECT * FROM conversations WHERE key=?', (card['conversation'],))
        if not rows:
            return '会话已不可用。'
        conversation = rows[0]
        if (not self.service.allowed(conversation) or operator != conversation['sender']
                or chat != conversation['chat']
                or (message_id and message_id != card['message_id'])
                or (conversation_key and conversation_key != conversation['key'])):
            return '仅本任务发起人可在原会话处理审批。'
        if card['state'] != 'pending' or time.time() >= card['expires']:
            return '审批按钮已处理或已过期，请到 TCode 查看。'
        # Persist before executing. An interrupted decision is never replayed.
        self.store.execute("UPDATE review_cards SET state='processing' WHERE token=?", (token,))
        try:
            return await decide(self.service.backend, conversation, card['review_id'], card['fingerprint'], decision)
        finally:
            self.store.execute("UPDATE review_cards SET state='handled' WHERE token=?", (token,))

    async def callback(self, event):
        value = event.get('action_value') or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return
        if not isinstance(value, dict) or not value.get('tcode_review'):
            return
        if not event.get('message_id') or not event.get('operator_id') or not event.get('chat_id'):
            return
        await self.act(value['tcode_review'], value.get('decision'), event['operator_id'],
                       event['chat_id'], message_id=event['message_id'])

    async def watch(self):
        while True:
            try:
                async for event in self.service.transport.events('card.action.trigger'):
                    await self.callback(event)
            except Exception:
                # Text commands remain usable without the card callback subscription.
                await asyncio.sleep(5)

    async def observe(self):
        for conversation in self.store.rows('SELECT * FROM conversations'):
            if not self.service.allowed(conversation) or not conversation['target']:
                continue
            for review in await reviews(self.service.backend, conversation):
                if review.get('status') == 'pending_review':
                    self.store.execute('INSERT OR IGNORE INTO review_cards(token,conversation,review_id,fingerprint,expires) VALUES (?,?,?,?,?)',
                        (secrets.token_urlsafe(18), conversation['key'], review['id'], fingerprint(review), time.time() + 1800))
                rows = self.store.rows('SELECT * FROM review_cards WHERE conversation=? AND review_id=?',
                                       (conversation['key'], review['id']))
                if not rows:
                    continue
                card = rows[0]
                pending = (review.get('status') == 'pending_review' and card['state'] == 'pending'
                           and card['expires'] > time.time() and card['fingerprint'] == fingerprint(review))
                status = '等待确认（30 分钟有效）' if pending else {
                    'approved': '已批准', 'rejected': '已拒绝'}.get(review.get('status'), '操作已失效，请在 TCode 处理')
                details = json.dumps(review.get('tool_args') or {}, ensure_ascii=False, indent=2)
                content = f"{self.service.label(conversation)}\n{review.get('tool_server', '')}.{review.get('tool_name', '')}\n{review.get('reason', '')}\n参数：\n{details}"
                # Never approve arguments that were truncated from the displayed card.
                if len(content) > 6000:
                    pending = False
                    content = content[:6000] + '\n参数过长，请在 TCode 查看完整审批单并处理。'
                elements = [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': content}}]
                if pending:
                    elements += [{'tag': 'action', 'actions': [
                        {'tag': 'button', 'text': {'tag': 'plain_text', 'content': label}, 'type': style,
                         'value': {'tcode_review': card['token'], 'decision': decision}}
                        for label, style, decision in [('批准本次', 'primary', 'approve'), ('拒绝', 'danger', 'reject')]]},
                        {'tag': 'div', 'text': {'tag': 'plain_text', 'content':
                            f"仅发起人可操作。按钮无响应时，在原会话发送：\n/approve {card['token']}\n或 /reject {card['token']}"}}]
                body = {'config': {'update_multi': True}, 'header': {'title': {'tag': 'plain_text', 'content': 'TCode 审批 · ' + status}}, 'elements': elements}
                rendered = json.dumps(body, ensure_ascii=False)
                if rendered != card['rendered']:
                    mid = await self.service.transport.card_message(conversation, rendered, card['token'], card['message_id'])
                    self.store.execute('UPDATE review_cards SET message_id=?,rendered=? WHERE token=?', (mid, rendered, card['token']))

    async def continue_results(self):
        from ..session.manager import SessionManager
        from ..runtime.async_completion import session_completion_events
        from ._shared import (_load_symphony_memory_issue_store, _resume_session_after_async_issue,
                              _extract_async_issue_resume_data)
        from .feishu_runtime import Binding
        for conversation in self.store.rows('SELECT * FROM conversations'):
            enabled = float(self.store.cursor('auto-continue:' + conversation['key'], '0'))
            if not enabled or not self.service.allowed(conversation):
                continue
            self.service.backend.request(conversation)  # Revalidate the active account.
            owner = Binding.model_validate_json(conversation['binding']).model_dump()
            session = await SessionManager.get(conversation['session_id'], **owner)
            if not session:
                continue
            owned = {r['guidance_id'] for r in self.store.rows(
                "SELECT guidance_id FROM inbox WHERE conversation=? AND status='accepted'",
                (conversation['key'],)) if r['guidance_id']}
            owned_messages = {m.id for m in session.messages if m.role == 'user'
                              and ((m.metadata or {}).get('guidance') or {}).get('id') in owned}
            for event in session_completion_events(session):
                origin = (event.get('task_context') or {}).get('message_id') or event.get('origin_turn_id')
                if origin not in owned_messages:
                    continue
                if (float(self.store.cursor('auto-continue:' + conversation['key'], '0')) != enabled
                        or not self.service.allowed(conversation)):
                    break  # /stop or rebinding can happen while the previous result is being read.
                identifier = event.get('identifier', '')
                key = 'auto-result:' + conversation['key'] + ':' + identifier
                if self.store.cursor(key) or event.get('continuation_status') != 'available':
                    continue
                try:
                    created = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00')).timestamp()
                except (KeyError, ValueError):
                    continue
                if created < enabled:
                    continue
                issues = _load_symphony_memory_issue_store(session.workdir).get('issues', [])
                issue = next((i for i in issues if i.get('identifier') == identifier), None)
                if not issue:
                    continue  # Result collection may not have committed yet.
                scope = _extract_async_issue_resume_data(issue)
                if (scope.get('parent_session_id') or scope.get('session_id')) != session.id:
                    continue
                result = await _resume_session_after_async_issue(issue, workdir=session.workdir)
                if result.get('reason') == 'session_already_running':
                    continue
                self.store.set_cursor(key, json.dumps(result))
                if result.get('status') == 'completed' and result.get('message_id'):
                    self.store.set_cursor('async-final:' + session.id + ':' + result['message_id'], '1')
                elif result.get('status') == 'failed':
                    self.store.notify(conversation, key, '任务结果已收集，但自动解读未完成，请在 TCode 查看并继续；未重复提交计算。')
