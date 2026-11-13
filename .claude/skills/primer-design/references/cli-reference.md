# Primer Design CLI Reference

仅在需要构造非默认命令、解释输出字段或排查退出码时读取本文件。

## 输入格式

- `--sequence FILE`：必填；FASTA 或仅含 DNA 的文本文件。
- `--mutation MUTATION`：可重复；支持 `C231A`、`C231A:GCC`、`C267NNK`、`267NNS`。
- `--mutations FILE`：TXT 每行一个突变；CSV 优先读取 `mutation` 列，否则读取第一列。
- `--output FILE`：必填；扩展名为 `.xlsx` 时导出 Excel，其他扩展名按 CSV 导出。
- `--include-near-misses`：将未通过硬约束的近似候选写入输出文件。

`--mutation` 和 `--mutations` 可以同时使用，任务会合并。不要重复提交同一突变，除非用户明确需要重复记录。

## 外部引物评价

使用 `scripts/run_primer_evaluation.py`，不要把外部网站引物交给设计入口。

批量模式：

```bash
python "<skill-dir>/scripts/run_primer_evaluation.py" \
  --sequence "<sequence-file>" \
  --external-primers "<input>.xlsx" \
  --output "<output>.xlsx"
```

输入文件每行一对引物，支持以下列：

| 列 | 必填 | 含义 |
|---|---|---|
| `mutation` | 是 | `C231A`、`C231A:GCC`、`C267NNK` 等 |
| `forward_sequence` | 是 | 正向引物 5′→3′ |
| `reverse_sequence` | 否 | 反向引物 5′→3′；省略时自动生成反向互补 |
| `forward_name` / `reverse_name` | 否 | 原始引物名称 |
| `source` | 否 | 网站或文件来源 |

XLSX 优先读取 `website_primers` 工作表，否则读取活动工作表。

单条模式：

```bash
python "<skill-dir>/scripts/run_primer_evaluation.py" \
  --sequence "<sequence-file>" \
  --mutation C231A \
  --primer "<forward-sequence>"
```

可选传 `--reverse`、`--forward-name`、`--reverse-name`、`--source-label` 和
`--output`。单条模式不传输出文件时直接打印 JSON。

评价排名字段：

| 字段 | 含义 |
|---|---|
| `preset` | 本次评价使用的 `quikchange_ii` 或 `primer3_generic` |
| `evaluation_status` | `RANKED` 已排名；`UNALIGNED` 无法按突变位点对齐 |
| `constraint_status` | `PASS`、`NEAR_MISS`；QuikChange 无法对齐时为 `UNCHECKED` |
| `generated_by_algorithm` | 当前候选范围是否实际生成了完全相同的 F/R |
| `ranking_type` | `generated` 实际候选排名；`inserted` 插入排名 |
| `comparison_scope` | `same_replacement_codon`，与相同目标密码子的候选比较 |
| `overall_rank` / `comparison_total` | 总排名与总比较数，PASS 始终在前 |
| `pass_rank` / `pass_total` | PASS 子集合排名 |
| `near_miss_rank` / `near_miss_total` | NEAR_MISS 子集合排名 |
| `pair_score` | 自定义惩罚分，越低越好，不代表实验成功率 |
| `violations` | 硬约束超限原因 |
| `advisories` | QuikChange 软性优选项提示，不影响 PASS |
| `quikchange_tm_*_c` | QuikChange 经验 Tm；通用模式为空 |
| `quikchange_mismatch_*_percent` | 突变错配比例范围 |
| `quikchange_estimated_energy_cost_*_percent` | 本地估算自由能成本，不是网站 mfold 原值 |

评价退出码：`0` 表示所有记录完成排名；`1` 表示输入/参数错误；`2` 表示至少一条
记录无法对齐排名。NEAR_MISS 是有效评价结果，不会单独导致退出码 2。

## 设计参数

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--preset` | `quikchange_ii` | `quikchange_ii` 或 `primer3_generic` |
| `--strategy` | `overlap_complementary` | 当前唯一策略；F/R 完全反向互补 |
| `--min-length` | 25 | 两种预设共用的最小长度，bp |
| `--opt-length` | 35 | 仅通用模式使用的排名目标长度，bp |
| `--max-length` | 45 | 仅通用模式使用的最大长度，bp |
| `--min-tm` | 65 | 仅通用模式使用的最低 Primer3 Tm，℃ |
| `--opt-tm` | 70 | 仅通用模式使用的排名目标 Tm，℃ |
| `--max-tm` | 75 | 仅通用模式使用的最高 Primer3 Tm，℃ |
| `--min-gc` | 40 | 仅通用模式使用的最低 GC，% |
| `--max-gc` | 65 | 仅通用模式使用的最高 GC，% |
| `--center-slack` | 2 | 突变密码子可偏离几何中心的碱基数 |
| `--num-return` | 3 | 每个位点正式候选数 |
| `--near-miss-count` | 3 | 每个位点保留的近似候选数 |
| `--relaxed` | 关闭 | 允许近似候选进入主要列表；状态仍为 `NEAR_MISS` |
| `--relax-length-by` | 3 | 搜索近似候选时向长度范围外扩展的 bp 数 |
| `--max-hairpin-tm` | 不限制 | 发卡最高 Tm；不传时只报告 |
| `--max-homodimer-tm` | 不限制 | 同源二聚体最高 Tm；不传时只报告 |
| `--max-degenerate-variants` | 4096 | 单条简并引物最大展开数量 |

通用模式下长度、Tm、GC 的 `min/max` 是硬约束；`opt-length` 和 `opt-tm` 只影响合格候选之间的内部排名。

`--opt-length`、`--min-tm`、`--opt-tm` 和 `--max-tm` 不作用于
`quikchange_ii`。QuikChange 必须使用下表的 `--qc-*` 参数。

QuikChange II 参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--qc-min-tm` | 78 | 最低 QuikChange 经验 Tm，℃；硬约束 |
| `--qc-opt-tm` | 关闭 | 扩展规则：排名目标 QuikChange Tm；软目标 |
| `--qc-max-tm` | 不限制 | 扩展规则：最高 QuikChange 经验 Tm；设置后为硬约束 |
| `--qc-opt-length` | 关闭 | 扩展规则：排名目标长度；软目标 |
| `--qc-preferred-max-length` | 45 | 优选长度上限；软提示 |
| `--qc-absolute-max-length` | 60 | 绝对长度上限，bp；硬约束 |
| `--qc-min-flank` | 9 | 密码子外侧最短匹配长度；等价于中央突变碱基距末端至少10；硬约束 |
| `--qc-preferred-min-gc` | 40 | 优选 GC 下限；软提示 |
| `--qc-energy-temp` | 68 | 估算错配自由能成本的温度，℃ |

上述 `--qc-*` 均有完整名称 `--quikchange-*`。`qc-opt-length` 必须位于
`min-length` 与 `qc-preferred-max-length` 之间；Tm 必须满足
`qc-min-tm <= qc-opt-tm <= qc-max-tm`（未设置的可选项忽略）。QuikChange
模式中，超过45 bp、GC低于40%或3′端不是G/C进入 `advisories`，不单独导致
`NEAR_MISS`；超过显式设置的 `qc-max-tm` 则属于硬失败。

三个扩展参数 `qc-opt-length`、`qc-opt-tm`、`qc-max-tm` 默认关闭，不改变原有
QuikChange 兼容排名。它们是本包提供的用户自定义规则，不是 Agilent 官方默认值。

## Primer3 反应体系参数

| 参数 | 默认值 | 单位 |
|---|---:|---|
| `--mv-conc` | 50.0 | mM 单价离子 |
| `--dv-conc` | 1.5 | mM 二价离子 |
| `--dntp-conc` | 0.6 | mM dNTP 总浓度 |
| `--dna-conc` | 50.0 | nM 引物浓度 |
| `--dmso-conc` | 0.0 | % DMSO |

## CDS 校验参数

- 默认要求长度为 3 的倍数、以 `ATG` 起始、末尾有终止密码子、内部无终止密码子。
- `--allow-non-atg-start`：允许非 ATG 开头。
- `--allow-missing-stop`：允许末尾没有终止密码子。
- `--allow-internal-stops`：允许内部终止密码子。
- `--no-cds-validation`：关闭以上校验；突变位置仍按序列首碱基起算的三联密码子坐标处理。

## 输出字段

每条引物一行，一对 F/R 占两行。同一引物对的突变、排名、侧翼长度、评分和状态会重复。

| 字段 | 含义 |
|---|---|
| `mutation` | 突变名称，例如 `C231A` |
| `preset` | 当前使用的两套规则之一 |
| `mutation_type` | `fixed` 定点突变或 `degenerate` 简并突变 |
| `wildtype_codon` | 母本密码子 |
| `replacement_codon` | 写入引物的普通或简并目标密码子 |
| `candidate_rank` | 当前突变内候选排名，从 1 开始 |
| `primer_name` | 引物名称；`F/R` 表示方向，`C2` 表示第二候选，`NM1` 表示第一近似候选 |
| `direction` | `F` 正向或 `R` 反向 |
| `sequence_5to3` | 5′→3′ 引物序列 |
| `length` | 长度，bp |
| `tm_min_c` / `tm_max_c` | Primer3 Tm 范围，℃ |
| `gc_min_percent` / `gc_max_percent` | GC 范围，% |
| `hairpin_tm_max_c` | 最大发卡 Tm，℃ |
| `homodimer_tm_max_c` | 最大同源二聚体 Tm，℃ |
| `variant_count` | 具体序列数；普通引物为 1，单个 NNK/NNS 为 32 |
| `left_flank_length` / `right_flank_length` | 正向引物中突变密码子两侧的母本侧翼长度 |
| `pair_score` | 自定义惩罚分，越低排名越靠前；不是实验成功率 |
| `status` | `PASS` 或 `NEAR_MISS` |
| `violations` | 未满足的硬约束，多个原因以分号分隔 |
| `advisories` | QuikChange 软性优选项提示 |
| `quikchange_tm_min_c` / `quikchange_tm_max_c` | QuikChange 经验 Tm 范围；通用模式为空 |
| `quikchange_mismatch_*_percent` | 突变密码子错配比例范围 |
| `quikchange_estimated_energy_cost_*_percent` | Primer3 模型估算的错配自由能成本；不是网站 mfold 原值 |

普通引物的 Tm/GC 最小值和最大值相同。简并引物会展开全部具体变体，Tm/GC 输出范围，结构指标输出最不利的最大值。

## 退出码和边界

- `0`：所有突变都有合格候选。
- `1`：输入文件、参数、序列或突变格式错误。
- `2`：正常生成输出，但至少一个突变没有合格候选。

当前不实现 Q5 背靠背引物、普通 PCR 独立引物对、模板特异性搜索或完整宿主密码子偏好。QuikChange 模式实现公开经验公式和兼容规则，但不复刻网站未公开的全部内部权重。
