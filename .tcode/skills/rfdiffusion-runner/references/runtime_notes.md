# Runtime Notes

这份文档只记录当前仓库里能直接证实的 RFdiffusion 运行行为。

## 主要来源

- `../../../../tmcp/funcdir/rfdiffusion/utils.py`
- `../../../../tmcp/funcdir/rfdiffusion/rfdiffusion_run_func.py`
- `../../../../tmcp/funcdir/rfdiffusion/rfdiffusion_run_manager.py`
- `../../../../tmcp/config/dockerdir/rfdiffusion-config.yaml`

## 当前已证实的运行事实

### 固定路由

- server: `rfdiffusion-mcp`
- tool: `rfdiffusion_mcp_server`

### 输入形状

当前包装层最稳妥的输入仍然是：

- `tools_input` 指向单个 `.pdb`

如果没传，会尝试复用当前 run 输入目录里已有的 `.pdb`。

### Docker 模板

当前可见模板为：

```text
inference.output_prefix={output_prefix} inference.model_directory_path={model_directory_path} inference.input_pdb={input_file} inference.num_designs={num_designs} contigmap.contigs={contigs} {additional_args}
```

### 字段映射

`utils.py` 会把当前模型字段映射为 Hydra 风格键，例如：

- `inference_*` -> `inference.*`
- `contigmap_*` -> `contigmap.*`
- `diffuser_*` -> `diffuser.*`
- `ppi_*` -> `ppi.*`
- `potentials_*` -> `potentials.*`
- `scaffoldguided_*` -> `scaffoldguided.*`

### 额外字段

当前模型允许额外字段透传。  
只有在你非常确定字段名和语义时，才应使用这条能力。

## 参数使用建议

### 常见且稳定

- `contigmap_contigs`
- `inference_num_designs`
- `inference_symmetry`
- `diffuser_T`
- `diffuser_partial_T`
- `ppi_hotspot_res`

### 不要主动假设

- 自定义 checkpoint 路径
- 自定义输出前缀
- 复杂 `scaffoldguided_*` 组合
- 任意未知的 extra 字段

如果用户没有明确设计约束，优先把参数集保持最小。
