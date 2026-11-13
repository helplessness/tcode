#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ROOT="$PROJECT_ROOT/.tcode-local"
TEMPLATE_ROOT="$PROJECT_ROOT/config/templates"

if [[ "${1:-}" == "--help" ]]; then
    echo "用法：$0 [--all | 模板文件名 ...]"
    echo "默认一次补齐全部配置模板；已有文件不会被覆盖。"
    exit 0
fi

templates=()
if [[ "$#" -eq 0 || ( "$#" -eq 1 && "$1" == "--all" ) ]]; then
    for template in "$TEMPLATE_ROOT"/*; do
        templates+=("${template##*/}")
    done
else
    for name in "$@"; do
        found=false
        for template in "$TEMPLATE_ROOT"/*; do
            filename="${template##*/}"
            if [[ "$name" == "${filename/.example/}" || "$name" == "$filename" ]]; then
                # 桌面和独立 MCP 主机配置都继承本机基础配置。
                if [[ "$filename" == desktop.env.example || "$filename" == mcp_host.env.example ]]; then
                    templates+=(local.env.example)
                fi
                templates+=("$filename")
                found=true
                break
            fi
        done
        if [[ "$found" == false ]]; then
            echo "未知配置模板：$name（用 --help 查看用法）" >&2
            exit 2
        fi
    done
fi

# 新建的本机配置可能会填入凭据，不让其他本机用户读取。
umask 077
mkdir -p "$LOCAL_ROOT"

copy_if_missing() {
    local source="$1"
    local target="$2"
    if [ -e "$target" ]; then
        echo "保留已有配置：$target"
        return
    fi
    cp "$source" "$target"
    echo "已创建配置：$target"
}

for filename in "${templates[@]}"; do
    target_name="${filename/.example/}"
    copy_if_missing "$TEMPLATE_ROOT/$filename" "$LOCAL_ROOT/$target_name"
done

cat <<EOF

本机配置目录：$LOCAL_ROOT
必填（使用聊天时）：在 tcode.json 或桌面设置中配置模型；有鉴权的服务需配置凭据。
按需填写：不使用的 MCP、插件和独立服务保持模板默认值，不需要再次生成文件。
配置说明：docs/CONFIGURATION.md；真实配置和密钥不要提交 Git。
EOF
