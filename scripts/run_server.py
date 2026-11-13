#!/usr/bin/env python3
"""
TCode Agent Web Server - Simple Launcher

Usage:
    python scripts/run_server.py [--port 8080] [--host 127.0.0.1]
"""

import sys
import os

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

if __name__ == "__main__":
    from tcode_agent.server.launcher import main

    main()
