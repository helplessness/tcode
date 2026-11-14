# 插件声明式 Workflow

SDK `1.2.0` 起，插件可以在包内携带声明式 Workflow。Workflow JSON 和 Markdown Prompt 由 Host 在插件入口导入前校验，运行时仍由 BenchThread 核心 Workflow 引擎执行；插件不能借此注入 Python 回调、自动启动任务或绕过宿主安全管线。

## 1. 适用场景

适合：

- 将多个已注册 Tool 编排为稳定的顺序或 DAG；
- 为每个阶段提供独立 Prompt、输入输出合同、验证器和重试策略；
- 随插件安装和卸载流程模板；
- 让社区插件复用宿主 Workflow 的运行记录、阶段状态和模板快照。

不适合：

- 在 Workflow JSON 中执行任意 Python；
- 绕过 Tool Permission、Approval、Policy 或 Sandbox；
- 安装后自动启动流程；
- 引用插件包外的 Prompt 或运行文件。

## 2. 目录结构

推荐每个 Workflow 使用独立目录：

```text
my-plugin/
├── plugin.json
├── plugin.py
└── workflows/
    └── echo-review/
        ├── workflow.json
        └── prompts/
            ├── prepare_echo.md
            └── review_echo.md
```

在 Manifest 中声明目录：

```json
{
  "workflows": ["workflows"]
}
```

`workflows` 是独立的声明字段，不应写进 `contributes`。它既可指向单个 JSON，也可指向目录；目录会发现子目录中的 `workflow.json` 和目录第一层的其他 JSON。一个插件最多声明 32 个模板。

## 3. 最小模板

```json
{
  "template_key": "echo-review",
  "display_name": "回显与复核",
  "domain": "example",
  "enabled": true,
  "stages": [
    {
      "stage_id": "prepare",
      "depends_on": [],
      "label": "生成结果",
      "owner": "build",
      "call_type": "agent",
      "prompt_file": "prepare.md",
      "allowed_tools": ["my_echo"],
      "required_skills": [],
      "input_contract": {},
      "output_contract": {},
      "expected_artifacts": [],
      "success_checks": [],
      "validators": [{"type": "non_empty"}],
      "retry_policy": {"max_attempts": 2},
      "failure_policy": {"action": "block"},
      "fallback_stage_ids": [],
      "review_gate": false,
      "metadata": {}
    }
  ],
  "metadata": {
    "version": "1.0.0",
    "prompt_dir": "prompts",
    "routing": {
      "enabled": true,
      "auto_start": false
    }
  }
}
```

完整可运行参考位于 `plugins/example_plugin/workflows/echo-review/`。

## 4. 名称与生命周期

插件 JSON 中的 `template_key` 是本地名称，必须匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`。Host 加载后自动加插件命名空间：

```text
example-plugin + echo-review
→ example-plugin.echo-review
```

这样可以避免两个社区插件抢占同一个模板键。模板注册与插件实例绑定：

- 插件发布成功后，模板才进入 `WorkflowTemplateRegistry`；
- 任一后续组件发布失败，模板与同一事务中的其他贡献一起回滚；
- 插件卸载后，模板立即移除，不能再创建新运行；
- 已创建运行保存完整 `template_snapshot`，插件卸载不破坏历史运行的审计和恢复数据。

## 5. 安全边界

Host 在导入 `plugin.py` 前执行以下检查：

- Manifest 和 Workflow JSON 都符合 Draft 2020-12 Schema；
- 所有声明、JSON、Prompt 和符号链接解析后仍位于插件包内；
- 单个 JSON 不超过 512 KiB，单个 Prompt 不超过 120,000 字节；
- 每个插件最多 32 个模板，每个模板最多 32 个阶段；
- 阶段 ID 唯一，依赖存在，依赖图无环；
- `call_type` 仅允许 `agent`、`subagent`；
- Prompt 必须存在且不允许绝对路径或 `..`；
- `metadata.routing.auto_start` 必须为 `false`；
- Host 会再次强制写入 `auto_start: false` 和 `requires_explicit_confirmation: true`。

Workflow 的 `allowed_tools` 只是阶段允许范围，不是授权。实际 Tool 调用仍进入统一参数验证、Policy、Permission、Approval、Sandbox 和结果收口管线。未知社区插件即使运行在容器中，也只能贡献声明数据，不能让 Host 导入其私有 Workflow 执行代码。

## 6. 输入合同与执行调用

Workflow 顶层 `input_contract` 决定启动前需要哪些输入。插件应显式区分必填字段和可选字段：

```json
{
  "input_contract": {
    "required": [
      {
        "field": "text",
        "question": "请输入需要处理的文本。"
      }
    ],
    "optional": [
      {
        "field": "review_note",
        "question": "是否补充复核说明？",
        "ask": true
      },
      "output_format"
    ]
  }
}
```

解释规则：

- 未声明 `required` 或 `optional` 时按空集合处理，不会继承任何 Protein 领域默认字段；
- `required` 中缺失的字段会通过宿主 Question 机制询问；
- 普通 `optional` 字段默认不询问，只有对象形式且设置 `ask: true` 或 `ask_optional: true` 才询问；
- 实际不可缺少的字段必须放入 `required`，不要依赖可选字段隐式阻塞流程。

Agent 或 Host 调用时，把插件字段放入 `input_payload`：

```json
{
  "template_key": "example-plugin.echo-review",
  "input_payload": {
    "text": "hello"
  },
  "continue_existing": false,
  "auto_continue": true,
  "max_auto_stages": 4
}
```

`review_gate: true` 表示该阶段承担独立复核语义。输出应尽量以明确的 `PASS`、`REVISE` 或 `ESCALATE` 开头；示例插件在 `PASS` 后会提交阶段，若它是最后阶段则 Run 进入 `completed`。不要在同一结论里用含糊方式混写多个决策词；“无需 REVISE/返工”虽然已能正确识别，结构化 Marker 仍是高风险流程的首选。

默认构建 Agent 可以在用户明确选择模板后调用 `workflow_run`，但这不是插件授权扩张：模板仍需启用，`auto_start` 仍固定为 `false`，每个阶段的 Tool 仍经过统一参数验证、Policy、Permission、Sandbox 和 `allowed_tools` 边界。

## 7. 校验与测试

```bash
python scripts/plugin.py validate ./plugins/example_plugin
python scripts/plugin.py test ./plugins/example_plugin
python scripts/plugin.py doctor ./plugins/example_plugin
```

建议插件测试至少覆盖：

1. 完整名称能从模板列表读取；
2. Prompt 正文已加载；
3. 能按完整名称创建运行；
4. 运行元数据包含 `template_snapshot`；
5. 插件卸载后模板消失；
6. 已创建运行的快照仍可读取；
7. `auto_start: true`、依赖环、缺失 Prompt 和包外路径均被拒绝。

仓库中的真实合同测试位于 `tests/test_plugin_sdk_v1.py`。

## 8. 当前限制

- Workflow 只支持 Host 声明式阶段，不支持插件自定义阶段执行器；
- `call_type` 暂只开放 `agent` 和 `subagent`；
- 安装或加载不会自动开始 Workflow，必须由用户或受控宿主入口明确选择；
- 插件卸载后不能新建对应运行，如需长期可用应保持插件启用；
- Workflow 引用的 Tool、Skill 仍需在目标工作区真实可用，Schema 校验不会替代运行时能力检查。
