#!/usr/bin/env python3
"""TCode Agent Web Server Launcher"""

import argparse
from contextlib import ExitStack
import json
import logging
import signal
import sys
import os
from pathlib import Path

from tcode_agent.runtime.env_file import load_process_env_file


def _load_env_file(env_file: str):
    if not env_file:
        return
    try:
        load_process_env_file(env_file)
    except Exception:
        pass


def _default_env_file(workdir: str) -> str:
    configured_root = str(os.environ.get("TCODE_CONFIG_ROOT", "") or "").strip()
    if configured_root:
        return os.path.join(
            os.path.abspath(os.path.expanduser(configured_root)),
            "config",
            "symphony_route.env",
        )
    return os.path.join(workdir, "config", "symphony_route.env")

# 源码运行时指向仓库根；安装后默认从当前工作目录读取 tcode.json。
package_root = Path(__file__).resolve().parents[1]
source_root = package_root.parent

# IMPORTANT: Remove current directory from sys.path to avoid importing
# tcode_agent/types.py instead of standard library 'types' module
if "" in sys.path:
    sys.path.remove("")
if "." in sys.path:
    sys.path.remove(".")
if str(source_root) in sys.path:
    sys.path.remove(str(source_root))

# Add project root at the END to ensure standard library imports work first
sys.path.append(str(source_root))


def _configure_local_web():
    os.environ.update({
        "TCODE_ENV_FILE": os.devnull,
        "TCODE_AUTH_REQUIRED": "0",
        "TCODE_ENTERPRISE_MODE": "0",
        "TCODE_SANDBOX_ENABLED": "false",
        "TCODE_SANDBOX_REQUIRE_RUNNER": "false",
        "TCODE_ASYNC_WATCHER_ENABLED": "0",
        "TCODE_RESEARCH_ENABLED": "false",
    })


def _configure_job_protocol(stack, args, workdir):
    from ..mcp.manager_auto import MCPManager
    from ..runtime.tracker_token import ensure_tracker_token
    from .managed_job_protocol import (
        async_enabled, bridge_worker_environment, configured_bridge_gateway, enabled_flag, external_entry,
        has_gateway_address, loopback_web, managed_job_protocol, read_config, stop_child,
    )
    import subprocess

    if args.local:
        return False
    try:
        primary = MCPManager.resolve_mcp_config_path(workdir)
        config = read_config(primary)
        _, external = configured_bridge_gateway(workdir, primary)
        external = external or external_entry()
        if external:
            # Keep existing Web and worker manifests authoritative. A URL-only
            # deployment also needs a broker entry in the runtime overlay.
            if not has_gateway_address(config.get("job-protocol-mcp")):
                os.environ["TCODE_INTERNAL_EXTERNAL_JOB_PROTOCOL"] = json.dumps(external)
            return
        from ..mcp.executor_config import _resolve_candidate_path
        executor_path = os.environ.get("TCODE_EXECUTOR_MCP_CONFIG")
        executor_path = _resolve_candidate_path(executor_path, workdir=workdir) if executor_path else None
        if not (enabled_flag() or async_enabled({**read_config(executor_path), **config})):
            return
        if not loopback_web(args.host) or args.reload or os.environ.get("WEB_CONCURRENCY", "1") != "1":
            logging.warning("内置 Job Protocol 只用于单机 Web；请配置外部网关")
            return
        if (os.environ.get("TCODE_ASYNC_EXECUTION_MODE", "").lower() == "symphony"
                or os.environ.get("TCODE_PROXY_SUBMIT_MODE", "").lower() == "symphony"
                or os.environ.get("SYMPHONY_ROUTE_ON_MCP_TOOL", "").lower() == "true"):
            logging.warning("保留现有 Symphony 路由；请配置外部 Job Protocol")
            return
        # Do not inherit an unrelated source checkout's executor configuration.
        configs = [primary, executor_path]
        with ExitStack() as started:
            started.enter_context(managed_job_protocol(Path(workdir), configs=configs))
            tracker_host = "[::1]" if args.host == "::1" else "127.0.0.1"
            os.environ.update(TCODE_ASYNC_EXECUTION_MODE="bridge", TCODE_PROXY_SUBMIT_MODE="bridge",
                              TCODE_ASYNC_WATCHER_ENABLED="1", TCODE_TRACKER_API_BASE=f"http://{tracker_host}:{args.port}")
            ensure_tracker_token()
            child = subprocess.Popen([sys.executable, "-m", "tcode_agent.runtime.bridge_worker"],
                                     stdin=subprocess.PIPE, env=bridge_worker_environment())
            started.callback(stop_child, child)
            marker = Path(workdir) / ".tcode/job-protocol/web-bridge.pid"
            marker.write_text(str(os.getpid()))
            started.callback(marker.unlink, missing_ok=True)
            stack.enter_context(started.pop_all())
            return True
    except (OSError, ValueError, RuntimeError):
        logging.warning("内置 Job Protocol 无法启动；Web 仍可聊天，请检查异步配置", exc_info=True)


def main():
    parser = argparse.ArgumentParser(
        description="TCode Agent Web Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --port 8080
  %(prog)s --workdir /path/to/your/project
  %(prog)s --port 9000 --workdir ./my-project
  TCODE_WORKDIR=/path/to/project %(prog)s
        """,
    )
    parser.add_argument("--local", action="store_true", help="Loopback-only setup without deployment environment files")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to bind to (default: 8080)"
    )
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (development mode)"
    )
    parser.add_argument(
        "--workdir",
        help="Working directory for agents (default: current directory or TCODE_WORKDIR env var)",
    )

    args = parser.parse_args()
    if args.local and args.host not in {"127.0.0.1", "::1"}:
        parser.error("--local only permits a loopback host")

    # Determine working directory with priority:
    # 1. --workdir argument
    # 2. TCODE_WORKDIR environment variable
    # 3. Current project directory
    workdir = None
    if args.workdir:
        workdir = os.path.abspath(args.workdir)
        if not os.path.isdir(workdir):
            print(
                f"Error: Working directory does not exist: {workdir}", file=sys.stderr
            )
            sys.exit(1)
    elif os.environ.get("TCODE_WORKDIR"):
        workdir = os.path.abspath(os.environ["TCODE_WORKDIR"])
        if not os.path.isdir(workdir):
            print(f"Error: TCODE_WORKDIR does not exist: {workdir}", file=sys.stderr)
            sys.exit(1)
    else:
        workdir = os.getcwd()

    # Set workdir in environment for app to use
    os.environ["TCODE_WORKDIR"] = workdir
    default_config_root = source_root if (source_root / "tcode.json").is_file() else Path(workdir)
    os.environ.setdefault("TCODE_CONFIG_ROOT", str(default_config_root))

    # Load optional routing/runtime env config even when launcher is started directly.
    env_file = os.environ.get("TCODE_ENV_FILE") or _default_env_file(workdir)
    if args.local:
        _configure_local_web()
    else:
        _load_env_file(env_file)

    with ExitStack() as stack:
        managed = _configure_job_protocol(stack, args, workdir)
        # Uvicorn re-raises SIGTERM after shutting down. Let Python unwind
        # owned sidecars/locks rather than exiting before ExitStack can run.
        previous = signal.signal(signal.SIGTERM, _exit_on_sigterm) if managed else None
        try:
            _run(args, workdir)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


def _exit_on_sigterm(signum, frame):
    raise SystemExit(128 + signum)


def _run(args, workdir):
    from tcode_agent.server.app import run_server, create_static_dir

    # Create static directory
    static_dir = create_static_dir()

    print("=" * 60)
    print("TCode Agent Web Server")
    print("=" * 60)
    print(f"  Host:       {args.host}")
    print(f"  Port:       {args.port}")
    print(f"  Reload:     {args.reload}")
    print(f"  Workdir:    {workdir}")
    print()
    print(f"  Web UI:     http://localhost:{args.port}")
    print(f"  API:        http://localhost:{args.port}/api")
    print(f"  API Docs:   http://localhost:{args.port}/docs")
    print("=" * 60)
    print()

    run_server(host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
