#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${TCODE_DESKTOP_PYTHON:-$ROOT/.desktop-venv/bin/python}"
OUTPUT="$ROOT/build/release/backend"
LOCK="$ROOT/build/.desktop-backend-build.lock"

[ "$(uname -s)" = Darwin ] || { echo "macOS backend must be built on macOS" >&2; exit 1; }
[ -x "$PYTHON" ] || { echo "Desktop Python not found: $PYTHON" >&2; exit 1; }
[ -f "$ROOT/web/dist/desktop.html" ] || { echo "Build web/dist before packaging" >&2; exit 1; }
mkdir -p "$ROOT/build"
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "Another desktop backend build is already running" >&2
    exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

if ! "$PYTHON" -c 'from importlib.metadata import version; assert version("pyinstaller") == "6.22.3"; assert version("fastmcp") == "3.2.4"; assert version("typer") == "0.21.2"' >/dev/null 2>&1; then
    command -v uv >/dev/null 2>&1 || { echo "uv is required to install the pinned packager" >&2; exit 1; }
    uv pip install --python "$PYTHON" --requirement "$ROOT/packaging/requirements-macos.txt"
fi

rm -rf "$ROOT/build/pyinstaller" "$ROOT/dist/tcode-backend" "$OUTPUT"
cd "$ROOT"
"$PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --workpath "$ROOT/build/pyinstaller" \
    --distpath "$ROOT/dist" \
    "$ROOT/packaging/tcode-backend.spec"

mkdir -p "$(dirname "$OUTPUT")"
mv "$ROOT/dist/tcode-backend" "$OUTPUT"
test -x "$OUTPUT/tcode-backend"
if find "$OUTPUT" -path '*/.tcode-local/*' -o -name 'local.env' -o -name 'desktop.env' | grep -q .; then
    echo "Private runtime configuration was included in the packaged backend" >&2
    exit 1
fi
echo "Built standalone desktop backend: $OUTPUT"
