#!/bin/bash
# Development entry point. Core runtime is separate from any existing .venv.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
COMPUTER_USE_ENABLED=0
MANAGED_E2E=0
DESKTOP_ARGS=()
for argument in "$@"; do
    if [ "$argument" = "--enable-computer-use" ]; then
        COMPUTER_USE_ENABLED=1
    elif [ "$argument" = "--managed-e2e" ]; then
        MANAGED_E2E=1
    else
        DESKTOP_ARGS+=("$argument")
    fi
done
export TCODE_COMPUTER_USE_ENABLED="$COMPUTER_USE_ENABLED"
if [ "$COMPUTER_USE_ENABLED" = 1 ]; then
    echo "Computer Use 已为本次开发会话显式启用；在 TCode 批准当前任务范围，超出范围的动作仍需确认。"
fi
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ "$(uname -s)" != Darwin ]; then
    echo "当前桌面开发版仅验证 macOS。" >&2
    exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
    echo "未找到 Node.js/npm，请先安装 Node.js 后重试。" >&2
    exit 1
fi
node -e 'const [major,minor]=process.versions.node.split(".").map(Number); if(major<22||(major===22&&minor<12)){console.error("需要 Node.js 22.12 或更高版本");process.exit(1)}'
if [ -z "${TCODE_DESKTOP_PYTHON:-}" ]; then
    export TCODE_DESKTOP_PYTHON="$PROJECT_DIR/.desktop-venv/bin/python"
    if [ ! -x "$TCODE_DESKTOP_PYTHON" ]; then
        echo "正在创建桌面专用 Python 环境……"
        python3 -c 'import sys; assert sys.version_info >= (3,12), "需要 Python 3.12 或更高版本"'
        python3 -m venv "$PROJECT_DIR/.desktop-venv"
    fi
    # A failed first download leaves a venv behind; repair it on the next run.
    if ! "$TCODE_DESKTOP_PYTHON" -c 'import tcode_agent, fastapi, uvicorn, anthropic, rdflib, pypdf, fastmcp, typer' >/dev/null 2>&1; then
        if command -v uv >/dev/null 2>&1; then
            uv pip install --python "$TCODE_DESKTOP_PYTHON" -e "$PROJECT_DIR[desktop]"
        else
            "$TCODE_DESKTOP_PYTHON" -m ensurepip --upgrade
            "$TCODE_DESKTOP_PYTHON" -m pip install -e "$PROJECT_DIR[desktop]"
        fi
    fi
fi
"$TCODE_DESKTOP_PYTHON" -c 'import sys, fastapi, uvicorn, anthropic, rdflib, pypdf, fastmcp, typer; assert sys.version_info >= (3,12)'
if [ ! -f "$PROJECT_DIR/desktop/node_modules/electron/package.json" ]; then
    npm --prefix desktop ci
fi
# Must match the installed binary checksum in prepare-computer-driver.sh.
EXPECTED_DRIVER_SHA256="512f488f631469decfebecc6e5f54e74a6f10e78f9644f89f1eb43ce7e8426ba"
DRIVER_PATH="$PROJECT_DIR/desktop/vendor/cua-driver"
if [ ! -x "$DRIVER_PATH" ] || [ "$(shasum -a 256 "$DRIVER_PATH" 2>/dev/null | awk '{print $1}')" != "$EXPECTED_DRIVER_SHA256" ]; then
    npm --prefix desktop run prepare:computer
fi
if [ ! -d "$PROJECT_DIR/web/node_modules" ]; then
    npm --prefix web ci
fi
if [ ! -f web/dist/desktop.html ] || [ -n "$(find web/src web/scripts web/public web/index.html web/desktop.html web/molstar.html web/package.json web/package-lock.json web/tsconfig.json web/vite.config.ts -newer web/dist/desktop.html -print -quit 2>/dev/null)" ]; then
    npm --prefix web run build
fi
if [ "$MANAGED_E2E" = 1 ]; then
    exec node desktop/scripts/managed-e2e.cjs
fi
# Desktop loads its own service configuration and an isolated runtime root.
# No existing Web process is reused or terminated.
if [ "${#DESKTOP_ARGS[@]}" -gt 0 ]; then
    exec npm --prefix desktop run dev -- "${DESKTOP_ARGS[@]}"
fi
exec npm --prefix desktop run dev
