# 运行指南

## Windows Web

按 [PowerShell Web 快速启动](Windows-Web快速启动.md)运行，不使用下方 Bash 脚本。仅提供 Web 源码启动路径，Windows 实机验收尚未完成。

## Linux Web 和 CLI

新用户按[快速开始](../README.md#快速开始)安装、填写模型并启动单机 Web；不需要先启动远程服务。需要按本机环境配置运行完整 Web 服务时，从仓库根目录执行：

```bash
./scripts/init_local_config.sh
PYTHON_BIN="$PWD/.venv/bin/python" ./scripts/start_web.sh --workdir "$PWD/workdirs/local"
```

先确保 `.tcode-local/local.env` 及其继承的配置已按用途设置；此启动方式可能拉起配置中启用的后台服务，不等同于 README 的最小启动方式。`/api/health` 验证 HTTP 服务启动，不验证模型、MCP 或远端计算。只使用 CLI 时，可在已安装的环境中运行 `TCODE_CONFIG_ROOT="$PWD/.tcode-local" tcode --workdir "$PWD/workdirs/local"`。

## macOS 桌面开发版

```bash
./scripts/init_local_config.sh
./scripts/start_desktop.sh
```

首次启动需要联网、Python 3.12+ 与 Node.js 22.12+。桌面使用自己的本机后端与运行数据目录，启动桌面不要求先启动 Web；首次使用按[桌面说明](../desktop/README.md)完成设置。打包见 [macOS 打包](gui/12-macos-dmg-packaging.md)。

## Docker 本机体验

```bash
mkdir -p workdirs/container
docker build -t tcode:local .
docker run --rm -p 127.0.0.1:8080:8080 \
  -e TCODE_BOOTSTRAP_PASSWORD \
  -e TCODE_DEMO_BASE_URL -e TCODE_DEMO_API_KEY \
  -v "$PWD/workdirs/container:/workspace" \
  tcode:local
```

先在终端设置这些环境变量，给 `TCODE_BOOTSTRAP_PASSWORD` 填写自己的强密码。镜像默认开启登录，初始账号为 `admin@example.com`。实际聊天还需用可用的模型 ID：准备一份 `config/examples/local-demo/` 的配置副本、填写 `tcode.json`，再通过 `-v /absolute/config:/config:ro` 挂载。不要把本机体验命令当作生产部署；公开服务需单独设置认证、隔离和持久存储。
