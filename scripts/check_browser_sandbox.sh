#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
if [ -n "${TCODE_ENV_FILE:-}" ]; then
    ENV_FILE="$TCODE_ENV_FILE"
elif [ -f "$PROJECT_DIR/.tcode-local/local.env" ]; then
    ENV_FILE="$PROJECT_DIR/.tcode-local/local.env"
else
    echo "未找到本机运行配置。请先执行：$PROJECT_DIR/scripts/init_local_config.sh" >&2
    exit 1
fi
WORKSPACE="${1:-${TCODE_WORKDIR:-}}"
TEST_URL="${2:-https://example.com}"

if [ ! -f "$ENV_FILE" ]; then
    echo "Sandbox env file not found: $ENV_FILE" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [ -z "$WORKSPACE" ] || [ ! -d "$WORKSPACE" ]; then
    echo "Usage: $0 /absolute/path/to/workspace [https://allowed.example]" >&2
    exit 1
fi

docker info >/dev/null
docker image inspect "${TCODE_BROWSER_SANDBOX_IMAGE:-tcode-browser:playwright}" >/dev/null

proxy_container="${TCODE_SANDBOX_EGRESS_PROXY_CONTAINER:-tcode-egress-proxy}"
proxy_running="$(docker inspect -f '{{.State.Running}}' "$proxy_container" 2>/dev/null || true)"
if [ "$proxy_running" != "true" ]; then
    echo "Egress proxy is not running: $proxy_container" >&2
    exit 1
fi

runner_url="${TCODE_SANDBOX_RUNNER_URL:-}"
if [ -z "$runner_url" ]; then
    echo "TCODE_SANDBOX_RUNNER_URL is required." >&2
    exit 1
fi
curl --fail --silent --show-error "${runner_url%/}/health" >/dev/null

export BROWSER_SMOKE_WORKSPACE="$WORKSPACE"
export BROWSER_SMOKE_URL="$TEST_URL"
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"

"${PYTHON_BIN:-python3}" - <<'PY'
import asyncio
import os
import re
from pathlib import Path

from tcode_agent.agent.agents import BuiltinAgents
from tcode_agent.tools.base import ToolContext
from tcode_agent.tools.builtin.browser import BrowserTool


def workspace_owner(workspace: Path) -> tuple[str, str, str]:
    parts = workspace.parts
    for index in range(len(parts) - 3):
        if parts[index] == "workdirs":
            return parts[index + 1], parts[index + 2], parts[index + 3]
    raise RuntimeError("Workspace must be under workdirs/<tenant>/<user>/<workspace>.")


async def main() -> None:
    workspace = Path(os.environ["BROWSER_SMOKE_WORKSPACE"]).expanduser().resolve()
    tenant_id, user_id, workspace_id = workspace_owner(workspace)
    browser_agent = BuiltinAgents.get("browser")
    permission = dict(getattr(browser_agent, "permission", {}) or {})
    if (
        browser_agent is None
        or permission.get("*") != "deny"
        or permission.get("browser") != "allow"
        or permission.get("bash") == "allow"
        or permission.get("read") == "allow"
    ):
        raise RuntimeError("Dedicated browser agent permission policy is not active.")

    context = ToolContext(
        session_id="browser-sandbox-smoke",
        message_id="browser-sandbox-smoke",
        agent="browser",
        abort=None,
        extra={
            "workdir": workspace.as_posix(),
            "control_workdir": workspace.as_posix(),
            "tenant_id": re.sub(r"[^A-Za-z0-9._@-]+", "-", tenant_id),
            "user_id": re.sub(r"[^A-Za-z0-9._@-]+", "-", user_id),
            "workspace_id": re.sub(r"[^A-Za-z0-9._-]+", "-", workspace_id),
        },
    )
    tool = BrowserTool()
    browser_opened = False

    blocked = await tool.execute(
        {"action": "open", "url": "file:///etc/passwd", "session": "sandbox-smoke"},
        context,
    )
    if not blocked.error or not blocked.metadata.get("browser_url_blocked"):
        raise RuntimeError("Unsafe file:// URL was not blocked.")

    try:
        result = await tool.execute(
            {
                "action": "open",
                "url": os.environ["BROWSER_SMOKE_URL"],
                "session": "sandbox-smoke",
                "screenshot_mode": "always",
                "timeout": 60,
            },
            context,
        )
        if result.error:
            raise RuntimeError(result.error)
        browser_opened = True
        if not result.metadata.get("sandbox") or result.metadata.get("network") != "egress":
            raise RuntimeError(f"Unexpected browser execution metadata: {result.metadata}")
        if not result.metadata.get("browser_live") or not result.metadata.get("browser_session_id"):
            raise RuntimeError(f"Browser live metadata is missing: {result.metadata}")
        observation_path = Path(result.metadata.get("host_observation_screenshot_path") or "")
        if not observation_path.is_file():
            raise RuntimeError(f"Model observation screenshot is missing: {result.metadata}")

        observation = await tool.execute(
            {
                "action": "observe",
                "session": "sandbox-smoke",
                "screenshot_mode": "never",
            },
            context,
        )
        if observation.error:
            raise RuntimeError(observation.error)
        revision = int(observation.metadata.get("page_revision") or 0)
        match = re.search(r"^- (@e\d+) ", observation.output, re.MULTILINE)
        if revision < 2 or match is None:
            raise RuntimeError(f"Structured observation is incomplete: {observation.output}")

        stale = await tool.execute(
            {
                "action": "click",
                "ref": match.group(1),
                "page_revision": revision - 1,
                "session": "sandbox-smoke",
            },
            context,
        )
        if not stale.error or stale.metadata.get("browser_error_code") != "stale_ref":
            raise RuntimeError(f"Stale ref was not rejected safely: {stale.metadata}")
        recovery_revision = int(stale.metadata.get("page_revision") or 0)
        recovery_ref = re.search(r"^- (@e\d+) ", stale.output, re.MULTILINE)
        if recovery_revision <= revision or recovery_ref is None:
            raise RuntimeError(f"Stale ref recovery observation is incomplete: {stale.output}")

        clicked = await tool.execute(
            {
                "action": "click",
                "ref": recovery_ref.group(1),
                "page_revision": recovery_revision,
                "session": "sandbox-smoke",
                "timeout": 60,
            },
            context,
        )
        if clicked.error:
            raise RuntimeError(clicked.error)

        print("Browser sandbox smoke test passed.")
        print(result.output)
        print(f"observed_revision: {revision}")
        print(f"recovered_revision: {recovery_revision}")
        print(f"clicked_revision: {clicked.metadata.get('page_revision', '')}")
        print(f"image: {result.metadata.get('image', '')}")
        print(f"network: {result.metadata.get('network', '')}")
        print(f"container: {result.metadata.get('container_name', '')}")
    finally:
        if browser_opened:
            await tool.execute(
                {"action": "close", "session": "sandbox-smoke"},
                context,
            )


asyncio.run(main())
PY
