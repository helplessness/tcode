# BenchThread Artifact Python SDK

同仓库维护、可独立安装的文件客户端。发行包名 `tcode-artifact-sdk`，导入名 `tcode_artifacts`。当前版本 0.1.0，仅提供源码／wheel 安装，**尚未发布到 PyPI**。

只依赖 `httpx`，不导入 `tcode_agent` 或 `tmcp`，不启动服务，不实现计算、队列、审核或轮询。沿用已有 Artifact HTTP v1 接口和 `ArtifactRef`／`BundleRef`，Python 3.10+。Linux／macOS 保留安全目录描述符实现；新增 **Windows 10+ 本地 NTFS 实验性实现（原生验收尚未完成）**，使用系统句柄，不提供关闭路径保护的降级方式。

## Windows：手动安装与验证，不依赖 CI

在仓库根目录的 PowerShell 中执行，使用独立环境，不要求修改执行策略：

```powershell
py -3.12 -m venv .venv-artifact-sdk
.\.venv-artifact-sdk\Scripts\python.exe -m pip install -e "./packages/artifact-sdk[test,example]"
.\.venv-artifact-sdk\Scripts\python.exe -m pytest -c packages/artifact-sdk/pyproject.toml packages/artifact-sdk/tests packages/artifact-sdk/examples/test_mcp_server.py -q
```

Windows 原生测试不能用 macOS/Linux 的通过结果代替。原有符号链接测试需要创建符号链接的权限（例如已启用开发者模式）；不自动修改系统权限。新增目录联接测试不依赖符号链接特权。

- 传入绝对的本地盘符路径，例如 `C:/TCodeTasks/task-123/input.dat`；SDK 不解释远端主机路径，不接受 UNC、设备命名空间或网络映射盘。
- 当前仅准入固定磁盘上的 NTFS。拒绝重解析点（包含 junction／符号链接）、硬链接源文件、ADS、保留设备名、尾随点/空格、短名别名和含 `~` 的名称；这些限制是显式拒绝，不会偷偷重命名。
- Bundle 在创建目录前检查大小写别名和文件/目录前缀冲突。跨平台传输仍使用 `/` 分隔的相对清单路径。
- 新建文件/目录具有仅当前进程用户与 SYSTEM 访问的保护 DACL；既有目录权限不修改。仍应使用应用创建的私有任务目录，不与不可信进程共享写权限。
- 子项使用相对于父目录句柄的 `NtCreateFile`，拒绝重解析；祖先句柄保留且不共享删除。失败下载通过打开的文件句柄标记删除，不按可能被替换的名字删除文件。

配置真实服务后可手动启动文件回显示例：

```powershell
$env:TMCP_ARTIFACT_SERVICE_URL = 'https://artifact.example.org'
# 按部署方式注入 TMCP_ARTIFACT_API_TOKEN，不把真实令牌提交到仓库
.\.venv-artifact-sdk\Scripts\python.exe packages/artifact-sdk/examples/mcp_server.py
```

这里只支持远端 MCP 使用 SDK 进行文件传输，不代表 BenchThread Web 全功能、Job Protocol 服务或 Windows 安全沙箱已经完成适配。

## 安装与打包

在 BenchThread 仓库根目录执行，建议使用新的虚拟环境，不修改正在运行的宿主环境：

```sh
python3 -m venv .venv-artifact-sdk
source .venv-artifact-sdk/bin/activate
python -m pip install './packages/artifact-sdk[test,example]'
```

不需要示例／测试依赖时：`python -m pip install ./packages/artifact-sdk`。开发安装可加 `-e`。不要直接执行 `pip install tcode-artifact-sdk` 并假定 PyPI 有此包。

交付到第三方服务器：

```sh
python -m pip install build
python -m build packages/artifact-sdk
# 将生成的 wheel 交付到远端，在远端自己的虚拟环境安装
python -m pip install /path/to/tcode_artifact_sdk-0.1.0-py3-none-any.whl
```

SDK 的最小依赖只有 httpx；运行 MCP 示例还需安装 `fastmcp>=3.2,<4`。无需安装整个 BenchThread。复制示例到远端时一并交付 wheel；SDK 不要求远端访问 BenchThread 的源码路径。

## 服务配置

```sh
export TMCP_ARTIFACT_SERVICE_URL='https://artifact.example.org'
# 只在实际部署时设置真实访问令牌，勿放进参数或文件引用
export TMCP_ARTIFACT_API_TOKEN='your-artifact-access-token'
```

双方必须能访问同一个 Artifact 服务。远端的 `127.0.0.1` 不是用户电脑；SDK 不自动部署服务、转发端口或把客户端路径当作远端路径。公网部署需要 HTTPS、鉴权和服务端配额。Artifact 凭据与 MCP 连接 Token 是两种不同凭据。

多服务配置与现有 TMCP 命名一致：

```sh
export TMCP_ARTIFACT_SERVICE_ID='primary'
export BACKUP_ARTIFACT_TOKEN='your-second-service-token'
export TMCP_ARTIFACT_SERVICES='{"backup":{"url":"https://backup.example.org","token_env":"BACKUP_ARTIFACT_TOKEN"}}'
```

带 `service_id=backup` 的输入只使用本地 allowlist 中 backup 的地址和令牌，未知 ID 立即失败，不回退到默认服务；引用内的 URL 不参与路由。没有 service_id 的旧引用沿用默认服务。默认输出归属由构造函数／环境配置确定，不会因读取另一个服务的输入而悄悄改变。SDK 不读取宿主的 target-server 路由设置，因为它不负责调度目标。

## 文件与目录 API

```python
from tcode_artifacts import ArtifactIO

io = ArtifactIO()  # 也可显式提供 base_url、token、service_id、services
input_path = io.materialize(
    {"kind": "artifact", "artifact_id": "art_example"},
    "/private/task-123/input.dat",
)
# 你的业务程序使用 input_path，生成 /private/task-123/output 下的文件
output_ref = io.upload_directory("/private/task-123/output")
result = {"status": "completed", "output_bundle": output_ref}
```

引用中的 ID 必须来自真实上传结果，上面的 `art_example` 只是结构说明。

- `materialize(ref, destination)`：接收 `kind=artifact` 或 `kind=artifact_bundle`，可带 `service_id`；返回本地 `Path`。
- `download_file(artifact_id, destination)`：默认服务上的单文件读取，兼容原示例入口。
- `upload_file(source, workspace_id="third-party")`：返回 ArtifactRef。
- `upload_directory(source, workspace_id="third-party")`：上传并完成目录包，返回 BundleRef；只有服务确认 ready 才返回成功。
- 默认总字节上限 500 MiB、文件数 10,000，可使用 `max_bytes`／`max_files` 构造参数调整。原示例的 `EXAMPLE_MAX_BYTES`／`EXAMPLE_MAX_FILES` 环境变量仍兼容；服务端及 BenchThread 宿主另有独立限制，不能只改 SDK 就视为全链路放宽。

输入文件不能覆盖现有文件，目录接收使用不存在的新目录；下载核对实际字节数及 SHA-256，并核对目录清单与文件元数据。拒绝目录穿越、符号链接和非普通输出文件。为每个任务分配私有任务目录，输出完成后再上传；不要在传输期间并发修改源文件或将任务目录交给不可信进程写入。同步 API 可用于同步 MCP 工具；不要直接放进异步事件循环的长阻塞代码中。

HTTP／校验错误会抛异常，不返回虚假成功、不跟随重定向、不自动重试 POST／PUT。失败的目录下载可能保留已校验的部分文件，失败的上传可能保留远端草稿包；应记录失败并在新的私有目录重试文件操作，不能把残留目录当作完整结果。不要因此重新提交状态不明的科学计算。

## 最小 MCP 示例

```sh
python packages/artifact-sdk/examples/mcp_server.py
```

默认仅监听 `127.0.0.1:10240/mcp`，暴露 `echo_file(input_file: dict)`；仅复制合成输入并上传 `echo.dat`，不运行蛋白或 GPU 计算。公开部署之前自行配置 MCP 认证／TLS，不直接把未认证示例暴露到公网。

在 BenchThread 中使用已有“设置 → MCP 服务 → 异步执行 → 输入文件传输”：发现 echo_file 并将 input_file 声明为文件字段，远端接收方式选“已支持产物引用”。BenchThread 负责读取授权、上传、队列和结果回传；用户批准后仅提交一次，收到受理编号后等待通知，不由示例自行轮询或重新提交。

这个最小工具是 **blocking** 接入：桌面把它作为已有异步任务执行，但 SDK 不会让远端同步函数自动获得幂等／断线续跑。需要提交幂等性、status/result/cancel 和远端恢复时，复用仓库 `examples/third_party_mcp/remote_server.py` 与 `async-plugin/async-targets.json` 的 remote-job v1 示例。该示例的 `artifact_io.py` 已转为本 SDK 的兼容导入，不另维护第二份传输实现；任务状态机仍留在原示例／宿主。

从远端进程部署到 BenchThread 目标登记的完整步骤见 [第三方异步 MCP 接入与运行](../../docs/服务隔离/第三方MCP插件接入与运行.md)。

## 契约测试

```sh
python -m pytest -c packages/artifact-sdk/pyproject.toml packages/artifact-sdk/tests -q
# 安装 example 依赖后：真实 FastMCP in-process 工具发现及回显
python -m pytest -c packages/artifact-sdk/pyproject.toml packages/artifact-sdk/examples/test_mcp_server.py -q
```

测试使用合成数据、MockTransport 和临时回环 HTTP 服务，不连接正式服务，不提交专业计算。包内测试不依赖 BenchThread／TMCP，验证现有 API 路径、multipart、文件／目录往返、服务凭据隔离、无自动重试／重定向及文件安全边界。宿主既有文件传输、审批和异步恢复测试仍单独运行；契约测试不等价于任意真实服务器的鉴权和业务验收。
