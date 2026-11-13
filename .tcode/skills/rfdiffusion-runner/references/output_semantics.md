# Output Semantics

## 当前包装层返回什么

RFdiffusion 顶层返回通常是：

- `result_code`
- `output_path`
- `run_id`

## 如何理解 `output_path`

`output_path` 是结果目录，不是单个生成结构文件。

当前 skill 不应仅凭目录名臆造：

- 具体 `.pdb` 文件名
- 最优候选是谁
- 某个文件一定存在

## 面向用户时怎么说

默认优先汇报：

1. 是否完成
2. `run_id`
3. `output_path`
4. 如有必要，补一句“具体生成结构文件需要在该目录内进一步确认”

如果用户只是要知道任务有没有跑完，这些信息通常已经足够。
