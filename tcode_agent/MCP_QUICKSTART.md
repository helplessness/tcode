# MCP 配置与验证

普通源码启动默认读取 Git 忽略的 `.tcode-local/mcp.json`。首次初始化生成的是 `{}`；不连接 MCP 时不用配置 TMCP。若文件缺失，运行 `./scripts/init_local_config.sh` 补齐。

## 配置一个服务

```json
{
  "my-mcp": {
    "transport": "streamable-http",
    "url": "https://mcp.example.com/mcp",
    "connect_timeout": 10000,
    "timeout": 300000,
    "tool_timeout_seconds": {
      "long_running_tool": 14400
    },
    "headers": {
      "Authorization": "Bearer ${TCODE_MCP_TOKEN}"
    },
    "enabled": true
  }
}
```

字段解释见 [配置字段中文说明](../docs/配置字段说明.md)。只添加需要的服务，替换 URL 并设置所需环境变量。可选的普通、异步和蛋白部署配置见 [MCP 接入示例](../config/examples/README.md)。

请求头支持 `${ENV_NAME}` 占位符，并在建立连接时读取环境变量。变量不存在时连接会失败，错误只显示变量名，不显示其他请求头内容。不要把真实令牌写入公开模板或提交到 Git。

## 选择配置文件

启动脚本会从 `.tcode-local/local.env` 读取：

```bash
TCODE_CONFIG_ROOT="$PROJECT_ROOT/.tcode-local"
TCODE_MCP_CONFIG="$TCODE_CONFIG_ROOT/mcp.json"
```

仅在主动设置 `TCODE_MCP_PROFILE=job-protocol-only` 时，Web / Server 启动脚本才改读 `mcp_job_protocol_only.json`。后台计算目标与前台服务分开配置。

直接调用安装后的命令时显式指定：

```bash
export TCODE_CONFIG_ROOT=/absolute/path/to/config
export TCODE_MCP_CONFIG="$TCODE_CONFIG_ROOT/mcp.json"
tcode-server --workdir /absolute/path/to/workspace
```

生产部署应把配置放在仓库之外，并通过进程环境或密钥系统注入令牌。

## 超时语义

- `connect_timeout`：初始化、会话建立和工具列表请求的超时，单位为毫秒；
- `timeout`：服务的默认调用超时，单位为毫秒；
- `tool_timeout_seconds`：按工具名覆盖调用超时，单位为秒，适合异步计算或长任务。

超时只控制等待边界，不保证远端任务可恢复。长任务还需要 Job Protocol、状态持久化和结果回收链路。

## 本地验证与远端验证

无需远端服务器即可运行本机协议桩 Smoke：

```bash
python scripts/dsh.py smoke \
  --scenario-timeout-seconds 45 \
  --total-timeout-seconds 120 \
  --report build/test-reports/real-entry-smoke.json
```

真实远端 MCP 需要另外验证：连接与认证、工具发现、长任务提交、进度恢复、取消、重连和最终 Artifact 完整性。本机 Smoke 通过不能替代这些结果。

Agent 可见的服务范围由 `.tcode-local/mcp_agent_config.json` 控制，模板和字段说明见 [配置字段中文说明](../docs/配置字段说明.md)。
