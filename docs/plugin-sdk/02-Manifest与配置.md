# Manifest 与配置

每个插件包根目录必须包含 `plugin.json`。BenchThread 只支持 Manifest v2，不维护旧格式兼容层。

## 1. 完整示例

```json
{
  "name": "community-echo",
  "version": "0.1.0",
  "apiVersion": "1.0",
  "sdk": ">=1.0.0,<2.0.0",
  "python": ">=3.11",
  "runtime": "python",
  "entry": "plugin.py:Plugin",
  "provides": ["community.echo@1"],
  "requires": [],
  "optional": [],
  "permissions": [],
  "scope": "workspace",
  "configSchema": "config.schema.json",
  "requestedTrust": "isolated",
  "description": "社区回显插件。",
  "author": "Example Team",
  "homepage": "https://example.invalid/community-echo",
  "contributes": ["tools"]
}
```

## 2. 必填字段

| 字段 | 规则 | 说明 |
| --- | --- | --- |
| `name` | 小写字母、数字、`.`、`_`、`-`，最多 128 字符 | 稳定插件 ID，必须与 `PluginMetadata.name` 一致 |
| `version` | SemVer，如 `1.2.3` | 必须与 `PluginMetadata.version` 一致 |
| `apiVersion` | 固定 `1.0` | Plugin API 主次版本 |
| `sdk` | PEP 440 范围 | 必须包含当前 SDK `1.4.0` |
| `python` | PEP 440 范围 | 必须包含实际 Host Python 版本 |
| `runtime` | 固定 `python` | 当前唯一插件语言 |
| `entry` | `<相对.py文件>:<符号>` | 例如 `plugin.py:Plugin` |
| `provides` | ServiceKey 数组 | 该包提供的版本化能力 |
| `requires` | ServiceKey 数组 | 缺少任一能力时拒绝加载 |
| `optional` | ServiceKey 数组 | 缺失时允许继续加载 |
| `permissions` | 权限对象数组 | 权限申请，不是授权 |
| `scope` | 固定 `workspace` | 当前唯一公开 Scope |
| `configSchema` | 相对 JSON 路径或 `null` | Draft 2020-12 Schema |
| `requestedTrust` | `builtin`、`trusted`、`isolated`、`quarantined` | 信任请求，不决定最终执行模式 |

## 3. 可选字段

| 字段 | 说明 |
| --- | --- |
| `description` | 给管理员和开发者看的简短描述 |
| `author` | 作者或团队名称 |
| `homepage` | 项目主页 |
| `contributes` | 当前允许 `tools`、`prompt`、`events`、`verifiers` |
| `commands` | 包内 Command 定义文件或目录 |
| `skills` | 包内 Skill 文件或目录 |
| `agents` | 包内 Agent JSON 文件或目录 |
| `mcpServers` | 包内 MCP 元数据文件或目录 |
| `workflows` | 包内声明式 Workflow JSON 文件或目录 |

社区隔离 Worker 的可执行贡献只接受 `contributes: ["tools"]`，并拒绝 Command、Skill、Agent、MCP、Prompt、Event 和 Verifier。Verifier 首版只允许宿主精确准入的 `controlled-host` 插件贡献；`workflows` 是由宿主解释的无代码声明，不进入插件 Worker 执行，因此未知社区插件也可以使用，但仍必须通过独立 Workflow Schema、安全收窄和显式启动边界。

未知插件虽然不能贡献自己的 Skill 或 MCP Server，但可以通过 `tools.invoke` 调用 Host 已注册且管理员精确授权的 Tool/MCP Tool；调用现有 Skill 时还必须通过 `skills.invoke` 的第二层授权。

## 4. ServiceKey

ServiceKey 格式为：

```text
命名空间.能力名称@主版本
```

示例：

```json
{
  "provides": ["community.echo@1"],
  "requires": ["tcode.tools@1"],
  "optional": ["tcode.events@1"]
}
```

`requires` 与 `optional` 不能包含同一个 ServiceKey。ServiceKey 用于依赖排序和可用性判断，不授予访问内部服务的权限。

## 5. 配置 Schema

推荐为每个插件提供 Draft 2020-12 JSON Schema：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "prefix": {
      "type": "string",
      "minLength": 1,
      "maxLength": 40,
      "default": "Echo"
    },
    "uppercase": {
      "type": "boolean",
      "default": false
    }
  },
  "additionalProperties": false
}
```

Host 会在调用 `start(ctx, config)` 前校验配置。几个重要语义：

- 配置根必须是对象；
- 未知字段应通过 `additionalProperties: false` 拒绝；
- Schema 中的 `default` 用于描述和工具，但验证过程不会把默认值自动写回配置；
- 插件仍需在代码中为可选字段提供显式默认值；
- 不要把 Secret 默认值写进 Schema、Manifest 或仓库。

读取配置：

```python
async def start(self, ctx: PluginContext, config: Mapping[str, Any]) -> None:
    """读取已经通过 JSON Schema 校验的插件配置。"""

    prefix = str(config.get("prefix") or "Echo")
    uppercase = bool(config.get("uppercase", False))
```

## 6. 路径安全

以下路径必须是包内相对路径：

- `entry`；
- `configSchema`；
- `commands`、`skills`、`agents`、`mcpServers`、`workflows`。

绝对路径、`..` 逃逸和指向包外的符号链接都会在导入插件代码之前被拒绝。

## 7. 常见错误

- `apiVersion` 写成 `0.1`：旧 API 不兼容，改为 `1.0`。
- 忘记 `sdk` 或 `python`：这两个范围是 v1 必填字段。
- `entry` 使用模块点号：必须写文件路径，如 `src/plugin.py:Plugin`。
- `version` 与代码中的 Metadata 不一致：加载会失败关闭。
- 添加未定义字段：Manifest 使用 `additionalProperties: false`，拼写错误不会被静默忽略。
- 在 `requestedTrust` 中写自定义值：只允许四个枚举值，而且它始终只是请求。
