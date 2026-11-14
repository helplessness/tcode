# BenchThread Python Plugin SDK v1 开发文档

本目录面向准备为 BenchThread 开发插件的 Python 开发者。它只描述当前可用的 Plugin SDK v1，不包含 DSH 改造历史，也不兼容 DeepSeek Harness 的 Manifest、SDK 或社区插件。

## 当前版本

| 项目 | 当前值 |
| --- | --- |
| Plugin API | `1.0` |
| Python SDK | `1.4.0` |
| Python | `>=3.11` |
| Manifest | v2，文件名固定为 `plugin.json` |
| 包格式 | `.tcode-plugin` |
| 社区默认运行方式 | 独立隔离 Worker |

## 推荐阅读顺序

动态业务请求头见 [MCP 动态 Header 配置与调用](./11-MCP动态Header配置与调用.md)：Agent 提取业务变量、单次会话隔离及异步上下文快照。

1. [快速开始](./01-快速开始.md)：十分钟创建并运行第一个工具插件。
2. [Manifest 与配置](./02-Manifest与配置.md)：理解插件身份、依赖、配置和贡献声明。
3. [SDK API 与生命周期](./03-SDK-API与生命周期.md)：编写 Tool、Prompt、Event、健康检查和卸载逻辑。
4. [安全、权限与信任](./04-安全权限与信任.md)：理解社区插件为何默认隔离，以及权限如何生效。
5. [测试、打包、发布与安装](./05-测试打包发布与安装.md)：完成合同测试、签名、审核和安装。
6. [故障排查与发布检查表](./06-故障排查与发布检查表.md)：根据稳定诊断快速定位问题。
7. [容器隔离与受控能力调用](./07-容器隔离与受控能力调用.md)：让未知插件安全调用网络、Tool、MCP Tool 和 Skill。
8. [插件声明式 Workflow](./08-插件声明式Workflow.md)：让插件携带由宿主校验、执行和回收的流程模板。
9. [记忆能力与生命周期钩子](./09-记忆能力与生命周期钩子.md)：在压缩、删除和日常读写时安全操作 Scope 化记忆。
10. [MCP 接入](../../tcode_agent/MCP_QUICKSTART.md)与 [Python 文件传输 SDK](12-Python文件传输SDK.md)。

## 最重要的开发规则

- 插件代码只能从 `tcode_agent.plugin_sdk` 导入公开 API。
- 插件入口的 `PluginMetadata.name`、`version` 必须与 `plugin.json` 完全一致。
- 社区插件从 `requestedTrust: isolated` 和 `permissions: []` 开始。
- Manifest 是权限申请，不是授权；签名和 `verified` 也不会自动获得宿主权限。
- 未知社区插件可以贡献 Tool 和无代码的声明式 Workflow；Workflow 只能编排宿主能力，不能绕过 Tool、Permission、Policy 与 Sandbox。
- 不要在模块导入阶段启动线程、进程、网络连接或修改全局 Registry。
- 所有函数和类应写清晰的中文注释，工具描述应说明适用场景、输入和副作用。
- 提交发布前必须依次通过 `validate`、`test`、`doctor` 和 `pack`。

## 命令入口

在 BenchThread 仓库根目录中可以使用：

```bash
python scripts/plugin.py --help
```

如果当前 Python 环境可以导入 BenchThread 的 Python 模块 `tcode_agent`，则也可以使用：

```bash
python -m tcode_agent.plugin_sdk --help
```

两种入口调用的是同一套 SDK v1 CLI。

## 参考实现

- 最小社区模板：`tcode_agent/plugin_sdk/templates/python-plugin/`
- 真实第三方隔离 Fixture：`plugins/third_party_fixtures/community_smoke_plugin/`
- 完整公开 API 示例：`plugins/example_plugin/`
- 声明式 Workflow 示例：`plugins/example_plugin/workflows/echo-review/`

`community_smoke_plugin` 不在官方插件信任清单中，是验证复制、登记、Docker 挂载、Tool/Workflow 加载和卸载的首选样例。具体装载命令见该 Fixture 的 `README.md`。

Paper Research 是 BenchThread 默认官方领域插件，依赖尚未向社区开放的能力，不应作为社区插件模板复制。原 Protein Design 插件已删除；通用 `protein_orchestrator` 和蛋白工作流能力仍属于核心系统，不是社区插件模板。
