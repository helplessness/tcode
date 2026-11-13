#!/usr/bin/env bash
# 离线合同验收；不读取私有配置，不访问远端 MCP，不启动计算任务。
set -euo pipefail
cd "$(dirname "$0")/.."
SELECTION_PYTHON="${PYTHON_BIN:-python3}"
export PYTHONPATH=".:..${PYTHONPATH:+:$PYTHONPATH}"
if [[ ! -f tests/test_protein_runs_api.py || ! -f tests/test_artifact_transport.py || ! -f tests/test_async_resume.py ]]; then
  echo "此检查需要本机私有回归套件（tests/ 不随仓库分发）。" >&2
  exit 2
fi
"$SELECTION_PYTHON" -m pytest -q \
  tests/test_protein_runs_api.py tests/test_artifact_transport.py tests/test_async_resume.py \
  ../tmcp/test/test_scientific_result_selection.py ../tmcp/test/test_gnina_result_adapter.py \
  ../tmcp/test/test_gnina_mcp_app.py ../tmcp/test/test_job_result_compatibility.py
npm --prefix web test -- src/components/McpAppHost.test.ts src/components/StructuredContentBlocks.test.tsx src/utils/contentBlocks.test.ts
npm --prefix web run build
