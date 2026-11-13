#!/usr/bin/env bash
# 精简后的离线回归：报告验收、候选边界与插件声明，不调用外部服务。
set -euo pipefail
cd "$(dirname "$0")/.."
ENZYME_CHECK_PYTHON="${PYTHON_BIN:-python3}"
export PYTHONDONTWRITEBYTECODE=1

if [[ ! -f tests/test_enzyme_review_actions.py || ! -f tests/test_plugin_manifest_v2.py ]]; then
  echo "此检查需要本机私有回归套件（tests/ 不随仓库分发）。" >&2
  exit 2
fi

"$ENZYME_CHECK_PYTHON" -m pytest -p no:cacheprovider -q \
  tests/test_enzyme_review_actions.py \
  tests/test_plugin_manifest_v2.py

