#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
VENV_DIR="${TCODE_VENV_DIR:-$PROJECT_ROOT/.venv}"
INSTALL_TARGET="."
LOCK_FILE="$PROJECT_ROOT/requirements.lock"
NPM_REGISTRY="${TCODE_NPM_REGISTRY:-https://registry.npmjs.org}"

if [[ "${1:-}" == "--full" ]]; then
  INSTALL_TARGET=".[full]"
  LOCK_FILE="$PROJECT_ROOT/requirements-full.lock"
elif [[ -n "${1:-}" ]]; then
  echo "用法: $0 [--full]" >&2
  exit 2
fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "未找到 $PYTHON_BIN；请安装 Python 3.12+ 或设置 PYTHON_BIN。" >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo "未找到 npm；请安装 Node.js 22+。" >&2
  exit 1
}

cd "$PROJECT_ROOT"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
npm --prefix web --registry="$NPM_REGISTRY" ci
npm --prefix web run build
"$VENV_DIR/bin/python" - "$PROJECT_ROOT/web/dist" "$PROJECT_ROOT/tcode_agent/server/static" <<'PY'
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
if not source.is_dir():
    raise SystemExit(f"前端构建目录不存在: {source}")
shutil.rmtree(target, ignore_errors=True)
shutil.copytree(source, target)
PY
"$VENV_DIR/bin/python" -m pip install --require-hashes -r "$LOCK_FILE"
"$VENV_DIR/bin/python" -m pip install --no-deps "$INSTALL_TARGET"
"$PROJECT_ROOT/scripts/init_local_config.sh"

cat <<EOF
TCode ${TCODE_VERSION:-$(cat VERSION)} 已安装到 $VENV_DIR
请先编辑 $PROJECT_ROOT/.tcode-local/tcode.json 并设置对应环境变量。
启动 Web：PYTHON_BIN="$VENV_DIR/bin/python" $PROJECT_ROOT/scripts/start_web.sh --workdir "$PROJECT_ROOT"
启动 CLI：TCODE_CONFIG_ROOT="$PROJECT_ROOT/.tcode-local" $VENV_DIR/bin/tcode --workdir "$PROJECT_ROOT"
EOF
