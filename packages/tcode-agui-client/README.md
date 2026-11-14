# @tcode-ai/ag-ui-client

UI 无关的 BenchThread AG-UI 客户端，可用于浏览器、Electron、React Native 或 Node.js。它固定匹配 BenchThread 当前锁定的 AG-UI `0.0.59`。

> 当前状态：`0.0.1`，尚未发布到 npm registry。仓库内开发使用 workspace；开源发布前验证使用 `npm pack` 生成的 tarball。

正式发布后：

```bash
npm install @tcode-ai/ag-ui-client @ag-ui/client@0.0.59 @ag-ui/core@0.0.59
```

当前仓库验证：

```bash
npm --prefix packages/tcode-agui-client ci
npm --prefix packages/tcode-agui-client test
npm --prefix packages/tcode-agui-client pack
```

```ts
import { createTCodeAgent } from '@tcode-ai/ag-ui-client';

const agent = createTCodeAgent({
  baseUrl: 'https://tcode.example.com',
  workspaceId: 'workspace-1',
  runtimeId: 'tcode-native', // 或 opencode
  modelId: 'deepseek/deepseek-v4-flash',
  apiKey: process.env.TCODE_API_KEY,
});
```

运行流由官方 `HttpAgent` 提供。客户端同时实现了 AG-UI `connect()` 会话重放，因此 CopilotKit 使用显式 `threadId` 连接或刷新页面时，会从 BenchThread `/api/sessions` 恢复持久化消息。`TCodeGatewayClient` 补充 Agent/Runtime 发现、Harness 状态与切换、取消、持久交互、Guidance、Artifact 列表和下载能力。

`createTCodeAgent` 支持可选的 `clientCapabilities`、`disconnectPolicy` 和 `clientRequestId`，并只把这些字段放入 `forwardedProps.tcode`。MCP App 内容块使用导出的 `TCodeMCPAppBlock` 类型；渲染和 iframe 策略仍由宿主应用决定。

只依赖官方 `HttpAgent` 的最小客户端见 `examples/agui-http-agent`。它不发送任何 BenchThread 扩展字段，用于证明标准 AG-UI 接入路径保持独立。

标准 AG-UI 客户端不依赖这个包也能调用 BenchThread Gateway。这个包用于需要 BenchThread 历史、Runtime、交互和其他扩展能力的客户端，且不依赖任何 UI 框架。

`TCodeGatewayClient.deleteThread(threadId, workspaceId, { timeoutMs?, signal? })` 删除持久会话及关联子会话。该方法只提交一次 `DELETE /api/sessions/{id}`，随后轮询 `delete-status`，清理完成才返回；错误直接抛出。默认最多等待 30 秒，超时或取消等待不会撤销服务端已接受的删除，调用方应刷新历史列表核对结果。它是 BenchThread 会话管理扩展，不是 AG-UI 标准事件。
