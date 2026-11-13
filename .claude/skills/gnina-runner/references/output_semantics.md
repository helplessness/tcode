# Output Semantics

## 当前包装层返回什么

Gnina 顶层返回通常是：

- `result_code`
- `output_path`
- `run_id`

## 如何理解 `output_path`

`output_path` 是结果目录，不是单个 docked 文件。

当前实现里，最常见的主结果文件通常是：

- `<receptor_stem>.sdf.gz`

它通常位于 `output_path` 目录中。

## 面向用户时怎么说

默认优先汇报：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时补一句“常见主结果文件通常是 `<receptor_stem>.sdf.gz`”

不要在用户没要求时，展开所有 pose、所有 SDF 记录或整个目录树。
