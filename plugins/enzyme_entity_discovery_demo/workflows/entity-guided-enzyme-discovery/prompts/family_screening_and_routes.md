# family_screening_and_routes：执行协议

排序前复核整体净变化：只有已确认存在净电子转移时，才把外部氧化还原当量缺失作为化学冲突。局部官能团氧化、原子对应未知或内部重排未核实时，应列为未验证条件，不能据此将整反应探索方向自动降为局部组件。角色仍取决于提出的酶数量与完整目标覆盖，不能据此保证任何特定候选晋级。

沿用回执的 `source_policy`：公共来源关闭时，不调用公共检索、能力候选或通路连接工具；只使用允许的私有证据和已冻结的模型草案。来源均关闭时，候选只用草案 `proposal_id`，证据数组为空，全部明确标注“未经过本体证据核实”；允许提交候选、假说路线和缺口，不因没有本体证据阻塞。私有证据通过工作区查询及证据回读获得，不得回退到已禁用的公共来源。

本阶段先冻结 chemistry-first baseline，再进行任何候选实体查询；后续本体查询只改变支持、反对或未知状态，不提供隐藏分数。直接单酶目标与多酶路线组件分别比较、分别排名，图路线只作为有界备选线索。

先独立提出候选，再用实体事实补充或核验；实体命中不是白名单，也不能自动加分。禁止联网、读任意文件、读取历史答案或评测材料。

先调用 `enzyme_entity_run_context(stage="family_screening_and_routes")`。只根据目标反应、反应分析、机制假说和模型自身知识，形成有界候选草案（默认按 5 条展示需求保留竞争余量，用户要求 Top N 时按 N 和本轮配置上限准备，不足不凑数），并立即调用 `enzyme_entity_baseline_candidates_commit`。草案不得包含任何实体 ID、断言、Evidence 或知识来源引用。工具返回 `baseline_frozen=true` 和新的 `context_digest` 后，才允许调用任何实体候选查询。

然后以机制产物中已有的 CatalyticCapability ID 调用 `enzyme_entity_capability_candidates`；必要时再做实体解析、单跳关系或反应类比。所有实体候选在提交前都要用 `enzyme_entity_read_evidence` 回读所引用证据。具体蛋白、未解析蛋白组、酶家族、模型候选和未匹配能力分别使用对应 `candidate_kind`。实体候选使用 `mechanistic_analogy`，并在 `source_fact_scope` 说明其来源事实是 `reported_exact` 还是 `reported_related`；模型候选使用 `model_background_unverified` 和 `none`，只能引用冻结草案中的 proposal_id。

基线草案保持不可修改，但它不是最终候选白名单。实体查询发现以下任一强关系时，必须执行一次“证据触发候选修订”，把相关实体作为新增 `entity_*` 候选并与基线并列比较：实体催化的已报道反应包含同一直接底物；实体使用可能统一多个净变化的共价辅因子；天然反应存在可改变的第二底物捕获分支；已报道反应共享关键离去基团或中间体。新增候选不得因为实体命中自动加分，必须在 `fit_reason` 中同时写明触发关系、目标分支推测和不能证明目标活性的边界，在 `comparison_notes` 记录它是“冻结后新增、未覆盖基线”的证据触发候选。

候选召回和比较必须覆盖四条并行通道：`同底物`、`同反应中心`、`同机制`、`同中间体或辅因子`。即使同底物已经命中，也至少完成一次跨底物的能力、转化模式或中间体查询。用现有 `fit_reason` 标记每个候选命中的通道，用 `comparison_notes` 汇总未命中或知识包不具备的通道，不新增 Schema 字段。排序分别比较底物/端点适配、反应中心闭合、机制事件、中间体/辅因子角色、产物方向和证据距离；任何单一通道都不能自动决定第一名。不同底物但机制和目标反应中心更吻合的候选可以高于同底物但只提供局部线索的候选。

上游弱假说只获得候选竞争资格，不强制保留对应实体蛋白、家族、未匹配能力或路线。复核正文证据和化学门控，按“核心键变化＋辅因子作用＋中间体类型＋终止/释放方式”聚类；阶段内最多处理 6 条弱线索、保留 3 个机制簇、允许 3 条晋级正式候选或 `hybrid_path_hypothesis`。只有同底物或同辅因子的线索不得晋级；违反元素骨架、不可补偿电子守恒或目标净变化的硬冲突予以淘汰，可验证的软冲突降低排序并写入 `conflicts` 或 `knowledge_gaps`。若一条晋级弱假说代表高排名候选未覆盖的新机制类型，可占 1 个机制多样性竞争名额，但仍需在对应角色组内与其他候选比较，不保证进入最终 Top N 或报告。已报道天然分支只引用事实证据，替代捕获/释放到目标产物放入 `hypothetical_steps`。

任何候选的目标反应状态都只能是工具写入的 `hypothesis_unvalidated`。不要把共享辅因子、家族成员关系、相似中间体或匹配数量解释为目标活性证明。没有离线序列/结构库时停在家族或筛选条件层，不虚构具体蛋白。

候选比较后，将用户起始物、目标物和已提供共底物解析成实体 ID，按需调用 `enzyme_entity_route_connect`。它只在已报道反应图上做最多四步的有界连接；返回顺序不是可行性评分。准备采用某个 `route_ref` 时，先用 `enzyme_entity_read_evidence` 回读该路线列出的全部 Evidence。目标物不在五卡实体包、没有本地路线或需要新增步骤时，使用 `hybrid_path_hypothesis` 或 `model_path_hypothesis`，把新增步骤放入 `hypothetical_steps`，不得虚构 reaction_id 或本地证据。

将 20 次工具作为常规检索预算，系统硬上限为 32 次；超出的 12 次只用于证据回读、路线调用以及最终提交修正。实体解析最多 6 次，关系查询最多 3 次，能力候选最多 3 次，路线连接最多 1 次；目标物解析未命中后立即把“目标物无本地实体”登记为缺口，禁止改用中英文同义词反复试探。最终 `candidates` 数量遵守本轮 `max_candidates` 配置和工具 Schema。实体候选只填写 `entity_id`，必须省略 `proposal_id`；模型候选只填写冻结草案中的 `proposal_id`，必须省略 `entity_id`。每个实体候选的 `supporting_evidence_ids` 只能取其 `supporting_assertion_ids` 直接连接的 Evidence 子集。必须至少预留 4 次给 `enzyme_entity_candidates_commit` 及其修正；即使本地没有闭合路线或所有目标活性均未验证，也要提交带明确缺口的候选和假说路线，不得返回 `UNVERIFIED`。首次收到校验错误时只修正提交对象，不要重新查询。

每条路线必须逐项填写七个 `closure_checks`：物料承接、方向、辅因子资源、能量平衡、副产物控制、条件兼容和中间体稳定性。五卡没有相应记录时使用 `unknown`，不能填 `pass`。根据缺口加入辅因子供给/再生、能量再生、副产物清除、中间体保护或分步条件等 `auxiliary_steps`，它们固定为未核实模型建议。为每个具体候选填写 `candidate_role`：可探索完整目标反应的单酶方向用 `primary`；仅承担多酶路线局部步骤用 `route_component`；只用于诊断竞争或发散机制用 `diagnostic_control`。实体家族和未解析蛋白组线索用 `family_hint`，未匹配到具体酶的能力用 `unmatched_capability`。候选提交顺序只表示组内优先级，不生成跨组总排名；局部步骤的匹配数量不能冒充完整目标反应分数。最后使用基线提交工具返回的 `context_digest` 调用 `enzyme_entity_candidates_commit`，同时提交候选、路线和全局缺口；成功后只返回阶段回执。

角色判据：如果某候选被提出为“同一种酶通过改造终止方式完成整个目标净反应”的探索方向，即使路线产物因混合已报道事实和预测而标为 `hybrid_path_hypothesis`，该具体酶也应标为 `primary`。只有方案明确还需另一种酶承担其余转化、该候选仅覆盖局部步骤时，才标为 `route_component`；路线类别与是否混合事实/预测不能代替酶数量判断。

输出预算优先：按本轮 `max_candidates` 与用户指定 Top N 控制数量，不足不凑数。候选和路线只保留有独立机制价值、且能在提交预算内清楚说明的项目；每个 fit_reason、conflict、knowledge_gap 只写一句不超过约 120 个汉字，证据引用每候选优先 1—2 条，路线每步只保留必要的输入、输出和缺口。不要把上游机制账本、工具回包或大量关系列表复制进提交参数；遇到输出长度压力，宁可减少候选数量，也不要一次生成超长 JSON。
本阶段沿用 Workflow 上游回执的 `run_id`；最终候选数量遵守本轮 `max_candidates` 配置，用户要求 Top N 时不足不凑数；阶段结束时返回 `enzyme_entity_candidates_commit` 的 `result` 对象。


当前 Workspace 私有知识参与规则：使用 `enzyme_entity_workspace_query` 按相关实体 ID 或精确名称查询（scope=both），保留公共与私有分组、条件及冲突；需要时用 next_cursor 续页，不能把一页当成全库。若返回 available=false，只使用公共工具并说明私有能力不可用。候选阶段必须先冻结 baseline，再查询私有知识。私有查询返回的 assertion ID 与 evidence_ids 可直接用于现有账本；引用前必须调用 `enzyme_entity_read_evidence`，不得编造 ID。已审核的 kind=hypothesis/analogy/annotation 仍不是已报道事实；opposes 不能充当候选正向支持。私有事实目前仅允许保守的 reported_related 归属，不冒充 reported_exact。查询到私有新蛋白时可结合它自己的已报道断言作为候选依据，目标活性始终未验证。旧公共通路搜索不含私有边，私有线索只能作为带引用的机制/候选支持，不能捏造 reported_path。冲突须在理由和限制中说明，不能只挑有利证据。
