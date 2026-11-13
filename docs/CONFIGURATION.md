# 配置指南

在仓库根目录运行 `./scripts/init_local_config.sh`，一次将全部 13 份[公开模板](../config/README.md)补齐到 `.tcode-local/`，已有文件不会覆盖。源码安装脚本已包含这一步。`.tcode-local/` 仅保存在本机，不提交 Git。

| 场景 | 必填或需确认 | 不使用时 |
| --- | --- | --- |
| 基础聊天 | `tcode.json` 中的 `model` 与 `provider` 模型 ID 匹配；填写模型服务地址，接口要求鉴权时设置密钥 | 可以不配置视觉模型、Embedding、插件和 MCP |
| macOS 桌面 | 在桌面模型设置或 `tcode.json` 配置模型；有登录要求时配置本机账号 | 不需要改 `desktop.env` 就保持默认；`desktop-mcp.json` 由桌面管理 |
| MCP | 只添加并启用需要的服务，填写 URL 与相应认证，确认服务和工具的准入范围 | 服务清单保持 `{}`，不需要部署 TMCP |
| 多人部署 | 配置认证、服务凭据、隔离执行器、持久化路径和网络边界 | 不直接使用 README 的单机体验参数 |

`model` 使用 `provider-id/model-id` 格式。`baseURLEnv` 和 `apiKeyEnv` 引用环境变量名称，不是地址和密钥本身。JSON 文件保持标准格式；注释见[配置字段说明](配置字段说明.md)。不要把真实凭据写进公开模板。

### 按需开启扩展

- `mcp.json` 是前台服务清单；`mcp_agent_config.json` 控制 Agent 可见范围。后台任务的服务配置与前台分开，见 [MCP 接入](../tcode_agent/MCP_QUICKSTART.md)。
- `plugins.json` 只登记已审阅的插件；安装包、权限申请和运行授权是不同环节。见[插件 SDK](plugin-sdk/README.md)。
- `human_review.json` 管理工具审核规则；`sandbox_runner_tool_policy.json` 是 Runner 侧工具执行的权限上限。不要为解决连接问题而放宽审核或隔离策略。
- `local.env` 继承同目录的 `symphony_route.env`。`desktop.env` 与 `mcp_host.env` 分别用于桌面与独立服务主机；非对应场景无需填写。

### 生效与本机数据

源码启动脚本默认使用 `.tcode-local/local.env`，可用 `TCODE_ENV_FILE` 显式指定环境文件；`TCODE_CONFIG_ROOT` 指定核心配置目录。修改环境文件后重启对应进程，桌面需完全退出再打开。运行记录、缓存和用户产物与配置分开，见[数据边界](MAINTENANCE.md#数据边界)。
