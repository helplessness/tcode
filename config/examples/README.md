# 用途示例

需要复制到 `.tcode-local/` 的主模板统一位于相邻的 `../templates/`；本目录只放不会参与一一对应初始化的用途样例。

- `local-demo/` 是可以直接复制并加载的最小配置，不启用插件和 MCP，也不包含密钥或私网地址。
- 字段中文说明集中在 [配置字段说明](../../docs/配置字段说明.md)，不再维护单独的 JSONC 讲解文件。
- `sandbox_production.env.example` 与其余 `sandbox_*.example.json` 用于多人部署和隔离执行器，不适合直接作为本地体验配置。

运行最小模板的完整步骤见[Docker 本机体验](../../docs/RUNNING.md#docker-本机体验)。源码部署可运行 `scripts/init_local_config.sh`，把可编辑配置放入 Git 忽略的 `.tcode-local/`；生产部署也可以使用仓库外目录，并通过 `TCODE_CONFIG_ROOT` 指向它。服务地址和凭据应由环境变量或密钥系统提供。

## 按需连接 MCP

- **普通工具**：将 [`mcp-direct.example.json`](mcp-direct.example.json) 的服务项合并到 `.tcode-local/mcp.json`，改成实际服务名和 URL，有鉴权时添加引用环境变量的请求头，最后启用。不需要部署 TMCP 或任务代理。
- **自建异步工具**：用 [`mcp-async-broker.example.json`](mcp-async-broker.example.json) 配置前台的任务代理，并复用[第三方 MCP 示例](../../examples/third_party_mcp/README.md)中的 `async-plugin/`（声明后台目标）与 `remote_server.py`（实现远端服务）。本机 Job Protocol 启动后再启用代理条目。第三方服务实现提交、状态查询、结果回收和取消，并用 `request_id` 去重、持久化 `job_id` 和状态。只填 MCP URL 不会自动获得异步恢复；涉及文件时再配置 Artifact。
- **已有 TMCP / 蛋白部署**：[`mcp-tmcp-frontend.example.json`](mcp-tmcp-frontend.example.json) 保留任务代理和轻量检索示例，[`mcp-protein-executor.example.json`](mcp-protein-executor.example.json) 保留后台蛋白计算目标示例。把需要的条目合并到对应前台或执行清单，替换地址后再启用。任务代理不要放入执行清单。

前台示例包含 Job Protocol、文献搜索、文献下载和轻量诊断；后台示例包含 Simulation、Boltz、GNINA、LigandMPNN、PLACER、PLIP、RFdiffusion、EvolvePro、突变评分、Homology、Pythia、FoldX，以及用于测试的 Simple Demo。**这些是可接入的示例，不是随 TCode 安装的计算服务**。示例不会在首次启动时自动连接，也不要整份覆盖已有本机配置。
