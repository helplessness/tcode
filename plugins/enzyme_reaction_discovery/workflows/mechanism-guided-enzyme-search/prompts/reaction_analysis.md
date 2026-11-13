# 阶段 1：反应定义与净变化分析

表达约定：工具参数的科学说明使用简洁中文；通行酶名、引用标识和 SMILES 保留原文。按字段各写一个要点，不抄长段原文，不以截断文字满足长度限制。

## 净氧化还原的自洽检查

分别核对整体氧化当量与各步当量，不以单次官能团变化的电子数代替全程电子数。采用候选原子映射时，按键连接估算氧化态变化，并与净失氢、质子/水和电荷收支交叉检查；两种估算不一致时明确保留未知，不能同时提交互相矛盾的确定结论。整体电子转移与分步电子转移之和必须一致。元素计数相同不证明氧原子来源相同；氧保留或交换只能作为未核验映射假说。净氧化偶联不自动等于借氢：须区分电子最终外排与回用于还原。

## 目标

把 Workflow `input_payload` 转成可审计的反应定义，只完成保守文本检查和净变化描述，为下游自动推理提供事实边界。本阶段禁止出现辅因子、酶家族或路线推荐。

## 执行协议

1. 本阶段提示已嵌入所需 Skill 约束，不调用 `skill`。
2. 取 `Workflow 运行时上下文.run_id` 作为插件 `run_id`；取 `stage_execution.idempotency_key` 作为阶段幂等键。
3. 若初始化回执为 `valid=true`，直接使用已规范化的输入。若没有成功回执，或回执为 `deferred_to_model=true`，先由你阅读原始请求，识别底物和产物，再调用一次 `enzyme_run_init`。保留原文于 `reaction.natural_language_request`，同时把识别结果写入 `reaction.reactants` 和 `reaction.products` 数组，每个组分单独填写 `{"smiles":"原文结构"}` 或 `{"name":"原文名称"}`。对 `底物>>产物` 写法，按箭头区分反应两侧，按点号拆分各侧独立组分，保留原文的结构、电荷和构型。适用于自然语言、分行标注及反应 SMILES。已有明确组分则保留，缺少结构时保留名称，不能猜造 SMILES。初始化成功后立即提交净变化，不在初始化中提交候选、机制或净变化。
4. 如果 Tool 返回输入错误，明确输出阻塞和错误代码，不生成化学结论，也不要通过空文档或占位对象反复尝试。
5. 如果有效，以 `enzyme_run_init.result.input_validation` 和已持久化的规范化输入为事实边界；模型只负责紧凑净变化判断。
6. 构建紧凑判断。`fact` 只能来自输入或 Tool；文本差异解释为 `inference`；组分角色猜测为 `hypothesis`；无法验证的原子映射、质量/电荷守恒为 `unknown`。
7. 只调用一次 `enzyme_reaction_commit`，传入 `evidence_statements`、`net_changes`、`component_interpretations`、`assumptions`、`warnings` 与 `unresolved_questions`。Tool 会从 run 读取规范化输入，确定性生成、校验并提交 `01_reaction_analysis.json`；不得复制完整输入、探测 Schema 或分别提交文件。`idempotency_key` 使用阶段幂等键；只有 `stage_execution.attempt > 1` 才设置 `allow_supersede=true`。
8. `user_confirmed=false` 只作为输入来源状态保留，不是人工门禁。只要输入有效，任何歧义都以 warning、unknown、低置信 hypothesis 或下游 falsifier 自动传递；不得写“等待用户确认/批准”，不得请求人工接管，也不得因此停止自动流程。
9. 提交参数最多修正一次；仍失败就原地阻塞。不得重新初始化、重复校验已通过内容或循环提交。

## 参数格式边界

- `enzyme_reaction_commit` 的顶层必须同时包含 `run_id` 和六个数组：`evidence_statements`、`net_changes`、`component_interpretations`、`assumptions`、`warnings`、`unresolved_questions`。数组直接使用 JSON `[]`，不要把它写成带转义引号的字符串，不要把整个数组嵌进单个文本字段。
- 先为全部必填字段组织完整结构，再填写简短判断；每个事实／净变化只表达一个独立要点，来源用短引用路径。不要用大量重复输入证据挤掉后面的净变化与未知项。无内容的可空数组明确提交 `[]`；`evidence_statements` 与 `net_changes` 不可为空，内容必须由你分析，运行器不会补写。
- `evidence_statements[].kind` 使用 `fact/inference/hypothesis/counterevidence/unknown`。
- `net_changes[].certainty` 和 `component_interpretations[].certainty` 只能使用 `observed/inferred/uncertain`；无法确定时填 `uncertain`，不能把证据类别 `unknown` 填进 certainty。
- evidence 条目使用 `statement_id/kind/statement/source_refs`；净变化条目使用 `statement_id/event/certainty/basis`。两种对象不能混放，不提交占位条目。
- 参数错误修正时保留原判断和其它字段，只修错误位置；发送完整合法 JSON。用短句表达，不重复抄写同一组 SMILES 或初始化返回。

## 必须保留

- 原始与规范化组分的对应关系；
- 每条净变化的 `statement_id/event/certainty/basis`；
- component role 的多种解释；
- assumptions、warnings、unresolved_questions；
- `chemical_structure_validated=false`、`atom_mapping_completed=false`。

## 化学一致性边界

以实际 SMILES 为准，不将名称相近的异构体或不同质子化态静默替换。若提出候选原子对应关系，放入 assumptions 并标为未验证；在同一分支内固定编号，核对该对应关系是否真的支持所写成断键。未编号 SMILES 不足以证明唯一映射。将碳骨架/杂原子去向、显式电荷、水/质子及电子收支的明显缺口记入 warnings/unresolved_questions；不能因为输入未列电子供受体就认定无净氧化还原。不新增结构解析或守恒验证工具，无法判断时保留 unknown。

## 完成格式

最终回复只包含一个 JSON 代码块，直接使用 Tool `result.stage_receipt` 的内部对象作为顶层，禁止再包一层 `stage_receipt` 或 `result`。块外不添加解释、标题或阶段摘要。不要复制 01 正文，不要输出 Workflow candidate/final marker。
