# Official Prediction Mapping

这份文档对照：

- 官方参数文档：`https://github.com/jwohlwend/boltz/blob/main/docs/prediction.md`
- 本地 MCP 参数模型：`../../../../tmcp/funcdir/boltz/utils.py`
- 当前 Docker 运行配置：`../../../../tmcp/config/dockerdir/boltz-config.yaml`

## 使用原则

当用户提到官方参数名时，agent 需要先判断：

1. 当前 MCP 是否暴露该字段
2. 当前 Docker 模板是否已经固定该参数
3. 本地默认值是否与官方默认值不同

## 参数对照

### 输入与路径

- 官方 `input_path`
  - 本地：`tools_input`
  - 说明：本地封装层入口，不是 Boltz 原生 CLI 字段

- FASTA 转换配体
  - 本地：`ligand_smiles`
  - 类型：`list[str]`
  - 正确：`["CCO", "CCC"]`
  - 错误：`"CCO,CCC"`；单字符串兼容逻辑只会将其变为 `["CCO,CCC"]`，不会按逗号拆分
  - 说明：不同字符串生成独立配体；重复字符串表示同一配体的多个副本

- Affinity binder 选择
  - 本地：`affinity_ligand_index`
  - 类型：零基整数或 `null`
  - 说明：从 `ligand_smiles` 中指定一个 affinity binder；`null` 表示不生成 affinity 配置

- DNA 输入
  - 本地：`dna_sequence`
  - 说明：仅在需要从 DNA 序列构造输入时提供

- YAML 输入中的配体
  - 本地：直接读取 `tools_input` 指向的 YAML/YML
  - 说明：现有 YAML 不会被 `ligand_smiles` 重写，多配体需要预先分别写入 YAML 的 `sequences`

- 官方 `--out_dir`
  - 本地：不作为用户常规字段暴露
  - 当前部署：由 Docker 模板固定设置

- 官方 `--cache`
  - 本地：`cache`
  - 默认：本地 `"/root/.boltz"`

### 常规预测参数

- 官方 `--checkpoint`
  - 本地：`checkpoint`

- 官方 `--devices`
  - 本地：`devices`

- 官方 `--accelerator`
  - 本地：`accelerator`

- 官方 `--recycling_steps`
  - 本地：`recycling_steps`

- 官方 `--sampling_steps`
  - 本地：`sampling_steps`

- 官方 `--diffusion_samples`
  - 本地：`diffusion_samples`

- 官方 `--max_parallel_samples`
  - 本地：`max_parallel_samples`
  - 默认：本地模型为 `5`

- 官方 `--step_scale`
  - 本地：`step_scale`

- 官方 `--output_format`
  - 本地：`output_format`
  - 默认：`mmcif`

- 官方 `--num_workers`
  - 本地：`num_workers`

- 官方 `--seed`
  - 本地：`seed`
  - 说明：需要可复现采样时显式设置

- 官方 `--method`
  - 本地：`method`

- 官方 `--preprocessing-threads`
  - 本地：`preprocessing_threads`
  - 差异：官方默认接近 `cpu_count()`；本地模型默认 `1`

### Affinity 相关

- 官方 `--affinity_mw_correction`
  - 本地：`affinity_mw_correction`

- 官方 `--sampling_steps_affinity`
  - 本地：`sampling_steps_affinity`

- 官方 `--diffusion_samples_affinity`
  - 本地：`diffusion_samples_affinity`

- 官方 `--affinity_checkpoint`
  - 本地：`affinity_checkpoint`

### MSA 相关

- 官方 `--use_msa_server`
  - 本地：当前没有作为普通 MCP 字段暴露
  - 当前部署：Docker 模板已固定开启

- 官方 `--msa_server_url`
  - 本地：`msa_server_url`
  - 差异：官方默认 `https://api.colabfold.com`；本地模型默认 `None`

- 官方 `--msa_pairing_strategy`
  - 本地：`msa_pairing_strategy`
  - 差异：官方默认 `greedy`；本地模型默认 `None`

- 官方 `--max_msa_seqs`
  - 本地：`max_msa_seqs`

- 官方 `--subsample_msa`
  - 本地：`subsample_msa`
  - 差异：官方默认 `False`；本地模型默认 `True`

- 官方 `--num_subsampled_msa`
  - 本地：`num_subsampled_msa`

### 其他布尔开关

- 官方 `--override`
  - 本地：`override`

- 官方 `--use_potentials`
  - 本地：`use_potentials`

- 官方 `--write_full_pae`
  - 本地：`write_full_pae`
  - 差异：官方默认 `False`；本地模型默认 `True`

- 官方 `--write_full_pde`
  - 本地：`write_full_pde`
  - 差异：官方默认 `False`；本地模型默认 `False`

- 官方 `--no_kernels`
  - 本地：`no_trifast`
  - 说明：语义接近，但命名不一致；agent 需要当作“禁用 trifast kernel”理解

## agent 的判断规则

1. 用户提到官方参数时，先转成本地字段名
2. 如果该参数当前没有暴露为 MCP 字段，检查是否已经在 Docker 层固定
3. 如果本地默认值和官方默认值不同，应优先提示“当前部署采用本地默认”
4. 对布尔参数，不要假设 `false` 一定能关闭一个已由 Docker 模板固定开启的选项
