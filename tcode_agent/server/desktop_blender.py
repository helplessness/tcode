"""Optional, desktop-owned Blender MCP HTTP bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time


def stop_blender(worker):
    if worker is None:
        return
    # The proxy also owns a stdio child; reap its entire process group.
    try:
        os.killpg(worker.pid, signal.SIGTERM)
        worker.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(worker.pid, signal.SIGKILL)
        worker.wait(timeout=2)
    except ProcessLookupError:
        worker.wait(timeout=2)


def start_optional_blender(project: Path, runtime: Path):
    """Never make desktop startup depend on this optional service."""
    root = Path(os.environ.get("TCODE_CONFIG_ROOT") or project / ".tcode-local")
    directory = root / "blender"
    log_path = runtime / ".tcode" / "blender" / "service.log"
    worker = None
    host, port, available = "127.0.0.1", 10131, False
    try:
        defaults = project / "config" / "blender"
        service = directory / "service.json"
        config = json.loads((service if service.is_file() else defaults / "service.json").read_text())
        host, port = config["host"], config["port"]
        if host != "127.0.0.1" or type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Blender MCP requires a loopback host and valid port")
        if config.get("enabled") is True:
            directory.mkdir(parents=True, exist_ok=True)
            for name in ("service.json", "upstream.json"):
                target = directory / name
                if not target.exists():
                    shutil.copyfile(defaults / name, target)
            # Do not adopt or terminate another service already on this port.
            with socket.socket() as probe:
                probe.bind((host, port))
            command = [part.replace("{python}", sys.executable) for part in config["command"]]
            upstream = (directory / config["upstream"]).resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with log_path.open("ab") as log:
                worker = subprocess.Popen(
                    [*command, "run", str(upstream), "--transport", "http",
                     "--host", host, "--port", str(port), "--no-banner"],
                    cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + min(10, max(0.1, float(config["startup_timeout_seconds"])))
            while time.monotonic() < deadline and worker.poll() is None:
                try:
                    with socket.create_connection((host, port), timeout=0.1):
                        available = True
                        break
                except OSError:
                    time.sleep(0.05)
            if not available:
                raise RuntimeError("Blender MCP did not start; continuing without it")
    except Exception as exc:
        stop_blender(worker)
        worker = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with log_path.open("a") as log:
                log.write(f"Optional Blender MCP skipped: {type(exc).__name__}: {exc}\n")
        except OSError:
            pass
    # Publish only a service that this desktop successfully started. A disabled
    # or failed optional bridge must not produce MCP connection errors on boot.
    try:
        path = Path(os.environ.get("TCODE_DESKTOP_MCP_CONFIG") or root / "desktop-mcp.json")
        config = json.loads(path.read_text()) if path.exists() else {}
        config["blender-mcp"] = {
            "url": f"http://{host}:{port}/mcp", "transport": "streamable-http",
            "invocation_mode": "direct", "enabled": available,
            "connect_timeout": 3000, "timeout": 180000,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temp, 0o600)
        temp.replace(path)
    except (OSError, ValueError, TypeError):
        pass
    if available:
        os.environ["TCODE_DESKTOP_BLENDER_MCP_URL"] = f"http://{host}:{port}/mcp"
    else:
        os.environ.pop("TCODE_DESKTOP_BLENDER_MCP_URL", None)
    return worker
