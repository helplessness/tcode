---
name: gnina-runner
description: 在当前项目中使用 Gnina 做分子对接、pose 打分、重打分，或用户提到“跑 gnina”“做 docking”“配体对接到受体”“autobox_ligand”“score pose”时务必使用。指导 agent 选择必要参数、严格通过 submit_job 异步提交到 gnina-mcp，并依据本地包装层的输入 staging、默认输出和结果目录语义正确汇报。
when_to_use: >-
  Use when the user wants Gnina molecular docking, rescoring, or pose scoring, or when the request clearly contains receptor + ligand inputs for docking. Examples: “跑 gnina”, “做分子对接”, “做 docking”, “配体对接到受体”, “autobox_ligand”, “score pose”.
routing:
  domains: [docking-interaction]
  intents: [molecular-docking, pose-scoring]
  aliases: [gnina, 分子对接, docking, 配体对接, autobox_ligand, score pose]
  positive_examples:
    - 使用 receptor 和 ligand 生成 docking pose
    - 对已有 pose 做 Gnina 重打分
  negative_examples: [分子动力学模拟, 分析已有复合物的氢键, PLIP 报告]
  required_inputs: [receptor, ligand]
  conflicts_with: [simulation-runner, plip-runner]
  skill_role: runner
  priority: 80
---

# Gnina Runner

这个 skill 用于当前项目里的 Gnina 长任务。

适用场景：
- 用户明确说“跑 gnina”“做 docking”“做分子对接”
- 用户已经提供 `receptor` / `ligand`，希望生成 docking pose
- 用户要把上游结构结果接到下游对接

不适用场景：
- 只是解释 Gnina 原理
- 没有受体和配体文件，只是泛泛讨论
- 更适合 `plip` 或 `simulation` 的后处理任务

## 先看哪份信息

优先级如下：

1. 本 skill：决定当前项目里怎么调 Gnina
2. `references/runtime_notes.md`：看当前包装层、Docker 命令和已证实的默认行为
3. `references/output_semantics.md`：看 `output_path` 和实际结果文件怎么理解

## 调用原则

1. 不直接调用 `gnina_mcp_server`
2. 始终通过 `mcp_job-protocol-mcp_submit_job` 提交
3. 默认严格异步：提交后的同一轮只返回受理信息，不调用 `get_job_status` / `collect_job_result`
4. 固定使用：
   - `target_server: "gnina-mcp"`
   - `target_tool: "gnina_mcp_server"`

提交形状应遵循项目约定：

```json
{
  "input_config": {
    "target_server": "gnina-mcp",
    "target_tool": "gnina_mcp_server",
    "...": "gnina 参数"
  }
}
```

## 参数选择

### 最小必要参数

- `receptor`
- `ligand`

身份与 Workspace 字段由运行时补齐，不要猜造。

### 可移植资源规范（强制）

- `receptor`、`ligand` 以及可选的 `autobox_ligand` 等文件字段必须直接使用前端提供的 ArtifactRef
- Artifact 不会出现在本地 Workspace；收到 Portable reference 后禁止用 `exec`、`glob`、`find` 或 `read` 搜索文件
- 不要转换成 `/workspace/...`、宿主机路径或二进制；目标 GNINA MCP 会在服务器任务目录中物化文件

示例（正确）：

```json
{
  "input_config": {
    "target_server": "gnina-mcp",
    "target_tool": "gnina_mcp_server",
    "receptor": {"kind": "artifact", "artifact_id": "art_receptor"},
    "ligand": {"kind": "artifact", "artifact_id": "art_ligand"},
    "autobox_ligand": {"kind": "artifact", "artifact_id": "art_ligand"}
  }
}
```

### 常用参数

- `autobox_ligand`
  - 如果用户没单独给，通常可直接复用 `ligand`
- `exhaustiveness`
  - 搜索强度；越大通常越慢
- `num_modes`
  - 返回 pose 数量
- `scoring`
  - 常见为 `vina`
- `autobox_add`
  - 自动建盒时的额外 buffer
- `cpu`
- `device`
- `no_gpu`
- `score_only`
- `minimize`

### 仅在明确需要时设置

- `center_x` / `center_y` / `center_z`
- `size_x` / `size_y` / `size_z`
- `flex`
- `flexres`
- `flexdist_ligand`
- `flexdist`
- `covalent_*`

### 一般不要主动改

- `config`
- `custom_scoring`
- `custom_atoms`
- `factor`
- `force_cap`
- `user_grid`

除非用户明确要求，或你已经确认这些字段对当前 docking 必要。

## 当前项目里最容易误解的点

### 1. `output_path` 是结果目录，不是最终的 docked 文件

当前包装层返回给 agent 的 `output_path` 是 run 目录下的结果目录。  
真正的 docked 文件通常在这个目录里，而不是 `output_path` 本身。

### 2. 输出文件名由包装层自动生成

当前 `gnina_run_func.py` 会根据 `receptor` 文件名生成：

`<receptor_stem>.sdf.gz`

通常不需要用户自己构造输出文件名。

### 3. `autobox_ligand` 不传时，包装层会优先回退到 `ligand`

因此：
- 用户没给单独的 autobox 文件时，不一定要再追问一次
- 但如果 docking 盒子必须来自别的参考配体，才应显式传 `autobox_ligand`

### 4. 包装层会先把输入文件复制到 run 目录

不要把 Artifact 服务在 MCP 服务器上的物化路径或容器内 `/data/...` 路径向用户汇报；对外只返回 ArtifactRef/BundleRef。

## 如何理解 output_path

`output_path` 是结果目录。  
真正常见的 docking 结果通常是其中的：

- `.sdf.gz`
- 可能还有日志或中间文件

如果用户没有明确要求，不要展开整个目录树。  
优先说明它是结果目录，并在需要时指出常见候选文件名。

## 默认汇报模板

### 首轮提交时

优先给出：

1. 已提交到 `gnina-mcp / gnina_mcp_server`
2. `LOCAL-*` 或当前 submit 返回的受理标识
3. 本次使用的关键输入
   - `receptor`
   - `ligand`
   - 如有则 `autobox_ligand`
4. 必要时的下一步建议

### 后续查结果时

需要比较 Pose 评分时，优先使用 Job Protocol 的只读 `list_result_items`，携带当前 `job_id` 和 `result_digest`；按 `next_offset` 分页，并检查 `source_complete`，不把部分候选称为全局最优。使用规范 `item_id` 调用 `read_result_item` 回取指定 Pose 的 SDF。`load_docking_structures` 用于三维展示，不用于向模型灌入完整受体及全部 Pose 来做评分比较；无需重新提交计算。

优先给出：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时指出常见结果文件通常是 `<receptor_stem>.sdf.gz`

避免在没有用户明确要求时，直接展开所有 pose 或整段日志。
