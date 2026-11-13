# Runtime Notes

这份文档只记录当前仓库里能直接证实的 simulation 运行行为。

## 主要来源

- `../../../../tmcp/funcdir/simulation/utils.py`
- `../../../../tmcp/funcdir/simulation/simulation_run_func.py`
- `../../../../tmcp/funcdir/simulation/simulation_run_manager.py`

## 当前已证实的运行事实

### 固定路由

- server: `simulation-mcp`
- tool: `simulation_run_mcp_server`

### 输入形状

当前最稳妥的最小输入是：

- `protein`
- `ligand`
- `project_id`
- `task_id`

提交时也建议带上 `user_id`，但当前 run manager 主要消费的是 `project_id` / `task_id`。

### 当前实现类型

这套实现直接使用 OpenMM / OpenFF / RDKit 的本地包装逻辑。  
当前仓库里没有与 `boltz`、`gnina` 对应的中心化 simulation Docker YAML 供此技能引用。

### 结果返回

`simulation_run_func.py` 最终返回：

- `result_code`
- `output_path`

当前顶层 schema 里没有 `run_id`。

## 参数使用建议

### 常见且稳定

- `steps`
- `interval`
- `temperature`
- `solvate`
- `padding`
- `water_model`
- `ionic_strength`
- `equilibration_steps`

### 不要主动假设

- 平台环境变量
- 高级力场组合一定可用
- `user_id` 一定参与路径布局

如果用户只要常规 MD，优先保持参数集简单。
