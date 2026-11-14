# MCP Apps 真实链路示例

这个服务通过真实 Streamable HTTP MCP 握手提供工具元数据、`ui://` HTML Resource
和 App 发起的反向工具调用。它只绑定本机地址，适合开发与发布候选验收。

启动服务：

```bash
python -m examples.mcp_apps_server --port 10131
```

把 `mcp.example.json` 中的服务合并到当前 MCP 配置，设置
`TCODE_MCP_APPS_ENABLED=1` 后启动 BenchThread。在聊天中让 Agent 调用
`mcp_tcode-mcp-app-example_show_counter`，消息里会出现计数器 iframe。点击
“通过 Host 增加一次”后，BenchThread 会先在 iframe 外要求一次授权，再调用
`refresh_counter`。

这个示例不连接外网，也不读取文件、环境变量或凭据。生产 MCP App 仍需遵守
BenchThread 的 URI、MIME、大小、CSP、短期 Token 和 Tool Policy 限制。
