"""插件权限声明与运行时强制执行辅助组件。"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import json
import os
import shlex
import shutil
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from ..runtime.sandbox import (
    build_tool_sandbox_spec,
    get_sandbox_runtime,
    load_sandbox_config,
    sandbox_tool_block_reason,
    tool_should_run_in_sandbox,
)
from ..tools.base import ToolContext


class PluginPermissionScope(str, Enum):
    """列出 Manifest v2 支持的权限 Scope。"""

    PROCESS_EXEC = "process.exec"
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    ENV_READ = "env.read"
    NETWORK_OUTBOUND = "network.outbound"
    MODEL_EMBEDDING = "model.embedding"
    TOOLS_INVOKE = "tools.invoke"
    SKILLS_INVOKE = "skills.invoke"
    SANDBOX_PROFILE = "sandbox.profile"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    MEMORY_DELETE = "memory.delete"
    MEMORY_PROMOTE = "memory.promote"
    MEMORY_MAINTENANCE = "memory.maintenance"
    ONTOLOGY_READ = "ontology.read"


@dataclass
class PluginPermission:
    """保存一项插件权限申请或 Host 授权。"""

    scope: str
    patterns: list[str] = field(default_factory=list)
    description: str = ""
    required: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PluginPermission":
        """从 Manifest 字典建立权限声明。"""

        patterns = value.get("patterns") or value.get("allow") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        return cls(
            scope=str(value.get("scope") or "").strip(),
            patterns=[str(item).strip() for item in patterns if str(item).strip()],
            description=str(value.get("description") or "").strip(),
            required=bool(value.get("required", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可序列化权限描述。"""

        return {
            "scope": self.scope,
            "patterns": list(self.patterns),
            "description": self.description,
            "required": self.required,
        }


class PluginPermissionError(PermissionError):
    """表示插件操作超过 Host 已授予权限。"""


def _has_glob(pattern: str) -> bool:
    """判断权限模式是否包含 glob 语法。"""

    return any(token in pattern for token in ("*", "?", "["))


def normalize_patterns(scope: str, patterns: list[str], workdir: str) -> list[str]:
    """把权限模式规范为与实际路径解析一致的绝对形式。

    对含通配符的模式只解析其固定目录前缀，既保留 glob 语义，也避免
    macOS ``/var`` 与 ``/private/var`` 等同一路径因文本不同而被误拒绝。
    """

    normalized: list[str] = []
    for raw_pattern in patterns:
        pattern = str(raw_pattern).strip()
        if not pattern:
            continue
        if scope in {
            PluginPermissionScope.FILESYSTEM_READ.value,
            PluginPermissionScope.FILESYSTEM_WRITE.value,
        }:
            expanded = os.path.expanduser(pattern)
            if not os.path.isabs(expanded):
                expanded = os.path.join(workdir, expanded)
            expanded = os.path.normpath(expanded)
            wildcard_indexes = [
                index for token in ("*", "?", "[")
                if (index := expanded.find(token)) >= 0
            ]
            if wildcard_indexes:
                first_wildcard = min(wildcard_indexes)
                fixed_prefix = expanded[:first_wildcard].rsplit(os.sep, 1)[0]
                fixed_prefix = fixed_prefix or os.sep
                resolved_prefix = str(Path(fixed_prefix).resolve())
                normalized.append(
                    resolved_prefix.rstrip(os.sep) + expanded[len(fixed_prefix):]
                )
            else:
                normalized.append(str(Path(expanded).resolve()))
        else:
            normalized.append(pattern)
    return normalized


def check_permission(
    permissions: list[PluginPermission],
    scope: str,
    target: str,
    *,
    workdir: str,
) -> bool:
    """判断目标是否命中指定 Scope 的任一授权模式。"""

    active = [permission for permission in permissions if permission.scope == scope]
    if not active:
        return False

    if scope in {
        PluginPermissionScope.FILESYSTEM_READ.value,
        PluginPermissionScope.FILESYSTEM_WRITE.value,
    }:
        target_text = os.path.normpath(str(Path(target).expanduser().resolve()))
        for permission in active:
            for pattern in normalize_patterns(scope, permission.patterns, workdir):
                if _has_glob(pattern) and fnmatch.fnmatch(target_text, pattern):
                    return True
                if target_text == pattern or target_text.startswith(pattern + os.sep):
                    return True
        return False

    if scope == PluginPermissionScope.PROCESS_EXEC.value:
        command = os.path.basename(str(target).strip())
        for permission in active:
            for pattern in permission.patterns:
                if fnmatch.fnmatch(command, pattern) or fnmatch.fnmatch(str(target).strip(), pattern):
                    return True
        return False

    if scope == PluginPermissionScope.ENV_READ.value:
        for permission in active:
            for pattern in permission.patterns:
                if fnmatch.fnmatch(str(target).strip(), pattern):
                    return True
        return False

    if scope == PluginPermissionScope.NETWORK_OUTBOUND.value:
        for permission in active:
            for pattern in permission.patterns:
                if fnmatch.fnmatch(str(target).strip(), pattern):
                    return True
        return False

    if scope in {
        PluginPermissionScope.TOOLS_INVOKE.value,
        PluginPermissionScope.SKILLS_INVOKE.value,
        PluginPermissionScope.SANDBOX_PROFILE.value,
        PluginPermissionScope.MEMORY_READ.value,
        PluginPermissionScope.MEMORY_WRITE.value,
        PluginPermissionScope.MEMORY_DELETE.value,
        PluginPermissionScope.MEMORY_PROMOTE.value,
        PluginPermissionScope.MEMORY_MAINTENANCE.value,
        PluginPermissionScope.MODEL_EMBEDDING.value,
        PluginPermissionScope.ONTOLOGY_READ.value,
    }:
        normalized_target = str(target).strip()
        for permission in active:
            for pattern in permission.patterns:
                if fnmatch.fnmatch(normalized_target, pattern):
                    return True
        return False

    return False


class PluginRuntime:
    """在 Host 内部强制执行 Manifest 权限与 Sandbox 的辅助面。"""

    def __init__(
        self,
        *,
        workdir: str,
        permissions: list[PluginPermission],
        agent_name: Optional[str] = None,
        require_sandbox: bool = False,
    ):
        """绑定工作目录、已授予权限和用于 Sandbox 的 Agent 名称。"""

        self.workdir = str(Path(workdir).resolve())
        self.permissions = list(permissions)
        self.agent_name = str(agent_name or "").strip()
        self.require_sandbox = bool(require_sandbox)

    def _ensure(self, scope: str, target: str):
        """确认目标获得指定权限，否则失败关闭。"""

        if check_permission(self.permissions, scope, target, workdir=self.workdir):
            return
        raise PluginPermissionError(
            f"plugin permission denied: scope={scope} target={target}"
        )

    def ensure_permission(self, scope: str, target: str) -> None:
        """供受控 Host Facade 校验一项非文件系统能力授权。"""

        self._ensure(str(scope or "").strip(), str(target or "").strip())

    def resolve_path(self, path: str, *, scope: str) -> Path:
        """解析路径并验证指定文件权限。"""

        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path(self.workdir) / target
        resolved = target.resolve()
        self._ensure(scope, str(resolved))
        return resolved

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """在读权限内读取文本。"""

        resolved = self.resolve_path(path, scope=PluginPermissionScope.FILESYSTEM_READ.value)
        return resolved.read_text(encoding=encoding)

    def write_text(self, path: str, content: str, encoding: str = "utf-8"):
        """在写权限内创建父目录并写入完整文本。"""

        resolved = self.resolve_path(path, scope=PluginPermissionScope.FILESYSTEM_WRITE.value)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding=encoding)

    def list_dir(self, path: str) -> list[str]:
        """在读权限内返回稳定排序的目录项。"""

        resolved = self.resolve_path(path, scope=PluginPermissionScope.FILESYSTEM_READ.value)
        if not resolved.is_dir():
            raise FileNotFoundError(str(resolved))
        return sorted(child.name for child in resolved.iterdir())

    def getenv(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """在环境变量读权限内取得一个值。"""

        self._ensure(PluginPermissionScope.ENV_READ.value, name)
        return os.environ.get(name, default)

    async def run_command(
        self,
        command: str,
        args: Optional[list[str]] = None,
        *,
        cwd: Optional[str] = None,
        timeout: int = 60,
        env: Optional[dict[str, str]] = None,
        sandbox_profile: Optional[str] = None,
        host_context: Optional[ToolContext] = None,
    ) -> dict[str, Any]:
        """在进程权限、目录权限与 Sandbox 决策后执行无 shell 命令。"""

        self._ensure(PluginPermissionScope.PROCESS_EXEC.value, command)
        requested_profile = str(sandbox_profile or "").strip()
        if requested_profile:
            self._ensure(
                PluginPermissionScope.SANDBOX_PROFILE.value,
                requested_profile,
            )
        target_cwd = self.workdir
        if cwd:
            target_cwd = str(self.resolve_path(cwd, scope=PluginPermissionScope.FILESYSTEM_READ.value))

        inherited_extra = (
            dict(host_context.extra or {})
            if host_context is not None and isinstance(host_context.extra, dict)
            else {}
        )
        sandbox_context = ToolContext(
            session_id="",
            message_id="",
            agent=self.agent_name or "plugin",
            abort=getattr(host_context, "abort", None),
            extra={
                **inherited_extra,
                "workdir": self.workdir,
                "agent_name": self.agent_name or "plugin",
            },
        )
        block_reason = sandbox_tool_block_reason("plugin_runtime", sandbox_context)
        if block_reason:
            raise PluginPermissionError(block_reason)

        if tool_should_run_in_sandbox("plugin_runtime", sandbox_context):
            command_line = " ".join(
                [shlex.quote(command), *[shlex.quote(str(item)) for item in (args or [])]]
            )
            spec = build_tool_sandbox_spec(
                tool_name="plugin_runtime",
                command=command_line,
                requested_workdir=target_cwd,
                timeout_seconds=timeout,
                context=sandbox_context,
                extra_env={str(k): str(v) for k, v in dict(env or {}).items()},
                metadata={"plugin_command": command, "plugin_args": list(args or [])},
                sandbox_profile=requested_profile or None,
            )
            result = await get_sandbox_runtime().run_once(spec)
            return {
                "command": command,
                "args": list(args or []),
                "cwd": target_cwd,
                "returncode": -1 if result.exit_code is None else int(result.exit_code),
                "stdout": result.stdout,
                "stderr": result.stderr,
                "sandbox": True,
                "sandbox_status": result.status,
                "container_name": result.container_name,
                **dict(result.metadata or {}),
            }

        if self.require_sandbox:
            raise PluginPermissionError(
                "isolated plugin process execution requires an active sandbox runner"
            )

        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=target_cwd,
            env=dict(os.environ, **(env or {})),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError(f"command timed out after {timeout}s: {command}")

        return {
            "command": command,
            "args": list(args or []),
            "cwd": target_cwd,
            "returncode": int(process.returncode or 0),
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    @staticmethod
    def _network_target(url: str) -> tuple[str, str]:
        """解析 HTTP URL，并拒绝显式私网、环回和链路本地地址。"""

        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PluginPermissionError("plugin network URL must use http or https")
        hostname = parsed.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise PluginPermissionError("plugin network target is not publicly routable")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return hostname, f"{hostname}:{port}"

    async def request_network(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: str | bytes | None = None,
        timeout: int = 30,
        host_context: Optional[ToolContext] = None,
    ) -> dict[str, Any]:
        """在固定 Egress Sandbox 中执行一次有界、禁止重定向的 HTTP 请求。"""

        normalized_method = str(method or "GET").strip().upper()
        if normalized_method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise PluginPermissionError("plugin network method is not allowed")
        hostname, authority = self._network_target(url)
        if not (
            check_permission(
                self.permissions,
                PluginPermissionScope.NETWORK_OUTBOUND.value,
                authority,
                workdir=self.workdir,
            )
            or check_permission(
                self.permissions,
                PluginPermissionScope.NETWORK_OUTBOUND.value,
                hostname,
                workdir=self.workdir,
            )
        ):
            raise PluginPermissionError(
                f"plugin permission denied: scope=network.outbound target={authority}"
            )

        normalized_headers: dict[str, str] = {}
        forbidden_headers = {
            "host",
            "connection",
            "proxy-authorization",
            "proxy-connection",
            "transfer-encoding",
        }
        for raw_name, raw_value in dict(headers or {}).items():
            name = str(raw_name or "").strip()
            if not name or name.lower() in forbidden_headers:
                raise PluginPermissionError(f"plugin network header is not allowed: {name}")
            normalized_headers[name[:128]] = str(raw_value)[:8192]
        body_bytes = (
            body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
        )
        if len(body_bytes) > 1024 * 1024:
            raise PluginPermissionError("plugin network request body exceeds 1 MiB")

        inherited_extra = (
            dict(host_context.extra or {})
            if host_context is not None and isinstance(host_context.extra, dict)
            else {}
        )
        sandbox_context = ToolContext(
            session_id="",
            message_id="",
            agent=self.agent_name or "plugin",
            abort=getattr(host_context, "abort", None),
            extra={
                **inherited_extra,
                "workdir": self.workdir,
                "agent_name": self.agent_name or "plugin",
            },
        )
        block_reason = sandbox_tool_block_reason("plugin_network", sandbox_context)
        if block_reason:
            raise PluginPermissionError(block_reason)
        if self.require_sandbox and not tool_should_run_in_sandbox(
            "plugin_network",
            sandbox_context,
        ):
            raise PluginPermissionError(
                "isolated plugin network access requires an active sandbox policy"
            )
        sandbox_config = load_sandbox_config()
        if not str(sandbox_config.egress_proxy or "").strip():
            raise PluginPermissionError(
                "isolated plugin network access requires a controlled egress proxy"
            )

        job_id = uuid.uuid4().hex
        relative_root = Path(".tcode-runtime") / "plugin-network" / job_id
        host_root = (Path(self.workdir) / relative_root).resolve()
        host_root.relative_to(Path(self.workdir))
        host_root.mkdir(parents=True, exist_ok=False)
        request_path = host_root / "request.json"
        response_path = host_root / "response.json"
        request_path.write_text(
            json.dumps(
                {
                    "method": normalized_method,
                    "url": str(url),
                    "headers": normalized_headers,
                    "body_hex": body_bytes.hex(),
                    "timeout": max(1, min(int(timeout), 300)),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        helper = """
import json,sys,urllib.error,urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
request_path,response_path=sys.argv[1],sys.argv[2]
data=json.load(open(request_path,encoding='utf-8'))
body=bytes.fromhex(data.get('body_hex','')) or None
request=urllib.request.Request(data['url'],data=body,headers=data.get('headers') or {},method=data['method'])
opener=urllib.request.build_opener(NoRedirect())
try:
    with opener.open(request,timeout=data['timeout']) as response:
        payload=response.read(2097153)
        if len(payload)>2097152: raise RuntimeError('response exceeds 2 MiB')
        result={'ok':True,'status':response.status,'headers':dict(response.headers.items()),'body_hex':payload.hex()}
except urllib.error.HTTPError as exc:
    payload=exc.read(2097153)
    result={'ok':False,'status':exc.code,'headers':dict(exc.headers.items()),'body_hex':payload[:2097152].hex(),'error':str(exc)}
json.dump(result,open(response_path,'w',encoding='utf-8'),ensure_ascii=False,separators=(',',':'))
""".strip()
        relative_request = (relative_root / "request.json").as_posix()
        relative_response = (relative_root / "response.json").as_posix()
        command = " ".join(
            shlex.quote(item)
            for item in (
                "python3",
                "-c",
                helper,
                relative_request,
                relative_response,
            )
        )
        try:
            spec = build_tool_sandbox_spec(
                tool_name="plugin_network",
                command=command,
                requested_workdir=self.workdir,
                timeout_seconds=max(1, min(int(timeout), 300)),
                context=sandbox_context,
                metadata={"plugin_network_authority": authority},
                sandbox_profile="networked",
            )
            result = await get_sandbox_runtime(sandbox_config).run_once(spec)
            if result.status != "completed" or result.exit_code not in {0, None}:
                raise RuntimeError(
                    f"plugin network sandbox failed: {result.stderr or result.stdout}"
                )
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            raw = bytes.fromhex(str(payload.get("body_hex") or ""))
            return {
                "ok": bool(payload.get("ok")),
                "status": int(payload.get("status") or 0),
                "headers": {
                    str(key)[:128]: str(value)[:4096]
                    for key, value in dict(payload.get("headers") or {}).items()
                },
                "body": raw.decode("utf-8", errors="replace"),
                "sandbox_job_id": result.sandbox_job_id,
                "container_name": result.container_name,
                "network": "egress",
            }
        finally:
            shutil.rmtree(host_root, ignore_errors=True)
