# 标准 AG-UI HttpAgent 示例

这个最小客户端只使用官方 `@ag-ui/client`，不依赖 BenchThread Web、CopilotKit 或
`@tcode-ai/ag-ui-client`。它也不发送 `forwardedProps.tcode`，用于验证第三方标准
AG-UI 客户端能够直接运行 BenchThread 默认 Agent。

先启动启用了 AG-UI 的 BenchThread：

```bash
TCODE_AGUI_ENABLED=1 ./scripts/start_web.sh --port 5555 --host 127.0.0.1 --workdir "$PWD"
```

再运行：

```bash
npm ci --prefix examples/agui-http-agent
TCODE_URL=http://127.0.0.1:5555 \
  npm --prefix examples/agui-http-agent start -- "请用一句话介绍你自己"
```

启用服务端认证时，只在当前终端设置 `TCODE_API_KEY`。不要把 Key 写进源码或提交到仓库。
