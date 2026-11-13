# 第三方 MCP 示例

- `direct-plugin/`：常规 MCP 插件，通过 `mcpServers` 直接连接。
- `async-plugin/`：声明式异步 MCP 插件，通过 `asyncTargets` 接入用户本地 Job Protocol。
- `remote_server.py`：部署在第三方服务器的可运行示例，提供查询与 remote-job v1 工具。
- `artifact_io.py`：同仓库独立文件 SDK 的兼容导入，不导入 TCode 或 TMCP。源码 checkout 保留直接运行；远端复制部署时先安装 SDK wheel。
- `local.env.example`、`mcp.local.example.json`、`plugins.example.json`：配置参考，按实际路径合并，不覆盖已有配置。

完整启动步骤、前端设置入口、认证、文件流转、API、恢复与测试见 [接入与运行文档](../../docs/服务隔离/第三方MCP插件接入与运行.md)。示例不执行真实分子计算。

可单独安装的 Python 文件 SDK、最小 echo_file MCP 与契约测试见 [Artifact SDK](../../packages/artifact-sdk/README.md)。在仓库根执行 `python -m pip install ./packages/artifact-sdk`；当前没有发布到 PyPI。原 remote-job 示例只更换文件适配器的导入，不改变任务提交、状态持久化或恢复协议。
