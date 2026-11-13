# EvolvePro 输入格式

## 运行环境

- 规范化脚本使用 `pandas` 和 `openpyxl` 读取、校验和生成 `.xlsx` 文件，依赖声明位于 Skill 自带的 `requirements.txt`。
- 缺少依赖时，只能在平台提供的隔离运行环境中准备；不得修改宿主机全局 Python 环境。
- 旧版 `.xls` 通常还需要 `xlrd`；当前环境未提供时，请将文件另存为 `.xlsx` 或 CSV。

## 实验轮次文件

官方实验数据加载器读取 Excel 的首个工作表，并要求以下列：

| 列名 | 含义 | 约束 |
|---|---|---|
| `Variant` | 突变标识 | 不得为空或重复 |
| `activity` | 实验测量值 | 必须是有限数值 |

其他列会保留，但不会参与 EvolvePro 顶层回归。

### 单突变

- WT 写作 `WT`。
- 突变写作“1-based 位置 + 新氨基酸”，例如 `25A`。
- 官方加载器根据 WT FASTA 自动把 `25A` 补成 `G25A`，用于匹配 embedding。
- 标准化脚本也接受完整写法 `G25A`，但会先核对 WT 第 25 位确实为 `G`。

示例：

```csv
Variant,activity
WT,1.0
25A,1.32
40L,0.81
```

### 多突变

- 使用完整单突变标识，并以下划线连接，例如 `G25A_V40L`。
- 每个组成突变的位置必须唯一，原始氨基酸必须与 WT 一致。
- 多突变名称必须与对应 embedding 的索引一致。

## WT FASTA

- 只允许一个 FASTA record。
- 序列使用标准 20 种氨基酸单字母编码：`ACDEFGHIKLMNPQRSTVWY`。
- 突变位置采用 1-based 编号。

## MCP 参数对应关系

| 数据 | MCP 参数 |
|---|---|
| WT 字符串 | `sequence` |
| WT FASTA | `wt_fasta_path` |
| 单突变轮次 Excel | `excel_files` |
| 多突变轮次 Excel | `excel_file_multi` |
| 用于生成组合的单突变 Excel | `mult_file` |
| 已生成的单突变 FASTA | `mutants_fasta_path` |
| 单突变 embedding | `precomputed_embedding_csv` |
| 额外多突变 embedding | `precomputed_embedding_csvs` |

## 禁止自动推断

以下情况必须询问用户：

- 同时存在多个可能的活性列或突变列。
- 不清楚测量值应直接使用、取反还是归一化。
- 突变位置越界，或标注的原始氨基酸与 WT 不一致。
- 用户提供多个 WT FASTA record，或文件之间使用不同 WT。
- 缺失实验测量值，或同一规范化突变出现多次。
