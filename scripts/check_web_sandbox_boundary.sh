#!/bin/bash

set -euo pipefail

require_runner="${TCODE_SANDBOX_REQUIRE_RUNNER:-false}"
require_runner="$(printf '%s' "$require_runner" | tr '[:upper:]' '[:lower:]')"
case "$require_runner" in
    1|true|yes|on)
        ;;
    *)
        exit 0
        ;;
esac

if [ -z "${TCODE_SANDBOX_RUNNER_URL:-}" ]; then
    echo "TCODE_SANDBOX_REQUIRE_RUNNER=true requires TCODE_SANDBOX_RUNNER_URL." >&2
    exit 1
fi

if [ -n "${DOCKER_HOST:-}" ]; then
    echo "Web service must not receive DOCKER_HOST in runner mode." >&2
    exit 1
fi

for socket_path in \
    /var/run/docker.sock \
    /run/docker.sock \
    "${XDG_RUNTIME_DIR:-/nonexistent}/docker.sock"
do
    if [ -S "$socket_path" ] && { [ -r "$socket_path" ] || [ -w "$socket_path" ]; }; then
        echo "Web service user can access Docker socket: $socket_path" >&2
        exit 1
    fi
done

if id -Gn 2>/dev/null | tr ' ' '\n' | grep -Fxq docker; then
    echo "Web service user must not belong to the docker group." >&2
    exit 1
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "Web service user still has working Docker access." >&2
    exit 1
fi
