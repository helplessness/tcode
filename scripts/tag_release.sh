#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作树仍有未提交改动；请先提交并确认测试通过。" >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < VERSION)"
TAG="v$VERSION"
PYTHON_VERSION="$(python -c 'from tcode_agent import __version__; print(__version__)')"
if [[ "$VERSION" == *"alpha."* ]]; then
  EXPECTED_PYTHON_VERSION="${VERSION/alpha./a}"
else
  EXPECTED_PYTHON_VERSION="$VERSION"
fi
if [[ "$PYTHON_VERSION" != "$EXPECTED_PYTHON_VERSION" ]]; then
  echo "VERSION ($VERSION) 与 Python 版本 ($PYTHON_VERSION) 不一致。" >&2
  exit 1
fi
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "标签已存在: $TAG" >&2
  exit 1
fi

git tag -a "$TAG" -m "TCode $VERSION"
echo "已创建本地标签 $TAG；确认后可手动执行: git push origin $TAG"
