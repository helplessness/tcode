#!/usr/bin/env python3
"""在本机隔离环境执行 CLI、WebSocket、插件和 Worker 真实入口门禁。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import aiohttp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCHEMA = "tcode.real-entry-smoke-report.v1"
SMOKE_RESPONSE = "tcode-real-entry-smoke-ok"
SCENARIO_IDS = (
    "server-websocket-plugin",
    "cli-turn",
    "worker-mcp",
)


class SmokeFailure(RuntimeError):
    """表示真实入口未满足外部结果、时限或资源清理契约。"""


@dataclass
class ProcessResult:
    """保存一次有界子进程的退出状态与受限输出。"""

    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float


@dataclass
class SmokeRecord:
    """保存一个真实入口场景的可序列化执行结论。"""

    scenario: str
    status: str
    duration_seconds: float
    assertions: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    output_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """返回不包含请求载荷和本机临时路径的报告记录。"""

        return {
            "scenario": self.scenario,
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 6),
            "assertions": dict(self.assertions),
            "error": self.error,
            "output_tail": self.output_tail,
        }


@dataclass
class StubState:
    """以计数器记录本机 LLM/MCP 协议桩被真实入口调用的事实。"""

    openai_calls: int = 0
    mcp_initialize_calls: int = 0
    mcp_list_calls: int = 0
    mcp_tool_calls: int = 0


class SmokeHTTPServer(ThreadingHTTPServer):
    """承载隔离 OpenAI-compatible 与 MCP JSON-RPC 协议桩。"""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: StubState) -> None:
        """绑定回环地址并关联只保留计数的协议桩状态。"""

        super().__init__(address, SmokeRequestHandler)
        self.state = state


class SmokeRequestHandler(BaseHTTPRequestHandler):
    """为真实入口提供确定性、无外部网络依赖的协议响应。"""

    server: SmokeHTTPServer

    def log_message(self, format_text: str, *args: Any) -> None:
        """关闭标准 HTTP 访问日志，避免把请求细节写入门禁报告。"""

        del format_text, args

    def _read_payload(self) -> dict[str, Any]:
        """读取有界 JSON 对象；非法或非对象请求统一视为空对象。"""

        try:
            length = max(0, min(int(self.headers.get("content-length", "0")), 8 * 1024 * 1024))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """写出一个带明确长度和可选 MCP Session 头的 JSON 响应。"""

        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        for name, value in dict(headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_openai(self, payload: dict[str, Any]) -> None:
        """返回一个无工具调用的标准 OpenAI ChatCompletion 终态。"""

        self.server.state.openai_calls += 1
        model = str(payload.get("model") or "smoke-model")
        self._send_json(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": SMOKE_RESPONSE,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            }
        )

    def _handle_mcp(self, payload: dict[str, Any]) -> None:
        """实现 Manager 当前使用的 initialize、tools/list 与 tools/call 子集。"""

        method = str(payload.get("method") or "")
        request_id = payload.get("id")
        headers = {"mcp-session-id": "dsh-002-smoke-session"}
        if method == "initialize":
            self.server.state.mcp_initialize_calls += 1
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "dsh-002-smoke", "version": "1.0"},
                    },
                },
                headers=headers,
            )
            return
        if method == "notifications/initialized":
            self._send_json({}, headers=headers)
            return
        if method == "tools/list":
            self.server.state.mcp_list_calls += 1
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "返回传入的 smoke 值。",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                    "required": ["value"],
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    },
                },
                headers=headers,
            )
            return
        if method == "tools/call":
            self.server.state.mcp_tool_calls += 1
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            arguments = (
                params.get("arguments")
                if isinstance(params.get("arguments"), dict)
                else {}
            )
            value = str(arguments.get("value") or "")
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": value}],
                        "structuredContent": {"ok": True, "echo": value},
                        "isError": False,
                    },
                },
                headers=headers,
            )
            return
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            },
            headers=headers,
        )

    def do_POST(self) -> None:
        """按请求路径分发本机 LLM 或 MCP 协议调用。"""

        payload = self._read_payload()
        if self.path.rstrip("/") == "/v1/chat/completions":
            self._handle_openai(payload)
            return
        if self.path.rstrip("/") == "/mcp":
            self._handle_mcp(payload)
            return
        self._send_json({"error": "not found"}, status=404)


class StubServices:
    """管理协议桩线程的启动、地址公开和确定性关闭。"""

    def __init__(self) -> None:
        """创建尚未绑定端口的空协议桩。"""

        self.state = StubState()
        self.server: SmokeHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """返回已经启动的回环监听端口。"""

        if self.server is None:
            raise SmokeFailure("协议桩尚未启动")
        return int(self.server.server_address[1])

    def start(self) -> None:
        """在守护线程启动由操作系统分配端口的回环协议桩。"""

        if self.server is not None:
            return
        self.server = SmokeHTTPServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="dsh-002-smoke-stub",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        """停止协议桩并回收监听 socket 与后台线程。"""

        server = self.server
        thread = self.thread
        self.server = None
        self.thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=3)


@dataclass(frozen=True)
class SmokeEnvironment:
    """描述一次门禁运行使用的全部隔离目录和配置文件。"""

    root: Path
    config_root: Path
    cli_config_root: Path
    server_root: Path
    server_workspace: Path
    cli_workspace: Path
    mcp_config: Path
    empty_mcp_config: Path
    empty_env_file: Path


def _iso_now() -> str:
    """返回便于机器解析的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def _reserve_loopback_port() -> int:
    """短暂绑定回环端口并返回可供真实 Server 使用的候选端口。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8 和稳定缩进写入隔离运行配置。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_environment(root: Path, stub: StubServices) -> SmokeEnvironment:
    """创建不会读取项目真实密钥、MCP 或 Session 的门禁运行目录。"""

    config_root = root / "config-root"
    cli_config_root = root / "cli-config-root"
    server_root = root / "server-runtime"
    server_workspace = (
        server_root / "workdirs" / "admin_tenant" / "local-user" / "workspace-1"
    )
    cli_workspace = root / "cli-workspace"
    for directory in (
        config_root,
        cli_config_root,
        server_root,
        server_workspace,
        cli_workspace,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    provider_config = {
        "model": "smoke/smoke-model",
        "runtime": {"llm_timeout_seconds": 10},
        "provider": {
            "smoke": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": f"http://127.0.0.1:{stub.port}/v1",
                    "apiKey": "dsh-002-smoke-key",
                    "timeoutSeconds": 10,
                },
                "models": {"smoke-model": {"name": "smoke-model"}},
            }
        },
        "plugins": [],
    }
    _write_json(config_root / "tcode.json", provider_config)
    _write_json(cli_config_root / "tcode.json", provider_config)
    _write_json(
        config_root / "plugins.json",
        {
            "auto_discover": False,
            "plugins": [
                {
                    "name": "example-plugin",
                    "path": (PROJECT_ROOT / "plugins" / "example_plugin").as_posix(),
                    "enabled": True,
                    "config": {"default_prefix": "Smoke"},
                    "source": "dsh-002-smoke",
                }
            ],
        },
    )
    _write_json(
        cli_config_root / "plugins.json",
        {"auto_discover": False, "plugins": []},
    )
    mcp_config = config_root / "mcp-smoke.json"
    _write_json(
        mcp_config,
        {
            "smoke-mcp": {
                "url": f"http://127.0.0.1:{stub.port}/mcp",
                "transport": "streamable-http",
                "timeout": 5000,
            },
        },
    )
    empty_mcp_config = config_root / "mcp-empty.json"
    empty_env_file = config_root / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    return SmokeEnvironment(
        root=root,
        config_root=config_root,
        cli_config_root=cli_config_root,
        server_root=server_root,
        server_workspace=server_workspace,
        cli_workspace=cli_workspace,
        mcp_config=mcp_config,
        empty_mcp_config=empty_mcp_config,
        empty_env_file=empty_env_file,
    )


def _base_env(
    environment: SmokeEnvironment,
    *,
    config_root: Path | None = None,
) -> dict[str, str]:
    """返回显式关闭外部依赖和生产 Canary 的隔离子进程环境。"""

    child_env = os.environ.copy()
    child_env.update(
        {
            "PYTHONPATH": PROJECT_ROOT.as_posix(),
            "TCODE_CONFIG_ROOT": (config_root or environment.config_root).as_posix(),
            "TCODE_ENV_FILE": environment.empty_env_file.as_posix(),
            "TCODE_AUTH_REQUIRED": "0",
            "TCODE_ENTERPRISE_MODE": "0",
            "TCODE_ASYNC_WATCHER_ENABLED": "0",
            "TCODE_RESEARCH_ENABLED": "false",
            "TCODE_SANDBOX_ENABLED": "false",
            "TCODE_PLUGIN_TRUSTED_ALLOWLIST": "example-plugin",
            "TCODE_PLUGIN_PROMPT_SHADOW_ENABLED": "false",
            "TCODE_PLUGIN_TRANSACTION_MODE": "enforce",
            "TCODE_PLUGIN_STARTUP_WAIT_SECONDS": "5",
            "TCODE_PLUGIN_REQUEST_WAIT_SECONDS": "5",
            "TCODE_LLM_TIMEOUT": "10",
        }
    )
    return child_env


def _sanitize_output(text: str, environment: SmokeEnvironment, limit: int = 4000) -> str:
    """移除临时根路径并只保留诊断输出尾部。"""

    clean = str(text or "")
    temporary_roots = {
        environment.root.as_posix(),
        environment.root.resolve().as_posix(),
    }
    for temporary_root in sorted(temporary_roots, key=len, reverse=True):
        clean = clean.replace(temporary_root, "<smoke-runtime>")
    return clean[-max(1, int(limit)) :]


def _stop_process_group(process: subprocess.Popen[str], timeout: float = 5.0) -> tuple[str, str]:
    """先 TERM 后 KILL 回收真实入口及其可能创建的全部子进程。"""

    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()
        try:
            process.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            process.wait(timeout=max(0.1, timeout))
    stdout, stderr = process.communicate(timeout=max(0.1, timeout))
    return str(stdout or ""), str(stderr or "")


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> ProcessResult:
    """在独立进程组执行一个真实入口并应用硬超时。"""

    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(0.1, timeout))
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _stop_process_group(process)
        raise SmokeFailure(
            f"子进程超过 {timeout:g} 秒硬时限: {Path(command[1]).name}"
        ) from exc
    return ProcessResult(
        return_code=int(process.returncode or 0),
        stdout=str(stdout or ""),
        stderr=str(stderr or ""),
        duration_seconds=time.monotonic() - started,
    )


def _json_request(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
) -> dict[str, Any]:
    """向真实 Server 发出有界 JSON HTTP 请求。"""

    request = Request(url, method=method, headers={"accept": "application/json"})
    try:
        with urlopen(request, timeout=max(0.1, timeout)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"HTTP 请求失败: {url}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure(f"HTTP 响应不是 JSON 对象: {url}")
    return payload


def _wait_for_health(process: subprocess.Popen[str], base_url: str, timeout: float) -> dict[str, Any]:
    """等待真实 Server 健康端点成功或提前报告进程退出。"""

    deadline = time.monotonic() + max(0.1, timeout)
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(f"Server 在健康检查前退出，return_code={process.returncode}")
        try:
            payload = _json_request(f"{base_url}/api/health", timeout=0.5)
            if payload.get("status") == "ok":
                return payload
            last_error = f"unexpected status={payload.get('status')!r}"
        except SmokeFailure as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise SmokeFailure(f"Server 健康检查超时: {last_error or 'no response'}")


async def _websocket_turn(base_url: str, session_id: str, timeout: float) -> dict[str, Any]:
    """通过真实 `/ws/chat` 完成一轮非流式对话并等待 done 终态。"""

    ws_url = base_url.replace("http://", "ws://", 1) + "/ws/chat"
    timeout_config = aiohttp.ClientTimeout(total=max(1.0, timeout))
    event_types: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout_config) as session:
        async with session.ws_connect(ws_url, heartbeat=5) as websocket:
            await websocket.send_json(
                {
                    "message": "执行 DSH-002 WebSocket smoke",
                    "session_id": session_id,
                    "request_id": "dsh-002-websocket-smoke",
                    "agent_name": "build",
                    "model": "smoke/smoke-model",
                    "stream": False,
                    "tenant_id": "admin_tenant",
                    "user_id": "local-user",
                    "workspace_id": "workspace-1",
                    "workdir_name": "workspace-1",
                }
            )
            deadline = time.monotonic() + max(1.0, timeout)
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                if message.type is aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError as exc:
                        raise SmokeFailure("WebSocket 返回非法 JSON") from exc
                    if not isinstance(payload, dict):
                        continue
                    event_type = str(payload.get("type") or "")
                    if event_type:
                        event_types.append(event_type)
                    if event_type == "error":
                        raise SmokeFailure(
                            f"WebSocket 返回错误: {str(payload.get('error') or '')[:200]}"
                        )
                    if event_type == "message" and payload.get("done") is True:
                        content = str(payload.get("content") or "")
                        if content != SMOKE_RESPONSE:
                            raise SmokeFailure("WebSocket 最终消息与协议桩响应不一致")
                        return {
                            "terminal_message": True,
                            "event_types": sorted(set(event_types)),
                        }
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise SmokeFailure(f"WebSocket 在终态前关闭: {message.type.name}")
    raise SmokeFailure("WebSocket 一轮对话超过场景时限")


def _current_session_messages(database: Path, session_id: str) -> list[dict[str, str]]:
    """直接读取 SQLite 当前 revision，验证 CLI 的外部持久化结果。"""

    if not database.is_file():
        raise SmokeFailure("CLI 没有生成 Session SQLite 数据库")
    connection = sqlite3.connect(database.as_posix())
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT current_revision, status FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SmokeFailure("CLI Session 未写入 SQLite")
        messages = connection.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ? AND session_revision = ?
            ORDER BY message_order ASC
            """,
            (session_id, int(row["current_revision"] or 0)),
        ).fetchall()
        return [
            {"role": str(message["role"] or ""), "content": str(message["content"] or "")}
            for message in messages
        ]
    finally:
        connection.close()


def _server_websocket_plugin_scenario(
    environment: SmokeEnvironment,
    stub: StubServices,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    """验证真实 Server、健康端点、插件、WebSocket 和 Session 投影。"""

    port = _reserve_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    child_env = _base_env(environment)
    child_env.update(
        {
            "TCODE_WORKDIR": environment.server_root.as_posix(),
            "TCODE_MCP_CONFIG": environment.empty_mcp_config.as_posix(),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_server.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workdir",
            environment.server_root.as_posix(),
        ],
        cwd=PROJECT_ROOT,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = time.monotonic()
    output = ""
    assertions: dict[str, Any] = {}
    scenario_error: Exception | None = None
    openai_before = stub.state.openai_calls
    try:
        health = _wait_for_health(process, base_url, min(timeout, 20.0))
        plugins = _json_request(f"{base_url}/api/plugins/list", timeout=min(timeout, 10.0))
        loaded_names = {
            str(item.get("name") or "")
            for item in list(plugins.get("plugins") or [])
            if isinstance(item, dict)
        }
        if "example-plugin" not in loaded_names:
            raise SmokeFailure("官方 example-plugin 未从隔离配置发现并加载")
        if dict(plugins.get("errors") or {}):
            raise SmokeFailure("插件真实入口存在加载错误")

        session_id = "dsh-002-websocket-smoke"
        elapsed = time.monotonic() - started
        websocket_result = asyncio.run(
            _websocket_turn(base_url, session_id, max(1.0, timeout - elapsed))
        )
        session_payload = _json_request(
            f"{base_url}/api/sessions/{session_id}",
            timeout=min(timeout, 10.0),
        )
        messages = list(session_payload.get("messages") or [])
        persisted_roles = [
            str(item.get("role") or "") for item in messages if isinstance(item, dict)
        ]
        assistant_positions = [
            index
            for index, item in enumerate(messages)
            if isinstance(item, dict)
            and str(item.get("role") or "") == "assistant"
            and str(item.get("content") or "") == SMOKE_RESPONSE
        ]
        if not assistant_positions or not any(
            role == "user" for role in persisted_roles[: assistant_positions[-1]]
        ):
            raise SmokeFailure(
                "Session API 未投影完整的一轮 user/assistant 消息，"
                f"roles={persisted_roles}"
            )
        if str(messages[assistant_positions[-1]].get("content") or "") != SMOKE_RESPONSE:
            raise SmokeFailure("Session API 最终消息与 WebSocket 终态不一致")
        if stub.state.openai_calls <= openai_before:
            raise SmokeFailure("WebSocket 真实入口没有调用本机 LLM 协议桩")
        assertions = {
            "server_health": health.get("status") == "ok",
            "plugin_loaded": True,
            "plugin_load_errors": 0,
            "websocket_terminal": bool(websocket_result.get("terminal_message")),
            "session_persisted": True,
            "llm_calls": stub.state.openai_calls - openai_before,
        }
    except Exception as exc:
        scenario_error = exc
    finally:
        stdout, stderr = _stop_process_group(process)
        output = f"{stdout}\n{stderr}".strip()
    if scenario_error is not None:
        raise SmokeFailure(
            f"{scenario_error}; server_output_tail={output[-1200:]}"
        ) from scenario_error
    return assertions, output


def _cli_turn_scenario(
    environment: SmokeEnvironment,
    stub: StubServices,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    """启动真实 chat_cli 一轮模式并检查 SQLite 中的最终 Session。"""

    child_env = _base_env(environment, config_root=environment.cli_config_root)
    child_env.update(
        {
            "TCODE_WORKDIR": environment.cli_workspace.as_posix(),
            "TCODE_MCP_CONFIG": environment.empty_mcp_config.as_posix(),
        }
    )
    session_id = "dsh-002-cli-smoke"
    openai_before = stub.state.openai_calls
    result = _run_process(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "chat_cli.py"),
            "--workdir",
            environment.cli_workspace.as_posix(),
            "--agent",
            "build",
            "--model",
            "smoke/smoke-model",
            "--session-id",
            session_id,
            "--prompt",
            "执行 DSH-002 CLI smoke",
            "--once",
        ],
        cwd=PROJECT_ROOT,
        env=child_env,
        timeout=timeout,
    )
    if result.return_code != 0:
        raise SmokeFailure(f"chat_cli 返回非零状态: {result.return_code}")
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    if "unhandled exception" in combined_output or "custom tool name conflict" in combined_output:
        raise SmokeFailure(
            "chat_cli 退出前存在未处理插件任务异常: "
            f"{result.stderr[-1400:]}"
        )
    if SMOKE_RESPONSE not in result.stdout:
        raise SmokeFailure("chat_cli 标准输出缺少最终协议桩消息")
    messages = _current_session_messages(
        environment.cli_workspace / ".tcode" / "sessions.sqlite",
        session_id,
    )
    if [item["role"] for item in messages[-2:]] != ["user", "assistant"]:
        raise SmokeFailure("CLI SQLite 未保存完整的一轮 user/assistant 消息")
    if messages[-1]["content"] != SMOKE_RESPONSE:
        raise SmokeFailure("CLI SQLite 最终消息与终端输出不一致")
    if stub.state.openai_calls <= openai_before:
        raise SmokeFailure("CLI 真实入口没有调用本机 LLM 协议桩")
    return (
        {
            "exit_code": result.return_code,
            "terminal_output": True,
            "session_persisted": True,
            "llm_calls": stub.state.openai_calls - openai_before,
        },
        f"{result.stdout}\n{result.stderr}".strip(),
    )


def _worker_mcp_scenario(
    environment: SmokeEnvironment,
    stub: StubServices,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    """启动真实 MCP Worker，验证 profile 解析和结果文件契约。"""

    child_env = _base_env(environment)
    child_env.update(
        {
            "TCODE_WORKDIR": environment.cli_workspace.as_posix(),
            "TCODE_MCP_CONFIG": environment.empty_mcp_config.as_posix(),
            "TCODE_EXECUTOR_MCP_CONFIG": environment.mcp_config.as_posix(),
            "SYMPHONY_DIRECT_MCP_SERVERS": "smoke-mcp",
            "SYMPHONY_ENFORCE_DIRECT_WHITELIST": "1",
            "MCP_TOOL_RUNNER_MAX_RETRIES": "0",
        }
    )
    output_path = environment.root / "worker-output" / "mcp_result.json"
    initialize_before = stub.state.mcp_initialize_calls
    list_before = stub.state.mcp_list_calls
    tool_before = stub.state.mcp_tool_calls
    result = _run_process(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "mcp_tool_job_runner.py"),
            "--server",
            "smoke-mcp",
            "--tool",
            "echo",
            "--args-json",
            json.dumps({"value": "worker-smoke"}),
            "--output-file",
            output_path.as_posix(),
            "--timeout-seconds",
            str(max(1, min(int(timeout), 30))),
        ],
        cwd=PROJECT_ROOT,
        env=child_env,
        timeout=timeout,
    )
    if result.return_code != 0:
        raise SmokeFailure(f"mcp_tool_job_runner 返回非零状态: {result.return_code}")
    if not output_path.is_file():
        raise SmokeFailure("Worker 没有生成 mcp_result.json")
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeFailure("Worker 结果文件不是有效 JSON") from exc
    structured = payload.get("structuredContent") if isinstance(payload, dict) else None
    if not isinstance(structured, dict) or structured.get("echo") != "worker-smoke":
        raise SmokeFailure("Worker 结果文件没有保留 MCP 结构化结果")
    if stub.state.mcp_initialize_calls <= initialize_before:
        raise SmokeFailure("Worker 没有通过选定 MCP profile 建立连接")
    if stub.state.mcp_list_calls <= list_before or stub.state.mcp_tool_calls <= tool_before:
        raise SmokeFailure("Worker 没有完成 tools/list 与 tools/call")
    if environment.mcp_config.as_posix() not in result.stdout:
        raise SmokeFailure("Worker 输出没有确认 execution-plane MCP profile")
    return (
        {
            "exit_code": result.return_code,
            "mcp_profile_selected": True,
            "mcp_initialized": True,
            "mcp_tool_called": True,
            "result_file_valid": True,
        },
        f"{result.stdout}\n{result.stderr}".strip(),
    )


def _run_scenario(
    scenario: str,
    environment: SmokeEnvironment,
    stub: StubServices,
    timeout: float,
) -> SmokeRecord:
    """执行一个场景并把异常转换为稳定的 pass/fail 记录。"""

    functions: dict[str, Callable[[SmokeEnvironment, StubServices, float], tuple[dict[str, Any], str]]] = {
        "server-websocket-plugin": _server_websocket_plugin_scenario,
        "cli-turn": _cli_turn_scenario,
        "worker-mcp": _worker_mcp_scenario,
    }
    started = time.monotonic()
    output = ""
    try:
        assertions, output = functions[scenario](environment, stub, timeout)
        return SmokeRecord(
            scenario=scenario,
            status="passed",
            duration_seconds=time.monotonic() - started,
            assertions=assertions,
            output_tail=_sanitize_output(output, environment),
        )
    except Exception as exc:
        return SmokeRecord(
            scenario=scenario,
            status="failed",
            duration_seconds=time.monotonic() - started,
            error=_sanitize_output(
                f"{type(exc).__name__}: {str(exc)[:1800]}",
                environment,
                limit=2000,
            ),
            output_tail=_sanitize_output(output, environment),
        )


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    """通过同目录临时文件原子替换最终 Smoke JSON 报告。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析场景选择、单场景硬时限、总窗口和报告路径。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIO_IDS,
        default=[],
        help="只执行指定场景；可重复，默认执行全部。",
    )
    parser.add_argument("--scenario-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--report",
        default="build/test-reports/real-entry-smoke.json",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行所选真实入口、清理隔离运行态并写出统一机器结论。"""

    args = _parse_args(argv)
    selected = list(dict.fromkeys(args.scenario or SCENARIO_IDS))
    scenario_timeout = max(5.0, min(float(args.scenario_timeout_seconds), 180.0))
    total_timeout = max(scenario_timeout, min(float(args.total_timeout_seconds), 600.0))
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = PROJECT_ROOT / report_path

    started_at = _iso_now()
    started = time.monotonic()
    records: list[SmokeRecord] = []
    stub = StubServices()
    try:
        stub.start()
        with tempfile.TemporaryDirectory(prefix="tcode-dsh-002-smoke-") as temp_dir:
            environment = _prepare_environment(Path(temp_dir), stub)
            for scenario in selected:
                remaining = total_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    records.append(
                        SmokeRecord(
                            scenario=scenario,
                            status="timeout",
                            duration_seconds=0.0,
                            error="总运行窗口已经耗尽",
                        )
                    )
                    continue
                records.append(
                    _run_scenario(
                        scenario,
                        environment,
                        stub,
                        min(scenario_timeout, remaining),
                    )
                )
    finally:
        stub.close()

    counts = {
        "passed": sum(record.status == "passed" for record in records),
        "failed": sum(record.status == "failed" for record in records),
        "timeout": sum(record.status == "timeout" for record in records),
    }
    success = bool(records) and counts["passed"] == len(records)
    report = {
        "schema": REPORT_SCHEMA,
        "success": success,
        "started_at": started_at,
        "finished_at": _iso_now(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "selected_scenarios": selected,
        "scenario_timeout_seconds": scenario_timeout,
        "total_timeout_seconds": total_timeout,
        "counts": counts,
        "controls": {
            "loopback_only": True,
            "external_llm_used": False,
            "external_mcp_used": False,
            "production_config_used": False,
            "temporary_runtime_cleaned": True,
            "production_runtime_mutated": False,
        },
        "records": [record.to_dict() for record in records],
    }
    _write_report(report_path, report)
    print(
        "[real-entry-smoke] "
        f"success={success} passed={counts['passed']} failed={counts['failed']} "
        f"timeout={counts['timeout']} report={report_path}"
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
