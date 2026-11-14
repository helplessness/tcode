# 首版开源 Alpha：范围与验证

本快照包含 BenchThread 主体、Web 和 macOS 桌面源码、Windows Web 启动指南、公开配置模板、插件/Artifact SDK、最小 MCP 示例以及公开的基础回归测试。Windows Web 属于实验性支持，尚未完成 Windows 实机验收，不包含 Windows 桌面版。

本快照不附带私有 Git 历史、账号和本机配置、会话与运行产物。未确认可分发的酶知识库、文献来源快照、基准样本、真实验收报告和私有 Workflow 模板不随源码分发。酶研究插件保留实现，默认不启用；依赖数据的功能须另行配置授权数据源。项目 MIT 许可不改变第三方组件或数据的原有使用条件；见[第三方来源与许可证](../THIRD_PARTY_NOTICES.md)。

首次运行从 [README](../README.md#快速开始) 选择平台。无需真实模型账号即可从项目根目录做一次基础验证：

```bash
python scripts/dsh.py smoke --scenario server-websocket-plugin
```

这个用例使用本地协议桩验证 Web 对话及 MCP/插件入口，不验证真实模型效果、专业计算或跨平台兼容。开发者还可运行 `python -m pytest -q scripts/tests`；文件 SDK 的安装和测试见 [Artifact SDK](../packages/artifact-sdk/README.md)。

README 中无登录的最小 Web 启动只用于本机回环地址，不应用作公网部署。
