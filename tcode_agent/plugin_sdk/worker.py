"""SDK v1 未知社区插件的有界双向 Worker v2 运行时。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import fnmatch
import importlib.util
import inspect
import json
import logging
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any


from .contracts import (
    PLUGIN_API_VERSION,
    PluginHealth,
    PluginMetadata,
    PromptContribution,
    PromptRenderContext,
    PublicEvent,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
)
from .protocol import (
    WORKER_PROTOCOL,
    decode_message,
    encode_message,
    request_message,
    response_message,
)


_CURRENT_HOST_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tcode_plugin_host_request_id",
    default="",
)


def _apply_resource_limits() -> None:
    """在支持 POSIX rlimit 的系统上限制 CPU、内存、进程和文件。"""

    try:
        import resource
    except ImportError as exc:
        raise RuntimeError(
            "Isolated plugin worker requires POSIX resource limits; "
            "Windows restricted execution is not supported yet"
        ) from exc

    limits = (
        (resource.RLIMIT_CPU, 30, 30),
        (resource.RLIMIT_AS, 768 * 1024 * 1024, 768 * 1024 * 1024),
        (resource.RLIMIT_FSIZE, 16 * 1024 * 1024, 16 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64, 64),
    )
    if hasattr(resource, "RLIMIT_NPROC"):
        limits += ((resource.RLIMIT_NPROC, 8, 8),)
    for kind, soft, hard in limits:
        try:
            resource.setrlimit(kind, (soft, hard))
        except (OSError, ValueError):
            continue


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """把 Worker 输出收窄为有深度和数量上限的 JSON 值。"""

    if depth >= 32:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:131_072]
    if isinstance(value, bytes):
        return value[:65_536].decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:128]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:128]]
    return f"<{type(value).__name__}>"


class _WorkerLogger:
    """把插件日志定向到 stderr，避免污染 stdout 协议。"""

    def __init__(self, name: str) -> None:
        """为当前插件建立固定日志名称。"""

        self._logger = logging.getLogger(f"tcode.isolated.{name}")

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录调试消息。"""

        self._logger.debug(message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录信息消息。"""

        self._logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录警告消息。"""

        self._logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录错误消息。"""

        self._logger.error(message, *args, **kwargs)


class _RpcBridge:
    """管理 Worker 发往 Host 的反向请求及其并发响应。"""

    def __init__(self) -> None:
        """保存原始协议输出流和待完成响应表。"""

        self._output = sys.stdout
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def write(self, message: Mapping[str, Any]) -> None:
        """串行写出一条完整消息，避免并发响应互相穿插。"""

        payload = encode_message(message).decode("utf-8")
        async with self._write_lock:
            self._output.write(payload)
            self._output.flush()

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """向 Host 发起带父调用身份的反向请求并等待对应响应。"""

        parent_id = _CURRENT_HOST_REQUEST_ID.get()
        if not parent_id:
            raise PermissionError("plugin capability calls require an active tool invocation")
        if method == "capability.invoke" and params.get("capability") in {"artifacts.read_text", "artifacts.publish_json"}:
            # Artifact data is evidence, not preview metadata. Preserve JSON
            # values exactly or fail before sending; never use _json_safe here.
            encoded = json.dumps(dict(params), ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 192 * 1024:
                raise ValueError("artifact request exceeds 192 KiB; no data was sent")
            wire_params = json.loads(encoded)
            if wire_params != dict(params):
                raise TypeError("artifact requests require JSON-native values and string object keys")
        else:
            wire_params = _json_safe(params)
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self.write(
                request_message(
                    request_id,
                    method,
                    wire_params,
                    parent_id=parent_id,
                )
            )
            response = await asyncio.wait_for(future, timeout=max(1.0, timeout))
        finally:
            self._pending.pop(request_id, None)
        if not response.get("ok"):
            error = dict(response.get("error") or {})
            raise PermissionError(
                f"[{error.get('code') or 'host.error'}] "
                f"{error.get('message') or 'host capability failed'}"
            )
        result = response.get("result")
        return dict(result) if isinstance(result, Mapping) else {"value": result}

    def resolve(self, message: Mapping[str, Any]) -> None:
        """把 Host 响应精确交给同 ID 的反向请求。"""

        future = self._pending.get(str(message.get("id") or ""))
        if future is not None and not future.done():
            future.set_result(dict(message))

    def cancel_all(self, reason: str) -> None:
        """在通道关闭时终止所有尚未完成的反向调用。"""

        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()


class _ToolCollector:
    """收集工具贡献，并通过 Host Broker 调用已授权工具。"""

    def __init__(self, bridge: _RpcBridge, allowed_side_effects: set[str]) -> None:
        """绑定反向 RPC 和本实例允许声明的副作用等级。"""

        self._bridge = bridge
        self._allowed_side_effects = frozenset(allowed_side_effects) | {"none"}
        self.definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        """登记工具，并拒绝重名或超过 Host 授权的副作用声明。"""

        if not isinstance(definition, ToolDefinition):
            raise TypeError("isolated worker requires ToolDefinition")
        if not isinstance(definition.on_demand, bool):
            raise TypeError("tool on_demand must be a boolean")
        if definition.side_effect_level not in self._allowed_side_effects:
            raise PermissionError(
                "isolated tool side_effect_level exceeds effective Host grants"
            )
        if definition.name in self.definitions:
            raise ValueError(f"duplicate isolated tool: {definition.name}")
        if not callable(definition.handler):
            raise TypeError("isolated tool handler must be callable")
        self.definitions[definition.name] = definition

    async def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        timeout: int = 300,
    ) -> Mapping[str, Any]:
        """请求 Host 通过完整 ToolExecutor 管线调用一个工具或 Skill。"""

        return MappingProxyType(
            await self._bridge.request(
                "capability.invoke",
                {
                    "capability": "tools.invoke",
                    "tool_id": str(tool_id),
                    "arguments": dict(arguments),
                    "timeout": max(1, min(int(timeout), 3600)),
                },
                timeout=max(2, min(int(timeout) + 5, 3605)),
            )
        )


class _DeniedPrompt:
    """拒绝未知社区插件修改生产 Prompt。"""

    def contribute(self, _contribution: PromptContribution) -> None:
        """以稳定权限错误拒绝 Prompt 贡献。"""

        raise PermissionError("isolated community plugins cannot contribute prompts")


class _PromptCollector:
    def __init__(self):
        self.definitions = {}

    def contribute(self, contribution):
        if not isinstance(contribution, PromptContribution) or contribution.name in self.definitions:
            raise ValueError("invalid or duplicate prompt contribution")
        if bool(contribution.text) == bool(contribution.renderer) or (contribution.renderer is not None and not callable(contribution.renderer)):
            raise ValueError("prompt requires exactly one text or callable renderer")
        self.definitions[contribution.name] = contribution


class _DeniedEvents:
    """拒绝未知社区插件订阅宿主事件。"""

    def subscribe(self, _event: PublicEvent, _handler: Any) -> None:
        """以稳定权限错误拒绝事件订阅。"""

        raise PermissionError("isolated community plugins cannot subscribe to events")


class _DeniedWorkspace:
    """拒绝 Worker 直接读写宿主工作区文件。"""

    def _deny(self, *_args: Any, **_kwargs: Any) -> Any:
        """对所有直接文件操作返回相同的失败关闭诊断。"""

        raise PermissionError("isolated community plugins have no direct workspace access")

    resolve_read_path = _deny
    resolve_write_path = _deny
    read_text = _deny
    write_text = _deny
    list_dir = _deny


class _TrustedWorkspace:
    """Synchronous workspace facade protected by manifest checks and Seatbelt."""

    def __init__(self, workdir: str, plugin_root: Path, original_plugin_root: str,
                 grants: list[dict[str, Any]], writable_root: str = "") -> None:
        self.workdir = Path(workdir).resolve()
        self.plugin_root = plugin_root.resolve()
        self.original_plugin_root = Path(original_plugin_root).resolve()
        self.writable_root = Path(writable_root).resolve() if writable_root else None
        self.grants = grants

    def _logical_path(self, path: str) -> tuple[Path, Path]:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.workdir / target
        target = target.resolve()
        try:
            relative = target.relative_to(self.plugin_root)
            logical = self.original_plugin_root / relative
            if self.writable_root is not None and relative.parts[:1] == (".cache",):
                target = self.writable_root / "plugin-cache" / Path(*relative.parts[1:])
        except ValueError:
            logical = target
        return target, logical.resolve()

    def _allowed(self, scope: str, logical: Path) -> bool:
        candidates = [str(logical)]
        # Desktop bundles live under the signed application resources directory,
        # while workspace-scoped manifests keep declaring ./plugins/<name>/**.
        # Preserve that logical manifest path after the package is copied into
        # the worker; do not grant anything outside the verified plugin root.
        try:
            relative = logical.relative_to(self.original_plugin_root)
            candidates.append(str(self.workdir / "plugins" / self.original_plugin_root.name / relative))
        except ValueError:
            pass
        for grant in self.grants:
            if str(grant.get("scope") or "") != scope:
                continue
            for raw in grant.get("patterns") or []:
                pattern = Path(str(raw)).expanduser()
                if not pattern.is_absolute():
                    pattern = self.workdir / pattern
                normalized = os.path.normpath(str(pattern))
                prefix = normalized.rstrip("/*")
                for candidate in candidates:
                    if fnmatch.fnmatch(candidate, normalized):
                        return True
                    if "*" not in normalized and "?" not in normalized and (
                        candidate == prefix or candidate.startswith(prefix + os.sep)
                    ):
                        return True
        return False

    def _resolve(self, path: str, scope: str) -> Path:
        target, logical = self._logical_path(path)
        if not self._allowed(scope, logical):
            raise PermissionError(f"plugin permission denied: scope={scope}")
        return target

    def resolve_read_path(self, path: str) -> str:
        return str(self._resolve(path, "filesystem.read"))

    def resolve_write_path(self, path: str) -> str:
        return str(self._resolve(path, "filesystem.write"))

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._resolve(path, "filesystem.read").read_text(encoding=encoding)

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> None:
        target = self._resolve(path, "filesystem.write")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding=encoding)
        temporary.replace(target)

    def list_dir(self, path: str) -> tuple[str, ...]:
        target = self._resolve(path, "filesystem.read")
        return tuple(sorted(item.name for item in target.iterdir()))


class _RemoteEmbeddings:
    """Expose only the host's configured embedding provider over the broker."""

    def __init__(self, bridge: _RpcBridge) -> None:
        self._bridge = bridge

    async def describe(self) -> Mapping[str, Any]:
        return MappingProxyType(await self._bridge.request(
            "capability.invoke", {"capability": "embeddings.describe"}, timeout=35,
        ))


class _RemoteArtifacts:
    def __init__(self, bridge):
        self._bridge = bridge
        self._read_lock = asyncio.Lock()

    async def read_text(self, path, *, sha256="", snapshot=False, max_bytes=192 * 1024):
        async with self._read_lock:
            return await self._read_text(path, sha256=sha256, snapshot=snapshot, max_bytes=max_bytes)

    async def _read_text(self, path, *, sha256, snapshot, max_bytes):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 4 * 1024 * 1024:
            raise ValueError("artifact read budget must be 1 to 4194304 bytes")
        params = {
            "capability": "artifacts.read_text", "path": path,
            "sha256": sha256, "snapshot": snapshot, "max_bytes": max_bytes,
        }
        result = await self._bridge.request("capability.invoke", params, timeout=300)
        chunks = [result["text"]]
        size = len(result["text"].encode("utf-8"))
        continuation = result.pop("next_token", "")
        for _ in range(128):
            if size > max_bytes:
                raise ValueError("artifact response exceeds requested analysis budget")
            if not continuation:
                return {**result, "text": "".join(chunks)}
            page = await self._bridge.request("capability.invoke", {
                **params, "continuation": continuation,
            }, timeout=300)
            if any(page.get(key) != result.get(key) for key in ("path", "sha256")):
                raise ValueError("artifact changed during worker transfer")
            chunks.append(page["text"])
            size += len(page["text"].encode("utf-8"))
            continuation = page.get("next_token", "")
        raise ValueError("artifact read exceeded bounded page count")

    async def publish_json(self, value):
        return await self._bridge.request("capability.invoke", {
            "capability": "artifacts.publish_json", "value": value,
        }, timeout=300)


class _RemoteOntology:
    """Expose the active host-owned ontology grant without copying store access."""

    def __init__(self, bridge: _RpcBridge, policy: Mapping[str, Any]) -> None:
        self._bridge = bridge
        self._policy = MappingProxyType(dict(policy))

    def source_policy(self) -> Mapping[str, Any]:
        return dict(self._policy)

    async def read_facts(self, references: Any) -> Mapping[str, Any]:
        return await self._bridge.request(
            "capability.invoke",
            {"capability": "ontology.read_facts", "references": list(references)},
            timeout=35,
        )

    async def query(self, **arguments: Any) -> Mapping[str, Any]:
        return await self._bridge.request(
            "capability.invoke",
            {"capability": "ontology.query", "arguments": dict(arguments)},
            timeout=35,
        )

    async def validate_report(self, claims: Any) -> Mapping[str, Any]:
        return await self._bridge.request(
            "capability.invoke",
            {"capability": "ontology.validate_report", "claims": list(claims)},
            timeout=35,
        )

    async def embed(self, texts: Any, *, expected_signature: str,
                    kind: str = "document") -> Mapping[str, Any]:
        return MappingProxyType(await self._bridge.request(
            "capability.invoke",
            {"capability": "embeddings.embed", "texts": list(texts),
             "expected_signature": expected_signature, "kind": kind},
            timeout=40,
        ))


class _RemoteProcess:
    """把插件命令请求发送给 Host，并由 tcode-executor 实际执行。"""

    def __init__(self, bridge: _RpcBridge) -> None:
        """绑定当前 Worker 的唯一 Host RPC。"""

        self._bridge = bridge

    async def run(
        self,
        command: str,
        args: tuple[str, ...] = (),
        *,
        cwd: str | None = None,
        timeout: int = 60,
        profile: str | None = None,
    ) -> Mapping[str, Any]:
        """通过 Host 权限和 Sandbox Profile 执行无 shell 命令。"""

        return MappingProxyType(
            await self._bridge.request(
                "capability.invoke",
                {
                    "capability": "process.run",
                    "command": str(command),
                    "args": [str(item) for item in args],
                    "cwd": str(cwd or ""),
                    "timeout": max(1, min(int(timeout), 3600)),
                    "profile": str(profile or ""),
                },
                timeout=max(2, min(int(timeout) + 5, 3605)),
            )
        )


class _RemoteNetwork:
    """把网络请求交给 Host 的固定 Egress Sandbox。"""

    def __init__(self, bridge: _RpcBridge) -> None:
        """绑定当前 Worker 的唯一 Host RPC。"""

        self._bridge = bridge

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        timeout: int = 30,
    ) -> Mapping[str, Any]:
        """通过 Host 校验域名后在 Egress 容器中执行 HTTP 请求。"""

        if isinstance(body, bytes):
            body_value = body.decode("utf-8", errors="strict")
        else:
            body_value = str(body) if body is not None else None
        return MappingProxyType(
            await self._bridge.request(
                "capability.invoke",
                {
                    "capability": "network.request",
                    "method": str(method),
                    "url": str(url),
                    "headers": dict(headers or {}),
                    "body": body_value,
                    "timeout": max(1, min(int(timeout), 300)),
                },
                timeout=max(2, min(int(timeout) + 5, 305)),
            )
        )


@dataclass(slots=True)
class _WorkerState:
    """保存 Worker 内唯一插件实例、工具和请求串行锁。"""

    bridge: _RpcBridge
    instance: Any = None
    tools: _ToolCollector | None = None
    prompts: Any = None
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _load_entry(plugin_root: Path, entry: str) -> Any:
    """从已校验包内路径导入唯一插件入口。"""

    module_name, symbol = entry.split(":", 1)
    entry_path = (plugin_root / module_name).resolve()
    entry_path.relative_to(plugin_root)
    # 每个 Worker 只承载一个插件，因此可以把包根固定加入导入路径。这样插件
    # 可以携带纯 Python 子包或 vendor 依赖，而不需要在隔离容器中在线安装依赖。
    plugin_root_text = str(plugin_root)
    if plugin_root_text not in sys.path:
        sys.path.insert(0, plugin_root_text)
    spec = importlib.util.spec_from_file_location("tcode_isolated_plugin", entry_path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot create isolated plugin module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, symbol, None)
    if factory is None:
        raise ImportError(f"plugin entry symbol is missing: {symbol}")
    return factory() if callable(factory) else factory


def _effective_side_effects(grants: list[dict[str, Any]]) -> set[str]:
    """把 Host 有效权限转换成工具允许声明的副作用等级。"""

    scopes = {str(item.get("scope") or "") for item in grants}
    allowed = {"none"}
    if scopes & {"filesystem.read", "network.outbound"}:
        allowed.add("read")
    if "filesystem.write" in scopes:
        allowed.add("write")
    if scopes & {"process.exec", "tools.invoke", "skills.invoke"}:
        allowed.add("process")
    return allowed


async def _initialize(state: _WorkerState, params: Mapping[str, Any]) -> dict[str, Any]:
    """导入插件、构造远程能力 Context 并返回工具目录。"""

    if state.instance is not None:
        raise RuntimeError("isolated worker is already initialized")
    root = Path(str(params.get("plugin_root") or "")).resolve()
    manifest = dict(params.get("manifest") or {})
    if manifest.get("apiVersion") != PLUGIN_API_VERSION:
        raise ValueError("isolated worker requires Plugin API v1")
    effective_trust = str(params.get("effective_trust") or "")
    if effective_trust not in {"isolated", "trusted-worker"}:
        raise PermissionError("worker requires a host-selected execution trust")
    trusted_worker = effective_trust == "trusted-worker"
    if set(manifest.get("contributes") or []) - ({"tools", "prompt", "events"} if trusted_worker else {"tools"}):
        raise PermissionError("isolated community v1 supports tool contributions only")
    grants = [
        dict(item)
        for item in list(params.get("granted_permissions") or [])
        if isinstance(item, Mapping)
    ]
    requested_scopes = {
        str(item.get("scope") or "")
        for item in list(manifest.get("permissions") or [])
        if isinstance(item, Mapping)
    }
    if any(str(item.get("scope") or "") not in requested_scopes for item in grants):
        raise PermissionError("effective permission was not requested by the Manifest")
    tools = _ToolCollector(state.bridge, _effective_side_effects(grants))
    prompts = _PromptCollector() if trusted_worker and "prompt" in manifest.get("contributes", []) else _DeniedPrompt()
    context = SimpleNamespace(
        instance=SimpleNamespace(
            name=str(manifest.get("name") or ""),
            version=str(manifest.get("version") or ""),
            instance_id=str(params.get("instance_id") or ""),
            api_version=PLUGIN_API_VERSION,
            requested_trust=effective_trust,
        ),
        scope=SimpleNamespace(
            kind="workspace",
            workspace_id=str(params.get("workspace_id") or "workspace:isolated"),
        ),
        logger=_WorkerLogger(str(manifest.get("name") or "plugin")),
        tools=tools,
        prompt=prompts,
        events=_DeniedEvents(),
        workspace=(
            _TrustedWorkspace(
                str(params.get("workdir") or ""), root,
                str(params.get("original_plugin_root") or root), grants,
                str(params.get("writable_root") or ""),
            )
            if trusted_worker else _DeniedWorkspace()
        ),
        process=_RemoteProcess(state.bridge),
        network=_RemoteNetwork(state.bridge),
        embeddings=(
            _RemoteEmbeddings(state.bridge)
            if trusted_worker and "model.embedding" in requested_scopes else None
        ),
        config=MappingProxyType(dict(params.get("config") or {})),
    )
    with contextlib.redirect_stdout(sys.stderr):
        instance = _load_entry(root, str(manifest.get("entry") or ""))
        info = getattr(instance, "info", None)
        if not isinstance(info, PluginMetadata):
            raise TypeError("plugin info must be public SDK PluginMetadata")
        if info.name != manifest.get("name") or info.version != manifest.get("version"):
            raise ValueError("isolated plugin metadata does not match manifest")
        start = getattr(instance, "start", None)
        if callable(start):
            result = start(context, context.config)
            if inspect.isawaitable(result):
                await result
        else:
            raise TypeError("isolated plugin must define start(ctx, config)")
    state.instance = instance
    state.tools = tools
    state.prompts = prompts
    return {
        "protocol": WORKER_PROTOCOL,
        "plugin": {
            "name": info.name,
            "version": info.version,
            "description": info.description,
            "author": info.author,
            "homepage": info.homepage,
            "plugin_type": list(info.plugin_type),
        },
        "tools": [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": _json_safe(definition.input_schema),
                "version": definition.version,
                "side_effect_level": definition.side_effect_level,
                "result_budget_tokens": definition.result_budget_tokens,
                "on_demand": definition.on_demand,
            }
            for definition in tools.definitions.values()
        ],
        "prompts": [{"name": item.name, "version": item.version, "order": item.order,
                     "token_budget": item.token_budget, "required": item.required, "agents": list(item.agents),
                     "structured_output_safe": item.structured_output_safe}
                    for item in getattr(prompts, "definitions", {}).values()],
        "limits": {
            "network": "host-broker",
            "workspace": "manifest-seatbelt" if trusted_worker else "direct-deny",
            "process": "host-broker",
            "tools": "host-broker",
            "tool_timeout_seconds": 10,
        },
        "granted_permissions": grants,
    }


async def _execute(state: _WorkerState, params: Mapping[str, Any]) -> dict[str, Any]:
    """在 Worker 内执行一个工具并限制返回大小。"""

    if state.tools is None:
        raise RuntimeError("isolated worker is not initialized")
    name = str(params.get("tool") or "")
    definition = state.tools.definitions.get(name)
    if definition is None:
        raise KeyError(f"isolated tool not found: {name}")
    raw_context = dict(params.get("context") or {})
    ontology_policy = raw_context.get("ontology_source_policy")
    call = ToolCallContext(
        session_id=str(raw_context.get("session_id") or ""),
        message_id=str(raw_context.get("message_id") or ""),
        agent=str(raw_context.get("agent") or ""),
        call_id=str(raw_context.get("call_id") or "") or None,
        execution=MappingProxyType(dict(raw_context.get("execution") or {})),
        user_messages=tuple(raw_context.get("user_messages") or ()),
        artifacts=_RemoteArtifacts(state.bridge),
        ontology=(
            _RemoteOntology(state.bridge, ontology_policy)
            if isinstance(ontology_policy, Mapping)
            else None
        ),
    )
    with contextlib.redirect_stdout(sys.stderr):
        result = definition.handler(
            MappingProxyType(dict(params.get("arguments") or {})), call,
        )
        if inspect.isawaitable(result):
            result = await result
    if not isinstance(result, ToolOutput):
        raise TypeError("isolated tool handler must return ToolOutput")
    budget = definition.result_budget_tokens
    maximum = min(131_072, budget * 4)
    return {
        "title": result.title[:200],
        "content": result.content[:maximum],
        "metadata": _json_safe(result.metadata),
        "truncated": len(result.content) > maximum,
    }


async def _render_prompt(state: _WorkerState, params: Mapping[str, Any]) -> dict[str, Any]:
    contribution = getattr(state.prompts, "definitions", {})[str(params.get("name") or "")]
    context = PromptRenderContext(workspace_id=str(params.get("workspace_id") or ""), agent=str(params.get("agent") or ""),
                                  variables=MappingProxyType(dict(params.get("variables") or {})))
    with contextlib.redirect_stdout(sys.stderr):
        content = contribution.renderer(context) if contribution.renderer else contribution.text
        if inspect.isawaitable(content):
            content = await content
    if not isinstance(content, str) or len(content) > 131072:
        raise ValueError("invalid or oversized prompt text")
    return {"content": content}


async def _health(state: _WorkerState) -> dict[str, Any]:
    """返回 Worker 和插件的有界健康摘要。"""

    if state.instance is None:
        return {"status": "unhealthy", "summary": "worker is not initialized"}
    handler = getattr(state.instance, "health", None)
    if not callable(handler):
        return {"status": "healthy", "summary": "worker is responsive"}
    with contextlib.redirect_stdout(sys.stderr):
        result = handler()
        if inspect.isawaitable(result):
            result = await result
    if isinstance(result, PluginHealth):
        return {
            "status": result.status.value,
            "summary": result.summary[:1000],
            "details": _json_safe(result.details),
        }
    return {"status": "unhealthy", "summary": "invalid plugin health response"}


async def _stop(state: _WorkerState) -> dict[str, Any]:
    """调用插件停止回调并清空 Worker 内引用。"""

    instance = state.instance
    state.instance = None
    state.tools = None
    state.prompts = None
    handler = getattr(instance, "stop", None) if instance is not None else None
    if callable(handler):
        with contextlib.redirect_stdout(sys.stderr):
            result = handler("unload")
            if inspect.isawaitable(result):
                await result
    return {"stopped": True}


async def _dispatch(state: _WorkerState, method: str, params: Mapping[str, Any]) -> Any:
    """把 Host 请求分发到唯一允许的 Worker 方法。"""

    handlers = {
        "initialize": _initialize,
        "execute": _execute,
        "prompt.render": _render_prompt,
        "health": lambda current, _params: _health(current),
        "stop": lambda current, _params: _stop(current),
    }
    handler = handlers.get(method)
    if handler is None:
        raise ValueError(f"unsupported worker method: {method}")
    # Prompt rendering is read-only; don't queue it behind a long awaited tool.
    if method == "prompt.render":
        return await handler(state, params)
    async with state.operation_lock:
        return await handler(state, params)


async def _handle_host_request(
    state: _WorkerState,
    message: Mapping[str, Any],
    stop_event: asyncio.Event,
) -> None:
    """并发处理一条 Host 请求，使读取循环仍可接收反向调用响应。"""

    request_id = str(message.get("id") or "")
    method = str(message.get("method") or "")
    token = _CURRENT_HOST_REQUEST_ID.set(request_id)
    try:
        result = await _dispatch(state, method, dict(message.get("params") or {}))
        response = response_message(request_id, result=_json_safe(result))
        # Serialization failures must reach the host instead of leaving its
        # request pending until timeout.
        encode_message(response)
    except Exception as exc:
        response = response_message(
            request_id,
            error={
                "code": f"worker.{type(exc).__name__.lower()}",
                "message": str(exc)[:2000],
            },
        )
    finally:
        _CURRENT_HOST_REQUEST_ID.reset(token)
    await state.bridge.write(response)
    if method == "stop":
        stop_event.set()


async def _serve() -> int:
    """持续读取双向消息，并发处理 Host 请求与能力响应。"""

    bridge = _RpcBridge()
    state = _WorkerState(bridge=bridge)
    stop_event = asyncio.Event()
    tasks: set[asyncio.Task[None]] = set()
    try:
        while not stop_event.is_set():
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            try:
                message = decode_message(line)
            except Exception as exc:
                print(f"plugin worker protocol error: {exc}", file=sys.stderr)
                break
            message_type = str(message.get("type") or "")
            if message_type == "response":
                bridge.resolve(message)
                continue
            if message_type == "cancel":
                continue
            if message_type != "request":
                continue
            task = asyncio.create_task(_handle_host_request(state, message, stop_event))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        bridge.cancel_all("plugin worker protocol closed")
    return 0


def main() -> int:
    """应用资源上限并启动 Worker v2 协议循环。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    _apply_resource_limits()
    return asyncio.run(_serve())


if __name__ == "__main__":
    raise SystemExit(main())
