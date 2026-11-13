#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TCODE_ROOT="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$TCODE_ROOT")"
source "$TCODE_ROOT/scripts/load_tcode_env.sh"
load_tcode_env "$TCODE_ROOT" "mcp_host.env" || exit 1

PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${LOG_DIR:-$TCODE_ROOT/runs/mcp_logs}"
PID_DIR="${PID_DIR:-$TCODE_ROOT/runs/mcp_pids}"
PID_FILE="$PID_DIR/artifact-server.pid"
LOG_FILE="$LOG_DIR/artifact-server.log"
mkdir -p "$LOG_DIR" "$PID_DIR"

is_running() { kill -0 "$1" 2>/dev/null; }

remove_pid_file_if_owned() {
    local pid="$1"
    if [ -f "$PID_FILE" ] && [ "$(<"$PID_FILE")" = "$pid" ]; then
        rm -f "$PID_FILE"
    fi
}

prepare_start() {
    if [ -f "$PID_FILE" ] && is_running "$(<"$PID_FILE")"; then
        echo "[skip] artifact-server already running (pid=$(<"$PID_FILE"), port=${TMCP_ARTIFACT_PORT:-10120})"
        return 1
    fi
    rm -f "$PID_FILE"
    if ! PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" "$PYTHON_BIN" -c "import fastapi, uvicorn, multipart" >/dev/null 2>&1; then
        echo "[error] artifact-server requires fastapi, uvicorn and python-multipart: $PYTHON_BIN" >&2
        exit 1
    fi
}

start_foreground() {
    if ! prepare_start; then return; fi
    echo "[start] artifact-server in foreground (port=${TMCP_ARTIFACT_PORT:-10120}, python=$PYTHON_BIN)"
    echo "  -> press Ctrl+C to stop"

    local child_pid
    local exit_code
    PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" "$PYTHON_BIN" -m tmcp.artifact_server &
    child_pid=$!
    echo "$child_pid" >"$PID_FILE"

    foreground_signal() {
        local exit_code="$1"
        trap - INT TERM HUP
        if is_running "$child_pid"; then
            kill -TERM "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
        fi
        remove_pid_file_if_owned "$child_pid"
        echo "[stop] artifact-server"
        exit "$exit_code"
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
    echo "[start] artifact-server in background (port=${TMCP_ARTIFACT_PORT:-10120}, python=$PYTHON_BIN)"
    PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" nohup "$PYTHON_BIN" -m tmcp.artifact_server >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    local pid
    pid="$(<"$PID_FILE")"
    local ready=0
    for _ in $(seq 1 30); do
        if ! is_running "$pid"; then break; fi
        if "$PYTHON_BIN" -c "import os, urllib.request; token=os.environ.get('TMCP_ARTIFACT_API_TOKEN') or os.environ.get('TMCP_SERVICE_API_TOKEN',''); request=urllib.request.Request('http://127.0.0.1:${TMCP_ARTIFACT_PORT:-10120}/health', headers={'X-Artifact-Token': token} if token else {}); urllib.request.urlopen(request, timeout=1).read()" >/dev/null 2>&1; then
            ready=1; break
        fi
        sleep 0.5
    done
    if [ "$ready" -eq 1 ]; then
        echo "  -> started in background pid=$pid log=$LOG_FILE"
        return
    fi
    remove_pid_file_if_owned "$pid"
    echo "  -> failed to become ready, check log: $LOG_FILE" >&2
    exit 1
}

stop_server() {
    if [ ! -f "$PID_FILE" ]; then echo "[skip] artifact-server not running"; return; fi
    local pid
    pid="$(<"$PID_FILE")"
    if is_running "$pid"; then kill "$pid"; sleep 1; fi
    if is_running "$pid"; then kill -9 "$pid"; fi
    rm -f "$PID_FILE"
    echo "[stop] artifact-server"
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
            echo "[up] artifact-server pid=$(<"$PID_FILE") port=${TMCP_ARTIFACT_PORT:-10120} log=$LOG_FILE"
        else
            echo "[down] artifact-server port=${TMCP_ARTIFACT_PORT:-10120} log=$LOG_FILE"
        fi ;;
    *) echo "usage: $0 {start|restart} [-d|--detach] | {stop|status}" >&2; exit 1 ;;
esac
