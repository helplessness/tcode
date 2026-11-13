#!/bin/bash
# TCode Agent Web Server - Background Runner

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# Web、后台 Server 和 Sandbox Runner 共用同一套配置文件选择规则。
# shellcheck source=scripts/load_tcode_env.sh
source "$PROJECT_ROOT/scripts/load_tcode_env.sh"
load_tcode_env "$PROJECT_ROOT" || exit 1

"$PROJECT_ROOT/scripts/check_web_sandbox_boundary.sh"

# 显式 profile 优先于 local.env 的通用前台路径，与 start_web.sh 一致。
select_tcode_mcp_profile "$PROJECT_ROOT"

# 将“运行目录”和“配置根目录”拆开。
# 本机开发配置由 .tcode-local/local.env 设置到 Git 忽略目录；
# 也可以在启动前显式设置 TCODE_CONFIG_ROOT。
if [ -z "${TCODE_CONFIG_ROOT:-}" ]; then
    export TCODE_CONFIG_ROOT="$PROJECT_ROOT"
fi

# Default values
PORT=${PORT:-8080}
HOST=${HOST:-127.0.0.1}
LOG_FILE=${LOG_FILE:-server.log}
PID_FILE=${PID_FILE:-server.pid}
PYTHON_BIN=${PYTHON_BIN:-python3}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            PORT="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --log)
            LOG_FILE="$2"
            shift 2
            ;;
        --stop)
            echo "Stopping server..."
            if [ -f "$PID_FILE" ]; then
                kill $(cat "$PID_FILE") 2>/dev/null
                rm -f "$PID_FILE"
                echo "Server stopped"
            else
                echo "Server not running"
            fi
            exit 0
            ;;
        --restart)
            $0 --stop
            sleep 1
            ;;
        *)
            shift
            ;;
    esac
done

# Check if server is already running
if [ -f "$PID_FILE" ]; then
    if kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "Server is already running (PID: $(cat "$PID_FILE"))"
        echo "Stop it first: $0 --stop"
        exit 1
    else
        rm -f "$PID_FILE"
    fi
fi

echo "Starting TCode Agent Web Server..."
echo "  Host: $HOST"
echo "  Port: $PORT"
echo "  Log:  $LOG_FILE"
echo "  Python: $PYTHON_BIN"
echo ""

# Start server in background
nohup "$PYTHON_BIN" -m uvicorn tcode_agent.server.app:app \
    --host "$HOST" \
    --port "$PORT" \
    >> "$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo $SERVER_PID > "$PID_FILE"

echo "Server started (PID: $SERVER_PID)"
echo ""
echo "Access:"
echo "  Web UI: http://localhost:$PORT"
echo "  API:    http://localhost:$PORT/docs"
echo ""
echo "Logs: tail -f $LOG_FILE"
echo "Stop: $0 --stop"
