

### **Open-Rosalind 的评估是“可执行 agent benchmark”：每条测试样例不只检查最终答案，还检查是否路由到正确 skill、是否调用了预期工具、工具输出是否形成 evidence、trace 是否完整，以及结构化结果是否通过规则校验。**

它的评估体系可以拆成三层：**测试集构建格式**、**运行过程记录**、**最终指标计算**。

### **1. 它评估的不是生物知识问答，而是 bio-agent 的执行能力**

这个项目反复强调 BioBench 不是测 LLM 参数知识，而是测一个生物医学 agent 能不能正确执行任务。也就是说，测试题不是简单问“BRCA1 是什么”，而是要求系统真的走到对应工具，例如 UniProt、PubMed、本地 sequence analyzer、mutation diff 等，并把工具返回结果作为证据。BioBench v1 的设计原则也明确写了：每个任务必须触发至少一个 skill，测试的是 system execution，不是 parametric knowledge；输出要可验证，避免主观评分。

从这个角度看，它的 benchmark 更像是一个 **agent execution benchmark**，而不是传统 QA benchmark。传统 QA 主要问“答案对不对”，Open-Rosalind 问的是“你是怎么得到这个答案的”：路由是否正确、工具是否正确、workflow 是否正确、fallback 是否触发、trace 是否完整、最终回答是否被 evidence 支撑。

### **2. 测试集有哪些版本**

当前项目里主要有三个 BioBench 测试集版本。

| 版本 | 文件 | 任务数量 | 主要评估对象 |
|---|---:|---:|---|
| BioBench v0 | `benchmark/biobench_v0.jsonl` | 32 | 基础 skill、工具调用、证据与 trace |
| BioBench v1 | `benchmark/biobench_v1.jsonl` | README 显示 49 | workflow、edge case、follow-up、stress |
| BioBench v0.3 | `benchmark/biobench_v03.jsonl` | 10 | 多步骤 harness 任务 |

BioBench v0 是最基础的 Mini BioBench，包含 5 类任务：`sequence_basic`、`literature_search`、`protein_annotation`、`mutation_effect`、`protocol_reasoning`。项目的 `BENCHMARK.md` 里说明，v0 的 32 个任务会固定用于每次 release 的回归测试，并报告五个固定指标。 

BioBench v1 在 v0 基础上扩展，重点测试 workflow correctness、multi-step reasoning、fallback strategies 和 session memory。它新增了 workflow、edge case、follow-up、stress 等类型。设计文档里写的是 50 个任务，但当前 README 显示最新 BioBench v1 是 49 个任务，所以实际仓库当前版本和设计稿之间可能有一处数量差异；从评估逻辑看，核心是 v1 增加了对 workflow、fallback、追问上下文和压力场景的检查。

BioBench v0.3 则专门评估 multi-step harness。它不是普通 `/api/analyze` 单步任务，而是通过 CLI 跑 `open_rosalind.cli task run`，检查 task 是否完成、步骤数量是否足够、evidence pool 是否聚合、最终报告是否包含预期关键词。

### **3. 测试集的 JSONL 格式**

测试集采用 **JSONL** 格式，也就是一行一个 JSON 对象。每行一个测试任务。一个典型样例如下：

```json
{
  "id": "seq-01",
  "category": "sequence_basic",
  "mode": "auto",
  "input": ">tiny ATGAAACGT",
  "expected_skill": "sequence_basic_analysis",
  "expected_tools": ["sequence.analyze"],
  "expected_keywords": ["dna", "9"],
  "must_have_evidence": true,
  "must_have_trace": true,
  "checks": {
    "evidence_path": "sequence_stats.records.0.type",
    "equals": "dna"
  }
}
```

它把“一个 agent 任务应该怎么执行”写成了可验证规范，而不是只写一个 gold answer。这里的 `input` 是用户输入，`mode` 是运行模式，`expected_skill` 是预期路由到的 skill，`expected_tools` 是预期 trace 中必须出现的工具，`expected_keywords` 是最终回答或 evidence 中应该出现的关键词，`must_have_evidence` 和 `must_have_trace` 规定是否必须有证据和执行轨迹，`checks` 则是对结构化返回结果做精确校验。当前 v0 文件中的任务就覆盖了 DNA/蛋白序列分析、PubMed 文献检索、UniProt 注释、突变影响分析和混合型 protocol reasoning。

### **4. `checks` 字段怎么做结构化校验**

`checks` 是这个 benchmark 的核心。它不是用自由文本相似度评分，而是从返回的结构化 JSON 里取某个路径，然后用规则判断。

例如当前页面里有这些校验方式：

```json
{
  "evidence_path": "sequence_stats.records.0.gc_percent",
  "approx": 71.43,
  "tol": 0.5
}
```

这表示从 `response.evidence.sequence_stats.records[0].gc_percent` 里取值，判断它是否约等于 71.43，误差容忍 0.5。

另一个例子：

```json
{
  "evidence_path": "entry.length",
  "equals": 110
}
```

这表示检查工具 evidence 中的 UniProt entry 长度是否等于 110。

runner 脚本支持的校验路径包括 `evidence_path`、`annotation_path`、`summary_contains`、`notes_contains`。支持的操作包括 `equals`、`approx`、`starts_with`、`contains`、`contains_any`、`min`、`min_length`。这使得它可以同时检查数值、字符串、列表、摘要文本和 fallback notes。

### **5. v1 测试集额外增加了 workflow 字段**

v1 里出现了一个重要字段：`expected_workflow`。它不是检查 skill 名，而是检查 trace 里实际出现的工具序列。例如：

```json
{
  "id": "wf-01",
  "category": "workflow",
  "mode": "auto",
  "input": "Analyze this protein: MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKT",
  "expected_skill": "sequence_basic_analysis",
  "expected_workflow": ["sequence.analyze", "uniprot.search"],
  "expected_keywords": ["protein", "56"],
  "must_have_evidence": true,
  "must_have_trace": true,
  "checks": {
    "evidence_path": "uniprot_hint.hits",
    "min_length": 0
  }
}
```


### **6. 每条测试过程记录什么**

从 runner 脚本看，每条任务最终会生成一条 row，里面记录了这次任务的详细评测结果。主要包括：

| 字段 | 含义 |
|---|---|
| `id` | 测试任务 ID |
| `category` | 测试类别 |
| `skill_correct` | 返回的 `response.skill` 是否等于 `expected_skill` |
| `skill_got` | 实际路由到的 skill |
| `skill_expected` | 预期 skill |
| `tool_called` | 预期工具是否都出现在 trace 中 |
| `tools_got` | 实际 trace 中的工具名列表 |
| `tools_expected` | 测试集声明的预期工具 |
| `evidence_ok` | 是否满足 evidence 要求 |
| `trace_ok` | 是否满足 trace 要求 |
| `keyword_ok` | 关键词命中是否达标 |
| `keyword_hit` / `keyword_total` | 命中关键词数和总关键词数 |
| `check_passed` | 结构化 check 是否通过 |
| `check_reason` | check 的解释，例如 got/expected |
| `task_passed` | 该任务是否整体通过 |
| `failed` | 是否发生 hard error |
| `fail_reason` | 失败原因 |
| `confidence` | response 返回的置信度 |
| `session_id` | response 返回的 session id |
| `elapsed_s` | 单任务耗时 |

也就是说，最终结果文件不只是一个总分，而是能看到每个任务到底死在哪一步：是 skill 路由错了，是工具没调用，是 evidence 为空，是 trace 缺失，是关键词没命中，还是结构化值不对。

### **7. trace 里检查什么**

runner 主要从 `response.trace_steps` 里取工具调用名：

然后判断预期工具是否都被调用了。

在项目设计里，trace step 通常还会包含每次工具调用的输入、输出、延迟和状态。`BENCHMARK.md` 也提到，mvp2 之后 trace 标准化，加入了 `latency_ms` 和 `status`，从而可以评估 trace completeness，并支持后续统计 latency P95。

换句话说，它的 trace 不是为了展示“模型思考过程”，而是为了记录**可审计的工具执行过程**：调用了什么工具、是否成功、耗时多久、输出是否形成 evidence。

### **8. evidence 怎么判断**

runner 里 evidence 的判断逻辑是：取 `response.evidence`，排除 `annotation` 和 `notes` 这类辅助字段后，只要有非空内容，就认为有 evidence：


如果测试样例里 `must_have_evidence` 是 `true`，那 `evidence_ok` 必须为真。也就是说，回答不能只是自然语言总结，它必须带着工具返回的结构化证据

例如序列分析任务会检查 `sequence_stats.records.0.type`、`length`、`gc_percent`；PubMed 任务会检查 `pubmed.count`；UniProt 任务会检查 `entry.id`、`entry.organism`、`entry.length`；突变任务会检查 `mutation.n_differences`、`mutation.differences.0.flags` 等。这些都来自工具输出，而不是最终回答文本。

### **9. 关键词怎么判断**

除了结构化 check，它还做关键词检查。runner 会把 `response.summary` 和 `response.evidence` 序列化后的文本拼在一起，然后检查 `expected_keywords` 是否出现。关键词是大小写不敏感匹配。

通过标准不是 100% 命中，而是 **至少 60% 关键词命中**。脚本里写的是：


这说明关键词检查是辅助性的，主要防止最终摘要和证据完全偏题；真正的硬判断还是 skill、tool、evidence、trace 和 structured check。
### **10. 单任务如何判定通过**

v0/v1 的单任务通过条件非常严格。runner 里的 `task_passed` 要求以下条件全部为真：


也就是说，一条任务要通过，必须同时满足：

1. `response.skill` 等于 `expected_skill`；
2. `expected_tools` 里的每个工具都出现在 `trace_steps`；
3. `checks` 的结构化规则通过；
4. 关键词命中达到阈值；
5. 如果要求 evidence，则 evidence 非空；
6. 如果要求 trace，则 trace 非空。

这也是为什么它适合评估 agent：它不是只看最后一句话有没有说对，而是同时看**选 skill、调工具、拿证据、留轨迹、结构化结果**是否都正确。 

### **11. 最终指标怎么计算**

v0/v1 的核心指标有五个，对所有任务的 row 做平均：

| 指标 | 计算方式 | 含义 |
|---|---|---|
| `task_accuracy` | `task_passed` 为真的任务比例 | 严格总通过率 |
| `tool_correctness` | `tool_called` 为真的任务比例 | 是否调用了所有预期工具 |
| `evidence_rate` | `evidence_ok` 为真的任务比例 | 是否有符合要求的 evidence |
| `trace_completeness` | `trace_ok` 为真的任务比例 | 是否有符合要求的 trace |
| `failure_rate` | `failed` 为真的任务比例 | 是否发生 hard error |

脚本还额外统计了 `skill_correct_rate`，以及 `by_category` 分类指标。分类指标会按 `category` 分组，分别算 task accuracy、tool correctness、evidence rate、trace completeness 和 failure rate。

`BENCHMARK.md` 对五个指标的定义更明确：Task accuracy 是严格 pass/fail，要求 skill 正确、工具正确、结构化 check 通过、至少 60% 关键词出现、evidence/trace 要求满足；Tool correctness 是预期工具是否都被调用；Evidence rate 是 response 是否有非空 evidence；Trace completeness 是是否有非空 trace；Failure rate 是 HTTP 5xx、网络异常或所有工具调用都报错这类 hard error。

### **12. 结果文件会输出什么**

一次运行会产出三个主要文件。

```text
benchmark/results.json
benchmark/results.md
benchmark/history.jsonl
```

`results.json` 保存机器可读的完整结果，结构大致是：

```json
{
  "meta": {
    "version": "v0.1",
    "date": "...",
    "git_sha": "...",
    "model": "...",
    "base_url": "...",
    "tasks_file": "...",
    "elapsed_s": 123.4
  },
  "metrics": {
    "n_tasks": 32,
    "task_accuracy": 1.0,
    "tool_correctness": 1.0,
    "evidence_rate": 1.0,
    "trace_completeness": 1.0,
    "failure_rate": 0.0,
    "skill_correct_rate": 1.0,
    "by_category": {}
  },
  "rows": []
}
```

`results.md` 是人类可读报告，`history.jsonl` 则每次 append 一行，把本次运行的版本、日期、模型、git sha 和核心 metrics 记录下来，方便生成跨版本 benchmark history。 


### **13. 用一个具体例子**

以当前页面里的 `ann-01` 为例：

```json
{
  "id": "ann-01",
  "category": "protein_annotation",
  "mode": "auto",
  "input": "P38398",
  "expected_skill": "uniprot_lookup",
  "expected_tools": ["uniprot.fetch", "uniprot.search"],
  "expected_keywords": ["BRCA1", "Homo sapiens"],
  "must_have_evidence": true,
  "must_have_trace": true,
  "checks": {
    "evidence_path": "entry.id",
    "equals": "BRCA1_HUMAN"
  }
}
```

评测时 runner 会把 `P38398` 发给 `/api/analyze`。如果 agent 返回的 `skill` 不是 `uniprot_lookup`，任务直接不通过；如果 trace 里没出现 `uniprot.fetch` 和 `uniprot.search`，也不通过；如果 evidence 为空，不通过；如果 trace 为空，不通过；如果 `response.evidence.entry.id` 不是 `BRCA1_HUMAN`，不通过；如果 summary/evidence 里没有命中足够的 `BRCA1`、`Homo sapiens` 关键词，也不通过。

这就是它的核心评测思想：**每个样例都是一份可执行规格，不只是一个输入输出对。**

### **14. 这个评估方法的特点**

Open-Rosalind 的评估有几个明显特点。第一，它把 agent 的过程纳入评分，而不是只看最终文本。第二，它用结构化 JSON path 做确定性校验，避免过度依赖主观判断。第三，它把 evidence 和 trace 作为硬要求，这符合它“tool-first”和“evidence-grounded”的设计。第四，它保留了 per-task rows 和 history，这样可以做回归测试和跨版本比较。第五，v1 和 v0.3 开始覆盖 workflow、fallback、follow-up 和 multi-step harness，比 v0 更接近真实 agent 使用场景。

它也有局限：很多检查是规则式的，任务集规模不大；v0/v1 主要检查工具是否出现和关键字段是否正确，对答案质量、文献综述深度、复杂科学推理的评估有限；v0.3 的 runner 当前实现比设计文档更简单，主要看 completed、steps、evidence 和关键词，还没有完全实现设计文档里所有 harness 指标。

所以一句话概括：**这个项目把 benchmark 写成 JSONL 的 agent 执行规格；运行时逐条调用真实服务或 CLI，记录 response、skill、trace、evidence、关键词、结构化 check 和耗时；最后用严格规则汇总 task accuracy、tool correctness、evidence rate、trace completeness、failure rate，以及 harness 的 completion/workflow/evidence 聚合指标。**