# example-plugin 配置说明

`example-plugin` 是 TCode 插件开发示例，演示配置校验、Tool、Prompt、Event 和声明式 Workflow 注册。

配置定义的最终依据是 `config.schema.json` 和 `plugin.py` 的显式默认值。

## 配置示例

```json
{
  "name": "example-plugin",
  "path": "./plugins/example_plugin",
  "enabled": true,
  "config": {
    "default_prefix": "Echo"
  }
}
```

## 配置项

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `default_prefix` | string | `Echo` | `example_echo` 没有显式传入 `prefix` 时使用的默认前缀。 |

示例：

```json
{
  "default_prefix": "TCode"
}
```

调用：

```json
{
  "echo_text": "hello"
}
```

输出：

```text
TCode: hello
```

如果工具调用显式传入 `prefix`，工具参数优先：

```json
{
  "echo_text": "hello",
  "prefix": "Custom"
}
```

输出：

```text
Custom: hello
```

## 启用后的组件

启用插件后会注册：

- `example_echo` Tool；
- `example_count` Tool；
- `example_guidance` Prompt；
- `tcode.event.config.loaded@1` Event 订阅；
- `example-plugin.echo-review` 两阶段声明式 Workflow；
- Manifest 中声明的示例 Command 和 Agent。

该插件当前不申请文件系统或网络权限。

## 配置校验和重载

- 未配置 `default_prefix` 时使用 `Echo`；
- `config.schema.json` 使用 `additionalProperties: false`，未知字段会被拒绝；
- 手工修改根 `plugins.json` 后，应重启 TCode Server，或显式重新加载插件。
