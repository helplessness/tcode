#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# TCODE_SANDBOX_RUNNER_ENV_FILE 仍可兼容使用，但新部署统一使用 TCODE_ENV_FILE。
# shellcheck source=scripts/load_tcode_env.sh
source "$PROJECT_DIR/scripts/load_tcode_env.sh"
load_tcode_env "$PROJECT_DIR" || exit 1

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${TCODE_SANDBOX_RUNNER_HOST:-127.0.0.1}"
PORT="${TCODE_SANDBOX_RUNNER_PORT:-19091}"
SERVICE_LOG_DIR="${TCODE_SANDBOX_RUNNER_SERVICE_LOG_DIR:-$PROJECT_DIR/runs/sandbox_runner}"
SERVICE_PID_DIR="${TCODE_SANDBOX_RUNNER_SERVICE_PID_DIR:-$PROJECT_DIR/runs/sandbox_runner}"
PID_FILE="$SERVICE_PID_DIR/sandbox-runner.pid"
LOG_FILE="$SERVICE_LOG_DIR/sandbox-runner.log"
mkdir -p "$SERVICE_LOG_DIR" "$SERVICE_PID_DIR"

prepare_local_runner_token() {
    local require_token auto_create token_file token_dir temp_file
    require_token="$(printf '%s' "${TCODE_SANDBOX_RUNNER_REQUIRE_TOKEN:-true}" | tr '[:upper:]' '[:lower:]')"
    auto_create="$(printf '%s' "${TCODE_SANDBOX_AUTO_CREATE_TOKEN:-false}" | tr '[:upper:]' '[:lower:]')"
    token_file="${TCODE_SANDBOX_RUNNER_TOKEN_FILE:-}"

    case "$require_token" in
        1|true|yes|on) ;;
        *) return 0 ;;
    esac
    if [ -n "${TCODE_SANDBOX_RUNNER_TOKEN:-}" ]; then
        return 0
    fi
    case "$auto_create" in
        1|true|yes|on) ;;
        *) return 0 ;;
    esac
    if [ -z "$token_file" ]; then
        echo "TCODE_SANDBOX_RUNNER_TOKEN_FILE is required for local token creation." >&2
        exit 1
    fi
    if [ -s "$token_file" ]; then
        chmod 600 "$token_file"
        return 0
    fi

    token_dir="$(dirname "$token_file")"
    mkdir -p "$token_dir"
    chmod 700 "$token_dir"
    temp_file="${token_file}.tmp.$$"
    # Limit the token in a subshell.  Changing the parent shell's umask here
    # would leak into uvicorn and make host/container job-control paths too
    # restrictive for the fixed container uid.
    (umask 077; "$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))' > "$temp_file")
    chmod 600 "$temp_file"
    mv "$temp_file" "$token_file"
    echo "Created local sandbox runner token: $token_file"
}

is_running() { kill -0 "$1" 2>/dev/null; }

remove_pid_file_if_owned() {
    local pid="$1"
    if [ -f "$PID_FILE" ] && [ "$(<"$PID_FILE")" = "$pid" ]; then
        rm -f "$PID_FILE"
    fi
}

prepare_start() {
    if [ -f "$PID_FILE" ] && is_running "$(<"$PID_FILE")"; then
        echo "[skip] sandbox-runner already running (pid=$(<"$PID_FILE"), port=$PORT)"
        return 1
    fi
    rm -f "$PID_FILE"
    if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "::1" ]; then
        echo "Refusing to expose sandbox runner on non-loopback host: $HOST" >&2
        exit 1
    fi
    if ! "$PYTHON_BIN" -c "import uvicorn, tcode_agent.runtime.sandbox.runner_app" >/dev/null 2>&1; then
        echo "[error] sandbox-runner requires uvicorn and tcode runtime: $PYTHON_BIN" >&2
        exit 1
    fi
    prepare_local_runner_token
}

start_foreground() {
    if ! prepare_start; then return; fi
    echo "[start] sandbox-runner in foreground (host=$HOST, port=$PORT, python=$PYTHON_BIN)"
    echo "  -> press Ctrl+C to stop"

    local child_pid
    local exit_code
    "$PYTHON_BIN" -m uvicorn tcode_agent.runtime.sandbox.runner_app:create_runner_app \
        --factory \
        --host "$HOST" \
        --port "$PORT" \
        --workers 1 &
    child_pid=$!
    echo "$child_pid" >"$PID_FILE"

    foreground_signal() {
        local signal_exit_code="$1"
        trap - INT TERM HUP
        if is_running "$child_pid"; then
            kill -TERM "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
        fi
        remove_pid_file_if_owned "$child_pid"
        echo "[stop] sandbox-runner"
        exit "$signal_exit_code"
    }
    trap 'foreground_signal 130' INT
    trap 'foreground_signal 143' TERM
    trap 'foreground_signal 129' HUP

    if wait "$child_pid"; then
        exit_code=0
    else
        exit_code=$?
    fi
    trap - INT TERM HUP
    remove_pid_file_if_owned "$child_pid"
    return "$exit_code"
}

start_detached() {
    if ! prepare_start; then return; fi
    echo "[start] sandbox-runner in background (host=$HOST, port=$PORT, python=$PYTHON_BIN)"
    nohup "$PYTHON_BIN" -m uvicorn tcode_agent.runtime.sandbox.runner_app:create_runner_app \
        --factory \
        --host "$HOST" \
        --port "$PORT" \
        --workers 1 >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"

    local pid
    local ready=0
    pid="$(<"$PID_FILE")"
    for _ in $(seq 1 30); do
        if ! is_running "$pid"; then break; fi
        if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=1).read()" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 0.5
    done
    if [ "$ready" -eq 1 ]; then
        echo "  -> started in background pid=$pid log=$LOG_FILE"
        return
    fi
    if is_running "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
        sleep 1
    fi
    if is_running "$pid"; then kill -9 "$pid" 2>/dev/null || true; fi
    remove_pid_file_if_owned "$pid"
    echo "  -> failed to become ready, check log: $LOG_FILE" >&2
    exit 1
}

stop_server() {
    if [ ! -f "$PID_FILE" ]; then
        echo "[skip] sandbox-runner not running"
        return
    fi
    local pid
    pid="$(<"$PID_FILE")"
    if is_running "$pid"; then kill "$pid"; sleep 1; fi
    if is_running "$pid"; then kill -9 "$pid"; fi
    remove_pid_file_if_owned "$pid"
    echo "[stop] sandbox-runner"
}

ACTION="${1:-start}"
MODE="${2:-}"
if [ "$ACTION" = "-d" ] || [ "$ACTION" = "--detach" ]; then
    ACTION="start"
    MODE="-d"
fi
if [ -n "${3:-}" ]; then
    echo "usage: $0 {start|restart} [-d|--detach] | {stop|status}" >&2
    exit 1
fi

case "$MODE" in
    "") DETACHED=0 ;;
    -d|--detach) DETACHED=1 ;;
    *) echo "usage: $0 {start|restart} [-d|--detach] | {stop|status}" >&2; exit 1 ;;
esac

case "$ACTION" in
    start)
        if [ "$DETACHED" -eq 1 ]; then start_detached; else start_foreground; fi ;;
    stop) stop_server ;;
    restart)
        stop_server
        if [ "$DETACHED" -eq 1 ]; then start_detached; else start_foreground; fi ;;
    status)
        if [ "$DETACHED" -eq 1 ]; then
            echo "usage: $0 {start|restart} [-d|--detach] | {stop|status}" >&2
            exit 1
        fi
        if [ -f "$PID_FILE" ] && is_running "$(<"$PID_FILE")"; then
            echo "[up] sandbox-runner pid=$(<"$PID_FILE") port=$PORT log=$LOG_FILE"
        else
            echo "[down] sandbox-runner port=$PORT log=$LOG_FILE"
        fi ;;
    *) echo "usage: $0 {start|restart} [-d|--detach] | {stop|status}" >&2; exit 1 ;;
esac
