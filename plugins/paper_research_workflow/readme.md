# paper-research-workflow

运行配置、默认值、生效时机和权限说明见 [CONFIG.md](CONFIG.md)。

本地 `paper-pdf-reader` skill 及其旧版 PDF/ODL 辅助脚本已移除。

当前 `paper_research_workflow` 仍以论文检索、阅读编排、卡片保存与综合分析为主链路。

当前还提供 Workspace 级 Paper Wiki：

- Wiki 是 Paper Library/EvidenceStore 的可编辑知识投影，不替代论文原文；冲突时以论文原文为准。
- 新论文入库后可自动生成待审核 Proposal，只有用户在“文献 Wiki”界面批准后才会写入正式页面。
- 用户可以编辑 Markdown、结构化 Claim、别名和实体类型；每次保存都会产生 Revision，可回滚、软删除和恢复。
- Proposal 支持按页面字段和单条 Claim 部分批准；不满意时可一键让受限 LLM 生成替代 Proposal，仍需人工审核。
- 外部 Markdown 可自动 Reconcile 回数据库；搜索采用 SQLite FTS5 + 可降级的 FAISS 向量混合召回。
- Agent 采用 `搜索少量摘要 -> 读取少数页面 Claims -> 按需回到原始证据` 的渐进方式，禁止把整个 Wiki 塞入上下文。
- 普通问答默认只读，不自动沉淀；综合 Agent 只能提交 Proposal，不能批准或应用。

完整设计、使用、运维和更新记录见 [`docs/wiki`](../../docs/wiki/README.md)。

当前额外支持一个工作区级本地文献库：
- 对同一篇本地 PDF 先做查重，已入库则直接复用
- 未入库时只有在用户明确要求“入库/保存到本地文献库/保存到本地数据库/force_reindex”，或经过 `question` 确认后，才解析并入库；如果只是临时分析，则走 PDF 解析/阅读流程，不写入本地库
- 团队内先查本地文献库，再决定是否继续外部检索、稳定阅读和综述
- 文献库采用显式分层：
  - Raw：原始 text/table/full_text 产物
  - L0：文献级摘要，用于粗召回
  - L1：段落或局部单元梗概，用于缩小范围
  - L2：开放 facts + chunk/table evidence，用于精确回答
- 默认 chunk 支持跨页，并通过 `plugins.json` 中的 `library_*` 参数调节长度、overlap 和查询阈值
- 当本地文献库证据不足时，是否自动继续进入 `paper_search / paper_reader / paper_synthesis / paper_review`，由 `plugins.json` 中的 `library_auto_escalate_on_insufficient_evidence` 控制

对于单篇本地 PDF：
- 优先由 `paper_generalist` 作为执行型成员加载 `pdf` skill，并按 skill 中明确给出的脚本流程处理正文、表格和必要的页图
- `paper_library_manager` 负责本地 PDF 查重和已确认保存后的入库；保存意图不明确时由 coordinator/generalist 先向用户确认
- `paper_library_retriever` 负责先查本地文献库并返回证据定位
- `paper_reader`、`paper_synthesis` 继续负责阅读、归纳和回答

对于 ZIP、目录、批量 PDF ingest：
- 仍不在 paper 团队内硬做，应通过专门的批量导入流程或外部工具处理

历史单 PDF 入库冒烟脚本已清理；当前入库使用上述团队流程。文献库 API 的正式回归从仓库根目录运行：
```bash
python -m pytest tests/test_paper_library_api.py -q
```



```mermaid
flowchart TD
    U["用户请求"] --> C["paper_auto_coordinator"]

    C --> D{"先分类任务"}

    D -- "流程/规划/说明/格式改写/已有结果重述" --> A["不检索\n直接回答或直接协调"]
    D -- "当前上下文已足够直接回答" --> A

    D -- "单篇本地 PDF 还没入库" --> L["paper_library_lookup_pdf"]
    L --> E{"已入库?"}
    E -- "否" --> G["paper_generalist\n执行 PDF skill 解析"]
    G --> M["paper_library_manager\n入库 Raw/L0/L1/L2"]
    M --> Q["按需查询本地库"]

    E -- "是" --> Q

    D -- "需要本地文献事实/证据问答" --> Q["按需查询本地库"]

    Q --> S{"这是简单一次性查询?"}
    S -- "是" --> T["coordinator 直接调用\npaper_library_query / get_record"]
    S -- "否，需要多轮证据整理" --> R["paper_library_retriever"]

    T --> F{"证据足够?"}
    R --> F

    F -- "足够" --> O["直接回答\n返回页码 / table_id / chunk_id 等证据"]
    F -- "不足" --> X["再进入 paper_search / paper_reader /\npaper_synthesis / paper_review"]

```



```mermaid
flowchart TD
    A["用户问题"] --> B{"是否已确定具体 doc_id / 单篇 PDF?"}

    B -- "否，先跨文献找候选" --> C["全局召回层
    读取:
    library/registry.json
    library/index/chunks.json
    library/index/chunks.faiss
    library/index/chunks.faiss.meta.json"]

    C --> D["得到候选 doc_id 列表"]

    B -- "是，已知 doc_id" --> E["直接进入单篇文献分层加载"]

    D --> E["对命中文献逐篇分层加载"]

    E --> L1["Level 1: 轻量摘要层
    主要读取:
    docs/<doc_id>/l0_summary.json
    docs/<doc_id>/materialized_views.json
    docs/<doc_id>/visual_manifest.json
    docs/<doc_id>/record.json(仅取轻量字段)"]

    L1 --> J1{"L1 是否足够回答?"}
    J1 -- "是" --> Z["直接回答"]
    J1 -- "否" --> L2["Level 2: facts + anchors 证据层
    主要读取:
    docs/<doc_id>/l2_facts.json
    docs/<doc_id>/evidence_anchors.json
    docs/<doc_id>/record.json(仅取 facts/anchors 对应字段)"]

    L2 --> J2{"L2 是否足够回答?"}
    J2 -- "是" --> Z
    J2 -- "否" --> L3["Level 3: 深入段落/块层
    主要读取:
    docs/<doc_id>/l1_passages.json
    docs/<doc_id>/l2_chunks.json
    docs/<doc_id>/index/chunks.faiss
    docs/<doc_id>/index/chunks.faiss.meta.json
    docs/<doc_id>/record.json(仅取深层字段)"]

    L3 --> J3{"L3 是否足够回答?"}
    J3 -- "是" --> Z
    J3 -- "否" --> R["必要时再读 Raw / Visual 原始产物
    可能读取:
    docs/<doc_id>/full_text.md
    docs/<doc_id>/text_extract.json
    docs/<doc_id>/tables_extract.json
    docs/<doc_id>/visual/page_images/*
    docs/<doc_id>/visual/table_images/*
    docs/<doc_id>/visual/figure_images/*"]

    R --> Z

```
