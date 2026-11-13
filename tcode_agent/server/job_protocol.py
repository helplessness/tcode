"""TCode's loopback-only Job Protocol for explicitly configured MCP targets."""

import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from itertools import islice
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
import time

from jsonschema import Draft202012Validator
from starlette.responses import JSONResponse

from ..artifact_transport import find_output_bundle
from ..mcp.dynamic_headers import context_schema, render, validate_rules
from .managed_job_protocol import LOCAL_TOKEN, WORKER_HEADER, WORKER_TOKEN, configured_async_targets, read_config

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
METHODS = ("submitTool", "statusTool", "resultTool")
OPTIONAL_METHODS = ("cancelTool", "lookupTool")
MAX_JOB_SECONDS = 30 * 86400
DEFAULT_BLOCKING_WAIT_SECONDS = 72 * 3600
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _schema(value):
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError("工具 Schema 必须声明对象")
    def check(node):
        if isinstance(node, dict):
            if node.keys() & {"$ref", "$dynamicRef", "$recursiveRef", "$id", "$schema"}:
                raise ValueError("工具 Schema 必须内联，不支持引用")
            for item in node.values():
                check(item)
        elif isinstance(node, list):
            for item in node:
                check(item)
    check(value)
    Draft202012Validator.check_schema(value)
    return value


def _errors(schema, arguments):
    errors = []
    for error in islice(Draft202012Validator(schema).iter_errors(arguments), 20):
        path = ".".join(map(str, error.absolute_path))
        if error.validator == "required" and isinstance(error.instance, dict):
            errors.extend({"field": ".".join(filter(None, (path, field))), "message": "缺少必填字段"}
                          for field in error.validator_value if field not in error.instance)
        else:
            errors.append({"field": path or "arguments", "message": f"不符合 {error.validator} 约束"})
    return errors[:20]


def _unwrap(result):
    if result.is_error:
        raise ValueError("远端 MCP 返回工具错误")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    if isinstance(result.data, dict):
        return result.data
    for block in result.content:
        if getattr(block, "type", "") == "text":
            try:
                value = json.loads(block.text)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("远端任务工具必须返回 JSON 对象")


def _client(target, context=None, timeout=20):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from ..mcp.manager_auto import MCPManager

    headers = MCPManager._resolve_header_environment(target.get("headers", {}))
    if target.get("token"):
        headers["Authorization"] = "Bearer " + target["token"]
    rules = validate_rules(target.get("dynamic_headers", {}), headers)
    supplied = context or {}
    if (not isinstance(supplied, dict) or set(supplied) - {"rules", "values", "target_version"}
            or (context is not None and supplied.get("rules", {}) != rules)):
        raise ValueError("异步调用上下文与已批准的 Header 规则不一致")
    if context is not None:
        headers.update(render(rules, supplied.get("values", {})))
    return Client(StreamableHttpTransport(target["url"], headers=headers), timeout=timeout, init_timeout=20)


def _wire(data):
    if not isinstance(data, dict):
        raise ValueError("需要对象参数")
    return data


def _input(input, input_config, payload):
    supplied = [value for value in (input, input_config, payload) if value is not None]
    if len(supplied) != 1:
        raise ValueError("请只提供一种参数封装")
    return _wire(supplied[0])


class JobProtocol:
    def __init__(self, root, config_paths=()):
        self.path = root / "tcode-jobs.sqlite3"
        self.config_paths = config_paths
        self.tasks = {}
        self.operations = {}
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS targets (id TEXT, version TEXT, active INTEGER, data TEXT, PRIMARY KEY(id, version))")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            os.chmod(self.path, 0o600)
            with db:
                yield db

    def static_targets(self):
        from .desktop_config import remote_url
        targets = {}
        for path in self.config_paths:
            for name, entry in read_config(path).items():
                targets.setdefault(name, entry)
        return {name: {**entry, "server": name, "version": _digest(entry)[:24], "execution": {"mode": "blocking"}}
                for name, entry in configured_async_targets(targets).items()
                if IDENTIFIER.fullmatch(name) and not entry.get("file_transfer")
                and entry.get("transport", "streamable-http") == "streamable-http"
                and remote_url(entry.get("url", ""), allow_loopback=True)}

    @staticmethod
    def scope(data):
        return [str(data.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id")]

    def registration(self, name, version=""):
        with self.db() as db:
            row = (db.execute("SELECT data FROM targets WHERE id=? AND version=?", (name, version)).fetchone()
                   if version else db.execute("SELECT data FROM targets WHERE id=? AND active=1", (name,)).fetchone())
        return json.loads(row[0]) if row else None

    def active(self):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM targets WHERE active=1")]

    @staticmethod
    def public(target):
        return {key: target[key] for key in ("server", "owner", "plugin", "version", "execution", "tools", "dynamic_headers") if key in target}

    async def check_target(self, name, target):
        from .desktop_config import remote_url
        if (not re.fullmatch(r"plugin-[a-f0-9]{32}", name) or target.get("server") != name
                or not isinstance(target.get("owner"), str) or target["owner"].count("/") != 1
                or not all(target["owner"].split("/")) or not target.get("plugin")
                or target.get("transport", "streamable-http") != "streamable-http"
                or not remote_url(target.get("url", ""), allow_loopback=True)):
            raise ValueError("目标注册信息无效")
        execution = _wire(target.get("execution"))
        mode = execution.get("mode")
        if mode not in {"remote-job", "blocking"}:
            raise ValueError("不支持的执行方式")
        methods = ([execution.get(key, "") for key in METHODS]
                   + [execution[key] for key in OPTIONAL_METHODS if key in execution]
                   if mode == "remote-job" else [])
        tools = target.get("tools")
        if not isinstance(tools, list) or not 1 <= len(tools) <= 100:
            raise ValueError("目标必须声明 1 至 100 个工具")
        names = set()
        for tool in tools:
            tool = _wire(tool)
            tool_name = tool.get("name", "")
            if not isinstance(tool_name, str) or not IDENTIFIER.fullmatch(tool_name) or tool_name in names:
                raise ValueError("工具名称无效或重复")
            names.add(tool_name)
            _schema(tool.get("inputSchema"))
            if tool.get("argumentStyle", "flat") not in {"flat", "input_config"}:
                raise ValueError("工具参数封装方式无效")
            if _wire(tool.get("output")).get("contract") not in {"json", "artifact-bundle-v1"}:
                raise ValueError("不支持的工具结果契约")
            for field in tool.get("files", []):
                if (not isinstance(field, dict) or not str(field.get("pointer", "")).startswith("/")
                        or field.get("kind") not in {"file", "directory"} or field.get("transport") != "artifact"):
                    raise ValueError("文件字段只允许已声明的 Artifact 引用")
        if not all(isinstance(method, str) and IDENTIFIER.fullmatch(method) for method in methods):
            raise ValueError("远端异步任务方法无效")
        async with _client(target) as client:
            available = {item.name: item.inputSchema for item in await client.list_tools()}
        if (set(methods) if methods else names) - available.keys():
            raise ValueError("远端 MCP 缺少声明的工具")
        for tool in tools:
            if mode == "blocking":
                actual = available[tool["name"]]
                if tool.get("argumentStyle") == "input_config":
                    actual = actual.get("properties", {}).get("input_config")
                if actual != tool["inputSchema"]:
                    raise ValueError("远端工具 Schema 与已声明契约不一致")
        return {"ok": True, "mode": mode, "tools": sorted(set(methods) or names), "resumable": bool(methods)}

    async def tool(self, data, *, submitting=False):
        server, name = data.get("target_server") or data.get("server"), data.get("target_tool") or data.get("tool")
        if not isinstance(server, str) or not isinstance(name, str):
            raise ValueError("缺少计算目标或工具")
        if server.startswith("plugin-"):
            scope = self.scope(data)
            active = self.registration(server)
            if not active or active["owner"] != "/".join(scope[:2]):
                raise ValueError("目标未启用或不属于当前用户")
            target = active
            tool = next((item for item in target["tools"] if item["name"] == name), None)
        else:
            target = self.static_targets().get(server)
            tool = None
            if target:
                self.check_version(data, target, submitting)
                async with _client(target) as client:
                    tools = await client.list_tools()
                found = next((item for item in tools if item.name == name), None)
                if found:
                    tool = {"name": name, "inputSchema": _schema(found.inputSchema),
                            "output": {"contract": "json"}}
        if not target or not tool:
            raise ValueError("内置网关未注册此工具；旧领域工具请使用原外部 Job Protocol")
        self.check_version(data, target, submitting)
        return target, tool

    @staticmethod
    def check_version(data, target, submitting):
        version = data.get("target_version")
        context_version = _wire(data.get("call_context", {})).get("target_version")
        if ((submitting or version is not None) and version != target["version"]
                or context_version and context_version != target["version"]):
            raise ValueError("异步目标版本已变化或未固定；未发送任务，请重新确认")

    async def describe(self, data):
        if not (data.get("target_server") or data.get("server")):
            owner = "/".join(self.scope(data)[:2])
            return {"ok": True, "plugin_targets": [self.public(t) for t in self.active() if t["owner"] == owner]}
        try:
            target, tool = await self.tool(data)
            schema = tool["inputSchema"]
            arguments = data.get("arguments")
            errors = _errors(schema, arguments) if arguments is not None else []
            result = {"ok": True, "schema_hash": _digest(schema), "call_context_supported": True,
                      "target_version": target["version"],
                      "required": schema.get("required", []), "validation": {"valid": not errors, "errors": errors}}
            if data.get("detail") == "full":
                result["input_schema"] = schema
            rules = target.get("dynamic_headers")
            if rules:
                result["call_context_schema"] = context_schema(rules)
            fields = data.get("fields")
            if isinstance(fields, list):
                result["fields"] = {key: value for key, value in schema.get("properties", {}).items() if key in fields}
            return result
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}

    def load(self, db, job_id):
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, db, job):
        db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job["job_id"], _json(job)))

    @staticmethod
    def receipt(job):
        return {key: job[key] for key in ("job_id", "process_id", "status", "submitted_at")}

    def submission_id(self, data):
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not IDENTIFIER.fullmatch(run_id) or "command" in data:
            raise ValueError("需要稳定的 run_id；内置网关不执行宿主命令")
        return "tcode-job-" + _digest([self.scope(data), run_id])[:32]

    def lookup_submission(self, data):
        with self.db() as db:
            job = self.load(db, self.submission_id(data))
        if not job:
            return {"submission_state": "unknown"}
        return {**self.receipt(job), "job_token": job["job_token"]}

    async def submit(self, data):
        scope = self.scope(data)
        job_id = self.submission_id(data)
        arguments = _wire(data.get("arguments", {}))
        context = _wire(data.get("call_context", {}))
        digest = _digest([data.get("target_server"), data.get("target_tool"), arguments, context, data.get("target_version")])

        def existing(db):
            job = self.load(db, job_id)
            if job and job["digest"] != digest:
                raise ValueError("同一任务标识不可改动获批参数")
            return job

        with self.db() as db:
            job = existing(db)
        if not job:
            target, tool = await self.tool(data, submitting=True)
            errors = _errors(tool["inputSchema"], arguments)
            if errors:
                raise ValueError("工具参数未通过契约校验: " + _json(errors))
            _client(target, context)  # Check frozen context before any outbound execution.
            timeout = data.get("timeout_seconds") or 0
            if type(timeout) not in {int, float} or not 0 <= timeout <= MAX_JOB_SECONDS:
                raise ValueError("无效的任务超时设置")
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                job = existing(db)
                if not job:
                    job = {"job_id": job_id, "process_id": job_id, "job_token": secrets.token_urlsafe(32),
                           "submitted_at": datetime.now(timezone.utc).isoformat(), "status": "queued",
                           "scope": scope, "digest": digest, "target": target, "tool": tool,
                           "arguments": arguments, "context": context, "timeout": timeout,
                           "call_timeout": (20 if target["execution"]["mode"] == "remote-job"
                                            else timeout or DEFAULT_BLOCKING_WAIT_SECONDS),
                           "deadline": time.time() + timeout if timeout else None}
                    self.save(db, job)
        self.schedule(job)
        return {**self.receipt(job), "job_token": job["job_token"]}

    def schedule(self, job):
        identifier = job["job_id"]
        if job["status"] == "queued" and identifier not in self.tasks:
            task = asyncio.create_task(self.dispatch(identifier))
            self.tasks[identifier] = task
            task.add_done_callback(lambda done: self.tasks.pop(identifier, None))

    async def dispatch(self, identifier):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.load(db, identifier)
            if job["status"] != "queued":
                return
            job["status"] = "running"
            self.save(db, job)
        target, tool, args = job["target"], job["tool"], job["arguments"]
        execution = target["execution"]
        sent = False
        persisted = False
        try:
            timeout = job["timeout"]
            if job["deadline"] and job["deadline"] <= time.time():
                job.update(status="timed_out", error="发送前已超过任务期限")
            else:
                call_timeout = job.get("call_timeout", 20 if execution["mode"] == "remote-job" else timeout or 20)
                async with _client(target, job["context"], timeout=call_timeout) as client:
                    if job["deadline"] and job["deadline"] <= time.time():
                        job.update(status="timed_out", error="发送前已超过任务期限")
                    else:
                        sent = True
                        await self._dispatch(client, job, tool, args, execution)
                        # Persist the receipt before session teardown can fail or be cancelled.
                        with self.db() as db:
                            self.save(db, job)
                        persisted = True
        except Exception:
            # A disconnect after sending is not proof of failure. Never resend.
            if not sent:
                job.update(status="failed", error="远端连接失败，尚未发送任务")
        if not persisted:
            with self.db() as db:
                self.save(db, job)

    async def _dispatch(self, client, job, tool, args, execution):
        if execution["mode"] == "remote-job":
            result = _unwrap(await client.call_tool(execution["submitTool"],
                {"request_id": job["job_id"], "tool": tool["name"], "arguments": args}, raise_on_error=False))
            remote_id = result.get("job_id")
            if not self.valid_remote_id(remote_id):
                raise ValueError("远端未返回有效任务标识")
            job["remote_id"] = remote_id
        else:
            payload = {"input_config": args} if tool.get("argumentStyle") == "input_config" else args
            result = await client.call_tool(tool["name"], payload, raise_on_error=False)
            if result.is_error:
                job.update(status="failed", error="远端 MCP 返回工具错误")
            else:
                job.update(status="completed", result=result.structured_content or result.data or
                           {"content": [block.model_dump(mode="json") for block in result.content]})

    @staticmethod
    def valid_remote_id(value):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 1024

    def authorized_job(self, data):
        identifier = data.get("job_id")
        with self.db() as db:
            job = self.load(db, identifier) if isinstance(identifier, str) else None
        token = data.get("job_token")
        if (not job or not isinstance(token, str) or not hmac.compare_digest(token.encode(), job["job_token"].encode())
                or data.get("process_id") not in (None, "", job["process_id"])
                or any(value and value != expected for value, expected in zip(self.scope(data), job["scope"]))):
            raise ValueError("任务不存在或凭据不匹配")
        return job

    async def query(self, data, action):
        job = self.authorized_job(data)
        identifier = job["job_id"]
        # Serialize queries/cancellation for one job, not unrelated jobs. This
        # avoids duplicate collection and racing terminal receipts.
        lock, count = self.operations.get(identifier, (asyncio.Lock(), 0))
        self.operations[identifier] = lock, count + 1
        try:
            async with lock:
                return await self._query(self.authorized_job(data), action)
        finally:
            _, count = self.operations[identifier]
            if count == 1:
                self.operations.pop(identifier)
            else:
                self.operations[identifier] = lock, count - 1

    async def _query(self, job, action):
        identifier = job["job_id"]
        execution = job["target"]["execution"]
        if job["status"] == "queued":
            if action == "cancel":
                job["status"] = "cancelled"
                with self.db() as db:
                    self.save(db, job)
                return {**self.receipt(job), "terminated": True}
            self.schedule(job)
        if action == "cancel" and job["status"] in TERMINAL:
            return {**self.receipt(job), "terminated": False, "reason": "already_finished"}
        orphaned = job["status"] == "running" and not job.get("remote_id") and identifier not in self.tasks
        if orphaned and execution["mode"] == "remote-job" and execution.get("lookupTool"):
            # Read-only receipt recovery. Never replay a third-party submit.
            async with _client(job["target"], job["context"]) as client:
                receipt = _unwrap(await client.call_tool(execution["lookupTool"],
                    {"request_id": identifier}, raise_on_error=False))
                if self.valid_remote_id(receipt.get("job_id")):
                    job["remote_id"] = receipt["job_id"]
                    with self.db() as db:
                        self.save(db, job)
        if job.get("remote_id") and (job["status"] not in TERMINAL or action == "result" and "result" not in job):
            async with _client(job["target"], job["context"]) as client:
                args = {"job_id": job["remote_id"]}
                expired = job["deadline"] and time.time() >= job["deadline"]
                if action == "cancel" and not execution.get("cancelTool"):
                    raise ValueError("此远端服务未提供取消接口")
                cancel = bool(execution.get("cancelTool") and
                              (action == "cancel" or expired and job["status"] not in TERMINAL))
                method = execution["cancelTool"] if cancel else execution["statusTool"]
                result = _unwrap(await client.call_tool(method, args, raise_on_error=False))
                status = result.get("status")
                if status not in TERMINAL | {"queued", "running"}:
                    raise ValueError("远端任务状态无效")
                if status in TERMINAL:
                    with self.db() as db:
                        job = self.load(db, identifier)
                        if job["status"] not in TERMINAL:
                            job["status"] = "timed_out" if cancel and expired and action != "cancel" and status == "cancelled" else status
                        self.save(db, job)
                if action == "result" and job["status"] == "completed" and "result" not in job:
                    collected = _unwrap(await client.call_tool(execution["resultTool"], args, raise_on_error=False))
                    if job["tool"]["output"]["contract"] == "artifact-bundle-v1" and not find_output_bundle(collected):
                        raise ValueError("远端结果缺少声明的 Artifact bundle")
                    with self.db() as db:
                        job = self.load(db, identifier)
                        job.setdefault("result", collected)
                        self.save(db, job)
        if action == "cancel":
            if job["status"] not in TERMINAL:
                raise ValueError("尚未确认停止；不会为取消而重新提交")
            return {**self.receipt(job), "terminated": job["status"] == "cancelled", "reason": "already_finished"}
        if action == "result":
            if job["status"] not in TERMINAL or job["status"] == "completed" and "result" not in job:
                raise ValueError("任务结果尚未可用")
            result = job.get("result")
            bundle = find_output_bundle(result)
            if job["status"] == "completed" and job["tool"]["output"]["contract"] == "artifact-bundle-v1" and not bundle:
                raise ValueError("远端结果缺少声明的 Artifact bundle")
            return {**self.receipt(job), "validated": job["status"] == "completed",
                    "target_result": result or {"error": job.get("error", "")},
                    "output_bundle": bundle}
        receipt = self.receipt(job)
        if orphaned and not job.get("remote_id"):
            receipt.update({"remote_outcome": "unknown"} if execution["mode"] == "blocking"
                           else {"submission_state": "unknown"})
        return receipt


def create_app(protocol, token, registry_token, host, worker_token):
    from fastmcp import FastMCP
    from fastmcp.server.dependencies import get_http_headers
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    server = FastMCP("TCode Job Protocol")

    class Gate(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            registry = request.url.path.startswith("/api/v1/target-registrations")
            key = "x-target-registry-token" if registry else "authorization"
            expected = registry_token if registry else f"Bearer {token}"
            if (request.headers.get("host") != host or request.headers.get("origin")
                    or not hmac.compare_digest(request.headers.get(key, "").encode(), expected.encode())):
                return JSONResponse({"detail": "Job Protocol credential required"}, status_code=401)
            return await call_next(request)

    @server.custom_route("/api/v1/target-registrations", methods=["GET"])
    async def list_targets(request):
        return JSONResponse({"servers": [t["server"] for t in protocol.active()],
                             "configured_servers": sorted(protocol.static_targets())})

    @server.custom_route("/api/v1/target-registrations/{name}/check", methods=["POST"])
    @server.custom_route("/api/v1/target-registrations/{name}", methods=["PUT", "GET", "DELETE"])
    async def registration(request):
        name = request.path_params["name"]
        try:
            if request.method in {"PUT", "POST"}:
                data = _wire(await request.json())
                info = await protocol.check_target(name, data)
                if request.method == "POST":
                    return JSONResponse(info)
                version = _digest(data)[:24]
                with protocol.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    rows = db.execute("SELECT data FROM targets WHERE id=?", (name,)).fetchall()
                    if any(json.loads(row[0])["owner"] != data["owner"] for row in rows):
                        raise ValueError("目标不属于当前所有者")
                    db.execute("UPDATE targets SET active=0 WHERE id=?", (name,))
                    data = {**data, "version": version}
                    db.execute("INSERT OR REPLACE INTO targets VALUES (?, ?, 1, ?)", (name, version, _json(data)))
                return JSONResponse(protocol.public(data))
            target = protocol.registration(name)
            if not target:
                return JSONResponse({"detail": "目标未启用"}, status_code=404)
            if request.method == "DELETE":
                with protocol.db() as db:
                    db.execute("UPDATE targets SET active=0 WHERE id=?", (name,))
                return JSONResponse({"status": "inactive"})
            return JSONResponse(protocol.public(target))
        except (ValueError, TypeError):
            return JSONResponse({"detail": "目标声明或远端工具契约无效"}, status_code=400)
        except Exception:
            return JSONResponse({"detail": "无法验证远端 MCP 连接"}, status_code=502)

    @server.tool
    async def describe_target_tool(input: dict | None = None, input_config: dict | None = None, payload: dict | None = None) -> dict:
        """Read or validate the contract of an enabled asynchronous target."""
        return await protocol.describe(_input(input, input_config, payload))

    def require_worker():
        supplied = get_http_headers().get(WORKER_HEADER, "")
        if not worker_token or not hmac.compare_digest(supplied.encode(), worker_token.encode()):
            raise PermissionError("此操作只允许内置 Job Protocol 的后台队列")

    @server.tool
    async def submit_job(input: dict) -> dict:
        """Submit an authorized asynchronous task with a stable run ID."""
        require_worker()
        try:
            return await protocol.submit(input)
        except ValueError as exc:
            return {"submission_state": "not_sent", "error": str(exc)}

    @server.tool
    async def lookup_job_submission(input: dict) -> dict:
        """Read a previously recorded receipt without starting or scheduling a job."""
        require_worker()
        return protocol.lookup_submission(input)

    @server.tool
    async def get_job_status(input: dict) -> dict:
        """Check the original job without resubmitting it."""
        return await protocol.query(input, "status")

    @server.tool
    async def collect_job_result(input: dict) -> dict:
        """Collect the original job result and portable artifact references."""
        return await protocol.query(input, "result")

    @server.tool
    async def terminate_job(input: dict) -> dict:
        """Request cancellation of the original job and verify the outcome."""
        return await protocol.query(input, "cancel")

    return server.http_app(path="/mcp", middleware=[Middleware(Gate)])


async def serve():
    import threading
    import uvicorn

    protocol = JobProtocol(Path(os.environ["TCODE_JOB_PROTOCOL_STATE"]), json.loads(os.environ["TCODE_JOB_PROTOCOL_CONFIGS"]))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app = create_app(protocol, os.environ[LOCAL_TOKEN], os.environ["TMCP_TARGET_REGISTRY_TOKEN"],
                         f"127.0.0.1:{port}", os.environ.pop(WORKER_TOKEN))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=2))
        threading.Thread(target=lambda: (sys.stdin.read(), setattr(server, "should_exit", True)), daemon=True).start()
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not task.done():
            await asyncio.sleep(0.02)
        if server.started:
            ready = Path(os.environ["TCODE_JOB_PROTOCOL_READY"])
            temp = ready.with_suffix(".tmp")
            temp.write_text(str(port))
            temp.replace(ready)
        await task


if __name__ == "__main__":
    asyncio.run(serve())
