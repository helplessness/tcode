# 项目架构

TCode 的 Web 与 Electron 桌面共用主要界面和 Python 后端；基本聊天独立运行，按需接入模型服务、MCP、插件和隔离执行器。

| 路径 | 职责 |
| --- | --- |
| `web/src/`、`desktop/` | SolidJS 界面；Electron 窗口、原生交互和桌面后端生命周期 |
| `tcode_agent/server/` | HTTP、WebSocket、身份、工作区与任务入口 |
| `tcode_agent/harness/`、`context/`、`tools/` | Agent 循环、上下文与工具执行 |
| `tcode_agent/runtime/`、`session/`、`workflow/`、`workspace/` | 运行状态、会话、异步恢复、工作流与领域 Run |
| `tcode_agent/mcp/`、`plugins/`、`packages/artifact-sdk/` | MCP 客户端、插件接入及可独立安装的文件传输 SDK |
| `config/`、`scripts/`、`tests/` | 公开模板、启动与开发入口、回归测试 |

一次对话由界面送入后端，Agent 读取任务上下文并调用当前获准的工具；结果和状态由后端回传。异步工作保留提交回执，再根据原任务引用查询状态和结果。提交状态不明时不要盲目重提；界面在线不代表远端任务已完成。同步聊天无需依赖这一链路。

## 数据边界

- `config/templates/` 与 `docs/` 随仓库发布；`.tcode-local/` 是初始化后产生的私有配置。
- 工作区、运行记录、日志和产物依启动方式保存在工作区或运行数据目录；仓库内的 `.tcode/`、`runs/`、`workdirs/` 不进入 Git。桌面另有应用数据目录，清理源码目录不等于清空桌面记录。
- 模型凭据、真实服务地址、账号绑定、用户附件和产物均不属于公开文档。提交示例时使用占位符和无敏感数据的夹具。
