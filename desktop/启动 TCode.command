#!/bin/bash
cd "$(dirname "$0")/.."
./scripts/start_desktop.sh
status=$?
if [ "$status" -ne 0 ]; then
    printf '\n启动失败。按回车关闭此窗口。\n'
    read -r _
fi
exit "$status"
