# Runtime Notes

这份文档只记录当前仓库里能直接证实的 PLACER 运行行为。

## 主要来源

- `../../../../tmcp/funcdir/placer/utils.py`
- `../../../../tmcp/funcdir/placer/placer_run_func.py`
- `../../../../tmcp/funcdir/placer/placer_run_manager.py`

## 当前已证实的运行事实

### 固定路由

- server: `placer-mcp`
- tool: `placer_mcp_server`

### 输入 staging

当前包装层会把：

- `ifile` 指向的单文件
- 或 `idir` 指向的目录

复制到当前 run 的输入目录，再交给 CLI 运行。

### 主输入规则

从 `placer_run_func.py` 看，最稳妥的输入是：

- 单文件：`ifile`
- 或目录：`idir`

二者通常二选一。

### 输出目录

包装层会把当前 run 的结果目录作为 `odir` 默认值写入参数。

### 外部配置边界

`placer_run_manager.py` 会依赖配置里的：

- `working_dir`
- `script`
- 可能的 Python 解释器

但当前仓库里没有可直接读取到的 `placer-config.yaml`。  
因此不要在 skill 中承诺这些默认值。

## 参数使用建议

### 常见且稳定

- `nsamples`
- `rerank`
- `suffix`
- `predict_ligand`
- `fixed_ligand`
- `predict_multi`

### 不要主动假设

- `workdir`
- `placer_script`
- `python_exec`
- 外部配置里的任何默认值

如果用户没有明确要求，优先使用最小参数集，让运行环境自身决定 CLI 细节。
