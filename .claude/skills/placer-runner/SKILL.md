---
name: placer-runner
description: 在当前项目中使用 PLACER 做小分子放置、局部结构补全或用户提到“跑 placer”“用 placer 处理 cif/pdb”“做 ligand placement”“做原子级补全”时务必使用。指导 agent 选择必要参数、严格通过 submit_job 异步提交到 placer-mcp，并依据当前 CLI 包装层、输入 staging 和结果目录语义正确汇报，同时避免臆造仓库里不存在的运行默认值。
when_to_use: >-
  Use when the user wants PLACER ligand placement, atomic completion, or structure completion from cif, pdb, pdbqt, sdf, or related structure directories. Examples: “跑 placer”, “用 placer 处理 cif/pdb”, “做 ligand placement”, “做原子级补全”.
routing:
  domains: [protein-structure]
  intents: [ligand-placement, atomic-completion]
  aliases: [PLACER, ligand placement, 小分子放置, 原子级补全, 结构补全]
  positive_examples:
    - 用 PLACER 对 CIF 结构做小分子放置
    - 对 PDB 做原子级补全
  negative_examples: [分子对接, 分子动力学, 相互作用分析]
  required_inputs: [structure]
  conflicts_with: [gnina-runner, simulation-runner]
  skill_role: runner
  priority: 80
---

# PLACER Runner

这个 skill 用于当前项目里的 PLACER 长任务。

适用场景：
- 用户明确说“跑 placer”“用 placer 放 ligand”
- 用户已有 `.cif` / `.pdb` / `.pdbqt` / `.sdf` 或相关输入目录
- 用户要做原子级补全、放置或多样本候选生成

不适用场景：
- 只是解释 PLACER 原理
- 输入并非结构文件或结构目录
- 更适合 `gnina`、`plip` 或 `simulation` 的任务

## 先看哪份信息

优先级如下：

1. 本 skill：决定当前项目里怎么调 PLACER
2. `references/runtime_notes.md`：看当前 CLI 包装层、外部配置边界和已证实行为
3. `references/output_semantics.md`：看 `output_path` 的含义

## 调用原则

1. 不直接调用 `placer_mcp_server`
2. 始终通过 `mcp_job-protocol-mcp_submit_job` 提交
3. 默认严格异步：提交后的同一轮只返回受理信息，不调用 `get_job_status` / `collect_job_result`
4. 固定使用：
   - `target_server: "placer-mcp"`
   - `target_tool: "placer_mcp_server"`

提交形状应遵循项目约定：

```json
{
  "input_config": {
    "target_server": "placer-mcp",
    "target_tool": "placer_mcp_server",
    "...": "placer 参数"
  }
}
```

## 参数选择

### 最小必要参数

以下二选一：

- `ifile`
- `idir`

身份与 Workspace 字段由运行时补齐，不要猜造。

### 可移植资源规范（强制）

- 单结构 `ifile` 和可选 `ligand_file` 使用 ArtifactRef；结构目录 `idir` 使用 BundleRef，二者不要混用
- `weights`、`residue_json` 等确需由调用方提供的文件字段也必须使用 ArtifactRef
- 收到 Portable reference 后直接提交；不要用 `exec`、`glob`、`find` 或 `read` 搜索 Workspace，也不要转换成任何路径

目录输入示例：

```json
{"input_config":{"target_server":"placer-mcp","target_tool":"placer_mcp_server","idir":{"kind":"artifact_bundle","bundle_id":"bundle_structures"}}}
```

### 常用参数

- `nsamples`
- `rerank`
- `suffix`
- `predict_ligand`
- `fixed_ligand`
- `target_res`
- `predict_multi`
- `exclude_common_ligands`
- `bonds`
- `mutate`
- `crop_centers`
- `corruption_centers`
- `ligand_file`

### 仅在明确需要时设置

- `weights`
- `fixed_ligand_noise`
- `residue_json`
- `ignore_ligand_hydrogens`
- `extra_args`

### 一般不要主动改

- `workdir`
- `placer_script`
- `python_exec`
- `use_sm`
- `no_use_sm`

除非你已经确认部署环境要求显式覆盖这些字段。

## 当前项目里最容易误解的点

### 1. 当前仓库里看不到统一追踪的 `placer-config.yaml`

代码会去读取 `tmcp/funcdir/placer/data/placer-config.yaml`，但当前仓库里没有这个文件。  
因此不要在没有运行环境佐证时，臆造：

- 默认工作目录
- 默认脚本路径
- 默认 Python 解释器

### 2. `ifile` 和 `idir` 是互斥式主输入

当前包装层会在二者之间二选一。  
如果用户已经给了单个文件，不要再同时塞目录输入。

### 3. 包装层会自动把 `odir` 指向当前 run 的结果目录

默认不需要用户自己提供输出目录。

### 4. `output_path` 是结果目录，不是某个固定文件名

当前实现并没有在仓库内给出统一的结果文件命名规范。  
不要仅凭 `output_path` 猜测具体产物名。

## 如何理解 output_path

`output_path` 是当前 run 的结果目录。  
里面会放置 PLACER 的输出文件，但具体文件名和结构依赖本次参数与运行环境。

## 默认汇报模板

### 首轮提交时

优先给出：

1. 已提交到 `placer-mcp / placer_mcp_server`
2. `LOCAL-*` 或当前 submit 返回的受理标识
3. 本次关键输入
   - `ifile` 或 `idir`
   - 如有则 `nsamples` / `rerank`
4. 必要时的下一步建议

### 后续查结果时

优先给出：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时说明具体输出文件需要在目录内进一步确认

不要在没有目录检查结果的情况下，直接报出具体文件名。
