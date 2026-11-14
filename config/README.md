# 配置目录

`config/templates/` 是公开模板；本机配置在 Git 忽略的 `.tcode-local/`。
首次运行 `./scripts/init_local_config.sh` 一次补齐全部公开配置模板，已有文件不会覆盖。
按场景填写即可，未使用的功能保持默认值，不需要再次生成配置。

| 模板 | 本机文件 | 何时使用 |
| --- | --- | --- |
| `tcode.example.json` | `tcode.json` | 模型、Provider、命令权限、学习与沙盒 Profile |
| `mcp.example.json` | `mcp.json` | 普通启动的前台服务清单，默认 `{}`，按需添加 |
| `mcp_agent_config.example.json` | `mcp_agent_config.json` | 每类 Agent 可见的 MCP 范围 |
| `mcp_job_protocol_only.example.json` | `mcp_job_protocol_only.json` | 只有显式选择 `job-protocol-only` 档案时才读取，默认 `{}` |
| `mcp_exec_only.example.json` | `mcp_exec_only.json` | 后台执行目标清单，默认 `{}`；不要在此登记任务代理，以免递归提交 |
| `plugins.example.json` | `plugins.json` | 插件登记、启用状态、配置与权限 |
| `human_review.example.json` | `human_review.json` | MCP 工具的人审范围、触发阈值与审核流程 |
| `skill_routing.example.yaml` | `skill_routing.yaml` | Skill 扫描范围、匹配阈值、向量索引与组合路由 |
| `sandbox_runner_tool_policy.example.json` | `sandbox_runner_tool_policy.json` | Runner 对每类工具强制执行的镜像、网络、只读和 root 上限 |
| `symphony_route.env.example` | `symphony_route.env` | 所有服务共享的环境变量默认值 |
| `local.env.example` | `local.env` | 本机 Web、Worker、Runner 和 Job Protocol 入口 |
| `desktop.env.example` | `desktop.env` | 可选修改：只在需要覆盖桌面启动参数时填写 |
| `mcp_host.env.example` | `mcp_host.env` | 远端 Artifact 与领域 MCP 主机入口 |

其余目录不是本机模板：

- [`examples/`](examples/README.md)：普通 MCP、第三方异步与可选蛋白部署示例，不会由初始化脚本复制。字段讲解统一见 `docs/配置字段说明.md`。
- `workdir_template/`：新建用户 Workspace 时使用的安全默认值。
- `tcode.schema.json`：编辑器校验 Schema，不含部署值，也不需要复制。
- `biosafety_policy.json`：BenchThread 的默认生物安全策略；新 Workspace 会获得独立副本。

完整的初始化步骤和字段说明见 [`docs/CONFIGURATION.md`](../docs/CONFIGURATION.md)。
