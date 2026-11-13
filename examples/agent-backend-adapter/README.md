# 第三方 AgentBackend 示例

这个最小 Python 包展示第三方 Agent 框架如何接入 TCode。适配器只依赖公开的
`AgentBackend`、规范化输入和 v2 事件，不依赖 FastAPI、WebSocket 或 TCode Web。

开发时从仓库根目录运行合同测试：

```bash
PYTHONPATH=.:examples/agent-backend-adapter \
  python -m pytest -q examples/agent-backend-adapter/test_backend.py
```

打包后安装即可通过 `tcode.agent_backends` entry point 被发现：

```bash
python -m pip install ./examples/agent-backend-adapter
```

生产适配器还应把框架自身的消息、工具、权限和取消语义映射成 v2 事件，并使用
`tcode_agent.adapters.conformance.collect_backend_events` 验证每个录制 fixture。
