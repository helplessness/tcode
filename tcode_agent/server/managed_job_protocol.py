"""Lifecycle of the optional, single-owner local Job Protocol."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from ..file_lock import lock_file

NAME = "job-protocol-mcp"
LOCAL_URL = "TCODE_INTERNAL_JOB_PROTOCOL_URL"
LOCAL_TOKEN = "TCODE_INTERNAL_JOB_PROTOCOL_TOKEN"
WORKER_TOKEN = "TCODE_INTERNAL_JOB_PROTOCOL_WORKER_TOKEN"
WORKER_HEADER = "x-tcode-job-worker-token"
_worker_token = ""


def read_config(path):
    if not path or not Path(path).is_file():
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("MCP config must be an object")
    return value


def has_gateway_address(entry):
    return isinstance(entry, dict) and bool(str(entry.get("url") or "").strip())


def external_entry(*configs):
    for config in configs:
        entry = config.get(NAME)
        if has_gateway_address(entry):
            return entry
    url = os.environ.get("TCODE_PLUGIN_JOB_PROTOCOL_URL", "").strip().rstrip("/")
    if url:
        return {"url": url.removesuffix("/mcp") + "/mcp", "enabled": True,
                "transport": "streamable-http", "timeout": 300000}
    return {}


def bridge_config_paths(workdir, primary=None):
    from ..config.manager import ConfigManager

    return (
        os.environ.get("TCODE_BRIDGE_MCP_CONFIG"),
        primary or os.environ.get("TCODE_MCP_CONFIG"),
        Path(ConfigManager.resolve_config_root(workdir)) / "config/mcp_job_protocol_only.json",
        ConfigManager.get_config_root_file_path("mcp.json", workdir, prefer_effective=True),
    )


def configured_bridge_gateway(workdir, primary=None):
    for path in bridge_config_paths(workdir, primary):
        try:
            entry = read_config(path).get(NAME)
        except (OSError, ValueError):
            continue
        if has_gateway_address(entry):
            return str(path), entry
    return "", {}


def bridge_worker_environment():
    if not _worker_token:
        raise RuntimeError("内置 Job Protocol 未就绪")
    return {**os.environ, WORKER_TOKEN: _worker_token}


def effective_config(config):
    """Overlay only the broker entry; never rewrite the user's MCP file."""
    url = os.environ.get(LOCAL_URL, "")
    entry = config.get(NAME)
    if url:
        return {**config, NAME: {"url": url, "transport": "streamable-http",
                               "headers": {"Authorization": "Bearer ${" + LOCAL_TOKEN + "}"},
                               "connect_timeout": 10000, "timeout": 300000}}
    if not has_gateway_address(entry):
        external = os.environ.get("TCODE_INTERNAL_EXTERNAL_JOB_PROTOCOL")
        if external:
            return {**config, NAME: json.loads(external)}
    return config


def stop_child(child):
    if child is None:
        return
    if child.stdin:
        child.stdin.close()
    if child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            return
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


@contextmanager
def managed_job_protocol(runtime: Path, *, configs=()):
    global _worker_token
    root = runtime / ".tcode/job-protocol"
    old_db = root / "jobs.sqlite3"
    if old_db.exists():
        raise RuntimeError("检测到旧 TMCP 任务库；请配置原 Job Protocol 地址，避免旧任务失联")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (root / "owner.lock").open("a+b") as owner, TemporaryDirectory(prefix="tcode-job-ready-") as temp:
        lock_file(owner, blocking=False)
        ready = Path(temp) / "ready"
        token = secrets.token_urlsafe(32)
        worker_token = secrets.token_urlsafe(32)
        registry = os.environ.get("TMCP_TARGET_REGISTRY_TOKEN", "").strip() or secrets.token_urlsafe(32)
        env = {**os.environ, LOCAL_TOKEN: token, WORKER_TOKEN: worker_token, "TMCP_TARGET_REGISTRY_TOKEN": registry,
               "TCODE_JOB_PROTOCOL_STATE": str(root), "TCODE_JOB_PROTOCOL_READY": str(ready),
               "TCODE_JOB_PROTOCOL_CONFIGS": json.dumps([str(p) for p in configs if p])}
        command = ([sys.executable, "--tcode-sidecar", NAME] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "tcode_agent.server.job_protocol"])
        child = subprocess.Popen(command, stdin=subprocess.PIPE, env=env)
        prior = {}
        prior_worker = _worker_token
        try:
            deadline = time.monotonic() + 8
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.is_file() or child.poll() is not None:
                raise RuntimeError("内置 Job Protocol 未就绪；请检查服务日志和可选依赖")
            port = int(ready.read_text())
            url = f"http://127.0.0.1:{port}/mcp"
            values = {LOCAL_URL: url, LOCAL_TOKEN: token, "TMCP_TARGET_REGISTRY_TOKEN": registry,
                      "TCODE_PLUGIN_JOB_PROTOCOL_URL": url.removesuffix("/mcp")}
            prior = {key: os.environ.get(key) for key in values}
            os.environ.update(values)
            _worker_token = worker_token
            yield url
        finally:
            try:
                stop_child(child)
            finally:
                _worker_token = prior_worker
                for key, value in prior.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


def loopback_web(host):
    return host in {"127.0.0.1", "::1", "localhost"}


def async_enabled(config):
    return bool(configured_async_targets(config))


def enabled_flag():
    return os.environ.get("TCODE_MANAGED_JOB_PROTOCOL", "").lower() in {"1", "true", "yes", "on"}


def configured_async_targets(config):
    from ..workspace.async_protein_run import _configured_servers
    from ..workspace.input_resources import PUBLISHED_INPUT_TARGETS
    protected = _configured_servers() | {server for server, _ in PUBLISHED_INPUT_TARGETS}
    return {name: entry for name, entry in config.items()
            if name.lower() not in protected | {NAME} and not name.startswith(("$", "plugin-"))
            and isinstance(entry, dict) and entry.get("enabled", True) is not False
            and entry.get("invocation_mode") == "async"}
