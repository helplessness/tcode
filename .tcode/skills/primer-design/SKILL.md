---
name: primer-design
description: 使用内置 primer_designer 包按 QuikChange II 或通用 Primer3 两套规则设计定点突变、NNK/NNS 饱和突变引物，并评价网站或其他工具生成的外部引物排名。用户提供 DNA/FASTA、突变列表或现成 F/R 引物，要求选择 QuikChange 试剂盒规则、设置长度/Tm/GC/结构阈值或 QuikChange 扩展目标、生成 CSV/XLSX、批量回评、评价单条引物或解释两种排名差异时使用。当前采用完全互补 overlap_complementary 策略；QuikChange 模式实现公开规则和近似自由能排名，不声称逐项复刻 Agilent 未公开权重。
---
# Primer Design

使用 `scripts/primer_designer/` 中内置的 Python 包完成设计；不要手工推导引物序列。

## 运行约定

- 使用当前隔离环境中的 `python`；如果命令不存在，再使用 `python3`。不要硬编码解释器路径。
- 新设计通过 `scripts/run_primer_design.py` 启动；外部引物评价通过 `scripts/run_primer_evaluation.py` 启动。两者只导入同目录中的内置包。
- 运行依赖记录在当前 Skill 的 `requirements.txt`。先检查 `primer3` 和 `openpyxl` 是否可导入；缺失时报告依赖问题，不要修改宿主机全局 Python。
- 当前算法生成包含突变密码子的正向引物，反向引物是它的完全反向互补序列。
- 使用 `--preset quikchange_ii`（默认）或 `--preset primer3_generic` 明确选择判定和排名逻辑。
- QuikChange 模式使用经验 Tm、最短侧翼、软长度/GC/3′ GC 建议及 Primer3 估算自由能成本；可选启用目标长度、目标 Tm 和最高 Tm 扩展；Primer3 Tm、发卡和二聚体仍作为辅助指标。
- 不要宣称 QuikChange 排名与网站逐项一致；网站的 mfold 2.3 数据、经验密码子权重和全部内部权重未公开。
- 将结果视为实验设计候选，提醒用户在订购和实验前复核序列、阅读框与反应体系。

## 工作流

### 1. 确认输入

至少取得：

1. 母本 DNA 序列或 FASTA/纯文本序列文件路径。
2. 一个或多个突变，例如 `C231A`、`C231A:GCC`、`C267NNK`。

只有缺少上述必需输入时才询问用户。接受用户给出的长度、Tm、GC、结构阈值和输出格式；未指定时使用包默认值。

如果用户直接粘贴 DNA，把它保存为当前任务输出目录中的 FASTA 文件，不修改用户已有文件。位置是从 1 开始的氨基酸坐标，默认输入应为从起始密码子到终止密码子的完整 CDS。

不要擅自补充目标密码子：

- `C231A`：让程序选择相对母本改动最少的同义密码子。
- `C231A:GCC`：仅在用户明确指定或要求严格复现某个密码子时使用。
- `C267NNK` / `C267NNS`：设计简并饱和突变引物。

### 2. 选择预设和参数

先按实验体系选择：

- 使用 Agilent QuikChange II 或评价该网站引物：选 `--preset quikchange_ii`。默认 QuikChange Tm ≥78℃、密码子外侧最短正确匹配9 bp（等价于中央突变碱基距末端至少10）、优选长度≤45 bp、绝对长度≤60 bp、优选GC≥40%。长度超过45 bp、GC低于40%或3′端不是G/C属于 `advisories`，不会单独导致失败。
- 普通自定义筛选或需要沿用原算法：选 `--preset primer3_generic`。默认长度 `25/35/45 bp`、Primer3 Tm `65/70/75℃`、GC `40%–65%`。

QuikChange 的参数必须使用独立名称，避免与通用模式混淆：

- `--qc-min-tm` 是最低经验 Tm 硬约束。
- `--qc-opt-tm` 是可选排序目标；`--qc-max-tm` 是可选硬上限。
- `--min-length` 是两种预设共用的最短长度硬约束。
- `--qc-opt-length` 是可选排序目标，`--qc-preferred-max-length` 是软上限，`--qc-absolute-max-length` 是硬上限。
- `--qc-opt-length`、`--qc-opt-tm`、`--qc-max-tm` 默认关闭，属于本包扩展，不是 Agilent 官方默认参数。不传时保留原 QuikChange 兼容行为。
- 不要把通用模式的 `--opt-length`、`--min-tm`、`--opt-tm`、`--max-tm` 用于 QuikChange；它们不会在该预设下参与判定或排名。

两种预设都默认 `center_slack=2`、每个位点返回3个候选，并报告 Primer3 Tm、GC、发卡和同源二聚体。

按用户意图调整：

- 要求突变密码子严格居中：传 `--center-slack 0`。
- 每个位点只要一对引物：传 `--num-return 1`。
- 输入不是完整 CDS：根据实际情况传 `--allow-non-atg-start`、`--allow-missing-stop`；只有用户确认无需 CDS 校验时才传 `--no-cds-validation`。
- 需要查看失败原因：传 `--include-near-misses`。
- 用户设置结构阈值时才传 `--max-hairpin-tm` 或 `--max-homodimer-tm`；未设置时结构指标只报告、不筛除。

自定义参数或解释输出列时读取 `references/cli-reference.md`。

### 3. 执行设计

先从当前已加载的 `SKILL.md` 确定 `<skill-dir>`，不要假定 Skill 的安装位置。引用包含空格或中文的输入、输出和脚本路径。

单个突变示例：

```bash
python "<skill-dir>/scripts/run_primer_design.py" \
  --sequence "<sequence-file>" \
  --mutation C231A \
  --output "<output-file>.xlsx" \
  --preset primer3_generic \
  --min-length 25 --opt-length 35 --max-length 45 \
  --min-tm 65 --opt-tm 70 --max-tm 75 \
  --min-gc 40 --max-gc 65
```

多个突变时重复传入 `--mutation`，或使用 `--mutations` 指向 TXT/CSV 清单：

```bash
python "<skill-dir>/scripts/run_primer_design.py" \
  --sequence "<sequence-file>" \
  --mutation C231A \
  --mutation Y248F \
  --mutation C267NNK \
  --output "<output-file>.xlsx" \
  --preset quikchange_ii \
  --include-near-misses
```

用户明确要求 QuikChange 长度 `25～45 bp`、目标 `35 bp`、Tm `65～75℃`、
目标 `70℃` 时，使用：

```bash
python "<skill-dir>/scripts/run_primer_design.py" \
  --sequence "<sequence-file>" \
  --mutation C231A \
  --output "<output-file>.xlsx" \
  --preset quikchange_ii \
  --min-length 25 \
  --qc-opt-length 35 \
  --qc-preferred-max-length 45 \
  --qc-absolute-max-length 45 \
  --qc-min-tm 65 \
  --qc-opt-tm 70 \
  --qc-max-tm 75
```

向用户说明：`opt` 只调整内部排名，不保证第一名恰好等于目标；`min`、
`absolute-max-length` 和 `max-tm` 才决定 PASS/NEAR_MISS；`65/70/75℃`
是用户扩展区间，不是 Agilent 官方最低 `78℃`。

不要自动安装、升级或替换 Python 依赖。如果启动脚本报告依赖缺失，停止并清楚报告缺失项；只有用户授权且当前环境明确为隔离环境时，才使用当前 Skill 的 `requirements.txt` 安装。

### 4. 评价网站或其他外部引物

批量输入 CSV/XLSX 时，每行一对引物，至少包含 `mutation` 和
`forward_sequence`；`reverse_sequence` 可省略。运行：

```bash
python "<skill-dir>/scripts/run_primer_evaluation.py" \
  --sequence "<sequence-file>" \
  --external-primers "<external-primers>.xlsx" \
  --output "<ranking-output>.xlsx" \
  --preset quikchange_ii
```

直接评价一条正向引物时传 `--mutation` 和 `--primer`。省略 `--reverse` 时自动
生成反向互补；结果直接以 JSON 打印：

```bash
python "<skill-dir>/scripts/run_primer_evaluation.py" \
  --sequence "<sequence-file>" \
  --mutation C231A \
  --primer "<forward-sequence>" \
  --preset quikchange_ii
```

区分以下结果：

- `generated`：网站 F/R 被当前候选生成范围实际枚举到。
- `inserted`：未被实际枚举，但已按同一评分键插入候选池计算名次。
- `overall_rank`：PASS 全部优先后的总排名。
- `pass_rank` 或 `near_miss_rank`：在同状态子集合中的排名。
- `UNALIGNED`：无法按指定突变位点对齐，不能计算排名。
- `preset`：本次评价使用的规则；不要跨预设直接比较名次。
- `quikchange_tm_*`：QuikChange 经验公式 Tm；通用模式为空。
- `advisories`：QuikChange 优选项提示，不等于硬失败。

外部评价使用相同目标密码子的候选作比较。读取批量输入格式或解释全部评价字段时，
查看 `references/cli-reference.md`。

### 5. 验证设计输出

按退出码处理：

- `0`：所有突变都有 `PASS` 候选。
- `2`：任务正常完成并生成文件，但至少一个突变没有 `PASS` 候选；不要误报成程序崩溃。
- `1`：输入、参数、序列、突变格式或文件读取错误；修正明确问题后最多重试一次。

确认输出文件存在且非空，然后运行：

```bash
python "<skill-dir>/scripts/summarize_results.py" \
  "<output-file>.xlsx"
```

如果导出时包含近似候选，并希望汇总程序在没有 PASS 时展示最佳近似候选，添加 `--fallback-near-miss`。

### 6. 汇报结果

向用户报告：

- 输出文件绝对路径。
- 成功位点数和总位点数。
- 使用的预设和所有非默认 QuikChange 扩展参数，以及每个位点排名第一的 F/R 序列（5′→3′）、长度、Primer3 Tm、GC、状态。
- QuikChange 模式还报告 QuikChange Tm、估算自由能成本和 `advisories`。
- 自动选择的目标密码子或用户指定的目标密码子。
- 所有假设、`NEAR_MISS` 原因和 CDS 校验放宽项。
- QuikChange 模式兼容公开协议，但不是网站未公开内部权重的完全复刻；启用 `qc-opt-*`/`qc-max-tm` 后属于本包扩展，结果不同仍可能发生。

评价外部引物时还要报告总排名及分母、PASS/NEAR_MISS 分类排名、
`generated/inserted`、超限原因以及是否与当前完全互补策略兼容。

不要把 `pair_score` 描述为实验成功率。它只是当前程序内部使用的惩罚分，越低排名越靠前。
