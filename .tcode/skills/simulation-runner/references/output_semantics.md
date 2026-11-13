# Output Semantics

## 当前包装层返回什么

simulation 顶层返回通常是：

- `result_code`
- `output_path`

当前默认不返回 `run_id`。

## 如何理解 `output_path`

`output_path` 是结果目录。  
当前实现成功时，常见产物通常包括：

- `<protein_stem>_complex.pdb`
- `<protein_stem>_minimised.pdb`
- `<protein_stem>_traj.dcd`

## 面向用户时怎么说

默认优先汇报：

1. 是否完成
2. `output_path`
3. 必要时补一句“常见产物通常包括 `*_complex.pdb`、`*_minimised.pdb`、`*_traj.dcd`”

不要在当前实现没有返回 `run_id` 的情况下凭空补一个。
