# MCP 动态 Header：配置、调用与隔离

更新：2026-10-23。适用于常规 MCP、自定义文件传输异步目标和插件异步目标。此能力增加的是请求上下文，不改变现有工具权限、文件读取授权、人工确认或任务结果协议。

## 1. 普通用户不需要知道 Header

配置者声明“X-Project-ID 对应项目编号，作用是选择项目数据”；Agent 看到的是项目编号变量及其业务说明。

用户说“查询 PPK2 项目的结果”，Agent 填写 `project_id=PPK2`。必填信息缺失或存在歧义时，应先询问用户，而不是猜测或沿用上次值。模型提取不是确定性认证机制：程序会验证类型、长度、未知字段等，但不能证明模型准确理解了自然语言。

## 2. 界面配置

### 常规 MCP

1. 进入“设置 → MCP 服务”，添加或编辑服务。
2. 展开“高级设置”。
3. 固定值和环境变量继续使用原有请求头入口。环境变量格式为 `${ENV_NAME}`，可填写 `Bearer ${MY_TOKEN}`；变量必须存在于 TCode 后端环境中。
4. 展开“本次调用变量 Header”，点击“添加动态请求头”。
5. 填写 Header 名称、业务变量名和业务含义，例如 `X-Project-ID`、`project_id`、“本次操作所属项目编号，用于选择项目数据”。
6. 可填写提取说明，例如“从用户明确指定的项目中提取”。默认必填、最大 128 字符；长度可在高级详情中调整为 1..4096。
7. 保存配置。保存／重新加载会执行现有连接发现流程，不会提交计算；不想访问服务时不要点击保存并连接或检查连接。

### 插件异步 MCP

1. 安装并加载声明 `asyncTargets` 的插件。
2. 在“插件异步 MCP”目标卡片点击“配置连接”。
3. 配置地址，在同名动态 Header 编辑区设置变量规则。
4. 点击“保存草稿（不连接）”可以只保存本地地址、令牌变量名和动态规则，不需要勾选发送授权，也不会访问网关、远端 MCP 或读取令牌值。卡片会显示“本地草稿已保存，尚未应用”。
5. 草稿与已生效配置分开保存：草稿的添加、修改、删除规则都不改变现有注册版本、授权和已有任务。要真正应用草稿，仍须重新明确勾选发送授权，再点击“启用／更新异步目标”。检查连接也仍是独立的联网操作。
6. 声明配置、草稿、授权和注册版本保存在当前工作区原有的异步目标存储中，不要求用户手动改 `plugins.json`。

动态规则按需展开，不常驻占用服务列表空间；字段均有文字标签。固定／环境变量值仍不回显，动态规则的说明和变量名可重新编辑。移除全部动态规则后保存即关闭此能力，已有任务快照不删除。

## 3. 配置文件格式

常规 MCP 的单个服务条目增加可选 `dynamic_headers`：

```json
{
  "url": "https://compute.example.com/mcp",
  "transport": "streamable-http",
  "invocation_mode": "direct",
  "headers": {
    "X-Client": "tcode",
    "Authorization": "Bearer ${MY_TOKEN}"
  },
  "dynamic_headers": {
    "X-Project-ID": {
      "variable": "project_id",
      "description": "本次操作所属的项目编号，用于选择项目数据",
      "extraction_hint": "从用户明确指定的项目中提取，例如 PPK2；存在歧义时询问用户",
      "required": true,
      "max_length": 128
    }
  }
}
```

配置文件保存规则，不保存“全局当前项目”。`description` 必填，最多 1000 字符；最多 32 个动态 Header。相同变量可供多个 Header 使用，但说明和约束必须一致。第一版没有任意表达式、脚本执行或正则模板。

插件开发者可以在 `async-targets.json` 的每个 target 对象上声明同样的 `dynamic_headers`，与 `id`、`connection`、`execution`、`tools` 并列，作为初始规则。用户在专用设置中编辑后的规则保存在绑定配置，插件声明的远端 `inputSchema` 不加入宿主字段。

## 4. Agent 和远端各自收到什么

TCode 在直连工具的宿主 Schema 中加入 `_tcode_context`，保留原有业务字段。例如：

```json
{
  "query": "查询结果",
  "_tcode_context": {"project_id": "PPK2"}
}
```

变量的业务含义、提取说明、必填要求和长度限制会提供给 Agent。Token 和固定认证 Header 不提供给模型。

TCode 校验后，远端 HTTP 收到 `X-Project-ID: PPK2`，远端工具 arguments 仍然是 `{"query":"查询结果"}`。宿主字段不混入工具参数，也不被当成本地输入文件上传。

异步工具仍先 `describe_target_tool`。TCode 会在返回中增加 `call_context_schema` 和填写说明，即使 `detail=compact` 也能了解要求。然后使用现有 `submit_job` 简化格式：

```json
{
  "input_config": {
    "target_server": "目标服务或已批准的插件目标 ID",
    "target_tool": "目标工具",
    "arguments": {"query": "查询结果"},
    "_tcode_context": {"project_id": "PPK2"}
  }
}
```

上面的 `arguments` 包装适用于插件及注册的自定义目标；内置领域工具继续使用原有 flat 业务参数形式，将 `_tcode_context` 放在相同层级即可。收到 LOCAL-* 后继续遵循现有异步等待约定，不重复提交。

若工具原本已有 `_tcode_context` 参数，启用动态规则会拒绝冲突，不能覆盖原参数。没有启用动态 Header 的服务保留原有参数契约。

## 5. 并行、排队和恢复

- 动态直连调用使用独立 SDK 会话，初始化、工具调用、会话清理携带同一组 Header；不修改共享连接和进程环境。
- 未启用动态 Header 的服务保留原有连接复用。
- 异步提交生成独立 `call_context`，与远端业务 `arguments` 分开进入本地队列及 Job Protocol 的 `job_request.json`。
- 注册目标同时记录版本；后续执行、断线恢复、查询、结果和取消沿用该任务的上下文。注册版本必须存在且属于同一所有者；停用目标仍阻止新提交，不绕过原有审批。
- 重试原任务沿用原快照；修改变量应发起新的提交。动态值和规则／版本参加去重，空格等原始值差异也不会被路径归一化吞掉。
- 上下文为了恢复而持久保存于既有任务记录，属于任务数据，不是全局默认值；不要把密钥放在业务变量中。

## 6. 必须同步升级的组件

常规动态 Header 需要更新并重启 TCode 后端和界面。异步支持还需要同步更新相邻 TMCP 代码并重启**用户本地 Job Protocol**；领域 MCP 只需能读取所配置的 Header，一般不必改工具参数。

本次没有新增端口、环境变量或独立提取服务。沿用现有桌面托管或手动 Job Protocol 启动方式，不要同时启动两份占用同一端口的网关。

为避免旧网关静默丢弃上下文，新 TCode 会检查 Job Protocol 的动态上下文能力；异步目标注册也必须回传已确认的动态规则。未支持时拒绝提交／启用并提示同步更新，不自动降级为无 Header 调用。

## 7. 安全与兼容边界

- Header 名称不区分大小写地检查重复，固定与动态不能同名。
- 禁止由自然语言变量设置认证、Cookie、uid、用户／租户身份、MCP 会话、协议及关键传输 Header。
- 既有固定 `uid` 或 `${ENV}` 身份配置不因此失效；只是不能改成模型可任意填写的动态身份。
- 必填缺失时阻止请求；可选缺失时省略；空值、控制字符、超长值、未知变量会被拒绝。
- Host 与 Job Protocol 各自校验；注册目标的快照规则须与批准版本一致。
- 业务 Header 不是权限凭据。不要因为用户在自然语言中写了某个项目或身份，就假设其拥有访问权限；服务端仍应执行自己的访问控制。
- 发现工具／检查连接仍使用固定认证，不猜测业务变量。若服务要求在工具发现时就必须提供动态业务值，目前不能凭空发现工具，需服务端允许已认证的 Schema 发现；不提供“自动填个项目试试”的兜底。
- 未改变计算型 MCP 必须异步执行的现有路由策略，也未放宽 Artifact／文件读取授权。

## 8. 本地验证与限制

测试位于 `tests/test_mcp_dynamic_headers.py`：规则、模型 Schema、配置读写与删除、插件授权和版本绑定、20 个 Mock 并发调用、回环地址真实 SDK 初始化及 A/B 并行工具调用、异步 envelope／去重、Mock 查询／结果／取消、输入文件转换隔离、旧网关与暂停策略。

前端测试位于 `web/src/components/DynamicHeadersEditor.test.tsx`，并运行服务／任务分层回归、类型检查与构建。

2026-10-23 本地最终结果：相关 Python 回归 224 项通过，另有 9 个子测试通过；前端 6 项通过；TypeScript 类型检查、Vite 构建和改动文件差异检查通过。Python 输出包含既有弃用警告，构建仍有既有的大 chunk 提示。

首轮使用本地回环模拟服务和 Mock，未连接远端 MCP／Artifact，未启动真实异步计算，也没有验证真实模型对自然语言的提取准确率。




后续已在原账号、原数据目录的正常桌面再验收通过，见同一验收文档第 9 节。用户授权的精确回环地址例外仅在该轮测试临时使用，测试后代码和配置已完整恢复、重新启动正常桌面，远端暂停保持开启；这不是新增的常驻配置能力。

可在 TCode 根目录执行：

```bash
PYTHONPATH=.. .desktop-venv/bin/python -m pytest tests/test_mcp_dynamic_headers.py -q
npm --prefix web test -- src/components/DynamicHeadersEditor.test.tsx src/components/ServiceTaskDisclosure.test.tsx
npm --prefix web run build
```

测试 Python 需安装项目依赖并能导入相邻 `tmcp`；`.desktop-venv` 是本机验证环境示例，不是要求所有用户使用该目录。
