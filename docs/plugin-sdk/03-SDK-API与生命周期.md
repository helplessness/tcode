# SDK API 与生命周期

插件只能从下面这个公共包导入：

```python
from tcode_agent.plugin_sdk import ...
```

导入 `tcode_agent.plugins.manager`、Registry、Server、数据库或其他私有运行时模块会被合同测试拒绝。

## 1. 插件实例协议

推荐使用类作为插件实例：

```python
class Plugin:
    """描述插件的职责和公开贡献。"""

    info = PluginMetadata(
        name="community-echo",
        version="0.1.0",
        description="社区回显插件。",
        plugin_type=("tool",),
    )

    async def start(
        self,
        ctx: PluginContext,
        config: Mapping[str, Any],
    ) -> None:
        """注册本实例拥有的所有公开贡献。"""

    async def health(self) -> PluginHealth:
        """返回不包含 Secret 和业务正文的健康摘要。"""

    async def stop(self, reason: str) -> None:
        """停止插件自行创建的任务并释放外部资源。"""
```

生命周期顺序：

1. Host 静态校验 Manifest、版本、路径、依赖、权限和配置；
2. Host 决定 `controlled-host` 或 `isolated-worker`，插件不能自行选择；
3. 导入 `entry` 并创建实例；
4. 校验 `PluginMetadata`；
5. 调用 `start(ctx, config)` 收集贡献；
6. 健康检查通过后一次性发布贡献；
7. 卸载时先撤销贡献，再调用 `stop(reason)` 并清理所属资源。

如果启动、健康检查或发布任一步失败，本次新增贡献会按实例所有权回滚。

## 2. PluginContext

`PluginContext` 只包含窄接口：

| 成员 | 用途 |
| --- | --- |
| `ctx.instance` | 当前实例名称、版本、实例 ID、API 版本和请求信任级别 |
| `ctx.scope` | 当前 Workspace Scope 和不透明 Workspace ID |
| `ctx.logger` | `debug/info/warning/error` 日志门面 |
| `ctx.tools` | 注册 `ToolDefinition` |
| `ctx.prompt` | 提交 `PromptContribution` |
| `ctx.events` | 订阅版本化 `PublicEvent` |
| `ctx.verifiers` | 注册由 Host 限时执行、校验输出并封顶证明等级的 `VerifierDefinition` |
| `ctx.memory` | 使用声明并获权的 Scope 化记忆能力；未声明 `tcode.memory@1` 时为 `None` |
| `ctx.workspace` | 受文件权限约束的读写门面 |
| `ctx.process` | 受进程权限和 Sandbox 约束的命令门面 |
| `ctx.network` | 经 Host Broker 与 Egress Sandbox 发起有界 HTTP 请求 |
| `ctx.config` | 只读、已校验配置映射 |

Context 不暴露 PluginManager、ToolRegistry、数据库连接、原始 Session、Host Logger 或环境变量字典。

## 3. ToolDefinition

```python
ctx.tools.register(
    ToolDefinition(
        name="community_echo",
        description="返回带前缀的文本，不读写外部资源。",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1}
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=self._echo,
        version="1.0.0",
        side_effect_level="none",
        result_budget_tokens=2048,
    )
)
```

字段说明：

- `name`：全局稳定工具 ID，建议使用插件前缀；
- `description`：告诉 Agent 何时调用，以及是否有副作用；
- `input_schema`：Draft 2020-12 风格的对象 Schema；
- `handler`：同步或异步处理器；
- `version`：工具自身版本；
- `side_effect_level`：`none`、`read`、`write` 或 `process`；
- `result_budget_tokens`：结果文本预算，不能依赖无限输出。

处理器签名：

```python
async def _echo(
    self,
    arguments: Mapping[str, Any],
    call: ToolCallContext,
) -> ToolOutput:
    """处理一次已经通过 Host 参数和策略校验的工具调用。"""

    return ToolOutput(
        title="Community Echo",
        content=str(arguments["text"]),
        metadata={"agent": call.agent},
    )
```

所有工具都会进入 Host 的参数验证、Policy、Permission、Approval、Sandbox、执行和 Finalize 管线。插件不能绕过显式拒绝。

## 4. VerifierDefinition

SDK `1.4.0` 为宿主精确准入的 `controlled-host` 插件提供实验性 Verifier 扩展；完成核心 Canary 前不视为稳定的第三方执行接口。Manifest 必须声明：

```json
{"contributes": ["verifiers"]}
```

注册示例：

```python
ctx.verifiers.register(
    VerifierDefinition(
        verifier_id="my-plugin.report_quality",
        version="1.0.0",
        description="检查报告是否包含领域要求的结构化事实。",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        handler=lambda payload: VerifierOutput(
            status="pass",
            reason_code="report_quality_verified",
            observations=("report_quality_verified",),
        ),
        requested_max_proof_level="P1",
        timeout_seconds=5.0,
    )
)
```

安全边界：

- `verifier_id` 必须使用 `<plugin-name>.` 命名空间，重复 ID 或版本冲突会让插件发布事务失败；
- Handler 只能返回 `VerifierOutput` 事实，不能返回 Goal 终态或自报 `admitted_proof_level`；
- Host 会校验 Input/Output Schema，并签发绑定 Contract、版本、Criterion 和当前输入摘要的运行期 HMAC 回执；修改结果或复用到不同输入都会失效；
- 插件首版固定最多获得 P1，不提供环境变量放宽入口；P2 只由核心确定性 Verifier 提供；
- P3 独立来源和 P4 人工审批权不向插件开放；超时会立即禁用该 Verifier 注册，异常、Schema 不合法和插件卸载均失败关闭；
- `controlled-host` 超时采用固定容量 Worker 池和熔断，只限制并发与重复调用，不等同于安全沙箱，也不能强制终止已经运行的 Python 线程；
- 未知社区插件仍在隔离 Worker 中，只能贡献 Tool，不能贡献可执行 Verifier。

## 5. PromptContribution

只有官方路径或被宿主精确授权的插件才能在首发版本中贡献 Prompt。未知隔离插件会被拒绝。

静态文本：

```python
ctx.prompt.contribute(
    PromptContribution(
        name="community_echo_guidance",
        text="用户明确要求回显时，可以使用 community_echo。",
        order=900,
        token_budget=128,
        required=False,
    )
)
```

动态渲染：

```python
async def render_guidance(render: PromptRenderContext) -> str:
    """根据有界渲染变量返回短 Prompt，不读取 Host 私有状态。"""

    return f"当前 Agent：{render.agent or 'default'}"


ctx.prompt.contribute(
    PromptContribution(
        name="community_dynamic_guidance",
        renderer=render_guidance,
        token_budget=64,
    )
)
```

不要在 Prompt 中包含 Secret、用户完整文件、未授权路径或无限增长内容。

## 6. 公共 Event

API v1 包含配置事件，以及记忆读写、上下文压缩和会话删除的版本化事件。完整载荷与权限要求见[记忆能力与生命周期钩子](./09-记忆能力与生命周期钩子.md)。

```python
PublicEvent.CONFIG_LOADED_V1
```

订阅示例：

```python
async def _on_config_loaded(self, event: EventEnvelope) -> None:
    """观察配置加载快照，不尝试写回 Host。"""

    self._last_event_source = event.source


ctx.events.subscribe(PublicEvent.CONFIG_LOADED_V1, self._on_config_loaded)
```

公共事件是只观察快照。Handler 返回值不会成为 Host 状态，也不能借事件获取内部 Runtime 对象。

## 7. Workspace 与 Process

受控宿主插件在 Manifest 申请且运行配置未收窄对应权限后，可以直接使用 Workspace。未知隔离插件不获得直接 Workspace 视图；它的 Process、Network 和 Host Tool 调用都通过 Broker：

```python
text = ctx.workspace.read_text("data/input.txt")
ctx.workspace.write_text("output/result.txt", "done")
entries = ctx.workspace.list_dir("output")

result = await ctx.process.run(
    "git",
    ("status", "--short"),
    cwd=".",
    timeout=30,
)

response = await ctx.network.request(
    "GET",
    "https://api.example.com/v1/items",
    timeout=30,
)

delegated = await ctx.tools.invoke(
    "paper_search",
    {"query": "plugin isolation"},
    timeout=120,
)
```

社区隔离 Worker 永远不能直接读取宿主路径，也不能自己连接 Docker、Runner、MCP 或 Skill 容器。Manifest 仅申请能力，管理员必须在运行配置中再次精确授权。完整语义见[容器隔离与受控能力调用](./07-容器隔离与受控能力调用.md)。

## 8. Provider 数据契约

SDK v1 冻结了 `ProviderDefinition`、`ProviderRequest`、`ProviderResponse` 和 `ProviderProtocol`，用于描述可序列化、版本化的 Provider 数据边界。

它不表示插件可以替换生产 LLM、Session、Job、Approval 或 Sandbox Provider。生产 Provider 的接入和授权仍由 Host 控制。

## 9. 健康检查与卸载

健康检查只能返回脱敏摘要：

```python
async def health(self) -> PluginHealth:
    """返回插件健康状态和有界诊断。"""

    return PluginHealth(
        status=PluginHealthStatus.HEALTHY,
        summary="插件已就绪",
        details={"registered_tools": 1},
    )
```

`details` 中不要放 Token、密码、完整配置、用户内容或绝对敏感路径。

插件自行创建的后台任务、客户端或文件句柄应在 `stop` 中关闭。Host 会负责撤销通过 Context 注册的 Tool、Prompt 和 Event，不要直接操作内部 Registry。
