---
name: simulation-runner
description: 在当前项目中使用 simulation-mcp 做 OpenMM 分子动力学模拟，或用户提到“跑模拟”“跑 simulation”“做 MD”“给 protein + ligand 做动力学”时务必使用。指导 agent 选择必要参数、严格通过 submit_job 异步提交到 simulation-mcp，并依据当前本地 OpenMM 包装层、结果目录布局和返回 schema 正确汇报。
when_to_use: >-
  Use when the user wants OpenMM molecular dynamics for protein-ligand systems or already has protein + ligand files and wants trajectories. Examples: “跑模拟”, “跑 simulation”, “做 MD”, “给 protein + ligand 做动力学”.
routing:
  domains: [simulation]
  intents: [molecular-dynamics]
  aliases: [OpenMM, 分子动力学, 做 MD, 跑模拟, simulation, 生成轨迹]
  positive_examples:
    - 对蛋白配体复合物跑分子动力学并生成轨迹
    - 使用 OpenMM 模拟 protein 和 ligand
  negative_examples: [分子对接, docking pose, PLIP 相互作用报告]
  required_inputs: [protein, ligand]
  conflicts_with: [gnina-runner, plip-runner]
  skill_role: runner
  priority: 80
---

# Simulation Runner

这个 skill 用于当前项目里的分子动力学模拟任务。

适用场景：
- 用户明确说“跑模拟”“做 MD”“跑 simulation”
- 用户已有蛋白结构和配体文件，希望生成轨迹
- 用户想基于现有复合物继续做动力学

不适用场景：
- 只是解释分子动力学原理
- 没有 `protein` 和 `ligand` 输入
- 更适合 `gnina`、`plip` 或别的分析工具

## 先看哪份信息

优先级如下：

1. 本 skill：决定当前项目里怎么调 simulation
2. `references/runtime_notes.md`：看当前 OpenMM 封装和已证实的参数/返回边界
3. `references/output_semantics.md`：看 `output_path` 和结果文件布局

## 调用原则

1. 不直接调用 `simulation_run_mcp_server`
2. 始终通过 `mcp_job-protocol-mcp_submit_job` 提交
3. 默认严格异步：提交后的同一轮只返回受理信息，不调用 `get_job_status` / `collect_job_result`
4. 固定使用：
   - `target_server: "simulation-mcp"`
   - `target_tool: "simulation_run_mcp_server"`

提交形状应遵循项目约定：

```json
{
  "input_config": {
    "target_server": "simulation-mcp",
    "target_tool": "simulation_run_mcp_server",
    "...": "simulation 参数"
  }
}
```

## 参数选择

### 最小必要参数

- `protein`
- `ligand`

身份与 Workspace 字段由运行时补齐，不要猜造。

### 可移植资源规范（强制）

- `protein` 和 `ligand` 都必须直接使用前端提供的 ArtifactRef
- 收到 Portable reference 后直接提交；不要用 `exec`、`glob`、`find` 或 `read` 搜索 Workspace
- 不要转换成 `/workspace/...` 或宿主机路径；目标 MCP 会在服务器任务目录中物化输入

### 常用参数

- `steps`
- `interval`
- `temperature`
- `solvate`
- `padding`
- `water_model`
- `positive_ion`
- `negative_ion`
- `ionic_strength`
- `equilibration_steps`

### 仅在明确需要时设置

- `step_size`
- `friction_coeff`
- `protein_force_field`
- `ligand_force_field`
- `water_force_field`
- `no_neutralize`

### 一般不要主动改

- `water_model` 以外的复杂力场组合
- 平台相关环境假设

除非用户明确要求或你已经确认环境兼容。

## 当前项目里最容易误解的点

### 1. 当前返回 schema 没有 `run_id`

`simulation_run_func.py` 返回的是：

- `result_code`
- `output_path`

当前顶层结果里没有 `run_id`。  
不要在结果汇报里凭空加一个 `run_id`。

### 2. 当前 run manager 主要使用 `project_id` / `task_id`

`SimulationConfig` 虽然定义了 `user_id`，但当前 `SimulationRunManager` 主要用：

- `project_id`
- `task_id`

来定位输入输出路径。  
提交时仍建议按统一约定传 `user_id`，但不要夸大它在当前本地实现中的作用。

### 3. 这套实现是本地 OpenMM 包装，不是当前仓库里可见的 Docker YAML 流程

不要把它说成和 `boltz` / `gnina` 完全一样的 Docker 长任务封装。

### 4. `output_path` 是结果目录

它不是单个轨迹文件，也不是单个最小化结构文件。

## 如何理解 output_path

`output_path` 是结果目录。  
当前实现成功时通常会写出：

- `<protein_stem>_complex.pdb`
- `<protein_stem>_minimised.pdb`
- `<protein_stem>_traj.dcd`

## 默认汇报模板

### 首轮提交时

优先给出：

1. 已提交到 `simulation-mcp / simulation_run_mcp_server`
2. `LOCAL-*` 或当前 submit 返回的受理标识
3. 本次关键输入
   - `protein`
   - `ligand`
   - 如有则 `steps` / `solvate`
4. 必要时的下一步建议

### 后续查结果时

优先给出：

1. 是否完成
2. `output_path`
3. 必要时补充常见产物通常包括 `*_complex.pdb`、`*_minimised.pdb`、`*_traj.dcd`

不要在没有真实返回值时承诺 `run_id`。
