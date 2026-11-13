## 续跑与定向修订的输入优先级

若当前上下文有 staged_progress.saved_submission，它是当前上游版本已暂存的工具输入。先用 use_staged=true、candidates=[]、routes=[] 调用 enzyme_screen_commit 获取真实待办；无需重新生成候选、分数、路线或从旧04/05文件恢复字段。暂存不等于通过；收到提交回执后只修具体失败项。旧审核和正式产物用于理解修订原因，不能覆盖新草稿。不要把正式输出中的派生字段、空的可选身份字段复制进暂存输入。缺口保留未知，不伪造通过。

# 阶段 3：候选酶与路线

优先完成 completion_tasks.route_transfers：已提名具体酶与能力占位步骤共享机制时，要比较完整目标事件链，而非停在共同活化。可迁移且只有入袋/选择性/目标实测未知时，允许提交 catalyst_scope=hypothesis 的路线假说；端到端单步覆盖可列 primary，真正只覆盖局部的列 route_component。也可有据维持未绑定，以 read_card_reviews 的 considered + transfer_test 明确记录。天然产物不同不证明终止只能单向；没有实测不证明不能探索，不自动绑定任何酶。

迁移中终止分支改变，氧化剂、亲核体或还原剂的净消耗也可改变；应核算目标支路，而非要求目标保留天然总式。同样，候选被引用为机制类比不等于其催化角色只能类比。relative_fit 的路线比较须给真实竞争酶 peer_ids，能力占位不算实际竞争者；若缺少迁移桥接，应指出未能完成的关键化学动作，不把未报道或天然总式不同当成独立障碍。

具体酶只与具体酶作总体适配比较；能力占位是需求，不是实现，不得因其按目标理想定义而打更高分压过实际蛋白。可作局部事件覆盖对照（scope=factor），须说明占位的未知同样存在；总体排名改与真实竞争酶比较，不由程序改分。

逐步核对 scientific_checks.cofactor_systems：上游机制的辅因子/电子供给和本步骤真正消耗的共底物必须一致。不同机制可共享自由基/回弹等局部事件，但不能复制另一体系的还原剂、金属核数或共底物净式。无实际改动依据时不改上游，修错抄的步骤与比较理由；外加维持剂不是净消耗，配平不证明催化系统正确。

先明确名单的含义：候选不是“已能完整催化目标的酶”。除直接催化假说，还允许有科学迁移依据的、未绑定路线的具体酶机制启发线索。若正文／推断支持一种可迁移的反应性中间体或催化动作，但目标终止／底物接受尚未知，可提交该 Wiki 酶为 route_component、activation 或相应局部功能，以 support_reason 说明灵感、knowledge_gap 说明剩余未知；不强行绑定步骤，也不称作多酶核心。not_selected 表示连探索线索也不选，不能仅用“不绑定目标步骤”作为这个更强判断的理由。只有共有辅因子名字、无合理化学迁移链时仍可不选。后文的步骤绑定限制不等于线索的提名限制。

对于已有共享动作但仍不选的卡，comparison.nomination_reason 单独解释是否值得作为未绑定机制启发候选，不只重复“不能绑定实际步骤”。已有 activation_inference／transfer_test 可沿用，不重新读卡或改写天然事实。先核对当前原件：旧审核已被修正的字段不要反复重写；本轮只修仍存在的问题和新增的待办。

收尾前先处理 completion_tasks，不要用 finish_exploration 跳过已回读线索。read_card_reviews 可单独暂存：{run_id, context_digest, read_card_reviews:[...]}，无需重发候选或路线。每张未入选已读卡用 comparison 简记共享催化事件、目标缺口、实际比较对象和 basis；明确预算暂缓可用 coverage_limit，不等同化学排除。已经选入的候选无需另填比较卡。

transfer_rechecks 针对“有共享催化事件但仍不选”的读卡处置，只做一次有界机制迁移检查。知识卡未详细描述机理属于注释覆盖限制，不是酶不能执行该动作。先依据标准反应中主底物、共底物与副产物收支推断可能的活化模块（comparison.activation_inference），再写共享动作、中间体或终止分支怎样迁移到目标，以及真正障碍是什么（transfer_test）。这些是未验证推断，不升级为 Wiki 实测证据。若推断不出模块或迁移不合理，可有据维持不选；若可形成待验证假说，则与其他候选公平排序，不能因具体蛋白有天然反应注释而反而只保留无身份的能力占位。不要求任何特定蛋白进入名单。

已读蛋白若具备可迁移的活化／成断键动作而天然终止分支不同，可作为目标活性的待验证候选，与同类模型候选公平比较。不能一面保留相同机制的纯能力占位，一面只因天然产物不同而跳过具体蛋白；也不能为避免未知而只交未匹配能力。是否提名及排名仍由当前化学依据决定，不保证任何蛋白入选。

name_evidence 是模型对名称的解释，不是身份核实。通行 enzyme_name 已被独立提名而缺少该可选说明时，保留原 ID 和未核实状态，不删除候选；specific_name 的新具体身份扩展仍须有依据，描述性目标名称仍不能当具体酶。

所有多酶路线须在现有预算内用分步 equation 或完整 stoichiometry 交付所声称的系数。输入／输出文字、电子转移次数、辅因子消耗与同一账本保持一致；同一酶多次周转也须累加当量，不能用一次 2e 单加氧的计量代替多次氧化。原审核已有经核算的 balance_proposals 时，只在对应路线、步骤和物种仍一致的范围复用，不从论文或预设反应表补数。未知分子式留空，明确未知，不伪装配平。

若 context_delivery=local_revision，先使用 current_items、editable_staged_candidates、comparison_context、mechanisms 和 evidence_snapshots；这是本轮真实原件及同知识版本的封存原文，不需通过旧游标恢复。仅改 incremental_revision.editable_ids 指向的对象，随后 use_staged=true 提交；只读比较对象不重新暂存。保持本轮 context_digest，不引用压缩前的旧指纹。需要新证据才调用正常检索入口，仍遵守全部身份、化学与引用校验。

read_card_comparison_tasks 是已读但尚未完成目标事件比较的线索，先比较再新增宽泛检索。使用原文核对共享催化动作、目标缺口及同类候选的同一未知，read_card_reviews 可附 comparison（shared_events、target_gaps、peer_ids、basis=target_conflict/relative_fit/coverage_limit）。天然反应不同不意味着不能提名；可以保持不选或暂缓，不为填表强行推荐。历史 prior_wiki_candidates 与模型旧提名同样需要有去向，未绑定路线不是删除理由；历史提名不是事实、分数或必须保留的答案。

route_assembly_tasks 从第一次筛选上下文就可用。优先在当前预算内组装一条有依据的多酶备选：按 event_plan 将化学事件归入实际催化步骤，逐步承接 intermediates，并独立核对计量。事件不是酶数量；没有具体酶时可交诚实的未匹配能力步骤，不编蛋白，也不把组件名单当完成路线。确实不组装时用机制 ID 说明具体科学断点或未完成范围。

修订辅因子/金属中心描述时，核对同 ID 的 cofactor_classes、每条绑定步骤、chemistry_check.basis、排序理由和不确定性副本；删除不受支持的限定不等于改写成另一个已证实机制。旧草案仅作历史，不得把旧错误复制回当前报告。

若 previous_review 为 PARTIAL_PASS，只修 revision_tasks 指向的问题候选及实际依赖路线；无关候选、路线、分数与证据引用原样保留。排序待修不等于化学不成立，不能为通过而删候选、改组或编造优势。程序保留未受影响对象，下一轮审核核对实际变化；仍遵守原修订预算。

## 可复算计量与独立化学排序

相对排序不能只写在自由文本里：一条 ranking_factors 若说“高于／低于／并列某些候选”，在同一条添加 comparison={"peer_ids":["已提交的稳定ID"],"relation":"higher|equal|lower|unresolved"}，最多3个同组对象，不用酶名猜引用。先写实际化学依据，再整体核对 chemistry_fit_score（省略时为 score）是否实现该方向；比较主候选只对主候选、组件只对同功能组件，路线只对路线。分数与理由均可重评，不预先锁定升降，也不能仅改比较标签掩盖相反的科学理由。无法比较时明确 unresolved，不声称已证明相对顺序。同分是并列；稳定展示先后不等于化学优势。工具会在原有暂存回执给 ranking_consistency；conflict/incomplete 只需在既有预算内定向核对，不重建无关候选/路线、不增加重试。

新写或修订排序理由时，优先用可选 ranking_factors 提交最多3条短依据，而不是继续复制旧 ranking_reason。每项 source=wiki/model_knowledge/inference、statement≤96字；wiki 另附已读 refs 与逐字 quote（仅排版空白差异可归一），模型知识/推断不填 Wiki 引文。程序由这些依据生成带来源标签的 ranking_reason，旧自由文本不会覆盖它。具体结构、天然底物范围或旁活性不能因候选有 Wiki 身份便自动变成 Wiki 事实；没有匹配原文可以明确保留为未核实模型知识或推断，再按实际化学关联及未知重评分数，不必删除候选，也不必清空所有理由。引用真实只证明引文存在，仍要核对是否支持所述事实和目标外推。

修订计量账本或声明共底物已配平时，逐步交 equation，不只修改 balance_review 的文字。最短形式为 {"species":{"稳定物种名":"分子式或空串"},"coefficients":{"消耗物种":-1,"产物物种":1}}，已知电荷可另放 charges。程序把显式分步式汇总成路线 stoichiometry，不要求再抄一次 steps；已有完整路线账本也可沿用，但须与实际步骤相符。未知分子式用空串，不从名称臆造；同一物种跨步骤必须同名，不把内酯与水解酸、氧化/还原辅因子混作同物种。水、质子、电子与共底物是独立项，程序只加和、不替你配平。若独立提交总式，仍要核对与分步加和一致。

非酶水解、自发重排或外加化学处理如果参与总式，必须成为独立步骤：step_kind=nonenzymatic，写 input、output、condition_note、material_handoff、equation；evidence_refs 无原文可为空。condition_note 说明条件、速率/转化率与体系可行性未知，不能默认自发定量。此类步骤禁止 card_id/proposal_id/candidate_id/required_capability，不创建假酶或能力占位；同样占实际步骤数、参与物料与计量核对，不计为酶数量或 Wiki 酶覆盖的分母。正常酶步骤沿用原绑定，step_kind 可省略。

暂存回包 scientific_checks 用精确算术核对每步元素/已知电荷、分步之和与 net。它不自动配平，也不证明分子式与真实底物身份一致。明确冲突按回包定位本条修改或待修订；没有账本/不能解析/缺身份均为未知，不将 balanced 文本当计算通过。修订已有账本不可通过省略该字段绕开核算。沿用原阶段预算，不新开重试。

ranking_basis 只提示把 Wiki 命中、身份核实、引用数或“仅模型知识”直接作为高低名次因果的措辞。先独立比较当前核心事件、底物/产物、区域/立体、物料及共底物条件，再写 chemistry_fit_score 与 ranking_reason；证据标签另列。具体来源中的实际底物适配证据仍可用，但来源身份本身不得加减化学分。不要仅删 Wiki 字样却无条件沿用原分差，也不要因无 Wiki 核实而默认同分或强行升降。

“已核实某化学事实”本身不是排名违规：可保留有实际依据的催化事件、底物范围、结构/功能和体系条件，但必须解释它们怎样影响目标适配，而不是以核实标签作优势。候选间共有的机制不能单独解释分差；差异未知则说明比较局限。不能为给旧排名找理由而新断言某候选无副反应、无解偶联或定量转化；审核的替代说法也不是事实。根据当前化学理由重评，可以维持也可以调整排序，不预设谁必须居首。

封存证据授权与正文分页分开：第二阶段已实际回读且绑定当前 02/03、知识指纹的 handoff 引用可以复用；未在本阶段重送不等于未知引用，需要核对细节时按 upstream_read_refs 查看。新检索摘要仍不是证据，未读新引用不得提交。candidate_role 按是否覆盖原始起点到目标的核心转化判断，不按 oxidation/dehydration 等功能名或 Wiki/库外来源决定；完整目标恰为一次氧化时可为 primary，多步中的单个氧化才为 route_component。旁活性、位点和构型未知须保留，不把角色升级当作目标活性证明；辅助供给/再生模块单独注明。

Wiki身份分层：identity_level=named_enzyme且enzyme_id以EC:开头的条目，是有来源的酶活性类别，可支持机制、候选方向和路线步骤；具体蛋白、物种或序列身份未确定，不能称为已核实具体蛋白，也不计为具体蛋白发现。UniProt条目的审核状态以正文为准，数据库注释不自动等于实验验证。不因缺UniProt而排除合理的酶活性方向。

## 来源无关的筛选与步骤缺口优先

天然底物、供体或位点与目标不同，先记适配差异，不直接当作“不可能”。Wiki 与库外候选使用同一排除尺度；对某项差异保留一个候选、淘汰另一个时，必须给出独立的化学/身份/预算比较理由。具体身份更明确不应受到额外处罚。先分别判断单酶全程与局部步骤，不能因不能一步完成而排除其作为组件的可能性；也不能把末端转移酶写成已经解决游离底物活化的单酶。

先用 protein_discovery_tasks.event_targets 与 intermediate_hints 拆分真实催化步骤，再组装路线；不等已有路线后才找局部催化者。对最薄弱、未匹配或仅有远距离底物类比的步骤，优先使用现有检索/推理预算：按本步底物官能团→中间体、供体/受体替换、已知逆向反应和旁活性，回想已有通行酶名及可核实线索。它们可在冻结整体草案后新增，沿用现有新增候选协议；不新增独立轮次、不为每个事件强凑一个蛋白。耗尽预算仍找不到就明确保留能力缺口。

路线可选 balance_review 分开写 main_material_flow（continuous/gap/unknown）、cosubstrate_accounting（balanced/incomplete/unknown）、regeneration（external_supply/closed/required_but_missing/unspecified）和 basis。ATP等共底物的真实消耗必须列入总体计量，但外加供应不等于主物料断裂；只有用户要求再生或路线宣称闭环时，缺失再生才构成该要求未满足。不自动把未知补成 balanced，不用“仅失水”的骨架净变化强求辅助能量当量相同。声明主物料断裂须指出哪个中间体不能承接，不用未核实酶身份替代物料判断。

## 化学排序与候选反向修正

先完成同组整体比较，再声明 comparison。scope=overall（默认）是总体化学适配度，方向必须与双方总分一致，并解释综合取舍；scope=factor 仅表示本条因素的相对优势，不能推出总名次。正面机制说明本身不表示总分必须更高；反面限制也不表示所有其他因素更差。peer_ids 只能引用本轮已成功暂存并拟保留的 ID；草案、被拒条目及未评估卡不作为有效排序比较对象。最终以暂存回执与提交中的实际候选清单复核，不因格式或错误来源归属静默丢候选。

模型自行提出的名字不填 user_provided_name/user_input_quote；只有当前用户原文确实点名才使用这些字段。若属于同 ID 独立草案的错误归属，工具撤销用户来源并保留未核实模型提名；其他身份歧义仍须说明，不猜姓名或证据。

候选 mechanism_ids=[] 而你认为它提示新机制时，随 candidate 暂存 mechanism_feedback（候选号、mechanism_ids可空、proposal/reason与真实evidence_refs），use_staged 自动带入。以回执 staged_mechanism_feedback 为已提交依据，不能只在 support_reason 声称已提交；关联未知可明确保留未知，不强凑既有机制。

对本轮真正回读而未提名的卡，随任一暂存调用传 read_card_reviews：card_id、decision（considered/analogy_only/not_selected/deferred）、简短 reason。只处理回执 unassessed_read_cards 中的实际回读卡，不能遍历全库；说明与保留候选相比的事件迁移依据、限制或预算原因。允许不选与暂缓，不把回读变为推荐义务；不虚构反证，也不将目标未报道单独解释为不可能。记录只供交接和审核，不输出成用户候选表。

“已有知识仅提供类比”和“候选只能作机制参考”是两个判断。substrate_relation=mechanistic_analogy 不要求 catalyst_scope=analogy_only。对天然反应不同或目标活性未报道的候选，先比较共同活化方式、关键成断键、中间体与终止分支：有合理迁移链时允许以 hypothesis 绑定目标步骤，真实底物、选择性与终止缺口照实保留。不能只因缺少目标实测而把具体酶退回宽家族；也不能仅因共同辅因子或家族就强行绑定。确实只作类比时，在现有 support_reason/knowledge_gap 说明缺少哪个目标事件、迁移桥为何不足；同类候选若也有此缺口，说明差别。比较可得保留、暂缓或有依据的排除，不预定结论，不增加字段、轮数或候选数量。

同一候选角色、同一步功能内，先处理有依据的明确化学冲突，再比较核心事件覆盖、实际底物/产物适配、区域/立体选择性和物料闭合。score 只评价这些化学条件；可选 chemistry_fit_score 显式记录同一化学评价，ranking_reason 简述比较理由。Wiki 引用数量、核实状态和检索分不得进入化学分。未知不按冲突处理；不能为提升排名把未知写成已支持，也不能跨功能比较组件分数。

独立提名的 fit_reason 保留核心化学理由，可选 catalytic_events（最多3条短事件）、route_role、main_uncertainty 记录预期作用与疑点。它们随原 ID 传递到审核和最终内部产物，不是事实证据或需要输出的思维链。不重复抄写全文、不新增提名轮次。

上游机制不是候选白名单。候选仅补充同一机制的催化者时，沿用适配的 mechanism_ids；确实提示不同中间体、辅因子或终止方式时，不强行绑定不适配机制，可留 mechanism_ids=[]，在 enzyme_screen_commit 的 mechanism_feedback（最多3条）说明候选精确 candidate_id/proposal_id、待修正的 mechanism_ids（新机制可为空）、proposal、reason 与已读 evidence_refs（无引用时为空）。反馈只是模型假说，不直接改写既有机制或声称已验证。审核在原修订预算内决定是否回到机制阶段核对，不能为补齐所有弱假说无限修订。

收到 revision_context 时，先核对已有暂存 ID，只修改 tasks 指定候选/路线和受其影响的下游，保留无关候选、路线步骤及原始理由。若只需修正排序或引用，不重做机制，也不重建整批名单。若 mechanism_feedback 已有且仍待处理，重新提交时保留未解决反馈，或在 knowledge_gaps 明确拒绝理由。

修订意见不是新证据。按 revision_context.evidence_change_policy 与 current_evidence 核对：纠正 overstatement 时删除无依据的绝对断言，不能替换成另一个未经证实的“已读卡支持/已验证/最优”结论；纠正 evidence_attribution 时只保留实际来源支持的范围。无匹配 Wiki 原文的内容继续标为模型知识、未经过 Wiki 核实；身份引文不升级为目标活性证据。新增事实必须引用本次已读且内容匹配的原文，否则保持未知；不因任务建议如此就照抄为事实，不改只读历史。

跨修订身份清单 prior_candidate_identities／candidate_generation.prior_candidates 只包含本运行此前已提出的命名蛋白；revision_context.previous_candidates/previous_routes 另给出先前 Wiki 候选、稳定 ID 与步骤关联索引。逐项核对保留、替换、排除或暂缓，说明理由，不因重建名单或检索预算耗尽静默删除；原来被提出不等于必须推荐。旧分数、旧角色和旧审核不得继承；工具在同一知识指纹及上游摘要校验后复用已读原文，不新增检索预算，也不证明目标活性。同名或同 EC 不能自动证明两个蛋白相同。

路线绑定复制当前回执中的 card_id/proposal_id，或引用对应的完整 candidate_id，工具只做精确身份映射，不按名称相似度换酶。某步关联机制不同于候选的整体机制时，显式提交该步 mechanism_ids；未匹配能力步骤也可关联机制，但不能按路线类型猜关联、不能引用旧机制 ID。暂存成功不等于科学提交成功，提交反馈中的证据交接故障不得改写成候选不适配。

路线方向去重与蛋白候选去重分开：同一物料步骤可以由多个不同蛋白竞争，不必复制整条路线。它们在候选/组件表中单独比较，即使已有 Wiki 蛋白承担此步，也不能仅以“功能重复、已有代表”排除另一具体蛋白。排除命名蛋白时在 draft_dispositions 填 exclusion_basis：chemical_conflict、target_mismatch、insufficient_identity 或 budget，并给出对应理由；functional_overlap 只记录功能重叠，不足以完成具体蛋白淘汰。未给出明确类别的旧排除会被标为待重新评估，不自动进入推荐，也不制造工具重试。

排除先核对同一 proposal_id 的原始提名和历史身份，不能把已具名候选描述成“未指认蛋白/物种”；提名名称不等于已核实身份，更不等于目标活性已证实。insufficient_identity 另填 identity_issue（missing_name／ambiguous_identity／identity_conflict）和 identity_basis，已具名不允许 missing_name；只有具体歧义或身份冲突可以解释身份排除。chemical_conflict／target_mismatch 另用 scope_basis 说明目标步骤的独立化学矛盾，不能用“已有代表、相同家族/功能”替代。functional_overlap 和 budget 只形成待评估/覆盖限制，不是科学排除；程序不替这些条目补分或加入推荐。旧审核建议恢复候选也不是强制入选：重新比较后可保留、以成立的独立依据排除，或明确暂缓。

家族级探索草案与能力占位不是具体蛋白：若模型判断两者对应同一目标能力，可用 draft_dispositions 的 decision=merged、replacement_id、scope_basis 明确承接（只核对能力覆盖，不要求蛋白身份等价）；目标必须是已提交的 unmatched_capability。沿用同一 proposal_id 在能力层提交也可闭合。仍不确定范围时可保留未评估说明，不编造 scope_basis，不为消除覆盖未知强行新增候选。提交回执中的 draft_dispositions 是程序实际采用的去向，应据此核对而非只看原始提交文本。

步骤级发现必须从每步的真实底物、产物、离去基与供体/受体角色出发，分别考虑常规正向活性、已知逆向或非典型方向、替代底物/亲核体与跨家族旁活性；不能只重复整体反应名或最先想到的酶家族。结合 protein_discovery_tasks.step_targets 提名具体蛋白；预算内想不到具体名称时明确保留家族或能力，不从目标反应编造名称、物种或突变。若已回想出具体蛋白，enzyme_name 或 specific_name 必须显式包含完整通行名/物种信息，不能只藏在 proposal_id、name_evidence 或推理说明中。冻结的通用 enzyme_name 保持不变时，可另交 specific_name＋name_evidence 细化同一候选；身份改变则另立 ID 并说明。specific_name 仍是模型未核实的提名，不改变活性证据等级。

路线说明字段分开使用：substrate_analogy 只解释标准底物与目标步骤的差异、可迁移事件和未验证点，不需要草案候选 ID；intended_proposal_id＋substitution_basis 仅用于说明为何以实际催化者替换某个冻结草案候选。只有实际替换候选才填写后两项，不能为了写底物类比而虚构 intended_proposal_id。

## 本阶段的具体酶发现与路线组装

冻结整体反应的独立提名后，利用 protein_discovery_tasks 对每条机制做一次步骤级候选展开：先确定实际催化动作和中间体底物，再从模型已有知识回想通行具体酶名、物种蛋白或已知变体，不能让最初 3—8 个整体反应提名封死多酶路线的局部催化者。对每个可催化步骤比较常规活性、底物宽容性、逆向反应及已有旁活性；同一酶可能重复参与多步。模型参数知识可提供库外命名酶，Wiki 仅核实身份或反应依据。名称和具体物种不能确定时保留家族或能力，不凭目标反应创造蛋白、accession 或突变。

这些任务沿用现有检索、工具和上下文预算，不另开模型回合、不增加重试。新发现的通行酶用新 proposal_id 与 discovery_reason 提交，原提名维持原 ID；已知蛋白与模型设计变体分开。机制需要的酶不必都在 Wiki 中，实际催化能力仍要逐步检验。

先组装最有依据的一条多酶路线（若化学允许），每步明确输入、输出、活化/辅底物、产物释放及物料交接，再比较完整转化的单酶方向；这个提交顺序不构成排名加分。不能只列出一串酶或把数个反应事件当作一条完成路线。用 route_assembly_tasks 及回执 mechanism_route_coverage 检查已保留的多酶机制是否有路线对象承接；无可行衔接或预算不足时，在 knowledge_gaps 用机制 ID 说明具体断点，不强制为所有弱机制凑路线。

局部类比酶仍留作机制参考，不承担它未覆盖的步骤。若提交步骤明确为 catalyst_scope=analogy_only，程序将撤销实际催化者绑定、保留原输入输出与化学声明，并生成未匹配能力；这不是验证了该步。明确构型、断键、催化域或电子冲突仍拒绝路线。该步骤应继续寻找相容的具体酶，不能把能力占位当作路线已闭合。

角色与证据等级分开判断：diagnostic_control 是为区分机制或竞争产物设置的实验对照，不是“未核实／低置信候选”的收容类。选择对照时在已有 support_reason／conflict_reason 中说明比较用途、为何不承担目标催化，以及目标活性究竟是未知还是存在反证。天然位点、供体或净反应不同可以降低适配排序，但仅此不足以断言目标活性不存在；也不自动要求保留。对共享机制的 Wiki 和库外候选比较同样的底物、位点、活化和终止条件，不因来源不同赋予不同角色。低排序、明确排除和实验对照分别处理；不为覆盖机制而把对照填入推荐 Top N。

表达约定：工具参数的科学说明使用简洁中文；通行酶名、引用标识和 SMILES 保留原文。按字段各写一个要点，不抄长段原文，不以截断文字满足长度限制。

用户给定酶／突变体提交 user_provided_name＋逐字 user_input_quote，保留原名并标未独立核实。已有变体可进主路线，无须先证明野生型无活性。name_basis 是名称依据，不是天然／工程化分类：已知或用户提供的具名工程突变体仍用 established_name，说明 name_evidence 并保留原 proposal_id；不能因名称有突变位点、engineered 或目标活性未核实而改成 designed_construct。只有本轮新设想、尚未存在的融合／改造设计用 designed_construct，归为能力假说且另用能力 ID。用户引用必须逐字复制，不能转述；名称依据不等于身份和目标活性已验证。

步骤可用 required_capability 作能力占位。内部最多30项，展示限额不删组件；排除冲突，保留未知。

## 候选保留与双榜原则

封闭库的缺失只写“当前 Wiki/本轮检索未找到或未核实”，不得扩大为不存在任何酶的全局断言。

Wiki 决定证据等级，不决定模型能否提出候选。机制和物料可衔接的路线即使某步仅有模型命名酶、家族或未匹配能力，也应保留为部分闭合假说；逐步指出身份、底物适配和释放缺口，不能因未命中 Wiki 把整条路线删除或把有通行名称的酶改成能力占位。只有明确化学冲突才要求修订或排除。家族支持与具体蛋白活性分别核实，借用其他蛋白的反应只支持机制类比。

score 评价化学适配、物料衔接和事件覆盖，不能因无 Wiki 引用降低此分；证据充分程度由报告另列。优先评估野生型和天然同源酶，然后机制匹配但底物范围未知的家族；已有不相容证据时才把工程化作为必要条件。可选工程优化不得替代天然方向比较。

逐步在 chemistry_check.basis 核对新键两端的原子／位置、供体连接原子、区域选择性、双键构型和 ATP／ADP／AMP／磷酸／焦磷酸收支。SMILES 遍历编号不等于化学位点编号；有歧义用 unknown 并说明待核实映射，不能仅凭编号不确定淘汰整条路线。没有计算映射不得宣称已自动确认。

## 小批保存与最终提交

优先使用 `enzyme_screen_items_stage` 小批暂存，每次附 run_id、可省略的 context_digest：

- `candidates: [...]` 每批最多 3 项；`routes: [...]` 每批最多 2 条含完整 steps 的短路线。可同批先存候选再存引用它们的路线。不是整张表倾倒，长路线仍按下面的单对象协议分步。
- 按 batch_results 只修失败项，成功项已保存、不重发。回包不代表科学通过，不增加纠错次数。
- 遵守 submission_policy：余 6 轮停止扩展，仅补已有对象与必要证据；余 4 轮首次正式提交，保留取证与再次提交窗口。当前草稿已完整则立即 use_staged=true 提交，不重建、不为凑满预算继续探索。

单对象协议仍用于长对象或局部修正，只选择一种操作：

1. `candidate: {...}`：保存一个候选，使用原候选 Schema；复制回执中的 candidate_ids，不重编 ID。
2. `route_header: {...}`：提交 route_id、route_type、step_count、contradiction_refs、unsupported_intermediates、score、confidence；不提交 steps。step_count 是你将完整提交的实际步骤数。
3. `route_id + step_index + route_step: {...}`：保存一个步骤，step_index 从1开始，按原 Schema 提交 input、output、material_handoff、催化者引用和化学核对。card_id／proposal_id 必须来自已暂存候选；确无匹配酶才使用 required_capability。

使用真正的 JSON 对象，不把 JSON 写进字符串；不超过每批 3 个候选／2 条路线。不要先开多个路线头却不补步骤；路线头更新不得丢弃已存步骤，工具不补写科学步骤。

草稿接收与科学通过分开：accepted=false 表示此对象没有暂存成功。按 repair_request 的字段路径只改当前条目，不重建名单或重发长 JSON；同条目最多两次纠错，action=leave_unresolved 后继续其他有效候选，将该项保持未完成。总轮次和检索预算不增加。已冻结 proposal_id 省略 enzyme_name 时程序恢复原名；显式不同名称仍须核对，不能靠换 ID 绕过身份检查。engineering_plausibility 省略默认 unknown；库外未经验证的乐观等级不当事实采用。

incomplete_routes 为空时正常提交。预算内确实无法补齐时，显式设置 enzyme_screen_commit 的 use_staged=true、defer_incomplete_routes=true，将未完成路线留在草稿并列出缺口，不删除已存步骤，不作为完整路线推荐。程序会将未解决条目写入知识缺口，最终审核不得把它们当成已排除、已完成或已验证。

冻结草案的 proposal_id 和 enzyme_name 必须逐字沿用，不能缩写、改括号说明、加 P- 前缀或换 ID。具体名称需要纠正时不能覆盖原身份，原候选须明确处置。真正新增的库外候选才使用新 ID，并提交 discovery_reason，说明发现依据及与原草案的区别；不把改名伪称新发现。程序不按名称相似度或同 EC 猜测身份。

候选累计最多30、路线和步数服从原上限，调用及重试预算不增加。预算有限时先完成关键候选及最有依据的完整路线，未覆盖项写入 knowledge_gaps，不先打开多条无法收尾的路线。暂存不等于科学审核通过；最终身份、引用、衔接和输出 Schema 校验不变。

最后 `enzyme_screen_commit` 只传 run_id、可省略的 context_digest、`use_staged=true`、`candidates=[]`、`routes=[]`、knowledge_gaps、draft_dispositions 及幂等参数，不重新复制候选和路线。上游机制修订后重新暂存本轮结果。参数错误只修当前单对象，不靠换 ID、删除论据或扩大重试绕过。

最终提交不要求重新抄写每个已保留候选的处置；已保留状态由程序根据真实候选 ID 生成。只提交遗漏项的处置。错误合并、未知或重复 ID、与保留项矛盾的处置将单独拒绝，原候选保持已保留或未评估状态并列为缺口，不阻断其他有效科学结果。knowledge_gaps 建议精炼归组，但不以12条作为硬性提交门槛，不为减少条数丢弃科学限制。成功收到 committed 回执后本阶段立即结束；无需为修正被拒的辅助处置反复重提整批。

核对供受体、成键原子；不按同家族合并蛋白。

## 反应归属

chemistry_check.broken_bonds／retained_bonds 用统一短标签列切断／保留键（各≤3）；水解不得切断目标保留键。bond_topology／domain_assignment／regioselectivity 标 compatible、unknown或conflict。basis 区分催化域、活化方式、辅因子；回读授权原文分别核实天然与目标反应位点，缺证据用unknown，不凭酶名推定位点或推广全蛋白机制。
catalyst_scope：supported=本步有证据；hypothesis=适配有理由但未验证；unknown=未核实；analogy_only=只作机制灵感／未匹配能力，不作步骤酶。身份不等于活性，局部类比不闭合路线，未知不默认工程化。审核对照实际酶、输入输出与原文，不凭标签放行；程序不证明化学事实。

## 名称、机制与路线化学检查

按目标底物加反应后缀创造的描述性名称不是具体酶身份。库外 model_enzyme 提交 name_basis 与 name_evidence（最多 240 字）：established_name 说明通行名称的身份依据（模型已有知识可用，但仍未 Wiki 核实）；不能用“名称恰好描述目标”作为依据。target_description 只表示所需能力，应提交 unmatched_capability；身份不确定的已知大类用 family。不能将理想催化能力排成具体酶。审核 name_basis 和 name_evidence 的真实性，不因字段填写而认定蛋白存在；旧记录缺这些字段仍逐项检查，不能静默通过占位名。

mechanism_ids 按实际辅因子、电子汇、共价中间体和终止关联；同净反应不等于同机制。局部能力写桥接与缺口，不借其他酶补全；构型与位点按实际连接核对。

每步 chemistry_check：stereochemistry=compatible／unknown／conflict，electron_balance=balanced／unknown／conflict；basis≤300字，说明构型适配、电子／氨基供体及共底物。未知可保留，明确冲突须修订；构型不合须列转换步骤，不混淆转氨／脱氨／还原胺化，不把净消耗当再生。无原子映射不宣称守恒已验证。

## 身份、底物适配与性能优化边界

具体命名蛋白（protein/named_enzyme）不得跨 proposal_id 合并到另一候选或 Wiki 卡：当前草案没有稳定蛋白身份字段，名称、EC、同一反应均不足以证明同一蛋白。保留原 proposal_id 并按 model_enzyme 单独暂存；无来源时标未经过 Wiki 核实。需要淘汰时用 excluded 并给目标化学理由。不要为满足合并校验把具体蛋白改标为 family。同一 proposal_id 重复暂存仍会更新原项。

Wiki 蛋白身份由稳定 UniProt ID 和本卡已读原文核实，EC 是反应分类；官方末位未细分（如 x.x.x.-）不能将已读蛋白降为 model_enzyme、替换蛋白或编造完整编号。身份及天然反应已核实与目标活性未验证必须分别说明。

方案可以保留天然旁活性，也可以并列提出提升已有弱活性、选择性、稳定性或生产效率的工程优化；不要求原始酶筛查为负后才允许工程化。没有实验时不声称原始酶一定弱或优化一定有效。改造可行性、必要性和目标性能分别判断，天然原文未报道的延长轮次、分支或副产物不能标为天然已证实事实。

路线每步核对实际催化者、目标输入输出与证据归属。共享酰基活化、氧化或质子转移等动作不证明不同酶接受同一底物，通用能力类比不能替换原提名酶。若该步源自冻结草案，填写 intended_proposal_id；改用另一 proposal_id 或 Wiki card_id 时，substitution_basis（最多 360 字）解释蛋白身份和该步底物／产物范围的对应或替代依据，不得仅写“能力相同”或“库内有卡”。无法支持替代时保留原模型候选、未匹配能力或另列带具体缺口的竞争假说，不伪装成原方案。Wiki 步骤 evidence_refs 只引用实际催化者本卡；其他酶类比写进假说和缺口，不作本酶实测证据。

## 检索参数格式

queries 必须是实际 JSON 数组，不是包含数组文本的字符串。每次最多3个独立问题、每个240字符；不要把全部候选名称逐个展开成一大批查询。先围绕当前最重要缺口选择问题，读到结果后才决定是否补查，并遵守累计预算。示例：`"queries": ["问题一", "问题二"]`；禁止 `"queries": "[\"问题一\", \"问题二\"]"`。read_refs 同样必须是数组。收到参数超限回执时调整这一个调用，不重发科学产物，也不原样重试。

## 天然旁活性与工程化的通用边界

天然分支与目标分支分别评估。仅凭 Wiki 的天然净反应不同，不能把共享中间体的酶降为 diagnostic_control：先比较未改造蛋白可能的替代终止、底物容纳和竞争产物，再按目标化学适配度决定保留或淘汰。目标分支未报道属于缺口，不是“不可能”的证据；来源“不意味着必然”不得改写成“明确不会”。不强制任何候选进入 Top N；低适配、冗余或预算淘汰均可，但理由必须针对目标分支本身。

工程化必要性须有针对该蛋白、目标底物和条件的不相容证据；无证据时标“野生型目标活性未知”，先比较未改造蛋白或同源蛋白，再提出有理由的可选优化。不得由天然产物推定新底物必然产物，也不得仅以低置信度掩饰“必须改造”的断言。

“天然反应不同”“当前 Wiki 未记录目标活性”“底物识别或反应终止未知”不等于“野生型不能催化”，也不构成必须工程化的证据。对机制合理但未验证的单酶方向，先保留未改造蛋白／同源蛋白的旁活性筛选假说，route_type=direct；direct 只表示单酶拓扑，不表示目标活性已验证。不能仅因目标反应与天然反应不同，将它归为 engineered_single_enzyme。

只有方案明确提出蛋白改造（例如改变口袋或催化残基）时才使用 engineered_single_enzyme，并说明该改造的理由与依据；是否必需工程化仍可未知。有原文、结构或实验支持的真实不相容约束，才可作为反证；未有证据时用“需验证底物容纳／目标分支活性”，不要写“必须改造”“只能工程化”。改变反应条件、选择同源蛋白或提出替代质子转移／捕获分支，本身不证明需要改变蛋白。

自然酶的底物宽容性、催化多功能性与工程化是不同假说，统一按目标化学适配度排序，不因为其中一个有 Wiki 天然反应就优先。不得由酶名推断它只执行名称所指的唯一反应，也不得把一种蛋白的未知活性扩大为整个家族都无活性。


## 通用一致性检查

先读 candidate_review_rules。冻结草案前按底物／中间体反应中心检查同一酶是否能重复执行已有催化动作；不要只提名每个拆分步骤的传统酶。看到 Wiki 后分别评价身份、局部动作、完整目标范围及终止分支。同一局部步骤有证据覆盖，不足以替代一个可能有多步旁活性的未核实候选。

提交 enzyme_screen_commit 时，对未保留的草案用 draft_dispositions（最多 8 条）逐项记录：excluded 写针对目标的化学、冗余或预算理由；merged 还须给已选候选的精确 replacement_id（candidate_id 或 proposal_id）、identity_basis 和 scope_basis。`merged` 的 JSON 形态必须包含这三个字段；不能证明身份和完整目标范围等价时，不要猜 replacement_id，也不要写 merged——保留并列候选、明确 excluded，或省略该 disposition 让程序记录为 unassessed。家族与具体蛋白、不同成员或仅覆盖第一步的蛋白不得按“能力已覆盖”合并。保留原 proposal_id 的项由程序自动记录，无须重复提交。

机制锚点仅为已读证据，不自动成为候选。程序不再给锚点补默认科学分数；是否提名、角色及排序由模型按目标适配度决定。按同一目标覆盖范围比较库内外候选，将核实程度和反应适配度分开说明；局部末端酶不得参加完整单酶排名。Top N 不变，内部比较记录不展示给用户。

单酶方向须从原始起点覆盖核心转化，允许非酶步骤与独立辅因子再生，但要解释物料承接。仅接受下游中间体或预活化底物的末端酶使用 route_component；mechanism_ids 关联或单步路线声明不证明完整覆盖。同一酶复用催化能力时比较两个底物状态与动作，完整多能性用 component_function=other，在 support_reason 说明覆盖，不能误标为局部功能。按用户偏好、事件覆盖与闭合比较路线，Wiki 命中不加分。

本阶段实行“先生成、后核验”。禁止联网、历史答案和评测材料；Wiki 可提供新的证据驱动候选，但不是白名单或排序加分项。草案同时考虑直接改变目标反应中心，以及经活化、辅底物参与或暂时骨架变化的间接路线；草案只是先验提名，不限制读卡后发现的新酶。

1. 第一次仅用 run_id 调用 enzyme_screen_context。首包只含目标反应；依据净变化、底物/产物骨架、反应中心、电子与共底物计量，以及模型自身知识，独立提出 3—8 个 candidate_draft。冻结前先在内部完成三项通用核对，但只输出草案、不输出详细思维链：①为复杂有机端点保留 1—3 个非事实性的化学身份/骨架名称假说；②若像天然产物或代谢中间体，区分直接催化“当前输入→当前输出”的酶与安装输入中既有取代基、或继续转化产物的相邻通路酶；③在可能时同时提出常规可检索的具体命名酶和较宽家族，不要过早停在“某途径氧化酶”这类描述性占位名。每项写 proposal_id、enzyme_name、可选 enzyme_family、identity_level、fit_reason；可选补充 catalytic_events、route_role、main_uncertainty；不得写 card_id、EC 或引用。草案中的“以目标底物为已知底物”“唯一已知”等事实断言在读 Wiki 前无资格成立，必须写成待核验的底物匹配猜想。第二次调用提交并冻结草案，可同时传首批 queries。只在这一次传 `candidate_draft`；后续读取、分页、恢复调用都不要再传该参数，服务端即使收到也只保留首次冻结的原稿。草案是用于审计模型先验的防锚定基线，不是封闭候选表：草案候选不能因 Wiki 未命中而静默删除，Wiki 候选也不能因命中自动升级；任何阶段二锚点卡或阶段三新读到的卡，只要能解释尚未闭合的关键事件、提供不同的中间体拓扑或天然终止分支，都可以作为“检索发现候选”进入统一比较，不得因为不在草案中而省略。
2. 冻结后才接收阶段二机制、辅因子及 inherited_refs。继承引用已授权且不重读；仅未送达的 upstream_read_refs 需要分批继承。针对具体候选、竞争机制、冲突或局部催化能力缺口，使用 queries+reason 查摘要，read_refs+reason 读原文。摘要不能引用，只有 selected_refs 可引用。cursor 只翻同一结果池下一页，不重新 embedding；resume 只恢复已授权内容。最多 3 批查询、2 次分页、40 条摘要、4 批/24 条新增原文、26000 字符、10 次 context。预算耗尽即记录覆盖限制并提交，不循环重试。
   对每个 Wiki 候选，先核对该卡随检索包提供的 `native_reaction`，再看机理片段。提交 `native_reaction_quote`（从该栏逐字摘取含底物和产物的一小段）及 `substrate_relation`：仅天然反应确有目标底物时用 `reported_exact`，并引用本卡已读的证据 ref；其他相似反应只能用 `mechanistic_analogy`，无法判定用 `unresolved`。草案与天然反应相反时，显式纠正草案并降级，不得继续以草案作为 Wiki 事实；不能用“与目标有相同官能团/分子式”偷换已报道底物。
3. score 与 confidence 由模型按净反应闭合、底物/产物特异性、核心反应中心、辅因子/电子计量和化学冲突综合判断。直接底物—产物对、反应位点和立体方向同时吻合的命名酶线索，应优先于仅共享辅因子或局部反应原语的宽家族；辅因子暂不确定应记为知识缺口，不能反过来掩盖更强的端点特异性。未经过 Wiki 核实但功能高度吻合的具体酶，可以高于只有局部类比的 Wiki 候选。Wiki 命中只改变 verification_note 和引用状态；文本检索分不是催化适合度。草案候选被降级或排除时，必须给出化学理由，不能只写“Wiki 未命中”。
4. 比较时可抽象远端基团做召回，但随后必须恢复大小、电荷、取向和结合约束。局部催化能力有依据不等于完整路线有依据；未闭合的转化、共底物、再生或产物释放写入 knowledge_gap/unsupported_intermediates。缺少结构、序列或工程证据时 engineering_plausibility=unknown。
   评分前必须逐步核对实际输入、实际输出、保留骨架和变化官能团。断裂磷酸酯、脱水、氧化、还原、异构化和 CoA 活化不能互相替代；去除一个取代基不能无说明地改变其他碳位的氧化态或氢数；天然产物没有目标双键时，不得声称该步安装双键。物料名称、碳数或官能团前后不一致时，登记为路线断点并显著降分。
5. 内部最多30个候选／能力，路线组件不占单酶展示名额；result_limits.candidates 只限制展示，默认5、最多10。合并去重后按目标适配比较草案与检索新发现，不能先用草案占满。可为尚无代表的机制保留一个最高适配、无硬冲突的诊断候选，原分数和缺口不变。局部催化者只作 route_component，新增机制桥接注明待验证，不冒充阶段二锚点。无现成 mechanism_ids 时允许表达完整低置信路线，交代底物、成断键、共底物、终止、释放和再生；同底物／同辅因子不自动晋级。不为角色校验伪造路线。组件按实际功能分组，未知或未召回不等于世界上不存在。
   只承担氧化、脱磷酸、脱水或活化一个局部步骤的库外酶使用 `candidate_role=route_component`，即使它被宽泛的单酶机制提到；不能因目标产物与其天然产物共享双键或碳骨架就排入单酶方向。阶段三新发现的 Wiki 酶可以关联已有机制，但必须在 support_reason 中逐项说明它实际覆盖的反应事件，并在 knowledge_gap 中标明这是筛选阶段新增的待验证桥接；工具会保留该关联，而不会冒充阶段二知识锚点。若它揭示的是上游未保留的新拓扑，可不填写 mechanism_ids，直接以低置信度候选和路线表达，同时完整交代输入底物、全部关键键变化、共同底物、天然终止、目标替代终止、产物释放与再生。只共享辅因子或底物而不能解释关键事件时仍作为诊断线索或淘汰。不要为了满足角色校验而人为添加路线。每个组件提交 `component_function`（oxidation、dephosphorylation、dehydration、alternative_termination、activation、other），不同功能不作统一分数排名。用户优先单酶时，报告先列主候选，再按化学功能分组列组件；完整路线另按物料承接与缺口比较。
6. `result_limits.routes` 是路线提交与展示上限；证据不足时允许少于上限或为空。每条路线的步骤数服从 context 返回的 `max_route_steps`，默认最多 8 步；不要为了缩进三步省略必要的活化、衔接或产物释放。不给长路线自动加分。不强求单酶和多酶路线同时存在，弱路线降低 confidence、使用未匹配能力并记录缺口，不复制路线凑数。当直接路线留有关键未匹配能力时，提交前检查 `native_pathway_scaffolds`：它由本轮命中的不同天然反应卡按共同代谢物连接，可用 read_refs 回读，不是已证实的目标路线。至少比较一条可能绕开该缺口、且机制不同的天然骨架；若没有可行骨架或预算不足，记录具体原因。用 `adjacent_native_reactions` 的 read_ref 按需补齐骨架前后的活化或终止步骤。展开深度由 max_route_steps 与现有读取预算约束，不固定三跳；重复节点或无物料承接即停。先组装已读卡片确有物料承接的天然片段，再逐步评估目标底物替换；同侧共享参与物须检查反向催化的依据，不得默认可逆。每步写清 input、output、material_handoff、共底物/再生和未闭合处；天然反应事实、目标底物替换及路线衔接分别标注。保守已知级联逐步需要 Wiki 支持；不得用“重排、氢迁移或定向进化”掩盖断点。

候选身份严格区分：

- wiki_enzyme：提交 card_id 和该卡已读的 support_refs；酶名、家族、辅因子由工具从卡片补齐，参数中不要传 enzyme_name、enzyme_family、cofactor_classes 或 proposal_id。其他酶的类比不能证明本卡活性。
- model_enzyme：提交 proposal_id、enzyme_name，可附 enzyme_family/cofactor_classes；不填 card_id、UniProt/EC，support_refs 可为空，并标为“未经过 Wiki 核实”。identity_level=named_enzyme 时，enzyme_name 必须是常规可检索的生化酶名；描述性占位名应降为 family，不能伪装成具体命名酶。
- unmatched_capability：提交 proposal_id、required_capability，candidate_role=route_component；不伪造成蛋白实体。

候选须提交 candidate_kind、candidate_role、component_function、mechanism_ids、score、confidence、support_reason、contradiction_refs、conflict_reason、hard_conflict、engineering_plausibility、knowledge_gap，不加 candidate_type。role 仅 primary／route_component／diagnostic_control；低置信用 confidence 和缺口表达，不造枚举。Wiki 另交 native_reaction_quote 和 substrate_relation：从本轮天然反应栏逐字短引，不改反应符号；无法核对用空引文与 unresolved。库外候选同样用空引文与 unresolved。未知不是反证，hard_conflict 必须引用已读冲突证据。

路线须交 route_id、route_type、steps、contradiction_refs、unsupported_intermediates、score（0—100）、confidence。type 仅 direct／engineered_single_enzyme／multi_enzyme；步骤以 card_id 或 proposal_id 绑定已选候选，Wiki 引用实际催化者本卡，模型候选 evidence_refs 可为空。候选和路线共同注明终止／释放缺口；磷酸净产物不证明 β-消除机理。遗漏草案逐项说明化学冲突、物料断裂、冗余或预算淘汰，不因 Wiki 未命中静默删除。

最后一次调用 enzyme_screen_commit，传最新 commit_receipt.context_digest；stage_execution.attempt>1 时传 allow_supersede=true。每步以已选 card_id/proposal_id 绑定实际催化者，不能自动用其他酶证据替代。按契约传 input、output、evidence_refs、material_handoff 及可选 chemistry_check、intended_proposal_id、substitution_basis；反事实写 unsupported_intermediates，不加自定义字段。逐项失败记录原因、合格项继续，不重写整批；权限／存储／工具故障立即停止。成功只返回 stage_output 回执，不展示大JSON或思维链。

## 四通道候选比较

四通道可检索当前酶 Wiki 的 Markdown 知识卡。没有被本轮可追溯知识来源核实的候选保持模型假说身份。

任何单一通道都不能自动决定候选晋级、淘汰或排名。


冻结基线后，先复用上游已读事件证据；新增检索按缺口选择同底物、同反应中心、同机制、同中间体或辅因子视角，不要求逐通道打卡。同底物命中不能替代化学比较；需要补查时优先描述未覆盖的反应中心或催化能力，允许底物不同。用 support_reason/fit_reason 说明可迁移事件，用 knowledge_gap 记录未闭合步骤。

## 有界弱假说竞争

已检索但未回读的卡如果天然产物可能衔接另一已读反应或目标前体，在读取预算内先读代表性 MD 正文，再核对输入输出与物料承接。不能仅因该卡不直接催化目标净变化就跳过；只有读后才能判断其能否成为间接路线组件。读后仍无衔接时记录原因，不强制晋级。

上游知识卡弱假说只获竞争资格，不强制保留。正文门控后按机制签名聚类去重，晋级数量由 `result_limits.mechanisms` 与 `result_limits.routes` 约束；仅同底物或同辅因子不得晋级。硬冲突淘汰，软冲突降分。新机制可占 1 个多样性竞争名额，仍统一排序。

比较上游机制的 `knowledge_anchor_card_ids` 与目标催化范围：锚点可以只作为机制灵感，不必成为候选酶。只有具备目标化学适配依据时才提名并与库外候选统一比较；记录关键冲突和预算内未完成的比较，不将未比较等同于不适合。工具不会自动补入锚点候选，也不会赋予默认分数；Wiki 被读取或命中本身不是入选理由。

提交前结合 context 的 `unread_retrieved_refs` 与本轮检索摘要，核对“可读但尚未回读”的片段：若其摘要显示与现有候选不同的反应中心、基团去向或天然分支终止，且可能解释一个尚未闭合的目标事件，在现有读取预算内优先读一条代表性 MD 原文，再与已选候选统一比较。可读仅表示取得读取资格，不是证据或保留名额；读后有硬冲突、只是同底物/同辅因子、或不能解释缺口时可淘汰，并在 `knowledge_gaps` 简记原因。若预算已尽，注明未读覆盖限制并正常提交；不得为了单张卡阻塞整批结果。

对锚点卡不能只用共同底物评分。按识别/结合、辅因子活化环境、有来源的净基团去向或成断键、天然分支终止、目标所需替代终止与释放分维度比较；最后一项始终保持未验证假说。局部图拓扑若与已有高排名候选不同，须进入机制多样性比较；天然终产物不同或替代释放未证明应降低 confidence 并写入 knowledge_gap，不得静默删除或升级为已验证活性。

## 事件证据复核

仅以本轮回读的 Markdown Wiki 为可引用知识，不读取配套事件文件或本体知识。复核上游事件的证据等级：来源净反应、结构解释和模型提出的中间体不能混为实测机理。比较天然反应与目标的实际成键原子、共同底物、净产物和电子收支；局部氧化不自动意味着额外氧化剂。替代终止路线须保留此前所有未证明步骤与验证信号，不能用自己提出的中间体证明候选成立。既有 event_evidence 的未知或冲突不能在筛选中升级为已知活性。

逐条读取上游 `counterfactual_bridges`。候选排序必须同时说明覆盖的共享事件、仍缺失的事件、天然终止冲突，以及目标替代终止依赖的未验证假设；命中数量不是成功概率。不得调用或引用 `enzyme_entity_*` 工具、本体文件或本体 Demo 运行目录。

### 可省略字段与上下文交付

context_digest 可省略，程序使用本会话最近已签发且版本一致的回执；主动传入旧指纹仍会拒绝。候选的 confidence、contradiction_refs、conflict_reason、engineering_plausibility，以及路线的 confidence、contradiction_refs、unsupported_intermediates 均可省略；未知不等于已验证，已发现的冲突仍须说明。
若返回 next_delivery_cursor，需要后续信息时，使用相同 context 工具仅传 run_id 与 delivery_cursor=next_delivery_cursor 继续读取；这是缓存内容分包，不是补查，不产生新 embedding 或重试。按 field 和 part 拼接 json_text 读取所需字段；已有信息足够时可提交，不强制读完所有传输页。引用授权、版本一致性和最终科学校验仍执行；分页是否读完不是科学正确性的依据。

辅助字段可省略：component_function 默认 other；Wiki 的 substrate_relation 默认 unresolved、native_reaction_quote 默认空，不代表已报道目标活性。明确声称 reported_exact 时仍须提供原文引文。路线 material_handoff 可省略，程序保留“物料连续性待核对”缺口，不视为已闭合。机制提交可省略 hypotheses 以使用当前有效暂存稿；筛选 use_staged=true 时可省略 candidates/routes，无需重复空数组；knowledge_gaps 无额外内容时可省略。缺少有效草稿、来源不明或明确化学冲突仍执行原校验。

步骤编号 step_index 建议放外层，也接受 route_step 内的 step_index（或 step/order）；同时给出时必须一致。省略时只恢复唯一空位或完全相同已存步骤；多个位置不明确时须显式编号，程序不猜化学顺序，也不覆盖不同的旧步骤。

字段位置兼容：单个 hypothesis/candidate/route_header/route_step 信封内误放的外层字段，以及外层误放的该对象专属字段，可由程序按 Schema 无歧义恢复并记录。不要依赖兼容层猜测身份或科学结论；多对象混用、同字段不同值、未知字段仍需修正。


## 未解决机制缺口的增量查证

knowledge_gaps 不只是最终备注。先找出会改变机制、辅因子或候选判断的未解决问题，优先在已有 context 预算内读取新证据。cursor 可与 unresolved_gaps（最多3个、每项400字）一起提交：仅在缓存池中按缺口重排尚未交付的片段，优先提供未见蛋白，不重算 embedding；这不是化学适配排序。需要改写问题时使用 queries，仍服从原有新查询额度，不重复原查询。

摘要以 question_id 关联 gap_questions 中的问题，短摘录最多360字且明确不是引用证据；必要时按 ref 回读完整正文。程序按已交付字数优先安排交付较少的问题，并为其他问题保留摘要空间。因字符限制未交付的相关条目在同问题的后续页优先续送；deferred_count 不是已读或已排除数量。next_cursor 自动延续原问题与预算。不能把“只在池中、未送达、只见摘要”写成“库内不存在”；预算不足仍注明具体未核实内容。续送不自动提名任何酶。

提交工具会检查 knowledge_gaps；有可行动的缺口且预算允许时，返回 committed=false、followup_required=true 和少量新摘要，而不是失败或阶段完成。只检查新摘要，必要时通过 read_refs 回读，再沿用原 ID 修订受影响条目并提交；不能重复提交原请求绕过检查。未读摘要不能引用，补查不强制提名或保留任何酶，也不按库内/库外加减化学分。优先在知识缺口中列出会改变科学判断的问题，纯实验验证需求不代表必须消除的未知。

缺口驱动分页不受普通两页限制，但仍共享本阶段40条去重摘要、10次context调用、4批/24条原文与证据字符预算；额外缺口摘要累计最多14000字符。提交补查为原文读取和收尾预留2次context空间，不增加阶段轮数或错误重试。相同缺口连续两次补查没有新增已读证据、没有未读线索、查询已重复、后端失败或预算耗尽时停止；明确覆盖未知，不声称已检索全库或不存在相应酶。续跑/科学修订不清零已花费预算与故障状态。

成功暂存后若收到 gap_check，条目已保存，不要重发。补查预约不阻止后续有效条目暂存，可以先完成当前批草稿；下一次 context 优先处理预约，明确请求的 read_refs 则正常回读。预算充足时尽早检查，避免积压到最后。额度耗尽后不再预约；保留缺口并提交。进入最后提交窗口时，直接提交已有草稿与科学未知，提交工具仍检查有额度的必要补查；不要为清除预约而重复暂存或发无操作请求。

finish_exploration=true 是收尾请求，不是跳过缺口检查的开关。尚未检查的检索问题仍在既有预算内补查；已检查同组问题、没有新增证据或达到既有预算时，保留未知并收尾。运行器不自动改写此字段，最后提交窗口保留 context 以便必要原文回读。预算与科学校验不放宽。

## 蛋白身份与目标活性分开核实

同一个蛋白可能有别名、历史反应注释与新活性假说。先核对蛋白身份，再逐项判断目标反应；旧注释不能支持目标反应，不等于它证明该蛋白不能催化目标。不要按旧注释另外创造一个水解酶/氧化酶实体，也不要因供体、辅因子或旧产物不同自动排除原蛋白。明确目标断键、构型、催化域或电子收支冲突仍须审核，不为命中强行保留。

筛选时沿用原 proposal_id 和 enzyme_name。若已读 Wiki 可对应同一具体蛋白，model_enzyme 可填 identity_binding：card_id、refs（该卡已授权原文）、name_quote（卡片显示名与候选名称/身份依据中共有的逐字名称）、organism_quote（该卡完整物种字段）、basis（解释为何同一蛋白，包括变体/物种边界）。不以同家族、EC 或功能相似推断同一蛋白；变体未知时保持身份未核实。服务端从卡片填 UniProt/EC，不要自己填 enzyme_id/ec_number。不要把天然辅因子自动复制到新活性，support_refs 只放目标催化支持，不以 identity_binding.refs 冒充活性证据。

identity_verification 只关联身份，不升级 candidate_kind、evidence_scope、化学评分或目标活性。审核须分别说明“身份已关联”和“目标活性未验证”，保留原 ID 的候选与理由到最终表。若仅因旧反应不同而排除一个别名候选，检查是否误拆同一蛋白，不把两个名称当成两种已证实催化者。原始输入已点名的酶仍注明用户来源，不计为独立发现。

修订更换科学判断后，同库指纹下已封存的 selected_refs 仍可 read_refs 回读或 resume 恢复，不需重新检索，不刷新读取预算。旧摘要、过期分页游标或外来运行引用不能因此升级为已读证据。


## 补查问题与相关性

knowledge_gaps 用于记录科学限制，不直接当作检索问题。需要补查时在提交中用 retrieval_questions 提供最多3个、每个240字的正向科学问题，只描述待核对的催化事件、供受体、中间体或终止方式；不要写“本库没有”、未验证状态、实验待办，也不能把尚待发现的答案写进查询。未提供时只复用本轮已提出的检索问题，不从限制段落编造新问题。新问题使用现有混合检索与额度，替代旧池盲翻，不增加次数。

缓存补查逐问题按科学正文与已有混合召回信号排序，再交替交付相关蛋白的代表片段；scope 通用声明不作匹配正文。gap_match 列出词面匹配及未匹配项，不是化学覆盖结论；仅共享辅因子、氧供体等词不证明能完成目标催化事件。需要核对的代表摘要先回读，再调整对应机制/候选；不相关则写明哪种化学事件不匹配，不能仅凭摘要断言全库不存在。没有相关线索时停止，不为凑页数/候选数读无关卡片或强行保留酶。

## 修订身份传递

修订自动沿用首次冻结的独立候选草案，不重新生成 candidate_draft。已有 proposal_id 的候选条目省略 enzyme_name，由程序从冻结草案填回；更新支持、冲突、评分和机制关联即可。不要把辅因子、机制说明或物种猜测改写进旧身份名称。真正新增的具体酶使用新 proposal_id 和 discovery_reason，家族不能借旧 ID 升格为具体蛋白。

## 主动核对蛋白身份

按 identity_review_tasks 逐项核对，不等最终审核才补身份。protein_identity_evidence 是当前已读卡的名称、UniProt、来源物种及可引用 ref，即使召回片段只有反应段，这些卡片头信息也已送达，不要误报“未提供”。已知别名可在 name_evidence 解释，仍须核对物种/变体；同家族、同 EC 不能判为同一蛋白。同一具体蛋白用原 proposal_id 显式提交 identity_level=protein 和 identity_binding，目标活性继续独立分层。信息确实缺失时用现有检索/原文读取补查，受原预算约束；无依据就保留未核实，不猜测、不增加重试。不得因旧反应不支持目标就否认蛋白身份。
