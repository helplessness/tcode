#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TCODE_ROOT="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$TCODE_ROOT")"
cd "$TCODE_ROOT"

# Artifact Server 与领域 MCP 默认使用 MCP 主机配置。
# 显式 TCODE_ENV_FILE 仍可为测试或特殊部署覆盖它。
# shellcheck source=scripts/load_tcode_env.sh
source "$TCODE_ROOT/scripts/load_tcode_env.sh"
load_tcode_env "$TCODE_ROOT" "mcp_host.env" || exit 1

PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${LOG_DIR:-$TCODE_ROOT/runs/mcp_logs}"
PID_DIR="${PID_DIR:-$TCODE_ROOT/runs/mcp_pids}"
TCODE_WORKDIR="${TCODE_WORKDIR:-$TCODE_ROOT}"

# Job Protocol 与所有计算 MCP 必须共享同一个短期凭据签名密钥。配置文件
# 只保存相对路径；启动时才锚定到项目目录，便于 macOS/Linux 间迁移。
if [ -z "${TMCP_BROKER_SECRET:-}" ]; then
    BROKER_SECRET_FILE="${TMCP_BROKER_SECRET_FILE:-runs/mcp_broker_secret}"
    if [[ "$BROKER_SECRET_FILE" = /* ]]; then
        BROKER_SECRET_PATH="$BROKER_SECRET_FILE"
    else
        BROKER_SECRET_PATH="$TCODE_ROOT/$BROKER_SECRET_FILE"
    fi
    mkdir -p "$(dirname "$BROKER_SECRET_PATH")"
    if [ ! -s "$BROKER_SECRET_PATH" ]; then
        PREVIOUS_UMASK="$(umask)"
        umask 077
        "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))' >"$BROKER_SECRET_PATH"
        umask "$PREVIOUS_UMASK"
    fi
    TMCP_BROKER_SECRET="$(tr -d '\r\n' <"$BROKER_SECRET_PATH")"
fi
if [ "${#TMCP_BROKER_SECRET}" -lt 24 ]; then
    echo "[error] TMCP_BROKER_SECRET must contain at least 24 characters" >&2
    exit 1
fi
export TMCP_BROKER_SECRET

# Resolve the portable TCode workspace scope after TCODE_WORKDIR is known.
# Protein Runs live below individual workspaces, so allowing the workdirs root
# also permits their nested .tcode/runs/protein directories without opening
# unrelated host paths. Explicit deployment overrides remain authoritative.
if [ -z "${TCODE_USER_WORKDIRS_ROOT:-}" ]; then
    TCODE_USER_WORKDIRS_ROOT="$TCODE_WORKDIR/workdirs"
elif [[ "$TCODE_USER_WORKDIRS_ROOT" != /* ]]; then
    TCODE_USER_WORKDIRS_ROOT="$TCODE_WORKDIR/$TCODE_USER_WORKDIRS_ROOT"
fi
export TCODE_USER_WORKDIRS_ROOT
if [ -z "${TMCP_ALLOWED_WORKSPACE_ROOTS:-}" ]; then
    TMCP_ALLOWED_WORKSPACE_ROOTS="$TCODE_USER_WORKDIRS_ROOT"
fi
export TMCP_ALLOWED_WORKSPACE_ROOTS
if [ -z "${TMCP_ISSUER_WORKSPACE_ROOTS:-}" ]; then
    TMCP_ISSUER_WORKSPACE_ROOTS="$(
        "$PYTHON_BIN" -c 'import json, sys; print(json.dumps({"tcode": sys.argv[1]}))' \
            "$TCODE_USER_WORKDIRS_ROOT"
    )"
fi
export TMCP_ISSUER_WORKSPACE_ROOTS

mkdir -p "$LOG_DIR" "$PID_DIR"

SERVERS="ligandmpnn boltz gnina simulation placer plip rfdiffusion evolvepro mutation-scoring simple-demo paper-search paper-download community-predict pythia foldx homology"
LEGACY_PAPER_SERVERS="paper-arxiv paper-pubmed paper-crossref paper-semanticscholar paper-openalex"

module_for() {
    case "$1" in
        ligandmpnn) echo "tmcp.funcdir.lingandmpnn.mcp_server.ligandmpnn_run_mcp" ;;
        boltz) echo "tmcp.funcdir.boltz.mcp_server.boltz_run_mcp" ;;
        gnina) echo "tmcp.funcdir.gnina.mcp_server.gnina_run_mcp" ;;
        simulation) echo "tmcp.funcdir.simulation.mcp_server.simulation_run_mcp" ;;
        placer) echo "tmcp.funcdir.placer.mcp_server.placer_run_mcp" ;;
        plip) echo "tmcp.funcdir.plip_func.mcp_server.plip_run_mcp" ;;
        rfdiffusion) echo "tmcp.funcdir.rfdiffusion.mcp_server.rfdiffusion_run_mcp" ;;
        evolvepro) echo "tmcp.funcdir.evolvepro.mcp_server.evolvepro_run_mcp" ;;
        mutation-scoring) echo "tmcp.funcdir.mutation_scoring.mcp_server.mutation_scoring_mcp" ;;
        simple-demo) echo "tmcp.funcdir.simple_demo_mcp" ;;
        paper-search) echo "tmcp.funcdir.paper.search.search_mcp" ;;
        paper-download) echo "tmcp.funcdir.paper.download.download_mcp" ;;
        community-predict) echo "community-predict" ;;
        pythia) echo "tmcp.funcdir.pythia.mcp_server.pythia_run_mcp" ;;
        foldx) echo "tmcp.funcdir.foldx.mcp_server.foldx_run_mcp" ;;
        homology) echo "tmcp.funcdir.homology.mcp_server.homology_run_mcp" ;;
        *)
            echo "unknown server: $1" >&2
            return 1
            ;;
    esac
}

port_for() {
    case "$1" in
        ligandmpnn) echo "10096" ;;
        boltz) echo "10097" ;;
        gnina) echo "10098" ;;
        simulation) echo "10099" ;;
        placer) echo "10100" ;;
        plip) echo "10101" ;;
        rfdiffusion) echo "10102" ;;
        evolvepro) echo "10106" ;;
        mutation-scoring) echo "10107" ;;
        simple-demo) echo "10103" ;;
        paper-search) echo "10104" ;;
        paper-download) echo "10105" ;;
        community-predict) echo "${EXAMPLE_PORT:-10230}" ;;
        pythia) echo "10108" ;;
        foldx) echo "10109" ;;
        homology) echo "10110" ;;
        *)
            echo "-" ;;
    esac
}

is_running() {
    local pid="$1"
    kill -0 "$pid" 2>/dev/null
}

start_server() {
    local name="$1"
    local module
    module="$(module_for "$name")"
    local python_bin="$PYTHON_BIN"
    local command=("$python_bin" -m "$module")
    if [ "$name" = "community-predict" ]; then
        command=("$python_bin" "$TCODE_ROOT/examples/third_party_mcp/remote_server.py")
        export EXAMPLE_HOST="${EXAMPLE_HOST:-0.0.0.0}"
        export EXAMPLE_PORT="${EXAMPLE_PORT:-10230}"
        export EXAMPLE_DATA_ROOT="${EXAMPLE_DATA_ROOT:-$TCODE_ROOT/runs/community-predict}"
    fi
    local pid_file="$PID_DIR/$name.pid"
    local log_file="$LOG_DIR/$name.log"

    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file")"
        if is_running "$pid"; then
            echo "[skip] $name already running (pid=$pid, port=$(port_for "$name"))"
            return 0
        fi
        rm -f "$pid_file"
    fi

    if ! PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" "$python_bin" -c "import fastmcp" >/dev/null 2>&1; then
        echo "[error] fastmcp not found for $name in interpreter: $python_bin" >&2
        return 1
    fi
    if [ "$name" = "mutation-scoring" ] && ! "$python_bin" -c "import pandas, pydantic, torch, transformers" >/dev/null 2>&1; then
        echo "[error] mutation-scoring requires pandas, pydantic, torch and transformers: $python_bin" >&2
        return 1
    fi

    echo "[start] $name (module=$module, port=$(port_for "$name"), python=$python_bin)"
    PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" TCODE_WORKDIR="$TCODE_WORKDIR" nohup "${command[@]}" >"$log_file" 2>&1 &
    local pid=$!
    echo "$pid" >"$pid_file"
    sleep 1

    if is_running "$pid"; then
        echo "  -> started pid=$pid log=$log_file"
    else
        echo "  -> failed to start, check log: $log_file"
        return 1
    fi
}

stop_server() {
    local name="$1"
    local pid_file="$PID_DIR/$name.pid"

    if [ ! -f "$pid_file" ]; then
        echo "[skip] $name not running (no pid file)"
        return 0
    fi

    local pid
    pid="$(cat "$pid_file")"
    if is_running "$pid"; then
        echo "[stop] $name (pid=$pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if is_running "$pid"; then
            echo "  -> force kill $pid"
            kill -9 "$pid" 2>/dev/null || true
        fi
    else
        echo "[skip] $name stale pid file (pid=$pid)"
    fi
    rm -f "$pid_file"
}

stop_legacy_paper_servers() {
    local name
    for name in $LEGACY_PAPER_SERVERS; do
        local pid_file="$PID_DIR/$name.pid"
        if [ ! -f "$pid_file" ]; then
            continue
        fi
        local pid
        pid="$(cat "$pid_file")"
        if is_running "$pid"; then
            echo "[stop legacy] $name (pid=$pid)"
            kill "$pid" 2>/dev/null || true
            sleep 1
            if is_running "$pid"; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pid_file"
    done
}

status_server() {
    local name="$1"
    local pid_file="$PID_DIR/$name.pid"
    local log_file="$LOG_DIR/$name.log"
    local port
    port="$(port_for "$name")"

    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file")"
        if is_running "$pid"; then
            echo "[up]   $name pid=$pid port=$port log=$log_file"
            return 0
        fi
    fi
    echo "[down] $name port=$port log=$log_file"
}

resolve_targets() {
    local target="$1"
    if [ "$target" = "all" ]; then
        echo "$SERVERS"
        return 0
    fi

    for s in $SERVERS; do
        if [ "$s" = "$target" ]; then
            echo "$target"
            return 0
        fi
    done

    echo "unknown target: $target" >&2
    exit 1
}

ACTION="${1:-start}"
TARGET="${2:-all}"
TARGETS="$(resolve_targets "$TARGET")"

case "$ACTION" in
    start)
        stop_legacy_paper_servers
        for s in $TARGETS; do
            start_server "$s"
        done
        ;;
    stop)
        stop_legacy_paper_servers
        for s in $TARGETS; do
            stop_server "$s"
        done
        ;;
    restart)
        stop_legacy_paper_servers
        for s in $TARGETS; do
            stop_server "$s"
        done
        for s in $TARGETS; do
            start_server "$s"
        done
        ;;
    status)
        for s in $TARGETS; do
            status_server "$s"
        done
        ;;
    *)
        echo "usage: $0 {start|stop|restart|status} [all|ligandmpnn|boltz|gnina|simulation|placer|plip|rfdiffusion|evolvepro|mutation-scoring|simple-demo|paper-search|paper-download|community-predict|pythia|foldx|homology]" >&2
        exit 1
        ;;
esac
