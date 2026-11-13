---
name: plip-runner
description: 在当前项目中使用 PLIP 做蛋白-配体相互作用分析，或用户提到“跑 plip”“分析相互作用”“看氢键/疏水作用”“给结构出 PLIP 报告”时务必使用。指导 agent 选择必要参数、严格通过 submit_job 异步提交到 plip-mcp，并依据当前包装层、结果目录语义和已知实现限制正确汇报。
when_to_use: >-
  Use when the user wants protein-ligand interaction analysis or a PLIP report from an existing complex structure. Examples: “跑 plip”, “分析相互作用”, “看氢键/疏水作用”, “给结构出 PLIP 报告”.
routing:
  domains: [docking-interaction]
  intents: [protein-ligand-interaction-analysis]
  aliases: [PLIP, 相互作用分析, 氢键分析, 疏水作用, PLIP 报告]
  positive_examples:
    - 分析已有复合物中的氢键和疏水相互作用
    - 给蛋白配体结构生成 PLIP 报告
  negative_examples: [分子对接, docking pose, 分子动力学轨迹]
  required_inputs: [complex_structure]
  conflicts_with: [gnina-runner, simulation-runner]
  skill_role: runner
  priority: 80
---

# PLIP Runner

这个 skill 用于当前项目里的 PLIP 长任务。

适用场景：
- 用户明确说“跑 plip”“做相互作用分析”
- 用户已有复合物结构文件，希望分析非共价相互作用
- 用户想为上游结构或对接结果生成 PLIP 报告

不适用场景：
- 只是解释 PLIP 原理
- 没有结构文件
- 更适合 `gnina`、`simulation` 或别的工具

## 先看哪份信息

优先级如下：

1. 本 skill：决定当前项目里怎么调 PLIP
2. `references/runtime_notes.md`：看当前包装层、外部配置边界和已知实现限制
3. `references/output_semantics.md`：看 `output_path` 应如何解释

## 调用原则

1. 不直接调用 `plip_mcp_server`
2. 始终通过 `mcp_job-protocol-mcp_submit_job` 提交
3. 默认严格异步：提交后的同一轮只返回受理信息，不调用 `get_job_status` / `collect_job_result`
4. 固定使用：
   - `target_server: "plip-mcp"`
   - `target_tool: "plip_mcp_server"`

提交形状应遵循项目约定：

```json
{
  "input_config": {
    "target_server": "plip-mcp",
    "target_tool": "plip_mcp_server",
    "...": "plip 参数"
  }
}
```

## 参数选择

### 最小必要参数

- `file`，或批量输入 `files`

身份与 Workspace 字段由运行时补齐，不要猜造。

### 可移植资源规范（强制）

- 单结构 `file` 使用 ArtifactRef；批量结构 `files` 使用 ArtifactRef 数组
- 收到 Portable reference 后直接提交；不要用 `exec`、`glob`、`find` 或 `read` 搜索 Workspace
- 不要转换成 `/workspace/...` 或宿主机路径；目标 MCP 会在服务器任务目录中物化输入

### 常用参数

- `xml`
- `pymol`
- `pics`
- `verbose`
- `quiet`
- `maxthreads`
- `breakcomposite`
- `altlocation`
- `dnareceptor`
- `keepmod`
- `nohydro`
- `peptides`
- `inter`
- `intra`
- `chains`
- `regions`

### 仅在明确需要时设置

- `stdout`
- `rawstring`
- `gzip`
- `name`
- `model`

### 一般不要主动改

- `nofix`
- `nofixfile`
- `nopdbcanmap`

除非用户明确知道自己在绕过什么结构修复逻辑。

## 当前项目里最容易误解的点

### 1. 当前仓库里看不到统一追踪的 `plip-config.yaml`

代码会尝试读取 `tmcp/funcdir/plip_func/data/plip-config.yaml`，但当前仓库里没有这个文件。  
因此不要臆造镜像、命令模板或默认报告格式。

### 2. 当前包装层是单文件导向

`plip_run_func.py` 会把 `file` 视为单个输入文件上传并运行。  
默认不要把它说成批量目录分析入口。

### 3. `txt` 是独立的文本报告开关

本地当前 `PlipConfig.model_dump()` 保留显式设置的 `txt`；需要文本报告时设置 `txt=true`，XML 则设置 `xml=true`。

因此：
- 不要根据参数或任务成功就断言报告存在，远端版本可能不同，应核对实际产物。
- 结果回传后用 `artifact_query` 按任务列出 XML/TXT，解析返回引用后用 `read` 或 `inspect_file` 检查；不要因摘要未列出而认定报告缺失。

### 4. `output_path` 是结果目录，不是单个报告文件

不要直接把它说成 XML、TXT 或 PyMOL 文件本身。

## 如何理解 output_path

`output_path` 是结果目录。  
目录里可能包含：

- XML
- 图片
- PyMOL session
- 其他报告文件

但具体有哪些，取决于这次参数和运行环境。

## 默认汇报模板

### 首轮提交时

优先给出：

1. 已提交到 `plip-mcp / plip_mcp_server`
2. `LOCAL-*` 或当前 submit 返回的受理标识
3. 本次关键输入
   - `file`
   - 如有则 `xml` / `pymol` / `pics`
4. 必要时的下一步建议

### 后续查结果时

优先给出：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时提醒：具体报告文件要以目录内实际产物为准

不要在没确认目录内容前，直接承诺某个 XML/TXT/PSE 文件一定存在。
