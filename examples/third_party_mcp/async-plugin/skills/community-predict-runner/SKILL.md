---
name: community-predict-runner
description: 使用 community-predict 插件在第三方服务器执行预测，自动回传输出目录。
---

1. 不直接连接计算 MCP。调用 `mcp_job-protocol-mcp_describe_target_tool`，`input_config` 传空对象，获得当前身份已启用的 `plugin_targets`。
2. 选择 `plugin=community-predict` 的目标，使用它的 `server` 标识；不要猜测或写死其他安装实例的标识。
3. 必要时 describe `target_server=<server>, target_tool=predict`，确认输入契约。
4. 调用 `mcp_job-protocol-mcp_submit_job`：

   ```json
   {"input_config":{"target_server":"<宿主返回的 server>","target_tool":"predict","arguments":{"input_file":"inputs/example.txt","delay_seconds":2}}}
   ```

   目录输入用 `input_directory`；可单独提供其中一个，或同时提供。路径必须属于当前授权工作区，宿主会上传为 ArtifactRef/BundleRef。
5. 收到 LOCAL-* 后不要自行轮询，不调用服务器路径读取工具。由 TCode 统一查询、恢复和通知。
6. 收到完成通知后分析本地落地的结果目录。失败时重试同一提交／查询链路，不切换执行方式，不重复发起相同计算。
