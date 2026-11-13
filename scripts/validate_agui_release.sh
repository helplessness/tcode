#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python}"

"$PYTHON_BIN" scripts/generate_web_protocol.py --check
if [[ -f tests/test_protocol_v2.py ]]; then
  "$PYTHON_BIN" -m pytest -q \
    tests/test_protocol_v2.py \
    tests/test_agui_gateway.py \
    tests/test_external_harness.py \
    tests/test_harness_sdk.py
else
  echo "未安装本机 Python 回归套件；继续执行公开构建与发布校验。"
fi

npm --prefix packages/tcode-agui-client ci
npm --prefix packages/tcode-agui-client run typecheck
npm --prefix packages/tcode-agui-client test
npm --prefix packages/tcode-agui-client run build

npm --prefix examples/agui-http-agent ci
npm --prefix examples/agui-http-agent run build
npm --prefix examples/agui-copilotkit ci
npm --prefix examples/agui-copilotkit run build

npm --prefix web ci
npm --prefix web run check:stage4
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
diff -qr web/dist tcode_agent/server/static

release_dir="$(mktemp -d)"
trap 'rm -rf "$release_dir"' EXIT
(
  cd packages/tcode-agui-client
  npm pack --pack-destination "$release_dir" --silent \
    >"$release_dir/npm-pack.txt"
)
"$PYTHON_BIN" -m build --outdir "$release_dir/python"
"$PYTHON_BIN" -m twine check "$release_dir"/python/*
npm_tarball="$(find "$release_dir" -maxdepth 1 -name '*.tgz' -print -quit)"
wheel="$(find "$release_dir/python" -maxdepth 1 -name '*.whl' -print -quit)"
sdist="$(find "$release_dir/python" -maxdepth 1 -name '*.tar.gz' -print -quit)"
"$PYTHON_BIN" scripts/verify_release_artifacts.py \
  --wheel "$wheel" \
  --sdist "$sdist" \
  --npm-tarball "$npm_tarball"

smoke_dir="$release_dir/npm-smoke"
mkdir -p "$smoke_dir"
(
  cd "$smoke_dir"
  npm init --yes >/dev/null
  npm install --ignore-scripts "$npm_tarball" >/dev/null
  node --input-type=module - <<'JS'
import { createTCodeAgent, TCodeGatewayClient } from "@tcode-ai/ag-ui-client";

if (typeof createTCodeAgent !== "function" || typeof TCodeGatewayClient !== "function") {
  throw new Error("TCode AG-UI SDK public exports are unavailable");
}
JS
)

echo "AG-UI open-source release validation passed."
