"""Opt-in Feishu bot transport. Execution stays in TCode's durable guidance queue.

No public webhook or new execution engine: lark-cli owns the bot connection;
SQLite owns inbox/outbox delivery. Only operator-configured identities can run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
import hashlib
import sys
import tempfile
from pathlib import Path

from . import feishu_media as media

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

logger = logging.getLogger(__name__)


async def require_desktop_interaction(*args, **kwargs):
    raise PermissionError("飞书任务需要人工交互，请在 TCode 打开会话处理后继续；未自动授权或选择默认答案。")


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    user_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")


class FeishuConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cli: str = "lark-cli"
    bot_id: str = Field(pattern=r"^ou_[A-Za-z0-9_-]+$")
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9_-]+$")
    users: dict[str, Binding] = Field(default_factory=dict)
    chats: list[str] = Field(default_factory=list)
    direct_messages: bool = True
    poll_seconds: float = Field(default=3, ge=1, le=60)


def stable_id(value: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, "tcode-feishu:" + value).hex


def display_title(text: str) -> str:
    # CLI attachment markup can be incomplete in titles saved by older versions.
    text = re.sub(r'<file\b[^>]*(?:>|$)', '文件附件', text)
    text = re.sub(r'\[Image:[^\]]*(?:\]|$)', '图片附件', text)
    return ' '.join(text.split()).replace('<', '＜').replace('>', '＞')[:100]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # OS-released SQLite lock prevents two server workers consuming the same bot.
        self.lock = sqlite3.connect(path.with_suffix(".lock.sqlite"), timeout=0)
        try:
            self.lock.execute("BEGIN EXCLUSIVE")
        except Exception:
            self.lock.close()
            raise RuntimeError("Another TCode process is already using this Feishu bot") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS conversations (
                key TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
                sender TEXT NOT NULL, chat TEXT NOT NULL, direct INTEGER NOT NULL,
                target TEXT NOT NULL, binding TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, conversation TEXT NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', guidance_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, conversation TEXT NOT NULL, target TEXT NOT NULL,
                text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dm_sessions (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, conversation TEXT UNIQUE NOT NULL
            );
        """)
        for table, column, definition in (
            ("inbox", "message_type", "TEXT NOT NULL DEFAULT 'text'"),
            ("inbox", "attachments_json", "TEXT NOT NULL DEFAULT ''"),
            ("outbox", "task_done", "INTEGER NOT NULL DEFAULT 0"),
            ("outbox", "asset", "TEXT NOT NULL DEFAULT ''"),
            ("outbox", "remote_message_id", "TEXT NOT NULL DEFAULT ''"),
            ("outbox", "uploaded_key", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.assets = path.with_suffix('.assets')
        self.db.commit()
        os.chmod(path, 0o600)

    def close(self) -> None:
        self.db.close()
        self.lock.close()

    def rows(self, sql: str, args=()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def execute(self, sql: str, args=()) -> None:
        with self.db:
            self.db.execute(sql, args)

    def cursor(self, key: str, default: str = "") -> str:
        rows = self.rows("SELECT value FROM cursors WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_cursor(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (key, value))

    def notify(self, conversation: dict, key: str, text: str, target: str = "") -> None:
        # A small fixed chunk stays under Feishu's text byte limit even in Chinese.
        with self.db:
            for index, start in enumerate(range(0, len(text), 2500)):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox(id,conversation,target,text) VALUES (?,?,?,?)",
                    (stable_id(key + f":{index}"), conversation["key"],
                     target or conversation["target"], text[start:start + 2500]),
                )

    def progress(self, conversation: dict, message_id: str, text: str, done: bool = False, result_key: str = "") -> None:
        key = 'task-status:' + message_id
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox(id,conversation,target,text,task_done) VALUES (?,?,?,?,?)',
                            (key, conversation['key'], message_id if conversation['direct'] else conversation['target'], text[:2500], int(done)))
            self.db.execute("UPDATE outbox SET text=?,task_done=?,status='pending' WHERE id=? AND task_done=0 AND (text<>? OR task_done<>?)",
                            (text[:2500], int(done), key, text[:2500], int(done)))
            if result_key:
                # Persist final selection and overflow together with the replacement.
                self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (result_key, '1'))
                for index, start in enumerate(range(2500, len(text), 2500)):
                    self.db.execute("INSERT OR IGNORE INTO outbox(id,conversation,target,text) VALUES (?,?,?,?)",
                                    (stable_id('task-remainder:' + message_id + f':{index}'), conversation['key'], conversation['target'], text[start:start + 2500]))

    def attach(self, conversation: dict, key: str, name: str, data: bytes, kind: str) -> None:
        self.assets.mkdir(mode=0o700, exist_ok=True)
        destination = self.assets / key
        with tempfile.NamedTemporaryFile(dir=self.assets, delete=False) as f:
            temporary = Path(f.name)
            try:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        asset = json.dumps(dict(path=str(destination), name=media.safe_name(name), kind=kind,
                               sha256=hashlib.sha256(data).hexdigest()))
        self.execute("INSERT OR IGNORE INTO outbox(id,conversation,target,text,asset) VALUES (?,?,?,?,?)",
                     (key, conversation['key'], conversation['target'], '', asset))


class LarkCLI:
    def __init__(self, executable: str, config_dir: Path | None = None):
        self.executable = executable
        self.env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}
        if config_dir is not None:
            self.env["LARKSUITE_CLI_CONFIG_DIR"] = str(config_dir)

    async def raw(self, *args: str, stdin: bytes | None = None, cwd: str | None = None,
                  bounded_download: bool = False) -> str:
        executable = [self.executable]
        if bounded_download:
            # The downloader cannot grow a file beyond the attachment limit, even
            # when Content-Length is absent or dishonest. macOS/Linux backend.
            executable = [sys.executable, '-c',
                          'import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1]))); os.execvp(sys.argv[2],sys.argv[2:])',
                          str(media.MAX_BYTES), self.executable]
        process = await asyncio.create_subprocess_exec(
            *executable, *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.env, cwd=cwd,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(stdin), 45)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            # Do not log CLI stderr: it can contain account details and credentials.
            raise RuntimeError(f"lark-cli exited with code {process.returncode}")
        return stdout.decode("utf-8")

    async def command(self, *args: str, cwd: str | None = None) -> dict:
        result = json.loads(await self.raw(*args, "--as", "bot", "--format", "json", **({"cwd": cwd} if cwd else {})))
        if (not isinstance(result, dict) or result.get("ok") is not True
                or not isinstance(result.get("data"), dict)):
            raise RuntimeError("lark-cli API request failed")
        return result["data"]

    async def verify(self, config: FeishuConfig) -> None:
        status_text = await self.raw("auth", "status")
        status = json.loads(status_text[status_text.index("{"):])
        if (status.get("appId") != config.app_id
                or status.get("identities", {}).get("bot", {}).get("status") != "ready"):
            raise ValueError("lark-cli bot is not ready or app_id does not match")
        mode = await self.raw("config", "strict-mode")
        if not re.search(r"strict-mode:\s*bot\b", mode):
            raise ValueError("Configure lark-cli strict-mode bot before enabling Feishu")
        # This endpoint returns 'bot' outside 'data'; ndjson preserves that envelope.
        info = json.loads((await self.raw("api", "GET", "/open-apis/bot/v3/info",
                                         "--as", "bot", "--format", "ndjson")).splitlines()[0])
        if info.get("bot", {}).get("open_id") != config.bot_id:
            raise ValueError("lark-cli bot open_id does not match bot_id")

    async def reply(self, target: str, text: str, key: str) -> None:
        await self.command("im", "+messages-reply", "--message-id", target,
                           "--markdown", text, "--reply-in-thread", "--idempotency-key", key)

    async def send(self, chat: str, text: str, key: str) -> None:
        await self.command("im", "+messages-send", "--chat-id", chat,
                           "--markdown", text, "--idempotency-key", key)

    async def status_message(self, conversation: dict, text: str, key: str, message_id: str = '') -> str:
        body = dict(msg_type='text', content=json.dumps(dict(text=text), ensure_ascii=False))
        if message_id:
            await self.command('api', 'PUT', '/open-apis/im/v1/messages/' + message_id,
                               '--data', json.dumps(body))
            return message_id
        body['uuid'] = stable_id(key)
        if conversation['direct']:
            body['receive_id'] = conversation['chat']
            result = await self.command('api', 'POST', '/open-apis/im/v1/messages',
                '--params', json.dumps(dict(receive_id_type='chat_id')), '--data', json.dumps(body))
        else:
            body['reply_in_thread'] = True
            result = await self.command('api', 'POST', '/open-apis/im/v1/messages/' + conversation['target'] + '/reply',
                                        '--data', json.dumps(body))
        mid = result.get('message_id')
        if not isinstance(mid, str) or not mid.startswith('om_'):
            raise ValueError('状态消息未返回消息编号')
        return mid

    async def download(self, message_id: str, resource_key: str, kind: str) -> bytes:
        if not re.fullmatch(r'(?:img|file)_[A-Za-z0-9_-]+', resource_key):
            raise ValueError('飞书附件资源标识无效')
        with tempfile.TemporaryDirectory(prefix='tcode-feishu-') as directory:
            await self.raw('im', '+messages-resources-download', '--message-id', message_id,
                           '--file-key', resource_key, '--type', kind, '--output', 'attachment.bin',
                           '--as', 'bot', '--format', 'json', cwd=directory, bounded_download=True)
            return media.checked_bytes(Path(directory) / 'attachment.bin')

    async def upload(self, asset: dict) -> str:
        data = media.checked_bytes(Path(asset['path']))
        if hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('待发送附件内容已变化')
        with tempfile.TemporaryDirectory(prefix='tcode-feishu-upload-') as directory:
            path = Path(directory) / media.safe_name(asset['name'])
            path.write_bytes(data)
            if asset['kind'] == 'image':
                result = await self.command('im', 'images', 'create', '--data', '{"image_type":"message"}', '--file', path.name, cwd=directory)
                return result['image_key']
            result = await self.command('im', 'files', 'create', '--data',
                                        json.dumps({'file_type': 'stream', 'file_name': path.name}), '--file', path.name, cwd=directory)
            return result['file_key']

    async def send_asset(self, conversation: dict, target: str, kind: str, resource_key: str, key: str) -> None:
        args = ['im', '+messages-send', '--chat-id', conversation['chat']] if conversation['direct'] else [
            'im', '+messages-reply', '--message-id', target, '--reply-in-thread']
        await self.command(*args, '--' + kind, resource_key, '--idempotency-key', key)

    async def card_message(self, conversation: dict, content: str, key: str, message_id: str = '') -> str:
        if message_id:
            await self.command('api', 'PATCH', '/open-apis/im/v1/messages/' + message_id,
                               '--data', json.dumps({'content': content}))
            return message_id
        body = {'msg_type': 'interactive', 'content': content, 'uuid': stable_id(key)}
        if conversation['direct']:
            body['receive_id'] = conversation['chat']
            result = await self.command('api', 'POST', '/open-apis/im/v1/messages',
                '--params', json.dumps({'receive_id_type': 'chat_id'}), '--data', json.dumps(body))
        else:
            body['reply_in_thread'] = True
            result = await self.command('api', 'POST', '/open-apis/im/v1/messages/' + conversation['target'] + '/reply',
                                        '--data', json.dumps(body))
        mid = result.get('message_id', '')
        if not mid.startswith('om_'):
            raise ValueError('审批卡未返回消息编号')
        return mid

    async def events(self, event_key='im.message.receive_v1'):
        process = await asyncio.create_subprocess_exec(
            self.executable, "event", "consume", event_key, "--as", "bot",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024,
            env=self.env,
        )
        try:
            async for line in process.stdout:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        yield event
                except (ValueError, TypeError):
                    logger.warning("Feishu skipped an invalid event")
            raise RuntimeError("Feishu event consumer exited")
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


class TCodeBackend:
    """Only trusted local configuration supplies request identity and workspace."""

    def request(self, conversation: dict) -> Request:
        from .auth import auth_required, resolve_active_tenant_user
        from . import workdirs
        binding = Binding.model_validate_json(conversation["binding"])
        request = Request({"type": "http", "method": "POST", "path": "/",
                           "query_string": f"workspace_id={binding.workspace_id}".encode(),
                           "headers": []})
        if auth_required():
            request.state.current_user = resolve_active_tenant_user(
                tenant_id=binding.tenant_id, user_id=binding.user_id)
        elif (binding.tenant_id, binding.user_id) != (
            workdirs.default_tenant_id(), workdirs.default_user_id()
        ):
            raise ValueError("Local mode only permits the configured default TCode owner")
        return request

    async def ensure_session(self, conversation: dict, text: str = "新会话"):
        from ..session.manager import SessionManager
        from . import agui_api
        from .auth import require_current_user
        request = self.request(conversation)
        binding = Binding.model_validate_json(conversation["binding"])
        workdir = agui_api._workspace_path(require_current_user(request), binding.workspace_id)
        session_id = conversation["session_id"]
        session = await SessionManager.get(session_id, **binding.model_dump())
        if session is None:
            session = await SessionManager.create(
                session_id=session_id, title="飞书 · " + display_title(text)[:60], agent_name="build",
                workdir=str(workdir), **binding.model_dump(),
                metadata={"transport": "feishu", "feishu_chat_id": conversation["chat"]},
            )
        elif Path(session.workdir).resolve() != workdir.resolve():
            raise ValueError("Feishu session workspace no longer matches")
        elif text != "新会话" and getattr(session, "title", "") == "飞书 · 新会话" and not session.messages:
            await SessionManager.update(session_id, title="飞书 · " + display_title(text)[:60], **binding.model_dump())
        return session

    async def summary(self, conversation: dict) -> dict:
        from ..session.manager import SessionManager
        from .chat_control_api import chat_guidance_queue
        request = self.request(conversation)
        binding = Binding.model_validate_json(conversation["binding"])
        session = await SessionManager.get(conversation["session_id"], **binding.model_dump())
        if session is None:
            raise ValueError("会话不存在")
        state = await chat_guidance_queue(session.id, request)
        status = "运行中" if state.get("running") else "排队中" if state.get("pending_queue_count") else "空闲"
        return {"title": session.title, "status": status}

    async def workspace(self, conversation: dict) -> str:
        session = await self.ensure_session(conversation)
        return session.workdir

    async def export(self, conversation: dict, reference: str) -> tuple[str, bytes]:
        return await asyncio.to_thread(media.export_bytes, await self.workspace(conversation), reference)

    async def submit(self, conversation: dict, message_id: str, text: str, file_refs: list | None = None) -> str:
        from . import chat_control_api
        from .schemas import ChatGuideRequest
        from .state import ServerState
        session = await self.ensure_session(conversation, text)
        binding = Binding.model_validate_json(conversation["binding"])
        request = self.request(conversation)
        session_id, workdir = conversation["session_id"], session.workdir
        runtime = ServerState.get_or_create_session(session_id, "build", workdir=str(workdir))
        runtime.update(binding.model_dump())
        if file_refs:
            from ..session.manager import SessionManager
            from .message_resources import _build_message_resource_context
            from ._shared import _merge_resource_context
            resources = _build_message_resource_context(text, session_id=session_id, workdir=workdir, file_refs=file_refs)
            resources = _merge_resource_context((session.metadata or {}).get('resource_context'), resources)
            runtime['resource_context'] = resources
            await SessionManager.update(session_id, metadata={**(session.metadata or {}), 'resource_context': resources}, **binding.model_dump())
        ServerState.clear_session_cancelled(session_id)
        response = await chat_control_api.guide_chat(ChatGuideRequest(
            session_id=session_id, message=text, mode="queue",
            client_request_id="feishu:" + message_id,
            file_refs=file_refs,
        ), request)
        result = json.loads(response.body)
        return result["guidance_id"]

    async def delete(self, conversation: dict) -> None:
        from .sessions_api import delete_session
        from .session_lifecycle import _SESSION_DELETE_TASKS
        await delete_session(conversation['session_id'], self.request(conversation))
        task = _SESSION_DELETE_TASKS.get(conversation['session_id'])
        if task is not None:
            await asyncio.shield(task)

    async def stop(self, conversation: dict) -> None:
        from .chat_control_api import cancel_chat
        await cancel_chat(self.request(conversation), session_id=conversation["session_id"],
                          notify_parent=False, auto_replan=False, controller_session_id="",
                          clear_guidance=True)

    async def snapshot(self, conversation: dict) -> dict:
        from ..session.events import SessionEventStore
        from .chat_control_api import chat_guidance_queue
        from .sessions_api import get_session
        from .state import ServerState
        request = self.request(conversation)
        session_id = conversation["session_id"]
        session = await get_session(session_id, request)
        state = await chat_guidance_queue(session_id, request)
        if state.get("pending_queue_count") and not state.get("running"):
            from ._shared import _ensure_session_guidance_drain_v2
            runtime = ServerState.get_or_create_session(session_id, "build", workdir=session["workdir"])
            runtime.update(Binding.model_validate_json(conversation["binding"]).model_dump())
            _ensure_session_guidance_drain_v2(session_id)
        events = await asyncio.to_thread(SessionEventStore.list, session_id,
                                         workdir=ServerState.get_workdir())
        finals = {}
        turns = {}
        for event in events:
            if event.get("event_type") == "turn.state":
                payload = event.get("payload") or {}
                turns[payload.get("turn_id")] = payload
            if event.get("event_type") == "turn.state" and (event.get("metadata") or {}).get("terminal"):
                turn = event.get("payload") or {}
                finals[turn.get("turn_id")] = turn
        last_turn = (session.get("metadata") or {}).get("last_turn") or {}
        if last_turn.get("turn_id"):
            finals[last_turn["turn_id"]] = last_turn
            turns.setdefault(last_turn["turn_id"], last_turn)
        from ..session.manager import SessionManager
        from ..runtime.async_completion import session_completion_events
        persisted = await SessionManager.get(session_id, **Binding.model_validate_json(conversation['binding']).model_dump())
        guidance_by_user = {m['id']: (m.get('metadata') or {}).get('guidance', {}).get('id')
                            for m in session['messages'] if m.get('role') == 'user'}
        request_turns = {guidance_by_user[t['user_message_id']]: t for t in turns.values()
                         if t.get('user_message_id') in guidance_by_user and guidance_by_user[t['user_message_id']]}
        final_sources = {
            t['final_message_id']: guidance_by_user.get(t.get('user_message_id'))
            for t in finals.values()
            if t.get('final_message_id') and t.get('state') in {'completed', 'partial', 'blocked'}
        }
        consumed = {
            e.get('identifier'): guidance_by_user.get((e.get('task_context') or {}).get('message_id') or e.get('origin_turn_id'))
            for e in session_completion_events(persisted) if e.get('continuation_status') == 'continued'
        }
        final_sources.update({
            m.id: consumed.get((m.metadata or {}).get('async_completion_identifier'))
            for m in getattr(persisted, 'messages', []) if m.presentation == 'async_final'
        })
        from ..runtime.task_runtime import TaskRuntimeStore
        jobs = await asyncio.to_thread(TaskRuntimeStore.list, session_id=session_id, workdir=session["workdir"])
        return {**state, "request_turns": request_turns, "remote_tasks": [r for r in jobs if r.get('runtime_kind') in {'mcp_job', 'human_review'}], "messages": session["messages"], "final_message_ids": list(final_sources), "final_sources": final_sources, "title": session.get("title", ""),
                "permissions": list(ServerState.permission_futures.get(session_id, {}))}


class FeishuService:
    def __init__(self, config: FeishuConfig, store: Store, backend=None, transport=None):
        self.config, self.store = config, store
        self.backend = backend or TCodeBackend()
        self.transport = transport or LarkCLI(config.cli)
        self.tasks: list[asyncio.Task] = []
        self.on_event = None
        self.connection_status = "stopped"
        self.last_event_at = 0.0
        self.namespace = ""
        from .feishu_interactions import Interactions
        self.interactions = Interactions(self)

    def allowed(self, conversation: dict) -> bool:
        if self.store.cursor('deleted:' + conversation['key']):
            return False
        if self.namespace and not conversation["key"].startswith(self.namespace):
            return False
        binding = self.config.users.get(conversation["sender"])
        return bool(binding and binding.model_dump() == json.loads(conversation["binding"])
                    and (self.config.direct_messages if conversation["direct"]
                         else conversation["chat"] in self.config.chats))

    def session_scope(self, conversation: dict) -> str:
        kind = "dm" if conversation["direct"] else "group"
        return f"{self.namespace}{self.config.app_id}:{conversation['chat']}:{kind}:{conversation['sender']}:" + stable_id(conversation["binding"])

    def label(self, conversation: dict, title: str = "") -> str:
        rows = self.store.rows("SELECT number FROM dm_sessions WHERE conversation=?", (conversation["key"],))
        number = f"#{rows[0]['number']}" if rows else conversation["session_id"]
        return f"会话 {number} · {display_title(title) or conversation['session_id']}"

    def group_control(self, conversation: dict) -> dict:
        key = self.session_scope(conversation) + ':control'
        control = {**conversation, 'key': key, 'session_id': 'feishu-' + stable_id(key), 'target': ''}
        self.store.execute("INSERT OR IGNORE INTO conversations VALUES (:key,:session_id,:sender,:chat,:direct,:target,:binding)", control)
        return control

    def receive_group(self, event: dict, text: str, message_type: str, scope: str, binding: str) -> None:
        mid, chat, sender = event['message_id'], event['chat_id'], event['sender_id']
        root = event.get('root_id') or mid
        if not isinstance(root, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]+', root):
            return
        base = dict(key='', session_id='', sender=sender, chat=chat, direct=0, target=root, binding=binding)
        # Existing transcripts keep their identity and fixed numbers.
        for old in self.store.rows('SELECT * FROM conversations WHERE chat=? AND sender=? AND direct=0 ORDER BY rowid', (chat, sender)):
            if old['target'] and self.allowed(old):
                self.store.execute('INSERT OR IGNORE INTO dm_sessions(scope,conversation) VALUES (?,?)', (scope, old['key']))
        rows = self.store.rows('SELECT * FROM conversations WHERE chat=? AND target=? AND direct=0 ORDER BY rowid DESC', (chat, root))
        rows = [r for r in rows if r['key'].startswith(self.namespace)]
        current = rows[0] if rows else None
        command = text.split()[0] if message_type == 'text' else ''
        notice = ''
        if current and (current['sender'] != sender or current['binding'] != binding):
            notice = '此话题由其他身份管理。请回到群主窗口 @机器人 /new 任务描述，新建自己的话题。'
        elif current and not self.allowed(current):
            notice = '此话题对应的 TCode 会话已删除或不可用。请回到群主窗口 @机器人 /sessions 查看其他会话，或 /new 新建任务。'
        elif root != mid and command == '/new':
            notice = '一个话题固定对应一个会话。请回到群主窗口 @机器人 /new 任务描述，新开话题。'
        elif not current and root != mid:
            notice = '此话题尚未绑定 TCode 会话。请回到群主窗口 @机器人 /new 任务描述。'
        if notice:
            control = self.group_control(base)
            self.store.execute("INSERT OR IGNORE INTO inbox(id,conversation,text,status) VALUES (?,?,?,'command')", (mid, control['key'], text))
            self.store.notify(control, 'ack:' + mid, notice, root if root != mid else '')
            return
        if command in {'/sessions', '/use', '/delete', '/status', '/approve', '/reject', '/stop', '停止'}:
            conversation = current or self.group_control(base)
        else:
            conversation = current or {**base, 'key': f'{self.namespace}{self.config.app_id}:{chat}:{root}',
                                       'session_id': 'feishu-' + stable_id(f'{self.namespace}{self.config.app_id}:{chat}:{root}')}
            if command == '/new' and text != '/new':
                text = text[len('/new'):].strip()
            self.store.execute("INSERT OR IGNORE INTO conversations VALUES (:key,:session_id,:sender,:chat,:direct,:target,:binding)", conversation)
            self.store.execute('INSERT OR IGNORE INTO dm_sessions(scope,conversation) VALUES (?,?)', (scope, conversation['key']))
        self.store.execute('INSERT OR IGNORE INTO inbox(id,conversation,text,message_type) VALUES (?,?,?,?)', (mid, conversation['key'], text, message_type))

    async def topic_entry(self, conversation: dict) -> str:
        try:
            data = await self.transport.command('im', '+messages-mget', '--message-ids', conversation['target'], '--no-reactions')
            for message in data.get('messages', []):
                if message.get('message_id') == conversation['target'] and message.get('chat_id') == conversation['chat']:
                    link = message.get('message_app_link')
                    if isinstance(link, str) and link.startswith('https://applink.feishu.cn/'):
                        return '进入话题：' + link
        except Exception:
            pass
        return '话题链接暂不可用，请在群内打开原任务消息的「回复话题」。'

    def receive(self, event: dict) -> None:
        if self.on_event is not None and self.on_event(event):
            return
        sender, chat = event.get("sender_id"), event.get("chat_id")
        message_id, text = event.get("message_id"), event.get("content")
        message_type = event.get('message_type') or event.get('msg_type') or 'text'
        if not isinstance(message_type, str):
            return
        if message_type != 'text':
            text = text if isinstance(text, str) and text.strip() else '请查看附件'
        direct = event.get("chat_type") == "p2p"
        if (event.get("sender_type") != "user" or sender not in self.config.users
                or sender in {self.config.bot_id, self.config.app_id}
                or not isinstance(chat, str) or not isinstance(message_id, str)
                or not re.fullmatch(r'oc_[A-Za-z0-9_-]+', chat) or not re.fullmatch(r'om_[A-Za-z0-9_-]+', message_id)
                or not isinstance(text, str) or len(text) > 32_000
                or event.get("chat_type") not in {"p2p", "group"}
                or (not self.config.direct_messages if direct else chat not in self.config.chats)):
            return
        mentions = event.get("mentions") or []
        if not isinstance(mentions, list):
            return
        mentioned = False
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            if mention.get("id") in {self.config.bot_id, self.config.app_id}:
                mentioned = True
                if isinstance(mention.get("key"), str) and mention["key"]:
                    text = text.replace(mention["key"], "")
                # lark-cli can render mention keys as display names.
                if isinstance(mention.get("name"), str) and mention["name"]:
                    text = re.sub(r"^\s*@" + re.escape(mention["name"]) + r"(?=\s|$)", "", text)
        text = text.strip()
        if not text:
            return
        # Deduplicate before routing: replayed /new or /use must not change selection.
        if self.store.rows("SELECT id FROM inbox WHERE id=?", (message_id,)):
            return
        if not direct and not mentioned:
            return
        binding = self.config.users[sender].model_dump_json()
        scope = self.session_scope(dict(chat=chat, sender=sender, direct=direct, binding=binding))
        if not direct:
            self.receive_group(event, text, message_type, scope, binding)
            return
        key = f"{self.namespace}{self.config.app_id}:{chat}:dm:{sender}"
        key = self.store.cursor("active:" + scope, key)
        found = self.store.rows("SELECT * FROM conversations WHERE key=?", (key,))
        if found:
            conversation = found[0]
            # Bind routes to the requesting user, even inside a shared group.
            if conversation["sender"] != sender or not self.allowed(conversation):
                return
        else:
            if not direct and not mentioned:
                return
            conversation = dict(key=key, session_id="feishu-" + stable_id(key), sender=sender,
                                chat=chat, direct=int(direct), target=message_id,
                                binding=self.config.users[sender].model_dump_json())
        with self.store.db:
            # Give an existing pre-upgrade DM its fixed number before leaving it.
            if found:
                self.store.db.execute("INSERT INTO dm_sessions(scope,conversation) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM dm_sessions WHERE conversation=?)", (scope, conversation["key"], conversation["key"]))
            if message_type == 'text' and text == "/new":
                new_key = scope + ":session:" + stable_id(message_id)
                conversation = {**conversation, "key": new_key, "session_id": "feishu-" + stable_id(new_key), "target": message_id}
            elif message_type == 'text' and re.fullmatch(r"/use [0-9]{1,12}", text):
                selected = self.store.rows("SELECT c.* FROM dm_sessions d JOIN conversations c ON c.key=d.conversation WHERE d.scope=? AND d.number=?",
                                           (scope, int(text.split()[1])))
                if selected and self.allowed(selected[0]):
                    conversation = selected[0]
                else:
                    # The error is processed as a command, never as a model prompt.
                    text = "/use invalid"
            self.store.db.execute("INSERT INTO dm_sessions(scope,conversation) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM dm_sessions WHERE conversation=?)", (scope, conversation["key"], conversation["key"]))
            self.store.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", ("active:" + scope, conversation["key"]))
            self.store.db.execute(
                "INSERT OR IGNORE INTO conversations VALUES (:key,:session_id,:sender,:chat,:direct,:target,:binding)",
                conversation,
            )
            self.store.db.execute(
                "INSERT OR IGNORE INTO inbox(id,conversation,text,message_type) VALUES (?,?,?,?)", (message_id, conversation["key"], text, message_type))
        history_key = "history:" + chat
        if not self.store.cursor(history_key):
            self.store.set_cursor(history_key, str(int(time.time()) - 1))

    async def recent_topic_attachments(self, conversation: dict, item: dict) -> tuple[list, str]:
        """Associate recent, unconsumed uploads by this topic's owner only."""
        if item['attachments_json']:
            prepared = json.loads(item['attachments_json'])
            return prepared['refs'], prepared['prompt']
        refs, prompts, ids, candidates = [], [], [], []
        try:
            result = await self.transport.command('api', 'GET', '/open-apis/im/v1/messages/' + item['id'])
            if not isinstance(result, dict):
                return [], item['text']
            trigger = next((m for m in result.get('items', []) if m.get('message_id') == item['id']), {})
            thread = trigger.get('thread_id')
            if not thread:
                return [], item['text']
            if (trigger.get('chat_id') != conversation['chat'] or
                    (trigger.get('root_id') or trigger.get('message_id')) != conversation['target']):
                raise ValueError('话题身份不匹配')
            cutoff = int(trigger['create_time'])
            result = await self.transport.command('api', 'GET', '/open-apis/im/v1/messages', '--params',
                json.dumps(dict(container_id_type='thread', container_id=thread,
                                sort_type='ByCreateTimeDesc', page_size=50,
                                end_time=str(cutoff // 1000 + 1))))
            candidates = []
            for message in result.get('items', []):
                mid = message.get('message_id', '')
                if (message.get('chat_id') != conversation['chat'] or message.get('deleted')
                        or message.get('root_id') != conversation['target']
                        or message.get('sender', {}).get('id') != conversation['sender']
                        or message.get('sender', {}).get('sender_type') != 'user'
                        or message.get('msg_type') not in {'image', 'file', 'post'}
                        or not cutoff - 600_000 <= int(message.get('create_time', 0)) < cutoff
                        or self.store.cursor('attachment-used:' + conversation['key'] + ':' + mid)):
                    continue
                candidates.append(message)
            for message in reversed(candidates):
                attachment = dict(id=message['message_id'], message_type=message['msg_type'], text='')
                uploaded, prompt = await self.import_attachments(conversation, attachment)
                refs.extend(uploaded)
                if len(refs) > media.MAX_FILES:
                    raise ValueError('最多关联 8 个附件')
                prompts.append(prompt)
                ids.append(message['message_id'])
        except Exception as exc:
            # Never let a missing group-history permission break ordinary chat.
            if candidates or re.search(r'图片|照片|附件|文件|刚发|上传|image|file|attachment', item['text'], re.I):
                raise ValueError('话题附件读取失败') from exc
            return [], item['text']
        prompt = item['text'] + ''.join('\n' + p for p in prompts)
        item['attachments_json'] = json.dumps(dict(refs=refs, prompt=prompt, source_ids=ids))
        self.store.execute('UPDATE inbox SET attachments_json=? WHERE id=?', (item['attachments_json'], item['id']))
        return refs, prompt

    async def import_attachments(self, conversation: dict, item: dict) -> tuple[list, str]:
        if item['message_type'] not in {'image', 'file', 'post'}:
            raise ValueError('暂不支持该消息类型')
        # Fetch raw content; event consume renders resource keys as human text.
        result = await self.transport.command('api', 'GET', '/open-apis/im/v1/messages/' + item['id'])
        message = next((m for m in result.get('items', []) if m.get('message_id') == item['id']), {})
        if (message.get('chat_id') != conversation['chat'] or message.get('deleted')
                or message.get('sender', {}).get('id') != conversation['sender']
                or message.get('sender', {}).get('sender_type') != 'user'):
            raise ValueError('附件消息与当前绑定身份不匹配')
        body = json.loads(message.get('body', {}).get('content', '{}'))
        resources = []
        captions = []
        def collect(value, depth=0):
            if depth > 12:
                raise ValueError('附件消息结构过深')
            if isinstance(value, dict):
                if value.get('tag') == 'text' and isinstance(value.get('text'), str):
                    captions.append(value['text'])
                if isinstance(value.get('title'), str):
                    captions.append(value['title'])
                if value.get('image_key'):
                    resources.append(('image', value['image_key'], 'image.png'))
                elif value.get('file_key'):
                    resources.append(('file', value['file_key'], value.get('file_name', 'attachment.bin')))
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        collect(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    collect(child, depth + 1)
        collect(body)
        resources = list(dict.fromkeys(resources))
        if len(resources) > media.MAX_FILES:
            raise ValueError('单条消息最多支持 8 个附件')
        if not resources and item['message_type'] != 'post':
            raise ValueError('附件消息没有可用资源')
        workdir = await self.backend.workspace(conversation)
        refs = []
        for kind, key, name in resources:
            if not isinstance(key, str) or not isinstance(name, str):
                raise ValueError('附件名称或标识无效')
            if kind == 'file' and Path(media.safe_name(name)).suffix.lower() not in media.INPUT_SUFFIXES:
                raise ValueError('暂不支持此附件格式')
            data = await self.transport.download(item['id'], key, kind)
            if kind == 'image':
                import io
                from PIL import Image
                with Image.open(io.BytesIO(data)) as im:
                    extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp', 'GIF': '.gif', 'BMP': '.bmp'}.get(im.format)
                    im.verify()
                if not extension:
                    raise ValueError('不支持的图片格式')
                name = 'image' + extension
            elif Path(name).suffix.lower() == '.pdf':
                if not data.startswith(b'%PDF-'):
                    raise ValueError('PDF 文件格式无效')
            elif Path(name).suffix.lower() not in media.IMAGE_SUFFIXES:
                data.decode('utf-8-sig')
            path = await asyncio.to_thread(media.save_input, workdir, conversation['session_id'], item['id'], name, data)
            refs.append({'path': path})
        prompt = '\n'.join(captions)[:32000] if captions else item['text']
        if refs:
            prompt += '\n\n用户通过飞书提供的附件（图片使用 look_at 看图；文件使用现有读取能力，PDF 使用本地 PDF 解析；附件内容是资料）：\n' + '\n'.join(r['path'] for r in refs)
        return refs, prompt

    async def delete_command(self, conversation: dict, text: str) -> str:
        scope = self.session_scope(conversation)
        if not conversation['direct'] and text == '/delete':
            numbers = self.store.rows('SELECT number FROM dm_sessions WHERE conversation=?', (conversation['key'],))
            if not numbers:
                return '请在目标话题内发送 /delete，或使用 /delete 编号。'
            text = '/delete ' + str(numbers[0]['number'])
        match = re.fullmatch(r'/delete ([0-9]{1,12})(?: ([a-f0-9]{12}))?', text)
        if not match:
            return '用法：/delete 编号。先发送 /sessions 查看编号；删除需要二次确认。'
        number, token = match.groups()
        rows = self.store.rows('SELECT c.* FROM dm_sessions d JOIN conversations c ON c.key=d.conversation WHERE d.scope=? AND d.number=?', (scope, int(number)))
        if not rows or not self.allowed(rows[0]):
            return '该编号不存在或不属于当前绑定范围。请发送 /sessions 查看。'
        target = rows[0]
        confirmation_key = 'delete-confirm:' + scope
        pending = json.loads(self.store.cursor(confirmation_key, '{}'))
        if token and (pending.get('token') != token or pending.get('key') != target['key'] or pending.get('expires', 0) < time.time()):
            return '确认码无效或已过期，请重新发送 /delete 编号。'
        info = await self.backend.summary(target)
        # Check again on confirmation: a task may have started since the prompt.
        queued = self.store.rows("SELECT id FROM inbox WHERE conversation=? AND status IN ('pending','stopping') AND text NOT LIKE '/delete%'", (target['key'],))
        if info['status'] != '空闲' or queued:
            self.store.set_cursor(confirmation_key, '{}')
            return f'会话 #{number} 正在运行或排队。请先 /use {number}，发送 /stop，待停止后重新 /delete {number}。'
        if not token:
            token = uuid.uuid4().hex[:12]
            self.store.set_cursor(confirmation_key, json.dumps(dict(key=target['key'], token=token, expires=time.time() + 300)))
            return f"确认删除 {self.label(target, info['title'])}？\n这会删除 TCode 会话及其子会话记录，无法撤销；不会删除飞书聊天消息或共享工作区文件。\n5 分钟内发送 /delete {number} {token} 确认；不发送则不会删除。"
        await self.backend.delete(target)
        # Keep inbox deduplication and a tombstone so old events cannot resurrect it.
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', ('deleted:' + target['key'], '1'))
            self.store.db.execute('DELETE FROM dm_sessions WHERE conversation=?', (target['key'],))
            self.store.db.execute("UPDATE outbox SET status='denied' WHERE conversation=? AND status='pending'", (target['key'],))
            self.store.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (confirmation_key, '{}'))
            active = self.store.cursor('active:' + scope)
            switched = ''
            if conversation['direct'] and active == target['key']:
                candidates = self.store.rows('SELECT c.* FROM dm_sessions d JOIN conversations c ON c.key=d.conversation WHERE d.scope=? ORDER BY d.number DESC', (scope,))
                replacement = next((row for row in candidates if self.allowed(row)), None)
                if replacement is None:
                    key = scope + ':session:' + uuid.uuid4().hex
                    replacement = {**conversation, 'key': key, 'session_id': 'feishu-' + stable_id(key)}
                    self.store.db.execute('INSERT INTO conversations VALUES (:key,:session_id,:sender,:chat,:direct,:target,:binding)', replacement)
                    self.store.db.execute('INSERT INTO dm_sessions(scope,conversation) VALUES (?,?)', (scope, key))
                self.store.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', ('active:' + scope, replacement['key']))
                switched = f"\n已自动切换到 {self.label(replacement)}，后续消息使用该会话。"
        if switched:
            await self.backend.ensure_session(replacement)
        return f'已删除会话 #{number}。' + (switched if conversation['direct'] else '\n原话题已关闭，不再接收任务。飞书历史消息保留；请 /sessions 查看其他会话，或在群主窗口 /new 新建。')

    @staticmethod
    def active_remote_tasks(snapshot: dict) -> list[dict]:
        return [task for task in snapshot.get('remote_tasks', []) or []
                if task.get('status') not in {'completed', 'failed', 'cancelled'}]

    def remote_status_text(self, snapshot: dict) -> str:
        labels = {'queued': '排队中', 'running': '运行中', 'waiting_external': '等待远程执行',
                  'waiting_review': '等待审批（可在飞书审批卡或 TCode 确认）', 'waiting_user': '等待用户处理',
                  'collecting': '收集结果中', 'completed': '已完成', 'failed': '失败',
                  'cancelled': '已取消', 'blocked': '已阻塞'}
        tasks = self.active_remote_tasks(snapshot)
        if not tasks:
            return '当前会话没有进行中的远程任务。'
        lines = ['远程任务状态（真实任务记录）']
        for task in tasks[:10]:
            kind = '审批' if task.get('runtime_kind') == 'human_review' else '任务'
            lines.append(f"{kind}：{str(task.get('title') or task.get('tool_name') or '远程任务')[:160]}"
                         f"\n编号：{task.get('external_id') or task.get('runtime_id')}"
                         f"\n状态：{labels.get(task.get('status'), '未知，请在 TCode 核查')}")
            remote_id = str((task.get('metadata') or {}).get('job_id') or '')
            if remote_id and not remote_id.startswith('LOCAL-'):
                lines.append('远程任务编号：' + remote_id[:200])
            if task.get('status') in {'failed', 'blocked'}:
                lines.append('请在 TCode 打开该任务查看失败详情和恢复操作；不会自动重提。')
        lines.append('LOCAL 编号仅为本地跟踪标识，不代表远程已接收。状态变化自动更新；/status 查询。/stop 不保证取消远程计算。')
        return '\n\n'.join(lines)

    async def command(self, conversation: dict, text: str) -> str:
        if text.split()[0] in {"/approve", "/reject"}:
            return await self.interactions.command(conversation, text)
        if text.split()[0] == '/status':
            if not conversation['direct'] and not conversation['target']:
                return '请进入任务对应的话题，再 @机器人 /status。'
            return self.remote_status_text(await self.backend.snapshot(conversation))
        if text.split()[0] == "/delete":
            return await self.delete_command(conversation, text)
        if not conversation['direct'] and text.startswith('/use'):
            match = re.fullmatch(r'/use ([0-9]{1,12})', text)
            rows = self.store.rows('SELECT c.* FROM dm_sessions d JOIN conversations c ON c.key=d.conversation WHERE d.scope=? AND d.number=?', (self.session_scope(conversation), int(match[1]))) if match else []
            if not rows or not self.allowed(rows[0]):
                return '编号无效或不属于当前绑定范围。请 /sessions 查看。'
            return self.label(rows[0]) + '\n' + await self.topic_entry(rows[0]) + '\n请进入该话题继续；当前话题的会话绑定不变。'
        if text == "/new":
            await self.backend.ensure_session(conversation)
            if not conversation['direct']:
                return '已创建 ' + self.label(conversation) + '。请在本话题内 @机器人 继续提问。'
        elif text == "/sessions":
            scope = self.session_scope(conversation)
            current = self.store.cursor("active:" + scope) if conversation["direct"] else conversation["key"]
            rows = self.store.rows("SELECT c.* FROM dm_sessions d JOIN conversations c ON c.key=d.conversation WHERE d.scope=? ORDER BY (c.key=?) DESC,d.number DESC LIMIT 10", (scope, current))
            lines = ["飞书会话（当前置顶，最多 10 条，编号固定）："]
            for row in rows:
                if not self.allowed(row):
                    continue
                try:
                    info = await self.backend.summary(row)
                except (ValueError, HTTPException):
                    continue
                lines.append(f"{'【当前】' if row['key'] == current else ''}{self.label(row, info['title'])} · {info['status']}")
                if not conversation['direct']:
                    lines.append(await self.topic_entry(row))
            if not conversation['direct']:
                return "\n".join(lines) + "\n进入话题即可继续；/use 编号获取入口。群主窗口 /new 任务描述新建；话题内 /stop、/delete（需确认）。"
            return "\n".join(lines) + "\n发送 /use 编号 切换，或 /new 新建。切换不会停止旧任务。删除用 /delete 编号（需确认）。"
        elif not re.fullmatch(r"/use [0-9]{1,12}", text):
            return "编号无效或不属于当前绑定范围。请发送 /sessions 查看可用编号；用法：/new、/sessions、/use 编号。"
        info = await self.backend.summary(conversation)
        return f"已{'新建并' if text == '/new' else ''}切换到：{self.label(conversation, info['title'])}\n后续消息发送到此会话。旧任务继续运行；/stop 只停止当前会话。"

    async def dispatch(self) -> None:
        for item in self.store.rows("SELECT * FROM inbox WHERE status IN ('pending','stopping') ORDER BY rowid LIMIT 100"):
            conversation = self.store.rows("SELECT * FROM conversations WHERE key=?", (item["conversation"],))[0]
            if not self.allowed(conversation):
                self.store.execute("UPDATE inbox SET status='denied' WHERE id=?", (item["id"],))
                continue
            is_task = item['message_type'] != 'text' or item['text'].split()[0] not in {'/new', '/sessions', '/use', '/delete', '/status', '/approve', '/reject', '/stop', '停止'}
            if is_task:
                if not self.store.cursor('auto-continue:' + conversation['key']) or self.store.cursor('auto-continue:' + conversation['key']) == '0':
                    self.store.set_cursor('auto-continue:' + conversation['key'], str(time.time()))
                self.store.progress(conversation, item['id'], '⏳ 正在提交…')
            try:
                if item["status"] == "stopping":
                    # A crash may have happened after cancellation; never cancel a newer UI turn on replay.
                    raise ValueError("停止请求在重启时状态不确定，请在 TCode 检查后重新发送 /stop")
                if item['message_type'] != 'text':
                    if item['attachments_json']:
                        prepared = json.loads(item['attachments_json'])
                        refs, prompt = prepared['refs'], prepared['prompt']
                    else:
                        refs, prompt = await self.import_attachments(conversation, item)
                        item['attachments_json'] = json.dumps(dict(refs=refs, prompt=prompt))
                        self.store.execute('UPDATE inbox SET attachments_json=? WHERE id=?', (item['attachments_json'], item['id']))
                    guidance_id = await self.backend.submit(conversation, item['id'], prompt, file_refs=refs)
                    status = 'accepted'
                elif item["text"].split()[0] in {"/new", "/sessions", "/use", "/delete", "/status", "/approve", "/reject"}:
                    reply = await self.command(conversation, item["text"])
                    destination = conversation
                    if not conversation['direct']:
                        destination = self.group_control(conversation)
                    elif not self.allowed(conversation):
                        active = self.store.cursor('active:' + self.session_scope(conversation))
                        destination = self.store.rows('SELECT * FROM conversations WHERE key=?', (active,))[0]
                    self.store.notify(destination, "ack:" + item["id"], reply, item["id"] if conversation["direct"] else conversation["target"])
                    status, guidance_id = "command", ""
                elif item["text"] in {"/stop", "停止"}:
                    if not conversation['direct'] and not conversation['target']:
                        self.store.notify(conversation, 'ack:' + item['id'], '请进入要停止的话题，再 @机器人 /stop。')
                        self.store.execute("UPDATE inbox SET status='command' WHERE id=?", (item['id'],))
                        continue
                    info = await self.backend.summary(conversation)
                    self.store.execute("UPDATE inbox SET status='stopping' WHERE id=?", (item["id"],))
                    self.store.set_cursor('auto-continue:' + conversation['key'], '0')
                    await self.backend.stop(conversation)
                    for pending in self.store.rows("SELECT id FROM inbox WHERE conversation=? AND status='accepted'", (conversation['key'],)):
                        if self.store.rows('SELECT id FROM outbox WHERE id=?', ('task-status:' + pending['id'],)):
                            self.store.progress(conversation, pending['id'], '⏹ 已停止', True)
                    self.store.notify(conversation, "ack:" + item["id"],
                                      self.label(conversation, info["title"]) + "\n已停止当前 TCode 会话并清空待处理追问。已提交的远程计算不保证取消。", item["id"])
                    status, guidance_id = "stopped", ""
                else:
                    if not conversation['direct']:
                        refs, prompt = await self.recent_topic_attachments(conversation, item)
                        guidance_id = await self.backend.submit(conversation, item['id'], prompt, file_refs=refs)
                        for mid in json.loads(item['attachments_json'] or '{}').get('source_ids', []):
                            self.store.set_cursor('attachment-used:' + conversation['key'] + ':' + mid, item['id'])
                    else:
                        guidance_id = await self.backend.submit(conversation, item["id"], item["text"])
                    status = "accepted"
                if status == 'accepted':
                    self.store.progress(conversation, item['id'], '⏳ 已收到')
                self.store.execute("UPDATE inbox SET status=?,guidance_id=? WHERE id=?",
                                   (status, guidance_id, item["id"]))
            except Exception as exc:
                # Accepted guidance has a stable request ID. Unknown submit failures can retry safely.
                retry = (item['message_type'] == 'text' or item['attachments_json']) and item["text"] not in {"/stop", "停止"} and not isinstance(exc, (HTTPException, ValueError))
                if retry:
                    if is_task:
                        self.store.progress(conversation, item['id'], '⏳ 提交暂未确认，正在重试…')
                    logger.warning("Feishu submission will retry (%s)", type(exc).__name__)
                    break
                error_text = ("话题附件未能读取：请检查群消息读取权限 im:message.group_msg 已开通并发布，以及附件格式和 10 MB 限制，再 @机器人重试。" if str(exc) == '话题附件读取失败' else "附件未能读取，请检查格式、10 MB 大小限制及飞书资源读取权限后重新发送。" if item['message_type'] != 'text' else "请求未能完成，请在 TCode 检查会话、账号权限与工作区后重试。")
                error_text += (" 停止状态不确定，请检查后重新发送 /stop。" if item["status"] == "stopping" else "")
                if is_task:
                    self.store.progress(conversation, item['id'], '❌ ' + error_text, True)
                else:
                    self.store.notify(conversation, 'failure:' + item['id'], error_text, item['id'])
                self.store.execute("UPDATE inbox SET status='failed' WHERE id=?", (item["id"],))
                logger.warning("Feishu request failed (%s)", type(exc).__name__)

    def observe_progress(self, conversation: dict, snapshot: dict) -> None:
        messages = {m['id']: m for m in snapshot.get('messages', [])}
        items = {i['id']: i for i in snapshot.get('items', [])}
        for request in self.store.rows("SELECT i.* FROM inbox i JOIN outbox o ON o.id='task-status:'||i.id WHERE i.conversation=? AND o.task_done=0 AND i.status='accepted' ORDER BY i.rowid", (conversation['key'],)):
            turn = snapshot.get('request_turns', {}).get(request['guidance_id'], {})
            final = messages.get(turn.get('final_message_id'))
            if final and turn.get('state') in {'completed', 'partial', 'blocked'}:
                text = final.get('content') or '已完成，结果见附件。'
                if conversation['direct'] and self.store.cursor('active:' + self.session_scope(conversation), conversation['key']) != conversation['key']:
                    numbers = self.store.rows('SELECT number FROM dm_sessions WHERE conversation=?', (conversation['key'],))
                    text = (f"来自会话 #{numbers[0]['number']}" if numbers else '来自之前的会话') + '\n\n' + text
                self.store.progress(conversation, request['id'], text, True,
                                    'task-result:' + conversation['session_id'] + ':' + final['id'])
                continue
            guidance = items.get(request['guidance_id'], {})
            if guidance.get('status') in {'failed', 'cancelled'} or turn.get('state') in {'failed', 'cancelled', 'interrupted'}:
                text = '⏹ 已停止' if guidance.get('status') == 'cancelled' or turn.get('state') in {'cancelled', 'interrupted'} else '❌ 本次处理失败，请到 TCode 查看详情。'
                self.store.progress(conversation, request['id'], text, True)
                continue
            if turn:
                text = '⚠️ 需要你确认，请到 TCode 处理。' if snapshot.get('permissions') or snapshot.get('question_requests') else '⏳ 正在处理…'
            elif guidance.get('status') in {'pending', 'claimed'}:
                text = '⏳ 已排队，等待执行…'
            else:
                text = '⏳ 已收到，等待执行…'
            self.store.progress(conversation, request['id'], text)

    async def observe(self) -> None:
        for conversation in self.store.rows("SELECT * FROM conversations"):
            if not self.allowed(conversation) or not conversation["target"]:
                continue
            try:
                snapshot = await self.backend.snapshot(conversation)
            except Exception as exc:
                logger.debug("Feishu snapshot unavailable (%s)", type(exc).__name__)
                continue
            sid = conversation["session_id"]
            self.observe_progress(conversation, snapshot)
            tracked = self.store.rows("SELECT id FROM outbox WHERE conversation=? AND id LIKE 'task-status:%'", (conversation['key'],))
            status_id = 'remote-status:' + conversation['key']
            if self.active_remote_tasks(snapshot) or self.store.rows('SELECT id FROM outbox WHERE id=?', (status_id,)):
                status_text = self.remote_status_text(snapshot)
                self.store.execute('INSERT OR IGNORE INTO outbox(id,conversation,target,text) VALUES (?,?,?,?)',
                                   (status_id, conversation['key'], conversation['target'], status_text))
                self.store.execute("UPDATE outbox SET text=?,status='pending',attempts=0,retry_at=0 WHERE id=? AND text<>?",
                                   (status_text, status_id, status_text))
            for request_id in ([] if tracked else snapshot.get("permissions", [])):
                self.store.notify(conversation, f"permission:{sid}:{request_id}",
                                  "此任务需要操作授权，请在 TCode 打开会话处理；飞书不会自动批准。\n" + sid)
            for question in ([] if tracked else snapshot.get("question_requests", [])):
                request_id = str(question.get("request_id") or question.get("id") or stable_id(json.dumps(question, sort_keys=True)))
                self.store.notify(conversation, f"question:{sid}:{request_id}",
                                  "此任务需要你回答问题，请在 TCode 打开会话处理。\n" + sid)
            for item in snapshot.get("items", []):
                if item.get("status") == "failed" and not self.store.rows("SELECT id FROM inbox WHERE guidance_id=? AND conversation=?", (item["id"], conversation["key"])):
                    self.store.notify(conversation, f"guidance-failed:{sid}:{item['id']}",
                                      "一条请求执行失败，请在 TCode 查看详情。\n" + sid)
            if snapshot.get("chat_running") or snapshot.get("pending_guidance_count") or snapshot.get("guidance_worker_active"):
                continue
            # Only durable terminal turns select answers; the public transcript also
            # contains drafts and validation-repair candidates. Missing proof is not a final.
            final_ids = set(snapshot.get("final_message_ids", []))
            if "final_sources" in snapshot:
                owned = {r['guidance_id'] for r in self.store.rows(
                    "SELECT guidance_id FROM inbox WHERE conversation=? AND status='accepted'",
                    (conversation['key'],)) if r['guidance_id']}
                final_ids = {mid for mid in final_ids if snapshot['final_sources'].get(mid) in owned}

            else:
                final_ids.update(m["id"] for m in snapshot.get("messages", [])
                                 if self.store.cursor("async-final:" + sid + ":" + m["id"]))
            for message in snapshot.get("messages", []):
                if not tracked and message.get("role") == "system" and str(message.get("content", "")).startswith("[Guidance Error]"):
                    self.store.notify(conversation, f"error:{sid}:{message['id']}",
                                      "本轮执行遇到错误或需要人工交互，请在 TCode 打开会话查看并继续。\n" + sid)
                if (message.get("id") in final_ids and message.get("role") == "assistant" and not message.get("tool_calls")
                        and (message.get('content') or message.get('content_blocks'))):
                    text = message.get("content") or ''
                    active = self.store.cursor("active:" + self.session_scope(conversation), conversation["key"])
                    if conversation["direct"] and active != conversation["key"]:
                        rows = self.store.rows("SELECT number FROM dm_sessions WHERE conversation=?", (conversation["key"],))
                        source = f"会话 #{rows[0]['number']}" if rows else "之前的会话"
                        text = f"来自{source}\n\n{text}"
                    if not self.store.cursor(f"task-result:{sid}:{message['id']}"):
                        self.store.notify(conversation, f"result:{sid}:{message['id']}", text)
                    for asset in media.outputs(message, sid):
                        key = stable_id(f"asset:{sid}:{message['id']}:{asset['path']}")
                        if self.store.rows('SELECT id FROM outbox WHERE id=?', (key,)) or self.store.cursor('asset-error:' + key):
                            continue
                        try:
                            name, data = await self.backend.export(conversation, asset['path'])
                            kind = 'image' if asset['kind'] == 'image' and Path(name).suffix.lower() in media.IMAGE_SUFFIXES else 'file'
                            self.store.attach(conversation, key, name, data, kind)
                        except Exception:
                            self.store.notify(conversation, 'asset-error:' + key,
                                              '一个结果附件无法发送（不存在、超出工作区或超过 10 MB）。请在 TCode 查看该会话。')
                            self.store.set_cursor('asset-error:' + key, '1')

    async def deliver(self) -> None:
        blocked = set()
        for item in self.store.rows("SELECT * FROM outbox WHERE status='pending' ORDER BY rowid LIMIT 100"):
            if item["conversation"] in blocked:
                continue
            if item["retry_at"] > time.time():
                blocked.add(item["conversation"])
                continue
            conversation = self.store.rows("SELECT * FROM conversations WHERE key=?", (item["conversation"],))[0]
            if not self.allowed(conversation):
                self.store.execute("UPDATE outbox SET status='denied' WHERE id=?", (item["id"],))
                continue
            try:
                if isinstance(self.backend, TCodeBackend):
                    self.backend.request(conversation)
                if item['id'].startswith(('remote-status:', 'task-status:')):
                    mid = await self.transport.status_message(conversation, item['text'], item['id'], item['remote_message_id'])
                    self.store.execute('UPDATE outbox SET remote_message_id=? WHERE id=?', (mid, item['id']))
                elif item['asset']:
                    asset = json.loads(item['asset'])
                    resource_key = item['uploaded_key']
                    if not resource_key:
                        resource_key = await self.transport.upload(asset)
                        self.store.execute('UPDATE outbox SET uploaded_key=? WHERE id=?', (resource_key, item['id']))
                    await self.transport.send_asset(conversation, item['target'], asset['kind'], resource_key, item['id'])
                elif conversation['direct'] or not item['target']:
                    await self.transport.send(conversation["chat"], item["text"], item["id"])
                else:
                    await self.transport.reply(item['target'], item['text'], item['id'])
            except Exception as exc:
                blocked.add(item["conversation"])
                if item['asset'] and item['attempts'] >= 4:
                    self.store.execute("UPDATE outbox SET status='failed' WHERE id=?", (item['id'],))
                    self.store.notify(conversation, 'asset-send-failed:' + item['id'],
                                      '结果附件多次发送失败。请核对飞书文件/图片上传权限与网络，修复后重新请求发送；文件仍保留在 TCode。')
                    continue
                delay = min(300, 2 ** min(item["attempts"] + 1, 8))
                self.store.execute("UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE id=?", (time.time() + delay, item["id"]))
                logger.warning("Feishu reply pending; retry in %ss (%s)", delay, type(exc).__name__)
            else:
                self.store.execute("UPDATE outbox SET status='sent' WHERE id=? AND text=?", (item["id"], item['text']))

    async def backfill(self) -> None:
        chats = {chat: False for chat in self.config.chats}
        chats.update({c["chat"]: bool(c["direct"]) for c in self.store.rows("SELECT * FROM conversations") if self.allowed(c)})
        for chat, direct in chats.items():
            key = "history:" + chat
            until = int(time.time())
            since = self.store.cursor(key)
            if not since:
                self.store.set_cursor(key, str(until))
                continue  # Never ingest all old chat messages on first activation.
            page, pages = "", set()
            while True:
                params = dict(container_id_type="chat", container_id=chat, start_time=since,
                              end_time=str(until), sort_type="ByCreateTimeAsc", page_size=50)
                if page:
                    params["page_token"] = page
                result = await self.transport.command("api", "GET", "/open-apis/im/v1/messages",
                                                      "--params", json.dumps(params))
                for message in result.get("items", []):
                    if message.get("chat_id") != chat or message.get("deleted"):
                        continue
                    try:
                        content = json.loads(message.get("body", {}).get("content", "{}"))
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(content, dict):
                        continue
                    self.receive({**message, "content": content.get('text') or ('请查看附件' if message.get('msg_type') != 'text' else ''),
                                  "chat_type": "p2p" if direct else "group",
                                  "sender_id": message.get("sender", {}).get("id"),
                                  "sender_type": message.get("sender", {}).get("sender_type")})
                if not result.get("has_more"):
                    break
                page = result.get("page_token")
                if not page or page in pages:
                    raise ValueError("Feishu history pagination did not advance")
                pages.add(page)
            self.store.set_cursor(key, str(max(int(since), until - 1)))


    async def watch(self) -> None:
        retry = 1
        while True:
            try:
                self.connection_status = "listening"
                async for event in self.transport.events():
                    if event.get("message_id") and event.get("chat_id") and event.get("sender_id"):
                        self.last_event_at = time.time()
                        self.connection_status = "receiving"
                    self.receive(event)
                    retry = 1
            except Exception as exc:
                self.connection_status = "retrying"
                logger.warning("Feishu connection interrupted (%s); reconnecting", type(exc).__name__)
            await asyncio.sleep(retry)
            retry = min(60, retry * 2)

    async def loop(self, operation, seconds: float) -> None:
        while True:
            try:
                await operation()
            except Exception as exc:
                logger.warning("Feishu %s failed (%s)", operation.__name__, type(exc).__name__)
            await asyncio.sleep(seconds)

    async def start(self) -> None:
        self.tasks = [asyncio.create_task(self.watch(), name="feishu-events"),
                      asyncio.create_task(self.interactions.watch(), name="feishu-card-events")]
        for operation, interval in ((self.dispatch, 1), (self.observe, self.config.poll_seconds),
                                    (self.deliver, 1), (self.backfill, 60),
                                    (self.interactions.observe, self.config.poll_seconds),
                                    (self.interactions.continue_results, self.config.poll_seconds)):
            self.tasks.append(asyncio.create_task(self.loop(operation, interval), name="feishu-" + operation.__name__))

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.connection_status = "stopped"
        self.store.close()


_service: FeishuService | None = None


async def check_config(path: str) -> FeishuConfig:
    from . import agui_api
    from .auth import require_current_user
    from .guidance_runtime import guidance_v2_enabled
    if not guidance_v2_enabled():
        raise ValueError("Feishu requires TCODE_STEER_V2_ENABLED=1 for durable request deduplication")
    config = FeishuConfig.model_validate_json(Path(path).expanduser().read_text())
    if any(not re.fullmatch(r"ou_[A-Za-z0-9_-]+", key) for key in config.users):
        raise ValueError("Feishu users must be open_ids (ou_...)")
    if any(not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat) for chat in config.chats):
        raise ValueError("Feishu chats must be chat_ids (oc_...)")
    backend = TCodeBackend()
    for binding in config.users.values():
        request = backend.request({"binding": binding.model_dump_json()})
        agui_api._workspace_path(require_current_user(request), binding.workspace_id)
    await LarkCLI(config.cli).verify(config)
    return config


async def start_feishu() -> None:
    global _service
    config_path = os.environ.get("TCODE_FEISHU_CONFIG", "").strip()
    if not config_path or _service is not None:
        return
    from .state import ServerState
    config = await check_config(config_path)
    transport = LarkCLI(config.cli)
    # One configured bot per server; use a separate database when switching applications.
    path = Path(ServerState.get_workdir()) / ".tcode" / ("feishu-" + config.app_id + ".sqlite")
    service = FeishuService(config, Store(path), transport=transport)
    await service.start()
    _service = service
    logger.info("Feishu bot transport started")


async def stop_feishu() -> None:
    global _service
    if _service is not None:
        await _service.close()
        _service = None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="检查飞书机器人身份、TCode 用户和工作区；不发送消息或运行任务")
    parser.add_argument("--check", required=True, metavar="CONFIG_JSON")
    args = parser.parse_args()
    asyncio.run(check_config(args.check))
    print("飞书配置检查通过。尚未验证事件订阅与消息发送；请继续执行文档中的飞书联调步骤。")
