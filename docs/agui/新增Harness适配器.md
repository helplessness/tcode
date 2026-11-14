# 新增 Harness Adapter

本文说明如何在 BenchThread 中增加一个本地 CLI Harness。第三方开发者优先使用 Runtime
Profile 或独立 Python Provider；只有由 BenchThread 官方共同维护的适配器才放入内置目录。
三条路径都复用传输、进程、Session、交接和事件协议公共层。

## 仓库外接入方式

不需要自定义 Python 行为时，创建一个 v2 Profile：

```yaml
schema: tcode.harness-runtime.v2
id: my-harness
displayName: My Harness
release: experimental
executables:
  primary: my-harness
probes:
  versionArgs: [--version]
  authStrategy: unknown
  modelStrategy: none
invocation:
  args: [acp, --cwd, "${TCODE_RESOLVED_WORKSPACE}"]
  promptMode: acp
  streamFormat: acp-json-rpc
session:
  strategy: acp
  resumeStrategy: acp-load
capabilities:
  text: true
  images: false
  tools: true
  permissionAuthority: shared
  resume: true
  cancel: true
  mcpInjection: acp
  negotiated: true
  guidanceMode: restart_turn
security:
  workspaceMode: configured
  inheritEnvironment: allowlist
  publicServer: false
compatibility:
  adapterApiVersion: "1"
  tcodeVersion: ">=0.0.1,<1"
```

把文件放入以下任一位置：

- `$TCODE_WORKDIR/.tcode/harnesses/*.yaml`；
- `~/.config/tcode/harnesses/*.yaml`；
- `TCODE_HARNESS_PROFILE_PATHS` 指定的目录。

先运行：

```bash
tcode runtimes validate ./my-runtime.yaml --json
tcode runtimes discover --json
tcode runtimes enable my-harness
tcode runtimes conformance my-harness --fixture --json
```

需要自定义 Backend 时，创建独立 Python 分发包，并声明：

```toml
[project.entry-points."tcode.harness_adapters"]
my-harness = "my_package.provider:provider"
```

Provider 实现 `get_runtime_definition()` 和 `create_backend(context)`。如果希望离线
conformance 覆盖自己的 Adapter，还应实现 `create_fixture_backend(context)`。完整可运行
项目见 `examples/harness-adapter`。

发现 entry point 时 BenchThread 只读取分发 metadata，不导入代码。管理员执行 `enable` 后，
信任记录才会写入配置文件；服务重启后加载 Provider。公开服务应设置
`TCODE_HARNESS_SETTINGS_PATH`，把该文件放在 Agent 无权写入的管理员配置目录。
Runtime ID 冲突会保留来源诊断并拒绝后发现的定义。

## 内置 Harness 目录合同

每个内置 Harness 必须拥有独立目录：

```text
tcode_agent/external_harness/runtimes/<runtime_package>/
├── __init__.py
├── adapter.py
├── definition.py
└── README.md
```

文件职责：

| 文件 | 职责 |
| --- | --- |
| `adapter.py` | 唯一组装入口；声明具体 Adapter 类并绑定稳定的 runtime ID |
| `definition.py` | 可执行文件、探测、argv、传输、Session、能力和安全声明 |
| `README.md` | 安装、认证、协议、Session、权限、公开服务和已知限制 |
| `__init__.py` | 只导出 `runtime_definition`，避免 Registry 初始化时形成循环导入 |

特殊探测、Session 或事件协议出现后，可以在该目录增加 `probe.py`、`session.py` 或
`event_mapper.py`。只有多个 Harness 复用的实现才移动到 `external_harness/transports/` 或
其他公共模块。

## 第一步：选择 runtime ID 和传输

runtime ID 使用小写字母、数字和连字符，例如 `openclaw`。先确认目标 CLI 提供的真实
非交互协议：

| CLI 能力 | 使用方式 |
| --- | --- |
| ACP JSON-RPC stdio | 复用 `AcpStdioDriver`，`streamFormat=acp-json-rpc` |
| Codex JSONL | 复用 `ProcessJsonlDriver`，`streamFormat=codex-jsonl` |
| Claude stream-json | 复用 `ProcessJsonlDriver`，`streamFormat=claude-stream-json` |
| 通用 NDJSON | 复用 `ProcessJsonlDriver`，`streamFormat=ndjson` |
| 纯文本 stdout | 复用 `ProcessJsonlDriver`，`streamFormat=plain` |
| 新协议 | 先实现有大小、超时和终态边界的 Driver，再将格式加入严格合同 |

不要通过 shell 字符串启动 CLI。所有参数必须是 `InvocationDef.args` 中的 argv 数组。

## 第二步：创建 Definition

创建 `runtimes/my_harness/definition.py`：

```python
from ...runtime_def import HarnessRuntimeDef


def runtime_definition() -> HarnessRuntimeDef:
    return HarnessRuntimeDef.model_validate(
        {
            "id": "my-harness",
            "displayName": "My Harness",
            "release": "experimental",
            "executables": {"primary": "my-harness"},
            "probes": {
                "versionArgs": ["--version"],
                "authStrategy": "unknown",
                "modelStrategy": "none",
                "timeoutSeconds": 5,
            },
            "invocation": {
                "args": ["acp", "--cwd", "${TCODE_RESOLVED_WORKSPACE}"],
                "promptMode": "acp",
                "streamFormat": "acp-json-rpc",
            },
            "session": {
                "strategy": "acp",
                "resumeStrategy": "acp-load",
            },
            "capabilities": {
                "text": True,
                "images": False,
                "tools": True,
                "permissionAuthority": "shared",
                "resume": True,
                "cancel": True,
                "mcpInjection": "acp",
                "negotiated": True,
                "guidanceMode": "queue_only",
            },
            "security": {"publicServer": False},
        }
    )
```

声明必须反映已经验证的能力。不能因为 CLI 文档提到某项功能就把它标为支持；至少需要
fixture 测试，公开服务能力还需要真实进程验证。

`guidanceMode` 决定平台如何把运行中指导投递给该 Harness：

- `restart_turn`：Adapter 已验证取消，并能在同一 Harness 中恢复原生 Session；BenchThread
  取消当前 Run 后自动在相同 runtime/model 上续跑。
- `queue_only`：默认值；当前 Run 完成后再把指导作为下一轮消息执行。
- `unsupported`：明确拒绝 Guidance。
- `in_turn`：需要 BenchThread GuidancePort、turn owner、safe point 和 completion fence 的完整
  合同；普通外部 Adapter 不能只靠能力声明获得该语义。

平台统一实现路由快照、切换锁、Run 取消和续跑。新增 Adapter 不应复制这部分代码。

支持的占位符只有 `${TCODE_RESOLVED_WORKSPACE}` 和 `${TCODE_MODEL_ID}`。未知占位符会被
拒绝。

## 第三步：创建具体 Adapter

创建 `runtimes/my_harness/adapter.py`：

```python
from tcode_agent.adapters.external_harness import ExternalHarnessBackend
from tcode_agent.external_harness.handoff_store import HarnessHandoffStore
from tcode_agent.external_harness.process_manager import HarnessProcessManager
from tcode_agent.external_harness.registry import HarnessRuntimeRegistry
from tcode_agent.external_harness.session_binding import HarnessSessionBindingStore
from tcode_agent.runtime.interaction_store import InteractionStore


class MyHarnessAdapter(ExternalHarnessBackend):
    RUNTIME_ID = "my-harness"

    def __init__(
        self,
        *,
        registry: HarnessRuntimeRegistry,
        interaction_store: InteractionStore,
        process_manager: HarnessProcessManager | None = None,
        binding_store: HarnessSessionBindingStore | None = None,
        handoff_store: HarnessHandoffStore | None = None,
    ) -> None:
        super().__init__(
            runtime_id=self.RUNTIME_ID,
            registry=registry,
            interaction_store=interaction_store,
            process_manager=process_manager,
            binding_store=binding_store,
            handoff_store=handoff_store,
        )
```

当前公共 Backend 根据 Definition 选择 ACP 或 JSONL Driver，并统一处理可执行文件解析、
启用状态、公开服务权限、取消以及私有活动过滤。Harness 特殊行为应以小型 Hook 添加到
自己的 Adapter；不要在公共 Backend 中继续累积 `if runtime_id == ...`。

## 第四步：导出并注册

`runtimes/my_harness/__init__.py`：

```python
from .definition import runtime_definition

__all__ = ["runtime_definition"]
```

然后修改 `runtimes/__init__.py` 的两个入口：

```python
from .my_harness import runtime_definition as my_harness_runtime_definition


def builtin_runtime_defs():
    return [
        # existing definitions...
        my_harness_runtime_definition(),
    ]


def builtin_adapter_types():
    from .my_harness.adapter import MyHarnessAdapter

    return [
        # existing adapters...
        MyHarnessAdapter,
    ]
```

Adapter 使用延迟导入是有意设计：`HarnessRuntimeRegistry` 初始化时只需要 Definition，
而具体 Adapter 又依赖 Registry。提前导入 Adapter 会形成循环依赖。

## 第五步：探测和模型

默认探测位于 `external_harness/detection.py`，负责：

- 从 `PATH` 解析可执行文件；
- 在受限环境中运行版本命令；
- 限制 stdout、stderr 和超时；
- 隐藏宿主机绝对可执行路径；
- 返回可用性、版本、模型和诊断。

目标 Harness 需要特殊认证或模型命令时，应新增命名明确的探测策略，并为成功、非零退出、
超时和畸形输出分别测试。探测不能读取或返回 API Key、Cookie 或配置文件正文。

## 第六步：Session、权限和取消

ACP Harness 默认复用：

- `initialize` 能力协商；
- 兼容版本与 capability digest 下的 `session/load`；
- 加载失败后的 `session/new`；
- `session/request_permission` 到 `InteractionStore`；
- `SIGINT → SIGTERM → SIGKILL` 的进程组取消。

非 ACP Harness 必须在 README 中明确能否恢复。未完成真实恢复测试前，设置
`resume=False`。`permissionAuthority=runtime` 的 Harness 不能直接用于公开服务；只有经过
BenchThread 或 shared 权限合同验证后才能设置为 `tcode` 或 `shared`。

## 第七步：插件投递边界

当前外部 Harness Adapter 会接收 Harness 交接上下文，但 BenchThread 平台插件快照尚未投递。
新增 Adapter 不得自行读取全部插件目录或主服务密钥。统一插件投递层完成后，Adapter 只
声明并消费以下经过准入的能力：

- Prompt blocks；
- 暂存的只读 Skill 目录和 Assets；
- 经策略过滤的 MCP server；
- 平台 Tool Bridge；
- 不支持能力的明确降级信息。

Workflow、Subagent 生命周期、Owner 隔离和 Job Protocol 仍由 BenchThread Platform 管理。

## 第八步：测试

至少增加以下测试：

1. Definition 严格校验、runtime ID 唯一；
2. Adapter 类型和 `backend_id` 一致；
3. CLI 缺失、版本失败和探测超时诊断；
4. argv 不经过 shell，Workspace 必须是绝对目录；
5. 敏感环境变量不继承；
6. Prompt、流式文本、工具和错误事件映射；
7. Session new/load 与不兼容降级；
8. 权限请求持久化和解决；
9. 取消后进程组退出；
10. handoff 只投递一次；
11. BenchThread v2 sequence 连续且只有一个终态；
12. Runtime 列表和 AG-UI Gateway 可见性。
13. `guidanceMode` 与真实取消/恢复能力一致；`restart_turn` 必须验证指导后仍使用相同
    runtime、model 和 Session 路由。
14. 原生 Session 加载失败时有界恢复上下文包含旧约束，且最新指导只出现一次。

最低回归命令：

```bash
PYTHONPATH=. pytest -q \
  tests/test_external_harness.py \
  tests/test_agent_backends.py \
  tests/test_agui_gateway.py

python -m compileall -q tcode_agent/external_harness
python scripts/generate_web_protocol.py --check
git diff --check
```

发布 beta 前还需用真实 CLI 完成一次初始化、模型选择、消息、工具、权限、取消和 Session
恢复测试，并在 `docs/agui/compatibility.md` 与 `docs/agui/validation.md` 记录版本和限制。

## 仓库外 Adapter 完成定义

第三方 Adapter 满足以下条件才算接入完成：

- 独立分发包或 Profile 无需修改 BenchThread 主仓库即可发现；
- 默认状态不能执行，管理员显式 `enable` 后才允许加载；
- `inspect` 能返回来源、版本、Definition digest、信任和兼容状态；
- `doctor` 能探测真实可执行文件；
- fixture conformance 通过并生成与 digest 绑定的能力证据；
- API 与前端显示的能力是声明与验证结果的交集；
- 禁用后现有 Backend 也会被 Registry 拒绝执行；
- 分发包包含 README、许可证、版本范围和自动化测试。

## 内置 Adapter 完成定义

一个 Adapter 只有同时满足以下条件才算接入完成：

- 独立目录包含 `adapter.py`、`definition.py` 和 README；
- `runtime_registry` 注册 Definition；
- `agent_backend_registry` 实例化具体 Adapter 类；
- 未复制已有 Transport 和进程管理代码；
- 能力声明全部有测试证据；
- CLI 未安装或未启用时安全降级；
- 公开服务不会绕过 Owner、Workspace、模型和权限策略；
- 文档能让贡献者只查看该目录就理解其特有行为和已知限制。
