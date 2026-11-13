# 运行协议

四阶段产物依次为 01_reaction_analysis.json；02_mechanisms.json 与 03_capabilities.json；04_candidates.json 与 05_routes.json；06_critique.json 与最终 07 JSON/Markdown/Mermaid。

第二阶段使用 enzyme_mechanism_context 增量读取、enzyme_mechanism_hypothesis_stage 逐条暂存、enzyme_mechanism_plan_commit 原子提交；第三阶段使用 enzyme_screen_context / enzyme_screen_commit。第二阶段首次只用 run_id 获取反应与检索协议，不注入预选知识卡。第三阶段第一次只用 run_id 获取不含任何 Wiki 卡、检索摘要或继承引用的目标反应；Agent 必须先按化学适配度形成 1—8 个 candidate_draft，并在下一次 context 调用中冻结草案，之后才会交付 Wiki。草案只记录检索前先验，不限制检索后从已读证据发现的新候选。随后用同一 context 工具传 queries（每批最多 3 个、每个 240 字）和 reason 获取最多 8 条摘要；用 read_refs（最多 6 个）和 reason 读取原始反应及来源。摘要不能作为证据，只引用 selected_refs；暂存和提交携带最新 context_digest。后续可把新 queries 与已知 read_refs 合并一次调用。同次检索还有结果时，用 cursor=next_cursor 和 reason 继续取下一批，不重新 embedding；queries 是新问题，resume 是恢复旧内容，不混用。

第二阶段提交后，只把 02/03 正式引用字段中的已授权 ref 及原文快照封存为 evidence_handoff。第三阶段只有在 candidate_draft 已冻结后，才把校验通过且已送达的片段列为 inherited_refs，可直接引用且不再读取 Wiki；它们不占第三阶段的新读取轮次、ref 数或原文字符预算。第三阶段的新检索会排除这些片段，只查具体候选、竞争解释、冲突或未匹配能力。单次传输未送达的上游 ref 才出现在 upstream_read_refs，可分批继承。没有 handoff 的旧运行保留显式重读，但新版运行仍必须先冻结草案。

每阶段最多 3 批新查询、2 次额外分页、40 条去重摘要、4 批新增读取/24 条新增原文；累计新增原文 26000 字符，context 最多 10 次。弱线索只有明确缺口时用 allow_weak=true。命中状态不是化学覆盖；查询、分页、原文读取预算独立且回放不清零。仅阶段重试清空上下文时，用 resume=true 恢复本 run 缓存，最多两批，可带已读 read_refs；不重新检索、不重置探索预算。预算用尽或没有新增证据时提交并注明覆盖限制，不原样重试。正常只用必要调用；参数可纠正一次，同类错误不循环。机制按 slot 逐条暂存，单条错误不重发其他机制；最终 commit 传空 hypotheses 并复用草稿。

首次检索来自输入净变化和不同催化事件，不预选家族或辅因子。后续可根据机制缺口补查不同催化方向，搜索摘要不能替代精确证据。检索使用 SQLite FTS5 反应/注释词法召回与 FAISS 语义召回，RRF 融合，不加重排序。向量由宿主项目配置的 embedding API 生成；语义相似不证明化学能力，也没有结构指纹验证。semantic.status 非 ready 表示该次只用词法，不能声称完整语义覆盖；同一运行的 API 故障不会原样重试。索引按知识清单和反应文本摘要增量更新。retrieval_backend.json 固定模型与策略，retrieval_mechanism.json / retrieval_screen.json 保存查询及实际降级状态。context_digest 固定当前输入和已读证据，模型更新要求新 run。既往来源随引用传递，机制和分数仍由模型撰写，检索不预置科学答案。

第四阶段使用 enzyme_report_context / enzyme_critique_commit / enzyme_report_finalize，不重复生成完整 JSON。没有人工门禁；知识不足不是人工确认请求。重大结构性问题明确失败，禁止无限回放。

所有工具只在授权知识和当前运行目录内读写，不执行 shell、不读取 hidden_gold；唯一额外模型能力是宿主固定的 embedding API，不提供 Agent 可选 URL 或模型，不访问论文网页和历史记忆。幂等回执可重用，明确参数错误允许一次修正，运行时故障不原样重试。阶段重试有上限，旧产物重做保存在 .history/。

run.json 记录知识版本和指纹。更新 Wiki 后重新初始化干净 run，不能把新知识混入旧运行。最终前端只展示 user_summary；用户明确要求时才展示选定 JSON。

## 开放假说的来源标记

Wiki 是外部证据入口，不是答案全集。仍先按需检索，零命中或证据不足也可继续提出机制和辅因子。每条 hypotheses/cofactors 指定 evidence_basis：有 Wiki 类比依据用 wiki_analogy 并引用已读 wiki_refs；凭一般推理或模型自身知识提出、尚未被 Wiki 支持时用 model_hypothesis，wiki_refs 可为空。analogy_reason/reason 说明推理依据与缺口，不能编造引用来过校验。非空 Wiki 引用仍必须来自本轮已读证据，不能借相似酶的引用把模型记忆升级为已核实活性。模型假说必须标注“未经过 Wiki 核实”；来源标记不等于降低或提高化学评分。

## 三种输出身份

Wiki 是外部证据库，不是候选白名单。命名酶比较最多 9 条，未匹配能力另设最多 3 条，共最多 12 条，分清 candidate_kind：
- wiki_enzyme：提供 card_id 和该卡的已读 support_refs，身份由工具填入。
- model_enzyme：凭模型自身知识提出具体酶，提供 proposal_id、enzyme_name，可附 enzyme_family/cofactor_classes；不填 card_id、UniProt/EC 编号。support_refs 可为空；若引用其他 Wiki，只能说明局部类比，不证明该酶的身份或活性，始终标注“未经过 Wiki 核实”。
- unmatched_capability：尚未找到具体酶，提供 proposal_id、required_capability，candidate_role=route_component；不填 enzyme_name/card_id，不把能力虚构成蛋白实体。

proposal_id 使用简短英文数字/下划线/连字符，字母开头，本轮唯一。后两类 engineering_plausibility=unknown；support_reason 写提出理由，knowledge_gap 写未核实内容，缺依据不是反证。不要为凑数强制三类齐全，也不要自动改写 Wiki。

路线步骤二选一引用：库内用 card_id，模型酶或能力缺口用 proposal_id；evidence_refs 只写本轮已读 Wiki，可在 proposal_id 步骤为空。每步仍必须说明 input/output/material_handoff，不能因为具体酶未知就省略必要的转换，也不能声称它已实现。未知具体酶与机理本身矛盾要区分。未匹配能力不是候选酶排名的一员，报告另列。

## 检索与归属约束

首批最多三个不同视角：输入净变化、局部所需能力、竞争机制。缺口来自本轮推理，不从预置答案生成。已有查询仍相关则取下一批；查询方向不合适则改查缺失事件。弱相关和未召回都不是反证。

引用只复制工具给出的稳定片段 ref，不自行拼接数组下标。source_scope 为 inherited_parent_sources_not_direct_proof 时，来源是父段落背景，不是该句直接实验支持。activity_status 为 related_protein_only 或 attribution_requires_review 时，不宣称本蛋白已测该活性。库外候选填写 identity_level：唯一具体蛋白为 protein，仅酶名线索为 named_enzyme，家族为 family；family 不计入 Top N 具体蛋白。

每个机制可用 event_evidence 对各 events 的 1-based event_index 标注 local_evidence/related_analogy/model_hypothesis/unmatched_capability/conflict、已读 wiki_refs 和短 reason；有依据的状态必须引用原文。未单独标注的事件只按模型假说保存，不继承整条机制的 Wiki 支持。辅因子用 mechanism_ids 关联本轮 M1/M2/M3，未有明确关联可为空；不能按排序自动配对。
