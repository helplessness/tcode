# Windows Web 快速启动（PowerShell）

这是 Windows 上的 **Web 源码启动**说明：在 PowerShell 启动服务，用浏览器访问，不安装 Electron。基础聊天和普通远程 MCP 不需要 WSL、Docker 或 GPU。远程异步计算和文件回传需要另行配置服务；依赖本地执行隔离的工具不在这套启动方式的范围内。Windows 实机验收尚未完成。

## 1. 首次准备

安装 Python 3.12（含 pip 和 Python Launcher）与 Node.js 22.12+（含 npm），在 TCode 仓库根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[desktop]'
npm.cmd --prefix web ci
npm.cmd --prefix web run build

# 一次补齐公开配置模板，已有文件不会被覆盖。
New-Item -ItemType Directory -Force .tcode-local | Out-Null
Get-ChildItem .\config\templates -File | ForEach-Object {
    $target = Join-Path .tcode-local ($_.Name -replace '\.example(?=\.|$)', '')
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
}
```

`[desktop]` 在这里仅用于安装已有的 MCP 客户端依赖，不会安装或启动 Electron。无需激活虚拟环境或修改 PowerShell 脚本执行策略。

**聊天必填：**编辑 `.tcode-local/tcode.json`，把 `model` 中的 `your-model-id` 和 `provider.demo.models` 下的同名键都改成模型服务实际提供的 ID。在启动终端设置以下变量，替换示例值：

```powershell
$env:TCODE_DEMO_BASE_URL = 'https://your-provider.example/v1'
$env:TCODE_DEMO_API_KEY = '填写自己的密钥'
```

未使用的服务保持模板默认值。不要把 `.tcode-local`、真实令牌或密钥提交到 Git。

## 2. 启动 Web

在同一个 PowerShell 窗口执行：

```powershell
$repo = (Get-Location).Path
$env:TCODE_WORKDIR = Join-Path $repo 'workdirs\windows-web'
New-Item -ItemType Directory -Force $env:TCODE_WORKDIR | Out-Null
$env:TCODE_CONFIG_ROOT = Join-Path $repo '.tcode-local'
$env:TCODE_MCP_CONFIG = Join-Path $env:TCODE_CONFIG_ROOT 'mcp.json'
# 此文件应保持不存在：最小启动不加载面向完整部署的环境模板。
$env:TCODE_ENV_FILE = Join-Path $env:TCODE_WORKDIR 'no-runtime.env'
$env:TCODE_AUTH_REQUIRED = '0'
$env:TCODE_ENTERPRISE_MODE = '0'
$env:TCODE_SANDBOX_ENABLED = 'false'
$env:TCODE_SANDBOX_REQUIRE_RUNNER = 'false'
$env:TCODE_ASYNC_WATCHER_ENABLED = '0'
$env:TCODE_RESEARCH_ENABLED = 'false'

& .\.venv\Scripts\tcode-server.exe --host 127.0.0.1 --port 8080 --workdir $env:TCODE_WORKDIR
```

打开 [Web 界面](http://127.0.0.1:8080)，选择或新建工作区，再发送一条普通消息。若页面未打开，在另一个 PowerShell 窗口运行 `Invoke-RestMethod http://127.0.0.1:8080/api/health`，先确认 Web 进程就绪。健康检查不会验证模型或 MCP。

按 `Ctrl+C` 停止。在同一个终端重启只需最后一条命令；新终端需重新设置环境变量。保持同一个运行目录可保留会话和工作区。

以上是**仅监听回环地址的单机体验**，不要改成对外监听或把该端口转发到公网。无沙箱不代表可以在 Windows 宿主机上运行原本要求隔离的工具。

## 3. 可选：连接远程 MCP

- **普通查询或文本工具：**在 **设置 → MCP 服务 → 添加 MCP** 填远程 MCP 地址及认证，按需选“直接调用”。也可以按 [MCP 配置与验证](../tcode_agent/MCP_QUICKSTART.md)编辑 `.tcode-local/mcp.json`。不需要 Job Protocol 或文件 SDK。
- **可恢复的异步任务：**需要已有的兼容 Job Protocol、Bridge Worker 与远端计算服务。按 [第三方异步 MCP 指南](服务隔离/第三方MCP插件接入与运行.md)将 [代理示例](../config/examples/mcp-async-broker.example.json)合并到 `.tcode-local/mcp.json`，填写实际服务地址和认证。Web 进程设置 `TCODE_ASYNC_EXECUTION_MODE=bridge`、`TCODE_ASYNC_WATCHER_ENABLED=1`、`TCODE_PLUGIN_JOB_PROTOCOL_URL`（不带 `/mcp` 的基址）和对应的 `TMCP_TARGET_REGISTRY_TOKEN`。仅启动 `tcode-server` **不会**自动启动 Bridge Worker、Job Protocol 或 Artifact 服务。
- **Bridge Worker：**Web 和 Worker 设置同一个 `TCODE_TRACKER_API_TOKEN`；Worker 另设 `TCODE_TRACKER_API_BASE`（例如同机的 `http://127.0.0.1:8080`）和 `TCODE_BRIDGE_MCP_CONFIG`（含 Job Protocol 条目的清单绝对路径）。在另一个 PowerShell 窗口设置相同的工作目录和配置路径；确认 Worker 能发现该工作区，或显式设置 `TCODE_TRACKER_SCOPES=租户ID:用户ID:工作区ID`，再运行 `& .\.venv\Scripts\python.exe .\scripts\shared_tracker_bridge_worker.py`。

## 4. 可选：让自定义异步 MCP 收发文件

要上传 Windows 本机文件，在 TCode Web 的虚拟环境里再安装当前仓库的文件 SDK；仅连接文本工具不需要：

```powershell
& .\.venv\Scripts\python.exe -m pip install ./packages/artifact-sdk
```

双方还需能访问同一个 Artifact HTTP 服务，并分别注入所需凭据（TCode 可用 `TMCP_ARTIFACT_SERVICE_URL` 和 `TMCP_ARTIFACT_API_TOKEN`）。在“添加 MCP”选择“异步执行（通过 Job Protocol）”，再配置“输入文件传输”和声明的文件字段。远端必须支持接收文件引用；仅填写远端路径不会自动接通传输。

上传源须在本地固定 NTFS 卷上；不支持 UNC、网络盘或经目录联接绕行的路径。每个文件或目录参数按自己的读取授权核对；发现对象不匹配、目录成员发生变化或缺少 SDK 时，不会绕过检查入队。原生文件测试见 [Artifact SDK 的 Windows 说明](../packages/artifact-sdk/README.md#windows手动安装与验证不依赖-ci)。

首次验收建议先用一个普通文本 MCP 验证连接和调用。若要验收异步文件传输，再提交一个无敏感内容的小文件，仅提交一次；核对回传文件，重启后继续查询原任务，确认没有重复提交。

## 任务预览

升级后若之前打开的预览提示票据失效，重新打开预览即可，无需重提任务。Windows 上未显式配置 `TCODE_JOB_PREVIEW_SECRET` 或 `TCODE_PREVIEW_SECRET` 时，Web 进程重启也会使旧票据失效。需要跨进程验证时，为各进程注入同一个高强度 `TCODE_JOB_PREVIEW_SECRET`。
