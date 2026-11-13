#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
# Keep model/login configuration reusable; desktop service routing is isolated
# by the Python launcher before importing the backend.
if [ "${TCODE_DESKTOP_TEST_INSTANCE:-}" != "1" ]; then
    source "$PROJECT_DIR/scripts/load_tcode_env.sh"
    if [ -n "${TCODE_DESKTOP_ENV_FILE:-}" ]; then
        export TCODE_ENV_FILE="$TCODE_DESKTOP_ENV_FILE"
    fi
    load_tcode_env "$PROJECT_DIR" "desktop.env"
fi
exec "$TCODE_DESKTOP_PYTHON" -m tcode_agent.server.desktop_launcher
