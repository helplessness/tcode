# mechanism_and_capability：执行协议

对共价辅因子或酶共价中间体的候选，检查完整催化循环：形成中间体、目标键变化、产物脱离及催化主体再生。消除或重排本身不自动等于游离产物释放；释放步骤没有证据时明确列为预测或未匹配能力，比较可行的终止方式而不指定某个酶或目标答案。

先复核上游净变化中的局部与整体氧化还原判断。局部官能团变化不自动意味着外部氧化还原当量需求；同时考虑内部补偿性重排、不同原子对应和离去基团的计量。整体电子与原子对应尚未确认时，不得以“缺少氧化辅因子”直接淘汰一条机制；把计量可行性和具体酶机理支持分开登记为待检验问题。不得为配平合理而虚构共价中间体或酶活性。

先读取工具回执的 `source_policy`。公共来源关闭时，跳过公共实体解析、关系、机制片段及反应检索，通过 `enzyme_entity_workspace_query` 查询允许的私有来源；不得把公共来源拒绝访问当作流程失败。两种来源均关闭时，只提出明确标注“未经过本体证据核实”的模型假说，引用数组为空，记录覆盖缺口并继续提交。来源关闭仅约束取证，不能成为 `UNVERIFIED` 或整段阻塞的理由。

先从目标净变化独立形成互异、可证伪的竞争机制，再用本插件的本体实体、关系与 Evidence 核验局部事实。实体命中不能反向替代目标事件分析，也不能因有引用而获得机制排序加分。

目标是把反应净变化转换成一到三个简洁、可证伪的机制与催化能力假说。可引用知识仅限本插件当前结构化本体发布包中经 Assertion/Evidence 核验的内容；禁止联网、读任意文件、读取历史运行或评测答案。公开结果只说明本体证据覆盖及其对科学结论的影响，不主动报告来源记录数量或运行实现。

先用上游回执中的 `run_id` 调用一次 `enzyme_entity_run_context(stage="mechanism_and_capability")`。将 16 次工具作为常规工作预算，系统硬上限为 20 次，额外 4 次只用于提交校验修正。严格按以下预算执行：`resolve` 最多 3 次、`relations` 最多 4 次、`reaction_analogs` 最多 2 次、`substrate_branch_scan` 最多 2 次、`mechanism_fragments` 最多 3 次、`read_evidence` 最多 3 次，至少预留 2 次给 `hypotheses_commit` 及其校验修正。优先解析用户明确给出的直接反应物和产物；不要用同义词连续试探整个知识库。若预算内没有本地事实，仍应提交仅含类比或 `model_background_unverified` 预测的可证伪假说，并把缺口写清楚，不得以 `UNVERIFIED` 代替阶段提交。

机制生成必须同时保留两条推理视角，不能只按官能团变化拆成多酶步骤：

1. `分步能力视角`：分别考虑成键/断键、氧化还原、消除、水解、异构化等局部能力。
2. `共价辅因子统一视角`：检查 ThDP、PLP、FAD、SAM、CoA 等是否可能先与底物形成共享中间体，再由不同第二底物、离去基团或终止方式产生分支产物。

先从阶段一的净变化提取反应中心、成断键与产物方向，调用一次 `enzyme_entity_mechanism_fragments(required_terms=[...])` 或按已解析转化/能力 ID 查询局部图；查询词只能来自用户反应或第一阶段事件，不预填候选酶名与辅因子答案。随后对每个已解析的直接反应物至少执行一次“同底物反应扫描”，调用 `enzyme_entity_substrate_branch_scan(substrate_ids=[...])`，它会一次性返回包含该底物的已报道反应、催化主体、催化主体的辅因子、其他左侧参与物、产物、转化模式、中间体和四类触发信号；只有需要补充证据或分页时才再调用 `enzyme_entity_relations` 或 `enzyme_entity_reaction_analogs`。目标产物未解析成功不能中止这项扫描。扫描结果的 `weak_hypothesis_required=true` 仅表示该线索必须进入有界弱线索池接受评估，不表示必须进入最终假说，更不是目标活性证据。

同底物扫描只是高特异性召回通道，不是候选门槛。还必须执行至少一次“跨底物机制扩展”（若首轮净变化片段查询已经覆盖该通道，可沿缺失事件补查）：优先用 `enzyme_entity_reaction_analogs` 按 `transformation_ids` 或 `capability_ids` 查相同反应中心/催化能力；必要时用 `enzyme_entity_relations` 沿共价中间体、辅因子或终止方式做一跳扩展。四类通道分别是 `同底物`、`同反应中心`、`同机制`、`同中间体或辅因子`；本地实体不具备某条通道时明确记为覆盖缺口。不同底物不能成为排除理由，共享辅因子也不能单独成为纳入理由。至少一个最终假说必须说明四类通道中实际检索了哪些、命中了哪些以及仍缺哪些。

在解析出参与物、转化、能力、辅因子或中间体 ID 后，调用 `enzyme_entity_mechanism_fragments`。它只把当前本体中的 Reaction 局部关系确定性投影为片段，不执行文本知识库检索，也不产生目标答案。首轮最多三个互补查询；只有针对缺失事件时才补查，查询次数不参与科学评分。每个进入最终假说的片段都必须形成 `counterfactual_bridge`：分别填写 `shared_events`、`missing_events`、`conflicting_events`、`native_termination`、`target_alternative_termination` 和 `unsupported_assumptions`。片段内 Assertion/Evidence 仍需通过 `enzyme_entity_read_evidence` 回读后才能写入事实或类比引用。

当本地事实同时出现以下线索中的任意两项时，将其加入内部有界弱线索池：同一直接底物、共价型辅因子、天然第二底物参与、相似离去基团、可替代的中间体捕获或释放方式。命中只赋予竞争资格。内部最多收集 6 条，必须回读正文，并至少覆盖反应中心、关键键变化或中间体之一、能解释一个未闭合关键步骤；只有同底物或只有同辅因子时不能晋级。按“核心键变化＋辅因子作用＋中间体类型＋终止/释放方式”聚类后最多保留 3 类，最多 3 条进入最终假说竞争。违反元素骨架、不可补偿电子守恒或目标净变化等硬约束时淘汰；原子映射、氧化态、底物适配或终产物释放存在可验证的软冲突时降低可信度并写入反对理由。若某条代表其他高排名机制未覆盖的新机制类型，可占 1 个机制多样性竞争名额，但不保证进入最终假说、候选或报告。

若命中已报道的同底物、辅因子或第二底物捕获分支，只把有证据的天然事件放入事实；未知共价中间体与不同捕获/终止方式应进入待门控的类比或预测，不保证保留。晋级时明确分开“已报道天然分支”和“未验证目标分支”，允许把未知中间体写成文字而不虚构实体 ID。

关系摘要和类比结果不能直接作为科学引用；凡准备写入 `fact_claims` 的内容，都要调用 `enzyme_entity_read_evidence` 回读原文。

为避免把卡片级证据错误拼接成蛋白级事实，`fact_claims` 最多写 2 条，并遵守以下提交规则：每条优先只引用 1 个 Assertion；`evidence_ids` 只能取该 Assertion 返回的 `evidence_ids` 子集，不能把本轮读过但未与该 Assertion 连接的 Evidence 混入；只有 Assertion 的 `attribution` 明确为 `exact_protein` 时才用 `reported_exact`，其他情况一律用 `reported_related`。无法确认 Assertion 与 Evidence 配对时，宁可省略该事实并写入 `evidence_gaps`。调用提交工具前逐条自检这三项；若提交返回校验错误，立即按错误删除多余引用或降级归属后重试，不要重新查询。

将内容放进三个分离的 claim 数组：

- `fact_claims` 只收本体中已经报告的事实，使用 `reported_exact` 或 `reported_related`，同时填写 Assertion 与已回读 Evidence。
- `analogy_claims` 只描述从既往反应到目标反应的迁移，固定使用 `mechanistic_analogy`。即使引用事实，也不能声称目标活性成立。
- `prediction_claims` 使用 `model_background_unverified`；只有本轮确实运行了计算方法时才可用 `computed_prediction`，并在 `scope_or_method` 写明方法和限制。

每个假说都要列出所需能力、可选辅因子和中间体的实体 ID；本地没有对应实体时，不得虚构 ID，把缺失内容写入 `evidence_gaps`。共享辅因子不代表共享活性，家族事实不得归给具体蛋白，当前包未记录不表示生物学上不存在。弱假说的事实部分只陈述同底物、辅因子和天然分支；替代捕获、旁路释放及目标产物形成必须分别放进 `analogy_claims` 或 `prediction_claims`。每个假说仅保留最关键的 1–2 条类比、1–2 条预测、1–2 个反对理由和 1–2 个可证伪实验，避免因提交参数过长而失败。

形成最终一到三个假说后，按槽位依次调用 `enzyme_entity_hypothesis_stage` 暂存；每条暂存对象必须包含完整 `counterfactual_bridge`。全部暂存成功后复制最新 context 的 `context_digest`，调用 `enzyme_entity_hypotheses_commit`，传入 `hypotheses=[]` 让账本原子校验并提交全部草稿。提交前确认顶层对象同时含有 `run_id`、`context_digest`、`hypotheses` 和 `global_knowledge_gaps`。若校验失败，修正对应槽位后必须重新暂存该完整假说，再重发完整提交对象；不得只重发假说片段，不得遗漏 `run_id`，也不得把科学上的“目标活性未验证”作为流程失败信号。成功后只返回阶段回执。
本阶段沿用 Workflow 上游回执的 `run_id`；阶段结束时返回 `enzyme_entity_hypotheses_commit` 的 `result` 对象。


当前 Workspace 私有知识参与规则：使用 `enzyme_entity_workspace_query` 按相关实体 ID 或精确名称查询（scope=both），保留公共与私有分组、条件及冲突；需要时用 next_cursor 续页，不能把一页当成全库。若返回 available=false，只使用公共工具并说明私有能力不可用。候选阶段必须先冻结 baseline，再查询私有知识。私有查询返回的 assertion ID 与 evidence_ids 可直接用于现有账本；引用前必须调用 `enzyme_entity_read_evidence`，不得编造 ID。已审核的 kind=hypothesis/analogy/annotation 仍不是已报道事实；opposes 不能充当候选正向支持。私有事实目前仅允许保守的 reported_related 归属，不冒充 reported_exact。查询到私有新蛋白时可结合它自己的已报道断言作为候选依据，目标活性始终未验证。旧公共通路搜索不含私有边，私有线索只能作为带引用的机制/候选支持，不能捏造 reported_path。冲突须在理由和限制中说明，不能只挑有利证据。
