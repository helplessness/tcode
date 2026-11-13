# Output Semantics

## 当前包装层返回什么

PLIP 顶层返回通常是：

- `result_code`
- `output_path`
- `run_id`

## 如何理解 `output_path`

`output_path` 是结果目录，不是单个报告文件。

目录里可能会出现：

- XML
- 图片
- PyMOL session
- 其他报告文件

但是否真的存在，取决于本次参数和外部运行配置。

## 面向用户时怎么说

默认优先汇报：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时说明“具体报告文件类型要以目录内实际产物为准”

这样比承诺某个特定报告格式更稳妥。
