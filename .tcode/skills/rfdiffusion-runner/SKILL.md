---
name: rfdiffusion-runner
description: 在当前项目中使用 RFdiffusion 做蛋白结构生成、motif scaffolding、contig 设计，或用户提到“跑 rfdiffusion”“做蛋白生成”“做 contig 设计”“motif scaffolding”时务必使用。指导 agent 选择必要参数、严格通过 submit_job 异步提交到 rfdiffusion-mcp，并结合本地 Hydra 风格参数映射、Docker 模板和结果目录语义正确汇报。
when_to_use: >-
  Use when the user wants RFdiffusion structure generation, contig design, motif scaffolding, or hotspot-constrained design from structure inputs. Examples: “跑 rfdiffusion”, “做蛋白生成”, “做 contig 设计”, “motif scaffolding”.
routing:
  domains: [protein-structure]
  intents: [protein-structure-generation, motif-scaffolding]
  aliases: [RFdiffusion, 蛋白结构生成, contig 设计, motif scaffolding, hotspot 约束]
  positive_examples:
    - 根据 motif 和热点残基生成蛋白骨架
    - 使用 RFdiffusion 做 contig 设计
  negative_examples: [蛋白序列设计, 结构预测, 分子对接]
  conflicts_with: [ligandmpnn-runner, boltz-runner]
  skill_role: runner
  priority: 80
---

# RFdiffusion Runner

这个 skill 用于当前项目里的 RFdiffusion 长任务。

适用场景：
- 用户明确说“跑 rfdiffusion”“做蛋白结构生成”
- 用户要做 contig 设计、motif scaffolding、热点残基约束生成
- 用户已经有输入 PDB，希望生成多个候选结构

不适用场景：
- 只是解释 RFdiffusion 原理
- 没有输入结构、没有设计约束却想直接猜参数
- 更适合 `boltz` 的预测或 `ligandmpnn` 的序列设计

## 先看哪份信息

优先级如下：

1. 本 skill：决定当前项目里怎么调 RFdiffusion
2. `references/runtime_notes.md`：看 Hydra 风格参数映射、Docker 默认项和当前封装边界
3. `references/output_semantics.md`：看 `output_path` 应如何解释

## 调用原则

1. 不直接调用 `rfdiffusion_mcp_server`
2. 始终通过 `mcp_job-protocol-mcp_submit_job` 提交
3. 默认严格异步：提交后的同一轮只返回受理信息，不调用 `get_job_status` / `collect_job_result`
4. 固定使用：
   - `target_server: "rfdiffusion-mcp"`
   - `target_tool: "rfdiffusion_mcp_server"`

提交形状应遵循项目约定：

```json
{
  "input_config": {
    "target_server": "rfdiffusion-mcp",
    "target_tool": "rfdiffusion_mcp_server",
    "...": "rfdiffusion 参数"
  }
}
```

## 参数选择

### 最小必要参数

- `tools_input`

身份与 Workspace 字段由运行时补齐，不要猜造。

### 可移植资源规范（强制）

- 单文件 `tools_input`、`inference_ckpt_override_path`、`scaffoldguided_target_path` 使用 ArtifactRef；`scaffoldguided_scaffold_dir` 目录使用 BundleRef
- 收到 Portable reference 后直接提交；不要用 `exec`、`glob`、`find` 或 `read` 搜索 Workspace
- 不要转换成 `/workspace/...` 或宿主机路径；目标 MCP 会在服务器任务目录中物化输入

多数真实任务里，还应尽快补齐：

- `contigmap_contigs`

### 常用参数

- `contigmap_contigs`
- `inference_num_designs`
- `inference_symmetry`
- `diffuser_T`
- `diffuser_partial_T`
- `ppi_hotspot_res`
- `potentials_guiding_potentials`
- `potentials_guide_scale`
- `potentials_guide_decay`

### 仅在明确需要时设置

- `contigmap_inpaint_seq`
- `contigmap_inpaint_str`
- `contigmap_inpaint_str_helix`
- `contigmap_inpaint_str_strand`
- `contigmap_provide_seq`
- `contigmap_length`
- `scaffoldguided_*`
- `potentials_substrate`
- `additional_overrides`

### 一般不要主动改

- `inference_output_prefix`
- `inference_ckpt_override_path`
- `denoiser_noise_scale_ca`
- `denoiser_noise_scale_frame`

除非用户明确要求，或你已经确认这些字段确实是当前设计策略的核心。

## 当前项目里最容易误解的点

### 1. 当前封装允许额外字段透传，但不要随意猜

`RfdiffusionConfig` 允许 `extra="allow"`，未知字段可能被继续透传为 Hydra 风格参数。  
这不等于你可以随意捏造字段名。

### 2. 多数字段会被映射成 `key=value` 形式

例如：

- `contigmap_contigs` -> `contigmap.contigs=...`
- `ppi_hotspot_res` -> `ppi.hotspot_res=...`

因此参数名必须使用当前包装层能识别的字段名，而不是混用别的命名风格。

### 3. `output_path` 是结果目录，不是单个生成结构

不要把 `output_path` 直接当成某个 `.pdb` 文件。

### 4. 不要主动改输出前缀和模型目录

当前 Docker 配置已经提供默认的：

- `inference.output_prefix`
- `inference.model_directory_path`

除非用户明确要求，否则不要主动覆盖。

## 如何理解 output_path

`output_path` 是当前 run 的结果目录。  
目录里通常会包含：

- 生成的结构文件
- 相关日志或中间文件

但默认不要根据目录名臆造具体文件名。

## 默认汇报模板

### 首轮提交时

优先给出：

1. 已提交到 `rfdiffusion-mcp / rfdiffusion_mcp_server`
2. `LOCAL-*` 或当前 submit 返回的受理标识
3. 本次关键约束
   - `tools_input`
   - `contigmap_contigs`
   - 如有则 `inference_num_designs`
4. 必要时的下一步建议

### 后续查结果时

优先给出：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时说明这是结果目录，具体结构文件应在目录内再确认

不要在没有用户明确要求时展开全部结构文件。
