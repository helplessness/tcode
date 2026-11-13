"""Desktop-only host: bound socket, private readiness channel and loopback gate.

The normal Web launcher is deliberately unchanged. Run via desktop/backend.sh;
desktop service routing is isolated before importing the application.
"""
from __future__ import annotations

import asyncio
import hmac
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
from urllib.parse import urlsplit

GATE_COOKIE = "tcode_desktop_gate"


def desktop_resource_root() -> Path:
    """Read-only application resources in packaged and source launches."""

    explicit = str(os.environ.get("TCODE_DESKTOP_RESOURCE_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[2]


def bind_socket(runtime: Path) -> socket.socket:
    """Reuse an available prior origin so Chromium keeps drafts and layout."""
    state = runtime / ".tcode/desktop-port.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    try:
        preferred = json.loads(state.read_text()).get("port", 0)
        if type(preferred) is not int or not 1024 <= preferred <= 65535:
            preferred = 0
    except (OSError, ValueError, AttributeError):
        preferred = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", preferred))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    temp = state.with_suffix(".tmp")
    temp.write_text(json.dumps({"port": sock.getsockname()[1]}))
    temp.replace(state)
    return sock


class DesktopGate:
    """An additional desktop transport boundary, not a replacement for auth."""

    def __init__(self, app, *, token: str, port: int, tracker_token: str = ""):
        self.app = app
        self.token = token
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"
        self.tracker_token = tracker_token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            pass
        supplied = cookie.get(GATE_COOKIE)
        valid_cookie = bool(supplied and hmac.compare_digest(supplied.value, self.token))
        # Existing sidecars use their existing tracker credential. Restrict this
        # exception to tracker endpoints; their owner validation still runs.
        valid_worker = bool(
            scope.get("path", "").startswith("/api/tracker/")
            and self.tracker_token
            and hmac.compare_digest(headers.get("x-tracker-token", ""), self.tracker_token)
        )
        valid_origin = headers.get("origin") in {None, self.origin}
        if headers.get("host") != self.host or not valid_origin or not (valid_cookie or valid_worker):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"Desktop transport authentication required"}'})
            return
        return await self.app(scope, receive, send)


def configure_environment(project: Path, runtime: Path, port: int) -> None:
    os.environ["TCODE_DESKTOP_MANAGED"] = "1"
    # A packaged desktop app is a single-user local process protected by the
    # per-launch loopback gate.  It must not depend on a developer bootstrap
    # password that cannot exist on a clean installation.
    if getattr(sys, "frozen", False):
        os.environ.setdefault("TCODE_AUTH_REQUIRED", "0")
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ["TCODE_WORKDIR"] = str(runtime)
    # Private Web configuration can contain an absolute workspace root. Do not
    # accidentally attach a second scheduler to the Web user's existing jobs.
    os.environ["TCODE_USER_WORKDIRS_ROOT"] = str(runtime / "workdirs")
    os.environ["TCODE_PROCESS_REGISTRY_FILE"] = str(runtime / ".tcode/processes.json")
    config_root = runtime / "config" if getattr(sys, "frozen", False) or os.environ.get("TCODE_DESKTOP_TEST_INSTANCE") == "1" else project / ".tcode-local"
    config_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TCODE_CONFIG_ROOT", str(config_root))
    os.environ.setdefault("TCODE_PROJECT_ROOT", str(project))
    # These URLs belong to this launch, not a separately running Web server.
    os.environ["TCODE_TRACKER_API_BASE"] = f"http://127.0.0.1:{port}"
    if os.environ.get("HUMAN_REVIEW_ENABLED", "").lower() == "true" or os.environ.get("TCODE_HUMAN_REVIEW_API_BASE"):
        os.environ["TCODE_HUMAN_REVIEW_API_BASE"] = f"http://127.0.0.1:{port}"
    os.environ["TCODE_TRACKER_API_TOKEN_FILE"] = str(runtime / ".tcode/tracker-api.token")


def _available_port(start: int = 10121) -> int:
    for port in range(start, min(start + 100, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("Job Protocol 没有可用的本地端口")


def start_managed_job_protocol(project: Path, runtime: Path) -> tuple[subprocess.Popen, str]:
    # Both local processes share a management credential, never exposed to LLMs.
    import secrets
    os.environ.setdefault('TMCP_TARGET_REGISTRY_TOKEN', secrets.token_urlsafe(32))
    port = _available_port(int(os.environ.get("TCODE_DESKTOP_JOB_PROTOCOL_PORT", "10121") or 10121))
    environment = dict(os.environ)
    environment.update(
        TMCP_JOB_PROTOCOL_HOST="127.0.0.1",
        TMCP_JOB_PROTOCOL_PORT=str(port),
        TMCP_JOB_DATA_ROOT=str(runtime / ".tcode/job-protocol"),
    )
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--tcode-sidecar", "job-protocol-mcp"]
    else:
        command = [sys.executable, "-m", "tmcp.funcdir.job_protocol_mcp"]
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (str(project.parent), environment.get("PYTHONPATH", ""))))
    worker = subprocess.Popen(command, cwd=runtime, env=environment)
    health_url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            raise RuntimeError("随桌面端启动的 Job Protocol 未能启动")
        try:
            with urlopen(health_url, timeout=1) as response:
                if 200 <= response.status < 300:
                    return worker, f"http://127.0.0.1:{port}/mcp"
        except Exception:
            time.sleep(0.2)
    worker.terminate()
    raise RuntimeError("随桌面端启动的 Job Protocol 在 30 秒内未就绪")


def start_optional_job_protocol(project: Path, runtime: Path) -> tuple[subprocess.Popen | None, str]:
    """Start the compute facade without making the desktop UI depend on it."""
    from .desktop_config import desktop_mcp_config_path, remote_services_paused
    if remote_services_paused():
        return None, ''
    try:
        path = desktop_mcp_config_path(project)
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        desktop = config.get("$desktop", {})
        job = config.get("job-protocol-mcp", {})
        managed = desktop.get("managed_job_protocol")
        url = str(job.get("url") or "").strip()
        legacy = managed is None and urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
        opted_in = os.environ.get("TCODE_DESKTOP_MANAGED_JOB_PROTOCOL") == "1" and not url
        if not (managed is True or legacy or opted_in):
            return None, ""
    except (OSError, ValueError, AttributeError):
        return None, ""
    try:
        return start_managed_job_protocol(project, runtime)
    except Exception as exc:
        print(f"Job Protocol 不可用，桌面端将以降级模式继续启动：{exc}", file=sys.stderr)
        return None, ""


def sidecar_scripts(environment) -> list[str]:
    if environment.get("TCODE_DESKTOP_MANAGED") == "1" and environment.get("TCODE_DESKTOP_REMOTE_STATE") != "configured":
        return []
    modes = {environment.get("TCODE_PROXY_SUBMIT_MODE"), environment.get("TCODE_ASYNC_EXECUTION_MODE")}
    scripts = []
    if "symphony" in modes or environment.get("SYMPHONY_ROUTE_ON_MCP_TOOL", "").lower() == "true":
        scripts.append("shared_tracker_symphony_sidecar.py")
    if "bridge" in modes:
        scripts.append("shared_tracker_bridge_worker.py")
    return scripts


async def stop_local_processes():
    from ..runtime.sandbox.macos_native import stop_native_jobs
    await stop_native_jobs()
    from ..tools.builtin.process_runtime import ProcessRuntime
    from ..runtime.sandbox.base import TERMINAL_STATUSES

    # Only live local process handles owned by this interpreter. Persisted PIDs
    # and remote sandbox jobs must not be interpreted as local children.
    for managed in list(ProcessRuntime._processes.values()):
        if managed.proc is None or managed.sandbox_job_id or managed.status in TERMINAL_STATUSES:
            continue
        try:
            if not await ProcessRuntime.terminate(managed.process_id):
                print("本地托管进程退出未确认", file=sys.stderr)
        except (OSError, RuntimeError):
            print("本地托管进程清理失败", file=sys.stderr)


async def serve(token: str, project: Path, runtime: Path, computer: dict | None = None, browser: dict | None = None) -> None:
    import uvicorn

    # Keep the socket bound throughout startup; no check-then-bind race.
    sock = bind_socket(runtime)
    port = sock.getsockname()[1]
    configure_environment(project, runtime, port)
    from ..runtime.computer import configure_computer_broker
    configure_computer_broker(computer)
    from ..browser.desktop import configure_desktop_browser
    configure_desktop_browser(browser)
    os.environ["TCODE_COMPUTER_USE_ENABLED"] = "1" if computer and computer.get("enabled") is True else "0"
    job_worker, job_url = start_optional_job_protocol(project, runtime)
    if job_url:
        os.environ['TCODE_PLUGIN_JOB_PROTOCOL_URL'] = job_url.removesuffix('/mcp')
    from .desktop_blender import start_optional_blender, stop_blender
    blender_worker = start_optional_blender(project, runtime)
    from .desktop_config import prepare_services
    prepare_services(project, runtime, managed_job_url=job_url)
    from ..runtime.tracker_token import ensure_tracker_token
    tracker_token = ensure_tracker_token()
    from .app import app, uvicorn_log_config
    from .desktop_api import desktop_router
    app.include_router(desktop_router(token))

    gate = DesktopGate(app, token=token, port=port, tracker_token=tracker_token)
    server = uvicorn.Server(uvicorn.Config(
        gate, host="127.0.0.1", port=port, log_config=uvicorn_log_config(),
        timeout_graceful_shutdown=5,
    ))
    workers: list[subprocess.Popen] = [job_worker] if job_worker is not None else []

    def parent_watch():
        # Electron keeps this pipe open. EOF means its process went away.
        sys.stdin.read()
        server.should_exit = True

    threading.Thread(target=parent_watch, daemon=True).start()

    async def announce_ready():
        while not server.started and not server.should_exit:
            await asyncio.sleep(0.05)
        if not server.started:
            return
        for name in sidecar_scripts(os.environ):
            command = (
                [sys.executable, "--tcode-sidecar", name]
                if getattr(sys, "frozen", False)
                else [sys.executable, str(project / "scripts" / name)]
            )
            workers.append(subprocess.Popen(command, cwd=runtime))
        with os.fdopen(3, "w", closefd=False) as ready:
            ready.write(json.dumps({"type": "ready", "protocol": 1, "port": port}) + "\n")
            ready.flush()

    announce = asyncio.create_task(announce_ready())
    try:
        await server.serve(sockets=[sock])
    finally:
        announce.cancel()
        await asyncio.gather(announce, return_exceptions=True)
        await stop_local_processes()
        await asyncio.to_thread(stop_blender, blender_worker)
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
        for worker in workers:
            try:
                await asyncio.to_thread(worker.wait, timeout=2)
            except subprocess.TimeoutExpired:
                worker.kill()
                await asyncio.to_thread(worker.wait)
        sock.close()


def main():
    if sys.version_info < (3, 12):
        raise RuntimeError("TCode Desktop requires Python 3.12+")
    payload = json.loads(sys.stdin.readline())
    token = payload.get("token", "")
    if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise RuntimeError("Missing desktop startup credential")
    project = desktop_resource_root()
    runtime = Path(os.environ["TCODE_DESKTOP_RUNTIME"]).expanduser().resolve()
    computer = payload.get("computer")
    if computer is not None and not isinstance(computer, dict):
        raise RuntimeError("Invalid computer broker startup payload")
    browser = payload.get("browser")
    if browser is not None and not isinstance(browser, dict):
        raise RuntimeError("Invalid desktop browser startup payload")
    asyncio.run(serve(token, project, runtime, computer, browser))


if __name__ == "__main__":
    main()
