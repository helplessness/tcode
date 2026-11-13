"""未知社区插件的宿主侧隔离 Worker 管理器。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiohttp

from ..plugin_sdk import (
    PromptContribution,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
)
from .manifest import PluginManifest
from .permissions import PluginPermission
from .permissions import PluginPermissionScope, normalize_patterns
from .capability_broker import PluginCapabilityBroker, PluginInvocationLease
from ..plugin_sdk.protocol import (
    MAX_PROTOCOL_LINE_BYTES,
    WORKER_PROTOCOL,
    decode_message,
    encode_message,
    request_message,
    response_message,
)

_REQUEST_TIMEOUT_SECONDS = 10.0

# Trusted pipe relay: its private control descriptor closes when the backend
# dies, so an isolated plugin cannot outlive its owning desktop process.
_DESKTOP_RELAY = r'''
import json, os, selectors, shutil, signal, subprocess, sys, time
cfg=json.loads(os.environ.pop('TCODE_PLUGIN_RELAY_CONFIG'))
control=int(os.environ.pop('TCODE_PLUGIN_RELAY_CONTROL_FD'))
child=subprocess.Popen(cfg['argv'],cwd=cfg['cwd'],env=cfg['env'],stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE,stderr=sys.stderr,close_fds=True,start_new_session=True)
def stop(*_):
    if child.poll() is None: child.kill()
signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
sel=selectors.DefaultSelector()
sel.register(control,selectors.EVENT_READ,'control')
sel.register(sys.stdin,selectors.EVENT_READ,'input')
sel.register(child.stdout,selectors.EVENT_READ,'output')
last_check=0
def measure(root,skip=None):
    total=0; count=0
    for parent,dirs,files in os.walk(root,followlinks=False):
        count+=len(dirs)+len(files)
        if skip and parent==root:
            dirs[:]=[name for name in dirs if os.path.join(parent,name)!=skip]
        for name in files:
            try: total+=os.lstat(os.path.join(parent,name)).st_size
            except FileNotFoundError: pass
        if count>4000 or total>134217728: break
    return total,count
# Seatbelt denies writes to the copied plugin tree: count it once, while
# retaining the same 200ms checks and limits for every writable directory.
immutable=cfg.get('read_only_root')
def write_all(fd,data):
    view=memoryview(data)
    while view: view=view[os.write(fd,view):]
try:
    fixed_total,fixed_count=measure(immutable) if immutable else (0,0)
    while child.poll() is None:
        if time.monotonic()-last_check>.2:
            last_check=time.monotonic()
            total,count=measure(cfg['root'],skip=immutable)
            if count+fixed_count>4000 or total+fixed_total>134217728:
                stop(); break
        for key,_ in sel.select(.2):
            if key.data=='control':
                if not os.read(control,1): stop(); break
            elif key.data=='input':
                data=os.read(sys.stdin.fileno(),65536)
                if not data: stop(); break
                try: child.stdin.write(data); child.stdin.flush()
                except BrokenPipeError: break
            else:
                data=os.read(child.stdout.fileno(),65536)
                if not data: break
                write_all(sys.stdout.fileno(),data)
finally:
    stop(); child.wait()
    shutil.rmtree(cfg['root'],ignore_errors=True)
sys.exit(child.returncode if child.returncode>=0 else 128-child.returncode)
'''


def isolation_backend() -> str:
    """返回当前主机可用的强制隔离后端。"""

    requested = str(
        os.environ.get("TCODE_PLUGIN_ISOLATION_BACKEND", "auto") or "auto"
    ).strip().lower()
    runner_url = str(os.environ.get("TCODE_SANDBOX_RUNNER_URL", "") or "").strip()
    if requested in {"container", "docker", "runner"}:
        return "container-runner" if runner_url else "unavailable"
    if requested == "auto" and runner_url:
        return "container-runner"
    if requested not in {"auto", "native", "process"}:
        return "unavailable"

    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "macos-sandbox-exec"
    python_path = Path(sys.executable).resolve()
    linux_python_visible = any(
        python_path == root or root in python_path.parents
        for root in (Path("/usr"), Path("/bin"))
    )
    if (
        sys.platform.startswith("linux")
        and shutil.which("bwrap")
        and linux_python_visible
    ):
        return "linux-bwrap"
    return "unavailable"


def _sandbox_literal(path: str | Path) -> str:
    """把绝对路径编码为 sandbox-exec 安全字符串字面量。"""

    return json.dumps(str(Path(path).resolve()))


def _macos_profile(
    plugin_root: Path,
    runtime_root: Path,
    writable_root: Path,
) -> str:
    """构造只允许读取 Python、SDK 和插件包的 macOS 隔离策略。"""

    python_prefixes = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path(sys.executable).resolve(),
    }
    read_rules = [
        f"(subpath {_sandbox_literal(path)})"
        for path in sorted(
            {
                plugin_root.resolve(),
                runtime_root.parent.resolve(),
                runtime_root.resolve(),
                writable_root.resolve(),
                *python_prefixes,
            },
            key=str,
        )
    ]
    read_rules.extend(
        [
            '(subpath "/System/Library")',
            '(subpath "/usr/lib")',
            '(literal "/dev/null")',
        ]
    )
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read*)",
            '(deny file-read* (subpath "/Users"))',
            '(deny file-read* (subpath "/Volumes"))',
            '(deny file-read* (subpath "/private/var/folders"))',
            '(deny file-read* (subpath "/private/tmp"))',
            f"(allow file-read* {' '.join(read_rules)})",
            f"(allow file-write* (subpath {_sandbox_literal(writable_root)}))",
            "(deny network*)",
        ]
    )


def _worker_command(
    plugin_root: Path,
    runtime_root: Path,
    writable_root: Path,
) -> tuple[list[str], Path, Path, Path]:
    """构造 Worker 命令，并返回隔离空间内三个可见根路径。"""

    backend = isolation_backend()
    if backend == "macos-sandbox-exec":
        return (
            [
                str(shutil.which("sandbox-exec")),
                "-p",
                _macos_profile(plugin_root, runtime_root, writable_root),
                str(Path(sys.executable).resolve()),
                "-m",
                "tcode_agent.plugin_sdk.worker",
            ],
            plugin_root,
            runtime_root,
            writable_root,
        )
    if backend == "linux-bwrap":
        isolated_root = Path("/tcode-plugin")
        isolated_plugin = isolated_root / "package"
        isolated_runtime = isolated_root / "runtime"
        isolated_work = isolated_root / "work"
        command = [
            str(shutil.which("bwrap")),
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--dir",
            str(isolated_root),
        ]
        for system_root in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
            if system_root.exists():
                command.extend(
                    ["--ro-bind", str(system_root), str(system_root)]
                )
        command.extend(
            [
                "--ro-bind",
                str(runtime_root),
                str(isolated_runtime),
                "--ro-bind",
                str(plugin_root),
                str(isolated_plugin),
                "--bind",
                str(writable_root),
                str(isolated_work),
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                str(isolated_work),
                str(Path(sys.executable).resolve()),
                "-m",
                "tcode_agent.plugin_sdk.worker",
            ]
        )
        return command, isolated_plugin, isolated_runtime, isolated_work
    raise RuntimeError(
        "no strong plugin isolation backend is available; install bwrap on Linux"
    )


class IsolatedPluginWorker:
    """管理一个插件一个进程的版本化 Worker 会话。"""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        config: Mapping[str, Any],
        instance_id: str,
        workspace_id: str,
        workdir: str,
        granted_permissions: tuple[PluginPermission, ...] = (),
        execution_mode: str = "isolated-worker",
    ) -> None:
        """保存已校验 Manifest 和不含 Host 对象的初始化参数。"""

        if manifest.package_root is None:
            raise ValueError("isolated plugin package root is unavailable")
        self.manifest = manifest
        self.config = dict(config)
        self.instance_id = instance_id
        self.workspace_id = workspace_id
        self.workdir = str(Path(workdir).resolve())
        self.granted_permissions = tuple(granted_permissions)
        self.execution_mode = execution_mode
        self.plugin_root = manifest.package_root.resolve()
        self._process: asyncio.subprocess.Process | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._remote_worker_id = ""
        self._remote_container_name = ""
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._control_fd: int | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._invocations: dict[str, PluginInvocationLease] = {}
        self._broker_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = ""
        self._tools: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []
        self.plugin_info: dict[str, Any] = {}
        self._broker = PluginCapabilityBroker(
            manifest=manifest,
            permissions=self.granted_permissions,
            workdir=self.workdir,
        )

    async def _drain_stderr(self) -> None:
        """持续消费 Worker stderr，并只保留有界尾部诊断。"""

        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            self._stderr_tail = (
                self._stderr_tail + chunk.decode("utf-8", errors="replace")
            )[-16_000:]

    async def start(self) -> dict[str, Any]:
        """启动强隔离进程并完成唯一初始化握手。"""
        desktop = os.environ.get('TCODE_DESKTOP_MANAGED') == '1'
        if desktop and os.environ.get('TCODE_DESKTOP_NATIVE_READY') != '1':
            raise RuntimeError('桌面原生隔离自检未通过，插件已阻断')

        if self._process is not None or self._websocket is not None:
            raise RuntimeError("isolated plugin worker is already running")
        backend = isolation_backend()
        if backend == "container-runner":
            worker_plugin_root = Path("/tcode/plugin")
            await self._start_container_channel()
        else:
            runtime_parent = Path(
                os.environ.get(
                    "TCODE_PLUGIN_RUNTIME_ROOT",
                    str(Path(__file__).resolve().parents[2] / "tmp" / "plugin-workers"),
                )
            ).expanduser().resolve()
            runtime_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._temporary = tempfile.TemporaryDirectory(
                prefix="tcode-plugin-worker-",
                dir=runtime_parent,
            )
            temporary_root = Path(self._temporary.name).resolve()
            runtime_root = temporary_root / "runtime"
            runtime_package = runtime_root / "tcode_agent"
            writable_root = temporary_root / "work"
            writable_root.mkdir()
            if self.execution_mode == "trusted-worker":
                shutil.copytree(
                    Path(__file__).resolve().parents[1], runtime_package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static"),
                )
            else:
                runtime_package.mkdir(parents=True)
                (runtime_package / "__init__.py").write_text(
                    '"""隔离插件 SDK 运行时包。"""\n', encoding="utf-8",
                )
                shutil.copytree(
                    Path(__file__).resolve().parents[1] / "plugin_sdk",
                    runtime_package / "plugin_sdk",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                # SDK ontology exports need their implementation in the private
                # runtime too; never add the host source tree to the read roots.
                shutil.copy2(
                    Path(__file__).resolve().parents[1] / "ontology_registry.py",
                    runtime_package / "ontology_registry.py",
                )
                shutil.copytree(
                    Path(__file__).resolve().parents[1] / "ontology_core",
                    runtime_package / "ontology_core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            plugin_root = self.plugin_root
            if desktop:
                from ..runtime.sandbox.macos_native import copy_tree
                plugin_root = temporary_root / 'plugin'
                plugin_root.mkdir()
                copy_tree(self.plugin_root, plugin_root)
            command, worker_plugin_root, worker_runtime_root, worker_writable_root = _worker_command(
                plugin_root,
                runtime_root,
                writable_root,
            )
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": str(worker_runtime_root),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TMPDIR": str(worker_writable_root),
                "TCODE_PLUGIN_WORKER": "1",
            }
            if desktop:
                from ..runtime.sandbox.macos_native import profile
                if backend != 'macos-sandbox-exec':
                    raise RuntimeError('桌面插件要求 macOS Seatbelt')
                read_roots: list[Path] = []
                write_roots: list[Path] = []
                if self.execution_mode == "trusted-worker":
                    for permission in self.granted_permissions:
                        if permission.scope not in {
                            PluginPermissionScope.FILESYSTEM_READ.value,
                            PluginPermissionScope.FILESYSTEM_WRITE.value,
                        }:
                            continue
                        for pattern in normalize_patterns(
                            permission.scope, permission.patterns, self.workdir
                        ):
                            prefix = pattern
                            for marker in ("*", "?", "["):
                                prefix = prefix.split(marker, 1)[0]
                            root = Path(prefix.rstrip(os.sep) or os.sep)
                            while not root.exists() and root != root.parent:
                                root = root.parent
                            (write_roots if permission.scope == PluginPermissionScope.FILESYSTEM_WRITE.value else read_roots).append(root.resolve())
                command[2] = profile(
                    temporary_root,
                    read_only=plugin_root,
                    extra_read=tuple(read_roots + write_roots),
                    extra_write=tuple(write_roots),
                )
                read_fd, write_fd = os.pipe()
                environment['TCODE_PLUGIN_RELAY_CONFIG'] = json.dumps({
                    'argv': command, 'cwd': str(writable_root), 'env': environment,
                    'root': str(temporary_root),
                    'read_only_root': str(plugin_root),
                })
                environment['TCODE_PLUGIN_RELAY_CONTROL_FD'] = str(read_fd)
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        sys.executable, '-I', '-S', '-c', _DESKTOP_RELAY,
                        cwd=writable_root, env=environment, pass_fds=(read_fd,),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, start_new_session=True,
                        limit=MAX_PROTOCOL_LINE_BYTES,
                    )
                    self._control_fd = write_fd
                except Exception:
                    os.close(write_fd)
                    raise
                finally:
                    os.close(read_fd)
            else:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=writable_root,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name != "nt",
                    limit=MAX_PROTOCOL_LINE_BYTES,
                )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._reader_task = asyncio.create_task(self._read_protocol())
        try:
            result = await self.request(
                "initialize",
                {
                    "plugin_root": str(worker_plugin_root),
                    "manifest": self.manifest.to_dict(),
                    "config": self.config,
                    "instance_id": self.instance_id,
                    "workspace_id": self.workspace_id,
                    "effective_trust": (
                        "trusted-worker"
                        if self.execution_mode == "trusted-worker"
                        else "isolated"
                    ),
                    "workdir": self.workdir,
                    "original_plugin_root": str(self.plugin_root),
                    "writable_root": str(worker_writable_root),
                    "granted_permissions": [
                        item.to_dict() for item in self.granted_permissions
                    ],
                },
                timeout=15.0,
            )
        except Exception:
            await self.stop()
            raise
        self.plugin_info = dict(result.get("plugin") or {})
        self._prompts = list(result.get("prompts") or [])
        if self._prompts and (self.execution_mode != "trusted-worker" or "prompt" not in self.manifest.contributes):
            await self.stop()
            raise PermissionError("worker prompts require trusted, declared contributions")
        self._tools = [
            dict(item) for item in result.get("tools") or [] if isinstance(item, dict)
        ]
        self._broker.bind_plugin_tools(
            [str(item.get("name") or "") for item in self._tools]
        )
        return dict(result)

    @staticmethod
    def _runner_token() -> str:
        """读取 Host 已配置的 Runner Token，不向插件容器传递。"""

        direct = str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN", "") or "").strip()
        if direct:
            return direct
        token_file = str(
            os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN_FILE", "") or ""
        ).strip()
        if not token_file:
            return ""
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()

    async def _start_container_channel(self) -> None:
        """通过 Runner 启动 Docker 插件容器并建立认证双向通道。"""

        runner_url = str(os.environ.get("TCODE_SANDBOX_RUNNER_URL", "") or "").rstrip("/")
        if not runner_url:
            raise RuntimeError("plugin container isolation requires sandbox runner URL")
        token = self._runner_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        try:
            async with self._http_session.post(
                f"{runner_url}/v1/plugin-workers",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "plugin_name": self.manifest.name,
                    "plugin_root": self.plugin_root.as_posix(),
                    "image": str(
                        os.environ.get(
                            "TCODE_PLUGIN_CONTAINER_IMAGE",
                            "tcode-plugin-runtime:py312",
                        )
                        or "tcode-plugin-runtime:py312"
                    ),
                },
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(
                        str(payload.get("detail") if isinstance(payload, dict) else payload)
                    )
            self._remote_worker_id = str(payload.get("worker_id") or "")
            self._remote_container_name = str(payload.get("container_name") or "")
            if not self._remote_worker_id:
                raise RuntimeError("sandbox runner returned no plugin worker id")
            ws_url = runner_url.replace("http://", "ws://", 1).replace(
                "https://", "wss://", 1
            )
            self._websocket = await self._http_session.ws_connect(
                f"{ws_url}/v1/plugin-workers/{self._remote_worker_id}/channel",
                headers=headers,
                max_msg_size=MAX_PROTOCOL_LINE_BYTES,
                heartbeat=20,
            )
        except Exception:
            await self._stop_container_channel()
            raise

    async def _stop_container_channel(self) -> None:
        """关闭协议通道并让 Runner 幂等回收精确容器。"""

        websocket = self._websocket
        self._websocket = None
        if websocket is not None and not websocket.closed:
            await websocket.close()
        session = self._http_session
        worker_id = self._remote_worker_id
        self._remote_worker_id = ""
        if session is not None and worker_id:
            runner_url = str(
                os.environ.get("TCODE_SANDBOX_RUNNER_URL", "") or ""
            ).rstrip("/")
            token = self._runner_token()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                async with session.delete(
                    f"{runner_url}/v1/plugin-workers/{worker_id}",
                    headers=headers,
                ):
                    pass
            except Exception:
                pass
        self._http_session = None
        if session is not None and not session.closed:
            await session.close()

    async def _send_protocol(self, message: Mapping[str, Any]) -> None:
        """串行写出一条 Worker v2 消息。"""

        payload = encode_message(message)
        async with self._write_lock:
            websocket = self._websocket
            if websocket is not None:
                if websocket.closed:
                    raise RuntimeError("isolated plugin container channel is closed")
                await websocket.send_str(payload.decode("utf-8").rstrip("\n"))
                return
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("isolated plugin worker is not running")
            if process.returncode is not None:
                raise RuntimeError(
                    f"isolated plugin worker exited rc={process.returncode}: {self._stderr_tail}"
                )
            process.stdin.write(payload)
            await process.stdin.drain()

    async def _read_protocol(self) -> None:
        """持续读取响应和 Worker 反向能力请求。"""

        failure: BaseException | None = None
        try:
            if self._websocket is not None:
                async for frame in self._websocket:
                    if frame.type != aiohttp.WSMsgType.TEXT:
                        if frame.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
                        continue
                    line = str(frame.data).encode("utf-8")
                    message = decode_message(line)
                    await self._dispatch_protocol_message(message)
            else:
                process = self._process
                if process is None or process.stdout is None:
                    return
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    message = decode_message(line)
                    await self._dispatch_protocol_message(message)
        except BaseException as exc:
            failure = exc
        finally:
            detail = (
                str(failure)
                if failure is not None
                else f"isolated plugin worker closed protocol: {self._stderr_tail}"
            )
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(detail))

    async def _dispatch_protocol_message(self, message: Mapping[str, Any]) -> None:
        """把一条已解码消息交给 Pending 请求或能力 Broker。"""

        message_type = str(message.get("type") or "")
        if message_type == "response":
            future = self._pending.get(str(message.get("id") or ""))
            if future is not None and not future.done():
                future.set_result(dict(message))
            return
        if message_type == "request":
            parent_id = str(message.get("parent_id") or "")
            task = asyncio.create_task(self._handle_worker_request(message))
            tasks = self._broker_tasks.setdefault(parent_id, set())
            tasks.add(task)

            def release(completed: asyncio.Task[None]) -> None:
                """从活动父调用中移除已经结束的 Broker 任务。"""

                current = self._broker_tasks.get(parent_id)
                if current is None:
                    return
                current.discard(completed)
                if not current:
                    self._broker_tasks.pop(parent_id, None)

            task.add_done_callback(release)

    async def _cancel_broker_tasks(self, parent_id: str) -> None:
        """取消已结束父调用仍遗留的反向能力请求。"""

        tasks = list(self._broker_tasks.pop(parent_id, set()))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_worker_request(self, message: Mapping[str, Any]) -> None:
        """校验活动父调用后执行一项 Host 能力请求。"""

        request_id = str(message.get("id") or "")
        parent_id = str(message.get("parent_id") or "")
        try:
            if str(message.get("method") or "") != "capability.invoke":
                raise PermissionError("unsupported worker-to-host method")
            lease = self._invocations.get(parent_id)
            if lease is None:
                raise PermissionError("plugin capability parent invocation is not active")
            result = await self._broker.invoke(
                dict(message.get("params") or {}),
                lease,
            )
            response = response_message(request_id, result=result)
        except Exception as exc:
            response = response_message(
                request_id,
                error={
                    "code": f"host.{type(exc).__name__.lower()}",
                    "message": self._public_error_message(exc),
                },
            )
        try:
            await self._send_protocol(response)
        except Exception:
            await self._terminate()

    def _public_error_message(self, exc: BaseException) -> str:
        """删除 Broker 错误中的宿主路径、Runner 地址和直传 Token。"""

        message = str(exc or "plugin capability failed")
        replacements = {
            self.workdir: "<workspace>",
            self.plugin_root.as_posix(): "<plugin>",
            str(os.environ.get("TCODE_SANDBOX_RUNNER_URL", "") or "").strip(): "<runner>",
            str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN", "") or "").strip(): "<redacted>",
        }
        for value, replacement in replacements.items():
            if value:
                message = message.replace(value, replacement)
        return message[:2000]

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        invocation: PluginInvocationLease | None = None,
    ) -> dict[str, Any]:
        """发送 Worker v2 请求，并允许处理期间发生反向 Host 调用。"""

        process = self._process
        if self._websocket is None and (
            process is None or process.stdin is None or process.stdout is None
        ):
            raise RuntimeError("isolated plugin worker is not running")
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        if invocation is not None:
            self._invocations[request_id] = PluginInvocationLease(
                request_id=request_id,
                tool_id=invocation.tool_id,
                side_effect_level=invocation.side_effect_level,
                context=invocation.context,
                artifacts=invocation.artifacts,
            )
        try:
            await self._send_protocol(
                request_message(request_id, method, dict(params or {}))
            )
            try:
                response = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                await self._terminate()
                raise TimeoutError(
                    f"isolated plugin worker request timed out after {timeout:g}s"
                ) from exc
        finally:
            self._pending.pop(request_id, None)
            self._invocations.pop(request_id, None)
            await self._cancel_broker_tasks(request_id)
        if not response.get("ok"):
            error = dict(response.get("error") or {})
            raise RuntimeError(
                f"[{error.get('code') or 'worker.error'}] {error.get('message') or 'worker failed'}"
            )
        return dict(response.get("result") or {})

    def prompt_contributions(self) -> list[PromptContribution]:
        contributions = []
        for descriptor in self._prompts:
            async def render(context, *, name=descriptor["name"]):
                result = await self.request("prompt.render", {"name": name, "agent": context.agent,
                    "workspace_id": context.workspace_id, "variables": dict(context.variables)})
                return result["content"]
            contributions.append(PromptContribution(renderer=render, **descriptor))
        return contributions

    def tool_definitions(self) -> list[ToolDefinition]:
        """把 Worker 工具目录投影为调用仍通过 Host Pipeline 的公开定义。"""

        definitions: list[ToolDefinition] = []
        for descriptor in self._tools:
            name = str(descriptor.get("name") or "")

            async def handler(
                arguments: Mapping[str, Any],
                context: ToolCallContext,
                *,
                tool_name: str = name,
            ) -> ToolOutput:
                """拒绝缺少 Host ToolContext 的内部误调用。"""

                raise RuntimeError(
                    "isolated plugin tool requires the Host context adapter"
                )

            async def invoke_with_host_context(
                arguments: Mapping[str, Any],
                context: ToolCallContext,
                host_context: Any,
                *,
                tool_name: str = name,
                side_effect_level: str = str(
                    descriptor.get("side_effect_level") or "none"
                ),
            ) -> ToolOutput:
                """绑定真实父调用后把工具请求转发到所属 Worker。"""

                result = await self.request(
                    "execute",
                    {
                        "tool": tool_name,
                        "arguments": dict(arguments),
                        "context": {
                            "session_id": context.session_id,
                            "message_id": context.message_id,
                            "agent": context.agent,
                            "call_id": context.call_id,
                            "execution": dict(context.execution),
                            "user_messages": list(context.user_messages),
                            "ontology_source_policy": (
                                context.ontology.source_policy()
                                if context.ontology is not None
                                else None
                            ),
                        },
                    },
                    timeout=max(
                        _REQUEST_TIMEOUT_SECONDS,
                        float(
                            os.environ.get(
                                "TCODE_PLUGIN_TOOL_TIMEOUT_SECONDS",
                                "600",
                            )
                            or 600
                        ),
                    ),
                    invocation=PluginInvocationLease(
                        request_id="",
                        tool_id=tool_name,
                        side_effect_level=side_effect_level,
                        context=host_context,
                        artifacts=context.artifacts,
                    ),
                )
                metadata = dict(result.get("metadata") or {})
                if result.get("truncated"):
                    metadata["worker_truncated"] = True
                return ToolOutput(
                    title=str(result.get("title") or tool_name),
                    content=str(result.get("content") or ""),
                    metadata=metadata,
                )

            setattr(
                handler,
                "__tcode_invoke_with_host_context__",
                invoke_with_host_context,
            )

            definitions.append(
                ToolDefinition(
                    name=name,
                    description=str(descriptor.get("description") or ""),
                    input_schema=dict(descriptor.get("input_schema") or {}),
                    handler=handler,
                    version=str(descriptor.get("version") or "1.0.0"),
                    side_effect_level=str(
                        descriptor.get("side_effect_level") or "none"
                    ),
                    result_budget_tokens=int(descriptor.get("result_budget_tokens") or 2048),
                    on_demand=descriptor.get("on_demand") is True,
                )
            )
        return definitions

    async def health(self) -> dict[str, Any]:
        """查询 Worker 健康状态，请求超时时直接报告不健康。"""

        try:
            return await self.request("health", timeout=3.0)
        except Exception as exc:
            return {"status": "unhealthy", "summary": str(exc)[:1000]}

    async def _terminate(self) -> None:
        """终止 Worker 进程组，并在宽限后强制回收。"""

        process = self._process
        if self._control_fd is not None:
            os.close(self._control_fd)
            self._control_fd = None
        if self._websocket is not None or self._remote_worker_id:
            await self._stop_container_channel()
            return
        if process is None or process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def stop(self, _reason: str = "unload") -> None:
        """请求插件停止，然后无条件回收进程和临时目录。"""

        process = self._process
        channel_active = self._websocket is not None and not self._websocket.closed
        if channel_active or (process is not None and process.returncode is None):
            try:
                await self.request("stop", timeout=3.0)
            except Exception:
                pass
        await self._terminate()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
        self._process = None
        self._stderr_task = None
        if self._reader_task is not None:
            if not self._reader_task.done():
                self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, RuntimeError):
                pass
        self._reader_task = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


__all__ = ["IsolatedPluginWorker", "WORKER_PROTOCOL", "isolation_backend"]
