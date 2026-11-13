# TCode × CopilotKit

这是一个可运行的 CopilotKit `1.70.1` 参考前端，直接使用 TCode 的 AG-UI `0.0.59` 端点。开发代理在服务端注入 API Key，浏览器不会读取密钥。

```bash
export TCODE_URL=http://127.0.0.1:8000
export TCODE_API_KEY=tcode_your_key
npm install
npm run dev
```

页面会从 TCode 读取当前账号的活跃 Workspace、可用 Harness 和模型，并允许在顶部直接切换。切换任一运行配置时会创建新的 thread，避免把旧 Workspace 或 Harness 的上下文混入新会话。

左侧历史栏会列出当前 Workspace 的 AG-UI 会话。点击历史项会从 TCode 恢复消息并继续使用原 thread；刷新页面时也会读取 URL 中的 `thread` 参数恢复当前会话。普通 AG-UI SSE 模式不依赖 CopilotKit Intelligence 的 thread 服务，历史数据由 TCode 自己的 `/api/sessions` 接口提供。

历史项右侧的“删除”按钮会先确认，再调用 TCode 的会话删除接口。客户端只提交一次删除请求，通过 `delete-status` 等待异步清理完成；失败会显示原因，等待超过 30 秒会提示刷新核对，不自动重发删除。删除当前会话成功后会新建空会话，删除其他会话不切换当前聊天。当前正在生成回复时，需先停止回复。删除包含关联子会话和会话运行资源，不删除整个 Workspace。

URL 参数可用于指定初始选项；无效或已删除的 Workspace 会自动回退到第一个活跃 Workspace：

```text
http://localhost:5173/?workspace=workspace-1&runtime=tcode-native
http://localhost:5173/?workspace=workspace-1&runtime=opencode
```

默认模型是 `deepseek/deepseek-v4-flash`。生产环境应把 `/api/ag-ui/*`、`/api/workdirs` 和 `/api/sessions` 放在应用自己的服务端代理之后，并从服务端密钥存储注入 `Authorization`。
