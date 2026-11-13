# 第三方 MCP：从普通工具到可恢复的远程任务

先选接入方式。短查询、文本工具直接按 [MCP 快速上手](../../tcode_agent/MCP_QUICKSTART.md)接入，不需要 TMCP 或任何 TCode SDK。下面是 TCode 的 **remote-job v1** 接入示例：适合远端先返回任务 ID，稍后查询、取消或取回结果的计算。它是本项目的任务契约，不是所有 MCP 服务自动具备的能力。

| 组件 | 放在哪里 | 做什么 |
| --- | --- | --- |
| [第三方 MCP 服务](../../examples/third_party_mcp/remote_server.py) | 计算主机 | 持久化提交和任务状态，执行任务，提供状态、结果和取消工具。示例只处理合成文件，不做分子计算。 |
| [Artifact Python SDK](../../packages/artifact-sdk/README.md) | 第三方 MCP 的 Python 环境 | 下载输入文件引用、上传输出并返回引用。只传文本时可以不用。 |
| [TCode 插件声明](../../examples/third_party_mcp/async-plugin/async-targets.json) | TCode 侧 | 将业务工具、远端四个任务方法、参数和文件字段声明给宿主。通过 [Plugin SDK](../plugin-sdk/README.md) 验证和分发。 |
| Job Protocol、后台执行与状态跟踪 | TCode 侧部署 | 接受宿主授权的提交，调用第三方 MCP，按原任务 ID 查询和回收。TCode 的最小聊天配置没有启用这条链路。 |
| Artifact 服务 | 双方都能访问的位置 | 示例需要它处理文件。只安装 Artifact SDK 不会创建这个 HTTP 服务。 |

## 1. 先确认 TCode 侧的服务就绪

- 这个示例要求一个支持插件目标注册的兼容 Job Protocol 实例。仅克隆或安装 TCode 不会安装 TMCP；源码启动脚本 [`start_job_protocol.sh`](../../scripts/start_job_protocol.sh) 和 [`start_artifact_server.sh`](../../scripts/start_artifact_server.sh) 分别依赖部署环境中的 `tmcp.funcdir.job_protocol_mcp` 与 `tmcp.artifact_server`。没有兼容服务时，可以先开发普通 MCP 或运行包内契约测试，不要把注册成功当作 SDK 的能力。
- Job Protocol 启动后，将 [代理示例](../../config/examples/mcp-async-broker.example.json)中的条目合并到前台 `.tcode-local/mcp.json`，填写实际 `/mcp` 地址、所需认证，再设为启用。若显式选用了 `job-protocol-only` 档案，则修改该档案对应的清单。注册 API 基址可通过 `TCODE_PLUGIN_JOB_PROTOCOL_URL` 指定，**不带** `/mcp`。跨进程部署时，TCode 与 Job Protocol 必须使用同一注册令牌 `TMCP_TARGET_REGISTRY_TOKEN`；同机源码启动可复用启动脚本生成的令牌文件。
- 使用启用后台执行和状态跟踪的运行配置，不要沿用根 README 中显式设置 `TCODE_ASYNC_WATCHER_ENABLED=0` 的纯聊天命令。涉及文件时，TCode 和远端都要能访问对应的 Artifact 服务。远端 MCP Bearer Token、Artifact Token 和 Job Protocol 注册令牌各管一层，不能混用。

## 2. 在计算主机安装 SDK 并启动示例

计算主机也可以是 Windows：SDK 已新增 Windows 本地 NTFS 的实验性句柄实现，原生验收尚未完成。安装及无 CI 手动测试见 [SDK 的 Windows 说明](../../packages/artifact-sdk/README.md#windows手动安装与验证不依赖-ci)。在该独立环境中，PowerShell 可设置 `$env:EXAMPLE_DATA_ROOT = 'C:/TCodeTasks/example-compute-data'`，再运行 `.\.venv-artifact-sdk\Scripts\python.exe examples/third_party_mcp/remote_server.py`；Artifact/MCP Token 仍由服务部署方注入。持久目录用于服务重启后的原任务恢复，不能每次启动清空。

Windows 远端不要求和 TCode 主机使用相同操作系统；双方只交换文件引用，通过可达的 Artifact HTTP 服务传输，不共享盘符或绝对路径。本次没有改 Job Protocol 或 Artifact 服务端的操作系统支持。

如果计算主机有 TCode 的源码副本，在该副本根目录执行以下命令；否则按 [SDK 打包说明](../../packages/artifact-sdk/README.md#安装与打包)安装 wheel，并交付 `remote_server.py`、`artifact_io.py` 和 `requirements.txt`。计算主机不需要安装整个 TCode 或 TMCP。

```bash
python3 -m venv .venv-remote-mcp
source .venv-remote-mcp/bin/activate
python -m pip install './packages/artifact-sdk[example]'

# 替换为双方能访问的真实地址；凭据由环境或密钥管理器注入。
export TMCP_ARTIFACT_SERVICE_URL='https://artifact.example.org'
export EXAMPLE_DATA_ROOT="$PWD/example-compute-data"
python examples/third_party_mcp/remote_server.py
```

示例默认监听 `127.0.0.1:10230/mcp`。同机验证可以直接使用回环地址；跨主机时配置受保护的网络或 HTTPS 入口，并在远端进程设置 `EXAMPLE_MCP_TOKEN`。不要把未鉴权的示例暴露在公网。Artifact 鉴权使用独立的 `TMCP_ARTIFACT_API_TOKEN`。任务数据库位于 `EXAMPLE_DATA_ROOT`，若要验证重启恢复，重启前后要使用同一个持久目录。

在自己的计算程序里，只在文件边界使用 SDK：`ArtifactIO().materialize(ref, destination)` 把输入引用下载到该任务的私有目录，`upload_directory(output_dir)` 返回输出 BundleRef，再把它放进结果的 `output_bundle`。单文件上传、目录引用、字节限制及异常处理见 [SDK 文件 API](../../packages/artifact-sdk/README.md#文件与目录-api)。SDK 不会读取用户电脑上的绝对路径，也不会替你排队或重跑任务。

## 3. 在 TCode 中登记并验证

1. 参考 [插件配置样例](../../examples/third_party_mcp/plugins.example.json)，只把 `community-predict` 条目合并进当前插件配置，路径指向示例的 `async-plugin/`。按现有插件信任和隔离策略加载；不要为通过验证关闭权限检查。可先在 TCode 仓库根运行 `python scripts/plugin.py validate examples/third_party_mcp/async-plugin` 检查插件声明。
2. 在目标工作区打开 **设置 → MCP 服务 → 插件异步 MCP**。给 `community-predict` 的 `compute` 填入实际远端 MCP URL。有 Bearer 鉴权时，在 **TCode 进程**注入 `TCODE_PLUGIN_TOKEN_DEMO`，其值与远端的 `EXAMPLE_MCP_TOKEN` 一致；界面只填环境变量名。先点 **检查连接**，确认允许向该地址发送输入后再点 **启用／更新异步目标**。
3. 在工作区放一个无敏感内容的短文本文件，例如 `inputs/example.txt`，请 Agent 使用 `community-predict` 异步处理它。Agent 按 [示例 Skill](../../examples/third_party_mcp/async-plugin/skills/community-predict-runner/SKILL.md)发现宿主登记的目标，不猜测目标标识。按界面要求审核，**只提交一次**；收到 `LOCAL-*` 只是本地受理，等完成通知后再核对回传的 `reports/summary.json` 和 `inputs/input.dat`。
4. 验证恢复时，用同一个持久目录重启测试服务，确认仍能查询原 `job_id` 并取回结果；取消测试用另一条无副作用任务。连接检查通过不等于提交、恢复或文件往返已验证。

## 4. 替换成自己的任务实现

保留 [异步目标声明](../../examples/third_party_mcp/async-plugin/async-targets.json)和服务端接口的一致性。提交工具接收 `request_id`、业务工具名和参数，先持久化再返回 `job_id`；相同请求只认同一份参数，不能启动第二次计算。状态、结果、取消按原 `job_id` 操作，完成后应重复返回已保存的结果引用。若提交状态不明，用原请求标识核对，不把超时当作可以盲目重提的失败。

示例以 SQLite 演示持久任务 ID 和文件回传，不是生产计算队列。生产部署还要实现工作进程、并发和取消边界、资源配额、鉴权及日志；不要照搬示例的单进程实现来承载生产计算。
