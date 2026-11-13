# Output Semantics

## 当前包装层返回什么

PLACER 顶层返回通常是：

- `result_code`
- `output_path`
- `run_id`

## 如何理解 `output_path`

`output_path` 是结果目录，不是某个固定文件。

当前仓库里没有统一追踪的输出命名规范，因此不要仅凭 `output_path` 推断：

- 哪个文件是主结果
- 某个 CSV 或结构文件一定存在

## 面向用户时怎么说

默认优先汇报：

1. 是否完成
2. `run_id`
3. `output_path`
4. 必要时说明“具体文件名需要在结果目录里进一步确认”

这样比臆造文件名更稳妥。
