---
name: ligandmpnn-runner
description: 在当前项目中使用 LigandMPNN 做蛋白序列设计，或用户提到“跑 ligandmpnn”“做序列设计”“根据复合物设计序列”“给 pdb 生成序列”时务必使用。指导 agent 选择必要参数、严格通过 submit_job 异步提交到 ligandmpnn-mcp，并依据本地 run 包装层、Docker 配置和结果目录语义正确汇报。
when_to_use: >-
  Use when the user wants LigandMPNN sequence design from a protein or protein-ligand complex structure. Examples: “跑 ligandmpnn”, “做序列设计”, “根据复合物设计序列”, “给 pdb 生成序列”.
routing:
  domains: [protein-sequence]
  intents: [protein-sequence-design]
  aliases: [LigandMPNN, 序列设计, 设计蛋白序列, 给 PDB 生成序列]
  positive_examples:
    - 根据蛋白配体复合物设计候选序列
    - 给 PDB 结构生成蛋白序列
  negative_examples: [蛋白结构生成, motif scaffolding, 突变评分]
  required_inputs: [structure]
  conflicts_with: [rfdiffusion-runner, multi-model-sequence-mutation-scoring]
  skill_role: runner
  priority: 80
---

# LigandMPNN Runner

这个 skill 用于当前项目里的 LigandMPNN 长任务。

适用场景：
- 用户明确说“跑 ligandmpnn”“做序列设计”
- 用户有蛋白或蛋白-配体复合物结构，希望生成候选序列
- 用户后续要把 LigandMPNN 结果接到 `boltz` 或别的下游步骤

不适用场景：
- 只是解释 LigandMPNN 原理
- 用户要做的是当前未接入的 score-only 流程
- 输入不是结构文件，或更适合别的工具

## 先看哪份信息

优先级如下：

1. 本 skill：决定当前项目里怎么调 LigandMPNN
2. `references/runtime_notes.md`：看当前 run 封装、Docker 模板和最重要的默认行为
3. `references/output_semantics.md`：看返回的 `output_path` 到底指向哪里

## 调用原则

1. 不直接调用 `ligandmpnn_run_mcp_server`
2. 始终通过 `mcp_job-protocol-mcp_submit_job` 提交
3. 默认严格异步：提交后的同一轮只返回受理信息，不调用 `get_job_status` / `collect_job_result`
4. 固定使用：
   - `target_server: "ligandmpnn-mcp"`
   - `target_tool: "ligandmpnn_run_mcp_server"`

提交形状应遵循项目约定：

```json
{
  "input_config": {
    "target_server": "ligandmpnn-mcp",
    "target_tool": "ligandmpnn_run_mcp_server",
    "...": "ligandmpnn 参数"
  }
}
```

## 参数选择

### 最小必要参数

- `tools_input`

身份与 Workspace 字段由运行时补齐，不要猜造。

### 可移植资源规范（强制）

- `tools_input` 必须使用 PDB 文件的 ArtifactRef；其他文件型参数同样使用 ArtifactRef，批量字段使用 ArtifactRef 数组
- 收到 Portable reference 后直接提交；不要用 `exec`、`glob`、`find` 或 `read` 在 Workspace 搜索同名文件
- 不要转换成 `/workspace/...` 或宿主机路径；目标 MCP 会在服务器任务目录中物化文件

### 常用参数

- `model_type`
  - 常见为 `protein_mpnn` 或 `ligand_mpnn`
- `batch_size`
- `number_of_batches`
- `temperature`
- `seed`
- `chains_to_design`
- `fixed_residues`
- `redesigned_residues`
- `omit_AA`
- `bias_AA`

### 仅在明确需要时设置

- `fixed_residues_multi`
- `redesigned_residues_multi`
- `bias_AA_per_residue`
- `bias_AA_per_residue_multi`
- `omit_AA_per_residue`
- `omit_AA_per_residue_multi`
- `symmetry_residues`
- `symmetry_weights`
- `transmembrane_*`
- `parse_these_chains_only`

### 一般不要主动改

- `checkpoint_*`
- `pdb_path_multi`
- `file_ending`
- `zero_indexed`
- `ligand_mpnn_use_atom_context`
- `ligand_mpnn_use_side_chain_context`
- `ligand_mpnn_cutoff_for_score`

除非用户明确要求，或你已经确认这些字段是当前设计任务的关键。

## 当前项目里最容易误解的点

### 1. 当前远端暴露的是 run 工具，不是 score 工具

仓库里虽然还有 `ligandmpnn_score_*` 代码，但当前接入服务固定是：

- `ligandmpnn-mcp`
- `ligandmpnn_run_mcp_server`

不要把 score 流程当成当前默认入口。

### 2. `tools_input` 实际上走的是 `--pdb_path`

当前 run manager 的 Docker 模板是：

`run.py --pdb_path {pdb_path} --out_folder {out_folder} {additional_args}`

因此最稳妥的输入仍然是单个结构文件。

### 3. `output_path` 返回的是 `seqs` 目录

当前 `ligandmpnn_run_func.py` 最终返回的是：

`run_paths.result.seqs.path`

也就是说，`output_path` 不是整个 run 根目录，而是更偏向序列结果目录。

### 4. `out_folder` 通常不需要用户自己传

包装层会把输出目录绑定到当前 run 的结果目录。  
默认不要主动让用户传 `out_folder`。

## 如何理解 output_path

`output_path` 通常指向序列结果目录，里面常见的是：

- `.fa`
- `.fasta`

如果用户没有明确要求，不要展开整个目录树；优先说明这是序列结果目录即可。

## 默认汇报模板

### 首轮提交时

优先给出：

1. 已提交到 `ligandmpnn-mcp / ligandmpnn_run_mcp_server`
2. `LOCAL-*` 或当前 submit 返回的受理标识
3. 本次关键输入
   - `tools_input`
   - 如有则 `model_type`
4. 必要时的下一步建议

### 后续查结果时

优先给出：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时说明它通常是 `seqs` 目录，常见文件为 `.fa` / `.fasta`

不要在用户没要求时，把所有序列全文贴出来。
