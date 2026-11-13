# Output Semantics

## 当前包装层返回什么

LigandMPNN 顶层返回通常是：

- `result_code`
- `output_path`
- `run_id`

## 如何理解 `output_path`

当前 `ligandmpnn_run_func.py` 返回的不是整个 run 根目录，而是：

- `run_paths.result.seqs.path`

因此 `output_path` 更接近“序列结果目录”。

## 目录里常见的东西

常见结果通常是：

- `.fa`
- `.fasta`

## 面向用户时怎么说

默认优先汇报：

1. 是否完成
2. `run_id`
3. `output_path`
4. 如有必要，补一句“这里通常是 `seqs` 目录，里面常见是 `.fa` / `.fasta` 文件”

不要在没有明确要求时直接贴出全部序列内容。
