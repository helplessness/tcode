"""PyInstaller entry point for the desktop backend and its owned sidecars."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


SIDECARS = {
    "job-protocol-mcp",
    "shared_tracker_bridge_worker.py",
    "shared_tracker_symphony_sidecar.py",
}


def resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    return Path(frozen_root).resolve() if frozen_root else Path(__file__).resolve().parents[1]


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--tcode-sidecar":
        name = sys.argv[2]
        if name not in SIDECARS:
            raise SystemExit(f"unsupported desktop sidecar: {name}")
        if name == "job-protocol-mcp":
            runpy.run_module("tmcp.funcdir.job_protocol_mcp", run_name="__main__")
            return
        runpy.run_path(str(resource_root() / "scripts" / name), run_name="__main__")
        return

    from tcode_agent.server.desktop_launcher import main as desktop_main

    desktop_main()


if __name__ == "__main__":
    main()
