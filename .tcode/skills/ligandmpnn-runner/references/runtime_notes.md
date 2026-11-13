# Runtime Notes

这份文档只记录当前仓库里能直接证实的 LigandMPNN run 行为。

## 主要来源

- `../../../../tmcp/funcdir/lingandmpnn/utils.py`
- `../../../../tmcp/funcdir/lingandmpnn/ligandmpnn_run_func.py`
- `../../../../tmcp/funcdir/lingandmpnn/ligandmpnn_run_manager.py`
- `../../../../tmcp/config/dockerdir/ligandmpnn_run.yaml`

## 当前已证实的运行事实

### 固定路由

- server: `ligandmpnn-mcp`
- tool: `ligandmpnn_run_mcp_server`

### 当前暴露的是 run 流程

当前远端服务对应的是 `ligandmpnn_run_mcp_server`。  
不要默认把仓库里的 `score` 相关代码当成已接入能力。

### 输入形状

包装层接收 `tools_input`，然后会把文件上传到 run 输入目录，最后把它作为：

`--pdb_path`

传给容器。

### Docker 模板

当前可见模板为：

```text
run.py --pdb_path {pdb_path} --out_folder {out_folder} {additional_args}
```

### 结果目录

包装层会把 `out_folder` 绑定到当前 run 的结果目录。  
用户通常不需要自己提供 `out_folder`。

## 参数使用建议

### 常见且稳定

- `model_type`
- `batch_size`
- `number_of_batches`
- `temperature`
- `seed`
- `chains_to_design`
- `fixed_residues`
- `redesigned_residues`

### 不要主动假设

- 各类 `checkpoint_*`
- `*_multi` JSON 映射输入
- 跨膜蛋白相关字段
- 评分相关字段

除非用户明确提出，否则保持最小参数集更稳。
