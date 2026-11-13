# Output Layout

官方输出目录文档来自：
- `https://github.com/jwohlwend/boltz/blob/main/docs/prediction.md`

官方说明中的典型结构是：

```text
out_dir/
├── lightning_logs/
├── predictions/
│   ├── [input_file1]/
│   │   ├── [input_file1]_model_0.cif
│   │   ├── confidence_[input_file1]_model_0.json
│   │   ├── affinity_[input_file1].json
│   │   ├── pae_[input_file1]_model_0.npz
│   │   ├── pde_[input_file1]_model_0.npz
│   │   ├── plddt_[input_file1]_model_0.npz
│   │   └── ...
│   └── [input_file2]/
└── processed/
```

## 当前项目里怎么看

当前项目直接返回给 agent 的通常只有：
- `result_code`
- `output_path`
- `run_id`

因此 agent 不应直接假设某一个具体结果文件路径，而应先把 `output_path` 当作结果目录来理解，再按当前任务需要决定是否继续查看目录内容。

## 对结果文件的优先级建议

### 结构文件

优先关注：
- `.cif`
- `.mmcif`
- `.pdb`

这些通常是下游结构分析、对接、相互作用分析的候选输入。

### 置信度与打分

优先关注：
- `confidence_*.json`
- `affinity_*.json`

这些通常用于判断候选结构的质量或结合倾向。

### 额外矩阵文件

可能包括：
- `pae_*.npz`
- `pde_*.npz`
- `plddt_*.npz`

这些更适合深入分析，不一定是面向普通用户汇报的首选内容。

## 对 agent 的建议

1. 面向用户时，不要一次性展开全部文件
2. 先汇报：
   - 是否成功
   - `run_id`
   - `output_path`
3. 如果用户明确要求衔接下游工具，再结合目录结构选择具体输入文件
