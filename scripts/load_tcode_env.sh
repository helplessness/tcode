#!/bin/bash

# 普通 Web/Server 共用显式 profile 优先级；不修改桌面生成的配置。
select_tcode_mcp_profile() {
    local project_dir="$1"
    case "${TCODE_MCP_PROFILE:-}" in
        job-protocol-only|job_protocol_only)
            export TCODE_MCP_CONFIG="${TCODE_CONFIG_ROOT:-$project_dir/.tcode-local}/mcp_job_protocol_only.json"
            echo "Using MCP profile: job-protocol-only ($TCODE_MCP_CONFIG)"
            ;;
    esac
}

# 为 Web、后台 Server、Sandbox Runner 和 TMCP 选择并加载运行环境配置。
# 第二个参数可以为特定服务指定默认配置；显式的
# TCODE_ENV_FILE 仍然拥有最高优先级。
load_tcode_env() {
    local project_dir="$1"
    local service_default="${2:-}"
    local env_file=""
    local migrated_env_file=""
    local selected_by=""

    if [ -n "${TCODE_ENV_FILE:-}" ]; then
        env_file="$TCODE_ENV_FILE"
        selected_by="TCODE_ENV_FILE"
    elif [ -n "${TCODE_SANDBOX_RUNNER_ENV_FILE:-}" ]; then
        env_file="$TCODE_SANDBOX_RUNNER_ENV_FILE"
        selected_by="TCODE_SANDBOX_RUNNER_ENV_FILE (兼容旧配置)"
        echo "Warning: TCODE_SANDBOX_RUNNER_ENV_FILE 已弃用，请改用 TCODE_ENV_FILE。" >&2
    elif [ -n "$service_default" ] && [ -f "$project_dir/.tcode-local/$service_default" ]; then
        env_file="$project_dir/.tcode-local/$service_default"
        selected_by="本机私有服务配置"
    elif [ -n "$service_default" ] && [ -f "$project_dir/$service_default" ]; then
        env_file="$project_dir/$service_default"
        selected_by="服务默认配置"
    elif [ -f "$project_dir/.tcode-local/local.env" ]; then
        env_file="$project_dir/.tcode-local/local.env"
        selected_by="本机私有配置"
    elif [ -f "$project_dir/config/sandbox_production.env" ]; then
        env_file="$project_dir/config/sandbox_production.env"
        selected_by="默认生产配置"
    else
        echo "未找到本机运行配置。请先执行：$project_dir/scripts/init_local_config.sh" >&2
        return 1
    fi

    # 与旧启动脚本一致：相对路径以项目根目录为基准，而不是调用者当前目录。
    if [[ "$env_file" != /* ]]; then
        env_file="$project_dir/$env_file"
    fi

    # 公开配置整理前，这三个本机环境文件位于 config/。只兼容这些已知旧路径，
    # 其他显式错误路径仍然快速失败，避免拼写错误被静默掩盖。
    if [ ! -f "$env_file" ]; then
        case "$env_file" in
            "$project_dir/config/local.env"|\
            "$project_dir/config/mcp_host.env"|\
            "$project_dir/config/symphony_route.env")
                migrated_env_file="$project_dir/.tcode-local/$(basename "$env_file")"
                if [ -f "$migrated_env_file" ]; then
                    echo "Warning: ${env_file} 已迁移到 ${migrated_env_file}；请更新 TCODE_ENV_FILE。" >&2
                    env_file="$migrated_env_file"
                    selected_by="旧配置路径兼容"
                fi
                ;;
        esac
    fi

    if [ ! -f "$env_file" ]; then
        echo "Runtime env file not found: $env_file" >&2
        return 1
    fi

    env_file="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"
    export TCODE_ENV_FILE="$env_file"

    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a

    # 本机配置可能从另一台机器迁移过来，其中的 Python 绝对路径会失效。
    # 保留有效的显式配置；失效时使用当前服务器 PATH 中的解释器。
    if [ -n "${PYTHON_BIN:-}" ]; then
        if [[ "$PYTHON_BIN" == */* ]]; then
            python_bin_available=false
            [ -x "$PYTHON_BIN" ] && python_bin_available=true
        else
            python_bin_available=false
            command -v "$PYTHON_BIN" >/dev/null 2>&1 && python_bin_available=true
        fi
        if [ "$python_bin_available" = false ]; then
            fallback_python="$(command -v python3 || command -v python || true)"
            if [ -n "$fallback_python" ]; then
                echo "Warning: PYTHON_BIN 不可用（$PYTHON_BIN），改用 $fallback_python。" >&2
                export PYTHON_BIN="$fallback_python"
            fi
        fi
    fi

    echo "Loaded runtime env: $env_file ($selected_by)"
}
