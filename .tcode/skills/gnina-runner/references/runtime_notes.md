# Runtime Notes

这份文档只记录当前仓库里能被代码直接证实的 Gnina 运行行为。

## 主要来源

- `../../../../tmcp/funcdir/gnina/utils.py`
- `../../../../tmcp/funcdir/gnina/gnina_run_func.py`
- `../../../../tmcp/funcdir/gnina/gnina_run_manager.py`
- `../../../../tmcp/config/dockerdir/gnina-config.yaml`

## 当前已证实的运行事实

### 固定路由

- server: `gnina-mcp`
- tool: `gnina_mcp_server`

### 输入 staging

`gnina_run_func.py` 会把：

- `receptor`
- `ligand`
- `autobox_ligand`

复制到当前 run 的输入目录，然后再交给 Docker 运行。

### 最小必需输入

从包装层看，最稳妥的最小输入是：

- `receptor`
- `ligand`
- `project_id`
- `task_id`
- `user_id`

如果 `autobox_ligand` 未提供，当前封装会优先回退到 `ligand`。

### Docker 命令模板

当前可见配置为：

```text
gnina -r {receptor} -l {ligand} --autobox_ligand {autobox_ligand} -o {output} {additional_args}
```

### 输出文件命名

`gnina_run_func.py` 会用受体文件名生成输出文件：

`<receptor_stem>.sdf.gz`

并把它写到 run 的结果目录下。

## 参数使用建议

### 常见且稳定

- `autobox_ligand`
- `exhaustiveness`
- `num_modes`
- `scoring`
- `cpu`
- `device`
- `no_gpu`
- `score_only`
- `minimize`

### 不要主动假设

- 柔性 docking 相关的 `flex*`
- 共价 docking 相关的 `covalent_*`
- 自定义评分文件
- 配置文件 `config`

除非用户明确提出，否则不要主动把这些字段塞进去。
