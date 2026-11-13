# 第三方 MCP 的独立 Python 文件 SDK

SDK 与 TCode 在同一仓库维护，位于 `packages/artifact-sdk`；可单独安装、打包为 wheel 交付。当前未发布到 PyPI。完整安装、API、限制与契约测试说明见 [SDK 开发文档](../../packages/artifact-sdk/README.md)。

## 职责边界

- **TCode／本地 Job Protocol**：现有文件字段声明、读取权限、审核、上传、持久队列、任务状态、产物落地与异步恢复。本次不改这些实现。
- **文件 SDK**：将已有 ArtifactRef／BundleRef 下载为远端私有目录中的文件，或将计算输出上传为引用。继续使用既有 `/api/v1/artifacts` 和 `/api/v1/bundles` HTTP 接口。
- **第三方 MCP**：调用业务程序，按已有 blocking 或 remote-job 契约返回结果；需要远端幂等性与恢复时保存 request_id、job_id 和实际状态。SDK 不替代这部分。

普通 MCP 不需要改成 TMCP 内部程序；只需在文件边界增加适配。SDK 不导入宿主，不启动服务，不接受引用内的任意 URL 或凭据，不增加第二套调度／恢复机制。

## 最短开发路径

1. 在仓库根安装 `python -m pip install './packages/artifact-sdk[example]'`，或在第三方服务器安装构建好的 wheel 和 FastMCP。
2. 从 `packages/artifact-sdk/examples/mcp_server.py` 开始。本地工具 echo_file 只接收一个 ArtifactRef，返回包含 echo.dat 的 output_bundle，不运行专业计算。
3. 配置双方可访问的 Artifact 地址与访问令牌。多服务沿用本地配置的 service_id allowlist；不要在插件包或工具参数中分发令牌。
4. 在已有 MCP 设置里声明 input_file 是文件字段、远端已支持引用；实际读取和计算仍按宿主规则批准。
5. 用唯一合成文本跑一次文件往返，核对输入和回传文件字节一致。连接检查／工具发现不等于文件传输成功。
6. 要求持久远端任务时复用 `examples/third_party_mcp/remote_server.py` 与原 async-plugin，保留原 submit/status/result/cancel 和 request_id 幂等机制。旧 artifact_io.py 已复用 SDK；不需要修改远端任务状态机。

## 安全与兼容性

当前安全文件打开面向 Linux／macOS，使用目录描述符和 no-follow 打开，不覆盖现有输入，不跟随文件或父目录符号链接。应用自己创建的私有临时根可先 canonicalize（macOS 的 /var 是系统链接）；不要对外部输入路径随意 resolve 来绕过拒绝。为各任务分配独立私有目录，传输时不要并发修改源文件。

传输异常会失败并保留可诊断状态，不自动重试状态不明的写请求，更不会重新提交计算。输入下载失败可能留下不完整目录，输出上传失败可能留下远端草稿包；不得将它们报告为完整结果。清理与重试文件操作由应用在私有任务范围内决定，不影响已排队任务。

原示例的环境变量、ArtifactIO 类名和主要方法保留；旧无 service_id 的引用继续使用默认服务。独立部署由“复制三个源码文件”改为“安装小 SDK wheel＋示例源码”，不是安装整个 TCode。既有已部署服务不会因仓库 SDK 提取而被自动升级。

## 本次验证（2026-10-27）

- 独立 SDK 契约 24 项、真实 FastMCP in-process 示例 1 项通过。包括临时本机 HTTP 的实际 multipart 往返、文件／目录字节与 SHA-256、配额、路径穿越、符号链接／源对象替换拒绝、服务与凭据隔离、重定向拒绝、未完成 Bundle 及写请求状态不明不自动重试。
- wheel 与 sdist 构建成功。在全新虚拟环境安装 wheel 后，从仓库外以 Python isolated 模式导入 SDK 成功；该环境没有 TCode／TMCP，最小运行依赖仅 httpx。
- 复用当前 TMCP Artifact FastAPI 应用与原 remote_server.py，在临时存储和 in-process MCP／HTTP 测试传输中完成合成文件上传→远端物化→报告目录回传→字节核对。相同 request_id 重复提交返回相同远端 Job ID；重建原服务对象后查询并收集已有 SQLite 持久结果，不重新上传、不重复执行。未连接正式 Artifact／MCP 服务。
- 宿主文件传输、插件异步目标、普通模式工具安全、目标模式、Protein Run、异步回传／恢复等相关回归 394 项通过，存在依赖弃用警告；没有跳过失败用例。未修改宿主权限、队列、任务状态机或恢复入口。
- 未发布 PyPI，未重启桌面、未提交蛋白／GPU 计算。本地契约与 in-process 验证不等于公网部署或真实桌面端的本轮人工验收。
