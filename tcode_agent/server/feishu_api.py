"""Account-scoped Feishu onboarding. Secrets go only to isolated CLI profiles."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .auth import require_current_user
from .feishu_runtime import Binding, FeishuConfig, FeishuService, LarkCLI, Store, stable_id
from .state import ServerState

router = APIRouter(prefix="/api/feishu", tags=["feishu"])
logger = logging.getLogger(__name__)
PERMISSIONS = [
    {"scope": "im:message.p2p_msg:readonly", "purpose": "接收私聊消息"},
    {"scope": "im:message.group_at_msg:readonly", "purpose": "接收群内 @ 消息"},
    {"scope": "im:message:send_as_bot", "purpose": "机器人回复与结果通知"},
    {"scope": "im:message:readonly", "purpose": "读取消息及历史，恢复遗漏的追问"},
    {"scope": "im:resource", "purpose": "获取与上传图片或文件资源（平台若提供 im:resource:upload，二选一即可）"},
]


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9_-]+$", max_length=100)
    app_secret: SecretStr | None = None
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=128)
    chats: list[str] = Field(default_factory=list, max_length=30)


class Manager:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        # ponytail: one managing process; multiple HTTP workers must route to that process.
        self.lease = sqlite3.connect(root / "manager-lock.sqlite", timeout=0)
        try:
            self.lease.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            self.lease.close()
            raise RuntimeError("飞书接入由另一个服务进程管理，请使用单进程部署") from None
        self.db = sqlite3.connect(root / "settings.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS integrations (
            owner TEXT PRIMARY KEY, app_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL,
            code_hash TEXT NOT NULL DEFAULT '', code_expires REAL NOT NULL DEFAULT 0
        )""")
        self.db.commit()
        (root / "settings.sqlite").chmod(0o600)
        self.services: dict[str, FeishuService] = {}
        self.errors: dict[str, str] = {}
        self.mutation = asyncio.Lock()

    def get(self, owner: str) -> dict:
        row = self.db.execute("SELECT data FROM integrations WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, owner: str, data: dict) -> None:
        with self.db:
            self.db.execute("""INSERT INTO integrations(owner,app_id,data) VALUES (?,?,?)
                ON CONFLICT(owner) DO UPDATE SET app_id=excluded.app_id,data=excluded.data""",
                (owner, data["app_id"], json.dumps(data)))

    def cli(self, data: dict) -> LarkCLI:
        return LarkCLI(os.environ.get("TCODE_LARK_CLI", "lark-cli"), self.root / data["profile"])

    def public(self, owner: str) -> dict:
        data = self.get(owner)
        service = self.services.get(owner)
        return {
            "configured": bool(data), "app_id": data.get("app_id", ""),
            "secret_configured": bool(data), "workspace_id": data.get("workspace_id", ""),
            "chats": data.get("chats", []), "bound": bool(data.get("sender")),
            "sender_id": data.get("sender", ""), "enabled": data.get("enabled", False),
            "status": service.connection_status if service else "stopped",
            "last_event_at": service.last_event_at if service else 0,
            "error": self.errors.get(owner, ""), "permissions": PERMISSIONS,
            "permissions_status": "manual_check_required",
        }

    def intercept(self, owner: str, event: dict) -> bool:
        text = event.get("content")
        if not isinstance(text, str) or not text.strip().startswith("/bind"):
            return False
        # Never pass binding codes to the model, history inbox or public transcript.
        if (event.get("sender_type") != "user" or event.get("chat_type") != "p2p"
                or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", str(event.get("sender_id", "")))
                or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", str(event.get("chat_id", "")))):
            return True
        parts = text.strip().split()
        if len(parts) != 2 or parts[0] != "/bind":
            return True
        digest = hashlib.sha256(parts[1].encode()).hexdigest()
        with self.db:
            changed = self.db.execute("""UPDATE integrations SET code_hash='',code_expires=0
                WHERE owner=? AND code_hash=? AND code_expires>?""", (owner, digest, time.time())).rowcount
            if changed:
                data = self.get(owner)
                data["sender"] = event["sender_id"]
                self.save(owner, data)
                self.services[owner].config.users = {data["sender"]: Binding(
                    tenant_id=data["tenant_id"], user_id=data["user_id"], workspace_id=data["workspace_id"])}
        return True

    async def stop(self, owner: str) -> None:
        service = self.services.pop(owner, None)
        if service:
            await service.close()

    async def start(self, owner: str) -> None:
        from .guidance_runtime import guidance_v2_enabled
        from .agui_api import _workspace_path
        from .feishu_runtime import TCodeBackend
        if not guidance_v2_enabled():
            raise HTTPException(409, "部署管理员需启用 TCODE_STEER_V2_ENABLED=1 后再启动飞书接入")
        await self.stop(owner)
        data = self.get(owner)
        binding = Binding(tenant_id=data["tenant_id"], user_id=data["user_id"], workspace_id=data["workspace_id"])
        identity_request = TCodeBackend().request({"binding": binding.model_dump_json()})
        _workspace_path(require_current_user(identity_request), binding.workspace_id)
        config = FeishuConfig(app_id=data["app_id"], bot_id=data["bot_id"], chats=data["chats"],
                              users={data["sender"]: binding} if data.get("sender") else {})
        transport = self.cli(data)
        await transport.verify(config)
        # Same application lock as the legacy operator configuration prevents double consumption.
        store = Store(self.root.parent / ("feishu-" + data["app_id"] + ".sqlite"))
        service = FeishuService(config, store, transport=transport)
        service.namespace = data["generation"] + ":"
        service.on_event = lambda event: self.intercept(owner, event)
        self.services[owner] = service
        try:
            await service.start()
        except BaseException:
            await self.stop(owner)
            raise
        self.errors.pop(owner, None)

    async def close(self) -> None:
        for owner in list(self.services):
            await self.stop(owner)
        self.db.close()
        self.lease.close()


_manager: Manager | None = None


def manager() -> Manager:
    global _manager
    if _manager is None:
        try:
            _manager = Manager(Path(ServerState.get_workdir()) / ".tcode" / "feishu-integrations")
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None
    return _manager


def owner_of(request: Request) -> tuple[str, object]:
    user = require_current_user(request)
    return stable_id(json.dumps([user.tenant_id, user.user_id])), user


@router.get("")
async def status(request: Request, response: Response):
    owner, _ = owner_of(request)
    response.headers["Cache-Control"] = "no-store"
    return manager().public(owner)


@router.put("")
async def configure(payload: Setup, request: Request):
    import re
    from .agui_api import _workspace_path
    owner, user = owner_of(request)
    _workspace_path(user, payload.workspace_id)
    if any(not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat) for chat in payload.chats):
        raise HTTPException(422, "群 ID 必须为 oc_ 开头的有效标识")
    m = manager()
    async with m.mutation:
        old = m.get(owner)
        collision = m.db.execute("SELECT owner FROM integrations WHERE app_id=? AND owner!=?",
                                 (payload.app_id, owner)).fetchone()
        if collision:
            raise HTTPException(409, "此应用已被其他 TCode 账号配置，请使用独立应用")
        secret = payload.app_secret.get_secret_value() if payload.app_secret else ""
        if not secret and (not old or old["app_id"] != payload.app_id):
            raise HTTPException(422, "首次配置或更换应用时必须填写 App Secret")
        if len(secret) > 512 or any(c in secret for c in "\r\n\x00"):
            raise HTTPException(422, "App Secret 格式不正确")
        profile = uuid.uuid4().hex if secret else old["profile"]
        directory = m.root / profile
        try:
            cli = m.cli({"profile": profile})
            if secret:
                await verify_secret(payload.app_id, secret)
                directory.mkdir(mode=0o700)
                # Official CLI SecretInput supports file references. Avoid config init's
                # shared OS keychain entry: a failed rotation must not replace old secrets.
                for filename, value in (("app-secret", secret), ("config.json", json.dumps({
                    "strictMode": "bot", "currentApp": payload.app_id, "apps": [{
                        "appId": payload.app_id, "brand": "feishu", "users": [],
                        "appSecret": {"source": "file", "id": str(directory / "app-secret")},
                    }],
                }))):
                    fd = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as output:
                        output.write(value)
            verified = json.loads(await cli.raw("auth", "status", "--json", "--verify"))
            if (verified.get("appId") != payload.app_id
                    or not verified.get("identities", {}).get("bot", {}).get("verified")):
                raise ValueError("Bot authentication failed")
            info = json.loads((await cli.raw("api", "GET", "/open-apis/bot/v3/info",
                                             "--as", "bot", "--format", "ndjson")).splitlines()[0])
            bot_id = info.get("bot", {}).get("open_id", "")
            if info.get("code") != 0 or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", bot_id):
                raise ValueError("Bot information unavailable")
        except Exception:
            if secret:
                shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(400, "鉴权未通过。检查 App ID、App Secret、机器人能力和平台访问策略；确认服务端已安装 lark-cli。原配置未修改。") from None
        same_app = old.get("app_id") == payload.app_id
        data = dict(app_id=payload.app_id, bot_id=bot_id, profile=profile,
                    workspace_id=payload.workspace_id, chats=list(dict.fromkeys(payload.chats)),
                    tenant_id=user.tenant_id, user_id=user.user_id,
                    enabled=old.get("enabled", False) if same_app else False,
                    sender=old.get("sender", "") if same_app else "",
                    generation=old["generation"] if same_app else uuid.uuid4().hex)
        service = m.services.get(owner)
        # Scope-only changes take effect in the existing listener. Keep the namespace
        # and DM selection; workspace is already part of the conversation binding.
        hot_update = same_app and profile == old["profile"]
        try:
            if not hot_update:
                await m.stop(owner)
            m.save(owner, data)
            if hot_update and service:
                service.config.chats = data["chats"]
                service.config.users = {data["sender"]: Binding(
                    tenant_id=data["tenant_id"], user_id=data["user_id"],
                    workspace_id=data["workspace_id"])} if data["sender"] else {}
            elif data["enabled"]:
                await m.start(owner)
        except Exception:
            if old:
                m.save(owner, old)
                if not hot_update and old.get("enabled"):
                    try:
                        await m.start(owner)
                    except Exception:
                        m.errors[owner] = "原配置已恢复，但连接恢复失败，请重新连接。"
            if secret:
                shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(400, "配置更新失败，原配置已保留；请检查连接状态后重试。") from None
        if not same_app:
            with m.db:
                m.db.execute("UPDATE integrations SET code_hash='',code_expires=0 WHERE owner=?", (owner,))
        m.errors.pop(owner, None)
        if old.get("profile") and old["profile"] != profile:
            shutil.rmtree(m.root / old["profile"], ignore_errors=True)
        return m.public(owner)


async def verify_secret(app_id: str, secret: str) -> None:
    # Validate the submitted secret itself, never accept a cached CLI tenant token as proof.
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                     json={"app_id": app_id, "app_secret": secret})
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise ValueError("Application authentication failed")


@router.delete("")
async def delete_configuration(request: Request):
    owner, _ = owner_of(request)
    m = manager()
    async with m.mutation:
        data = m.get(owner)
        service = m.services.get(owner)
        if service:
            service.config.users.clear()
        await m.stop(owner)
        # Stop automatic restoration before removing files. A failed filesystem cleanup
        # retains the record (disabled) so deletion can be retried from the UI.
        if data:
            data.update(enabled=False, sender="")
            m.save(owner, data)
            try:
                directory = m.root / data["profile"]
                if directory.exists():
                    shutil.rmtree(directory)
            except OSError:
                raise HTTPException(500, "连接已停止，但凭据文件清理失败，请重试删除或联系管理员") from None
        with m.db:
            m.db.execute("DELETE FROM integrations WHERE owner=?", (owner,))
        m.errors.pop(owner, None)
        return m.public(owner)


@router.post("/start")
async def start(request: Request):
    owner, _ = owner_of(request)
    m = manager()
    async with m.mutation:
        data = m.get(owner)
        if not data:
            raise HTTPException(409, "请先保存并验证应用凭据")
        try:
            await m.start(owner)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(409, "启动失败：请检查凭据、lark-cli 安装和是否有其他进程使用此机器人") from None
        data["enabled"] = True
        m.save(owner, data)
        return m.public(owner)


@router.post("/stop")
async def stop(request: Request):
    owner, _ = owner_of(request)
    m = manager()
    async with m.mutation:
        await m.stop(owner)
        data = m.get(owner)
        if data:
            data["enabled"] = False
            m.save(owner, data)
        return m.public(owner)


@router.post("/binding-code")
async def binding_code(request: Request, response: Response):
    owner, _ = owner_of(request)
    m = manager()
    async with m.mutation:
        if owner not in m.services:
            raise HTTPException(409, "请先启动连接")
        if m.get(owner).get("sender"):
            raise HTTPException(409, "请先解除已有绑定，再生成绑定码")
        code = secrets.token_urlsafe(24)
        expires = time.time() + 600
        with m.db:
            m.db.execute("UPDATE integrations SET code_hash=?,code_expires=? WHERE owner=?",
                         (hashlib.sha256(code.encode()).hexdigest(), expires, owner))
        response.headers["Cache-Control"] = "no-store"
        return {"command": "/bind " + code, "expires_at": expires}


@router.delete("/binding")
async def unbind(request: Request):
    owner, _ = owner_of(request)
    m = manager()
    async with m.mutation:
        data = m.get(owner)
        if data:
            service = m.services.get(owner)
            if service:
                service.config.users.clear()
            await m.stop(owner)
            data.update(sender="", enabled=False, generation=uuid.uuid4().hex)
            m.save(owner, data)
            with m.db:
                m.db.execute("UPDATE integrations SET code_hash='',code_expires=0 WHERE owner=?", (owner,))
        return m.public(owner)


async def start_managed_feishu() -> None:
    # Existing installations remain untouched until someone configures an integration.
    path = Path(ServerState.get_workdir()) / ".tcode" / "feishu-integrations" / "settings.sqlite"
    if not path.exists():
        return
    try:
        m = manager()
    except HTTPException:
        logger.warning("Feishu integrations are managed by another server process")
        return
    for row in m.db.execute("SELECT owner FROM integrations").fetchall():
        owner = row[0]
        if m.get(owner).get("enabled"):
            try:
                await m.start(owner)
            except Exception:
                m.errors[owner] = "自动恢复失败，请检查配置后重新启动连接"
                logger.warning("Feishu integration could not resume")


async def stop_managed_feishu() -> None:
    global _manager
    if _manager is not None:
        await _manager.close()
        _manager = None
