# Runtime Defaults

这份文档描述的是 **当前项目部署出来的 Boltz 实际运行默认值**，不是官方文档的抽象默认值。

## 结论

当前项目里，Boltz 的最终行为由 4 层共同决定：

1. Docker 命令模板中的硬编码参数
2. 用户通过 MCP 显式传入的 `input_config`
3. 本地 `BoltzConfig` 模型中的字段默认值
4. Boltz 官方 CLI 默认值

## 当前最关键的硬编码

配置文件：
- `../../../../tmcp/config/dockerdir/boltz-config.yaml`

命令模板当前是：

```text
predict {input_file} --out_dir {out_dir} --use_msa_server {additional_args}
```

这意味着：

- `--use_msa_server` 在当前部署里是默认固定开启的
- 即使 MCP 层没有显式暴露 `use_msa_server` 参数，运行时仍会启用
- agent 不应要求用户额外手动提供 MSA，除非任务本身另有特殊限制

## 当前配置来源

### Docker 命令模板

来自：
- `../../../../tmcp/config/dockerdir/boltz-config.yaml`

当前固定内容包括：
- `--out_dir`
- `--use_msa_server`

### MCP 参数模型

来自：
- `../../../../tmcp/funcdir/boltz/utils.py`

注意：
- `BoltzConfig.model_dump()` 只会输出“用户显式设置且非默认值”的字段
- 这意味着很多本地默认值不会被拼到 CLI 命令中
- 没有被显式传出的参数，最终效果常常回落到 Docker 硬编码或官方 CLI 默认值

### Docker 追加参数拼装

来自：
- `../../../../tmcp/utils/load_docker.py`

布尔值逻辑是：
- `true` -> 追加 `--flag`
- `false` -> 不追加任何内容

因此：
- 某些布尔参数可以显式打开
- 但无法用 `false` 可靠地“关闭一个已经写死在命令模板里的 flag”

## 实际推断规则

### `affinity_ligand_index`

- FASTA 转换的 MCP 新版默认值为 `null`，不会自动添加 `properties.affinity`；只有显式索引才开启亲和力预测。
- 旧部署可能仍默认 `0`。跨版本调用时，不做亲和力必须显式传 `null`，不能只依赖省略字段。
- 这是 MCP FASTA 转换配置，不是 Boltz CLI 开关；直接 YAML 中已有的 `properties.affinity` 不会被该参数删除。提交前核对文件和审批范围，冲突时先澄清，不能修改原件后静默提交。
- 内部转换函数及独立批处理入口的默认值未在本次修改；本节只约束 Boltz MCP，通过 `BoltzConfig` 显式传值给转换函数。

### `use_msa_server`

- 当前部署：视为 **始终启用**
- 原因：Docker 命令模板硬编码了 `--use_msa_server`

### `out_dir`

- 当前部署：总是由 Docker 模板设置
- 对应容器内路径通常是 `/output`

### `tools_input`

- 不是直接传给 Boltz CLI 的官方参数
- 它是当前项目封装层的输入入口
- 会先被转换或复制，再映射成容器里的 `input_file`

### 其他常规预测参数

例如：
- `recycling_steps`
- `sampling_steps`
- `diffusion_samples`
- `output_format`
- `write_full_pae`
- `write_full_pde`

如果用户显式设置，通常会进入 `additional_args`。  
如果用户没显式设置，则未必会按 `utils.py` 的默认值进入 CLI，很多时候会退回 Boltz 官方默认值。

## 对 agent 的使用建议

1. 讨论默认行为时，不要只引用官方文档
2. 先判断这个参数是否已经被 Docker 模板固定
3. 再判断这个参数是否在当前 MCP 中可显式传入
4. 如果用户问“为什么没传也生效”，优先检查 Docker 模板
