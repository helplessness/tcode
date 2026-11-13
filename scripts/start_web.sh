#!/bin/bash

# TCode Agent Web Server Launcher

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Web、后台 Server 和 Sandbox Runner 共用同一套配置文件选择规则。
# shellcheck source=scripts/load_tcode_env.sh
source "$PROJECT_DIR/scripts/load_tcode_env.sh"
load_tcode_env "$PROJECT_DIR" || exit 1

"$PROJECT_DIR/scripts/check_web_sandbox_boundary.sh"

# 显式 profile 优先于 local.env 的通用前台路径，与 start_server.sh 一致。
select_tcode_mcp_profile "$PROJECT_DIR"

PYTHON_BIN=${PYTHON_BIN:-python3}
WORKER_LOG_DIR="${TCODE_WORKER_LOG_DIR:-$PROJECT_DIR/runs/worker_logs}"
SERVER_PORT="8080"
WORKDIR_PATH=""
WORKER_PIDS=()
SERVER_PID=""
CLEANUP_DONE=0

mkdir -p "$WORKER_LOG_DIR"

normalize_local_api_base() {
    local current_value="$1"
    local port="$2"

    if [ -z "$current_value" ] || [[ "$current_value" =~ ^https?://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]]; then
        echo "http://127.0.0.1:$port"
        return 0
    fi

    echo "$current_value"
}

should_start_symphony_sidecar() {
    # 职责：判断当前进程是否需要启动 Symphony sidecar。
    # 输入：只读取 TCODE_PROXY_SUBMIT_MODE、TCODE_ASYNC_EXECUTION_MODE 与
    # SYMPHONY_ROUTE_ON_MCP_TOOL，不修改环境或启动进程。
    # 返回：需要启动时返回 0，否则返回 1。Symphony 是最新生产单轨；已删除
    # 的 hybrid 值不会再隐式同时启动两套 worker。
    local proxy_mode="${TCODE_PROXY_SUBMIT_MODE:-}"
    local async_mode="${TCODE_ASYNC_EXECUTION_MODE:-}"
    local route_on="${SYMPHONY_ROUTE_ON_MCP_TOOL:-false}"

    if [ "$route_on" = "true" ]; then
        return 0
    fi
    if [ "$proxy_mode" = "symphony" ]; then
        return 0
    fi
    if [ "$async_mode" = "symphony" ]; then
        return 0
    fi

    return 1
}

should_start_bridge_worker() {
    # 职责：判断是否显式启用独立 Bridge 运维 worker。
    # 输入：只读取 TCODE_PROXY_SUBMIT_MODE 与 TCODE_ASYNC_EXECUTION_MODE，
    # 不读取旧服务名单，也不修改环境或启动进程。
    # 返回：任一模式精确等于 bridge 时返回 0，否则返回 1。生产权威配置使用
    # Symphony，因此正常启动不会同时拉起 bridge worker。
    local proxy_mode="${TCODE_PROXY_SUBMIT_MODE:-}"
    local async_mode="${TCODE_ASYNC_EXECUTION_MODE:-}"

    if [ "$proxy_mode" = "bridge" ]; then
        return 0
    fi
    if [ "$async_mode" = "bridge" ]; then
        return 0
    fi

    return 1
}

start_background_worker() {
    local worker_name="$1"
    local script_path="$2"
    local log_file="$WORKER_LOG_DIR/$worker_name.log"

    if [ ! -f "$script_path" ]; then
        echo "Skipping $worker_name: script not found at $script_path"
        return 0
    fi

    echo "Starting $worker_name..."
    PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}" TCODE_WORKDIR="$WORKDIR_PATH" "$PYTHON_BIN" "$script_path" >"$log_file" 2>&1 &
    local worker_pid=$!
    WORKER_PIDS+=("$worker_pid")
    echo "  -> pid=$worker_pid log=$log_file"
}

wait_for_web_server() {
    local health_url="http://127.0.0.1:${SERVER_PORT}/api/health"
    local started_at=$SECONDS
    local last_warning=0
    local elapsed

    while true; do
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "Error: web server exited before becoming ready." >&2
            return 1
        fi
        if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$health_url', timeout=1).read()" >/dev/null 2>&1; then
            return 0
        fi
        elapsed=$((SECONDS - started_at))
        if (( elapsed >= 300 )); then
            echo "Error: web server did not become ready within 300 seconds: $health_url" >&2
            return 1
        fi
        if (( elapsed - last_warning >= 30 )); then
            echo "Web server is still starting (${elapsed}s); waiting before starting background workers..." >&2
            last_warning=$elapsed
        fi
        sleep 1
    done
}

cleanup_children() {
    local pid

    if [ "${CLEANUP_DONE:-0}" -eq 1 ]; then
        return
    fi
    CLEANUP_DONE=1
    # A second Ctrl-C used to interrupt the grace period and leave users unsure
    # whether shutdown was progressing. Finish this bounded cleanup once started.
    trap '' INT TERM
    echo "Stopping background workers and web server..."

    stop_process() {
        local target_pid="$1"
        local label="$2"
        local _

        if [ -z "$target_pid" ] || ! kill -0 "$target_pid" 2>/dev/null; then
            return
        fi

        kill "$target_pid" 2>/dev/null || true

        for _ in {1..20}; do
            if ! kill -0 "$target_pid" 2>/dev/null; then
                break
            fi
            sleep 0.25
        done

        if kill -0 "$target_pid" 2>/dev/null; then
            echo "Force killing $label (pid=$target_pid)"
            kill -9 "$target_pid" 2>/dev/null || true
        fi

        wait "$target_pid" 2>/dev/null || true
    }

    # Stop pollers first so they cannot keep issuing claim requests while the
    # application is draining its own startup/shutdown hooks.
    for pid in "${WORKER_PIDS[@]:-}"; do
        stop_process "$pid" "background worker"
    done

    stop_process "${SERVER_PID:-}" "web server"
}

frontend_needs_build() {
    if [ ! -f "web/dist/index.html" ]; then
        return 0
    fi

    if [ -n "$(find web/src web/scripts web/index.html web/desktop.html web/molstar.html web/package.json web/package-lock.json web/tsconfig.json web/vite.config.ts -newer web/dist/index.html -print -quit)" ]; then
        return 0
    fi

    return 1
}

# Check if --workdir is specified
WORKDIR_ARG=""
OTHER_ARGS=()

# Parse arguments without using shift in for loop
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
    arg="${args[$i]}"
    case $arg in
        --port=*)
            SERVER_PORT="${arg#*=}"
            OTHER_ARGS+=("$arg")
            i=$((i + 1))
            ;;
        --port)
            i=$((i + 1))
            if [ $i -lt ${#args[@]} ]; then
                SERVER_PORT="${args[$i]}"
                OTHER_ARGS+=("--port=${SERVER_PORT}")
                i=$((i + 1))
            else
                echo "Error: --port requires a port argument"
                exit 1
            fi
            ;;
        --workdir=*)
            WORKDIR_ARG="$arg"
            i=$((i + 1))
            ;;
        --workdir)
            i=$((i + 1))
            if [ $i -lt ${#args[@]} ]; then
                WORKDIR_ARG="--workdir=${args[$i]}"
                i=$((i + 1))
            else
                echo "Error: --workdir requires a path argument"
                exit 1
            fi
            ;;
        *)
            OTHER_ARGS+=("$arg")
            i=$((i + 1))
            ;;
    esac
done

# If no workdir specified and running interactively, prompt user
if [ -z "$WORKDIR_ARG" ] && [ -t 0 ]; then
    echo ""
    echo "Select working directory:"
    echo "  1) Current project directory ($PROJECT_DIR)"
    echo "  2) Choose custom directory"
    echo ""
    read -p "Enter choice [1-2] (default: 1): " choice
    
    if [ "$choice" = "2" ]; then
        read -p "Enter working directory path: " custom_workdir
        if [ -d "$custom_workdir" ]; then
            WORKDIR_ARG="--workdir=$custom_workdir"
        else
            echo "Warning: Directory does not exist, using current project directory"
        fi
    fi
fi

if [ -n "$WORKDIR_ARG" ]; then
    WORKDIR_PATH="${WORKDIR_ARG#--workdir=}"
else
    WORKDIR_PATH="$PROJECT_DIR"
fi

# --workdir 是运行数据根目录，首次部署或完整清理后允许它不存在。
# 由启动脚本创建该目录，避免要求管理员在启动前额外执行 mkdir。
if [ ! -e "$WORKDIR_PATH" ]; then
    echo "Creating runtime workdir: $WORKDIR_PATH"
    mkdir -p -- "$WORKDIR_PATH"
elif [ ! -d "$WORKDIR_PATH" ]; then
    echo "Error: --workdir points to a non-directory path: $WORKDIR_PATH"
    exit 1
fi

WORKDIR_PATH="$(cd "$WORKDIR_PATH" && pwd)"

# 默认的 Workspace 存储根目录是 <server-workdir>/workdirs。若把项目自身的
# workdirs 目录误传给 --workdir，就会生成 workdirs/workdirs，并让会话库与
# Workspace 数据落到错误位置。显式配置 TCODE_USER_WORKDIRS_ROOT 时不应用此规则。
PROJECT_WORKDIRS_PATH="$PROJECT_DIR/workdirs"
if [ -z "${TCODE_USER_WORKDIRS_ROOT:-}" ] && [ "$WORKDIR_PATH" = "$PROJECT_WORKDIRS_PATH" ]; then
    echo "Error: --workdir must point to the Tcode runtime root, not its workdirs directory." >&2
    echo "Received: $WORKDIR_PATH" >&2
    echo "This would make the Workspace storage root: $WORKDIR_PATH/workdirs" >&2
    echo "Use: ./scripts/start_web.sh --workdir \"$PROJECT_DIR\"" >&2
    exit 1
fi

export TCODE_WORKDIR="$WORKDIR_PATH"
if [ -n "$WORKDIR_ARG" ]; then
    WORKDIR_ARG="--workdir=$WORKDIR_PATH"
fi

# 将“运行 workspace”和“读取配置的根目录”解耦。
# 本机开发配置由 .tcode-local/local.env 设置到 Git 忽略目录；
# 这样即使 --workdir 指向其他目录，也不会把核心配置切到用户 workspace。
# 如需改成读取某个独立配置目录，可在启动前显式设置 TCODE_CONFIG_ROOT。
if [ -z "${TCODE_CONFIG_ROOT:-}" ]; then
    export TCODE_CONFIG_ROOT="$PROJECT_DIR"
fi

# 先完成并验证运行根目录解析，再执行可能耗时的前端构建。
if frontend_needs_build; then
    echo "Building web frontend..."
    cd web
    npm install
    npm run build
    cd "$PROJECT_DIR"
fi

export TCODE_TRACKER_API_BASE="$(normalize_local_api_base "${TCODE_TRACKER_API_BASE:-}" "$SERVER_PORT")"
if [ -n "${TCODE_HUMAN_REVIEW_API_BASE:-}" ] || [ "${HUMAN_REVIEW_ENABLED:-false}" = "true" ]; then
    export TCODE_HUMAN_REVIEW_API_BASE="$(normalize_local_api_base "${TCODE_HUMAN_REVIEW_API_BASE:-}" "$SERVER_PORT")"
fi
export TCODE_TRACKER_API_TOKEN_FILE="${TCODE_TRACKER_API_TOKEN_FILE:-$PROJECT_DIR/tmp/tracker-api.token}"
TCODE_TRACKER_API_TOKEN="$(
    PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -m tcode_agent.runtime.tracker_token ensure
)"
export TCODE_TRACKER_API_TOKEN
echo "Loaded shared tracker API token for external workers."

# Start server
echo "Starting TCode Agent Web Server..."

# Build command arguments safely
CMD_ARGS=()
for arg in "${OTHER_ARGS[@]}"; do
    CMD_ARGS+=("$arg")
done
if [ -n "$WORKDIR_ARG" ]; then
    CMD_ARGS+=("$WORKDIR_ARG")
fi

trap cleanup_children EXIT INT TERM

"$PYTHON_BIN" -m tcode_agent.server.launcher "${CMD_ARGS[@]}" &
SERVER_PID=$!

# Worker 会立即轮询本机 Tracker API。等待 Web 健康后再启动，避免每次正常
# 启动都产生一次误导性的 connection refused 日志。
if ! wait_for_web_server; then
    # EXIT trap stops an unready process instead of leaving a live Web without workers.
    exit 1
fi

if should_start_symphony_sidecar; then
    start_background_worker "shared_tracker_symphony_sidecar" "$PROJECT_DIR/scripts/shared_tracker_symphony_sidecar.py"
fi

if should_start_bridge_worker; then
    # The launcher owns its Bridge when it started a local broker. External
    # broker deployments retain the historical shell-owned worker.
    marker="$WORKDIR_PATH/.tcode/job-protocol/web-bridge.pid"
    if [ ! -f "$marker" ] || [ "$(<"$marker")" != "$SERVER_PID" ]; then
        start_background_worker "shared_tracker_bridge_worker" "$PROJECT_DIR/scripts/shared_tracker_bridge_worker.py"
    fi
fi

set +e
wait "$SERVER_PID"
EXIT_CODE=$?
set -e

exit "$EXIT_CODE"
