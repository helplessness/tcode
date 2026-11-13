# Runtime Notes

这份文档只记录当前仓库里能直接证实的 PLIP 运行行为。

## 主要来源

- `../../../../tmcp/funcdir/plip_func/utils.py`
- `../../../../tmcp/funcdir/plip_func/plip_run_func.py`
- `../../../../tmcp/funcdir/plip_func/plip_run_manager.py`

## 当前已证实的运行事实

### 固定路由

- server: `plip-mcp`
- tool: `plip_mcp_server`

### 输入 staging

包装层会把 `file` 指向的结构文件上传到当前 run 输入目录，然后交给运行环境。

### 单文件导向

当前 `plip_run_func.py` 只围绕单个 `file` 参数组织输入。  
不要默认把它描述成目录批处理入口。

### 外部配置边界

`plip_run_manager.py` 会依赖外部 `plip-config.yaml`。  
当前仓库里没有这个配置文件，因此：

- 不要承诺固定镜像名
- 不要承诺固定命令模板
- 不要承诺固定输出格式

### 已知实现限制

本地当前 `PlipConfig.model_dump()` 会透传显式设置的 `txt`。需要文本报告时设为 true；远端版本和实际报告仍须按返回产物核对，不根据摘要缺失推断文件不存在。

## 参数使用建议

### 常见且稳定

- `xml`
- `pymol`
- `pics`
- `maxthreads`
- `breakcomposite`
- `altlocation`
- `chains`
- `regions`

### 不要主动假设

- `txt`
- 任意外部配置默认值
- 批量目录运行能力

如果用户只要常规相互作用分析，优先使用更稳妥的最小参数集。
