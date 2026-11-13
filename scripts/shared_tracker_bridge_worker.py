#!/usr/bin/env python3
"""Compatibility entry point for existing Bridge deployments."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tcode_agent.runtime import bridge_worker

if __name__ == "__main__":
    bridge_worker.run()
else:
    sys.modules[__name__] = bridge_worker
