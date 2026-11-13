# paper-research-workflow 配置说明

本文档说明 `paper-research-workflow` 在根目录 `plugins.json` 中支持的运行配置。

配置定义的最终依据是 `plugin.py` 中的 `PaperResearchPluginConfig`。插件加载时，TCode 会先用该 Pydantic Model 校验 `config`，再调用插件的 `initialize()`。

## 完整配置示例

```json
{
  "name": "paper-research-workflow",
  "path": "./plugins/paper_research_workflow",
  "enabled": true,
  "config": {
    "workspace_dir": "./.tcode/plugins/paper_research_workflow",
    "auto_bootstrap": true,
    "default_max_results": 12,
    "auto_resolve_pdf_links": true,
    "auto_download_pdf_files": false,
    "browser_pdf_fallback_enabled": true,
    "browser_pdf_fallback_limit": 2,
    "browser_pdf_fallback_timeout_seconds": 90,
    "pdf_enrichment_limit": 8,
    "pdf_download_subdir": "download",
    "library_auto_escalate_on_insufficient_evidence": false,
    "library_chunk_max_chars": 900,
    "library_chunk_overlap_chars": 120,
    "library_allow_cross_page_chunks": true,
    "library_paragraph_summary_max_chars": 220,
    "library_query_candidate_doc_score_threshold": 0.0,
    "library_query_candidate_doc_max_hits": 20,
    "library_query_candidate_doc_limit": 5,
    "library_query_candidate_passage_limit": 8,
    "library_query_evidence_limit": 8,
    "library_evidence_context_max_records": 500,
    "library_evidence_ensure_max_references": 200,
    "library_query_limit_docs_max": 10,
    "library_query_limit_evidence_max": 12,
    "library_render_page_images": true,
    "library_render_table_images": true,
    "library_render_figure_images": true,
    "library_visual_image_dpi": 200,
    "library_visual_image_max_dim": 1000,
    "library_visual_table_padding_px": 12,
    "library_visual_figure_min_dim": 48,
    "library_visual_figure_min_area": 4096,
    "library_visual_max_pages": 0,
    "library_wiki_enabled": true,
    "library_wiki_compile_after_ingest": true,
    "library_wiki_auto_approve_new_pages": true,
    "library_wiki_require_human_review": true,
    "library_wiki_auto_apply": false,
    "library_wiki_require_evidence": true,
    "library_wiki_max_pages_per_compile": 12,
    "library_wiki_query_page_limit": 8,
    "library_wiki_lint_after_apply": true,
    "library_wiki_regeneration_enabled": true,
    "library_wiki_markdown_sync_enabled": true,
    "library_wiki_markdown_sync_interval_seconds": 5,
    "library_wiki_vector_search_enabled": true,
    "library_wiki_vector_index_min_pages": 1,
    "library_wiki_vector_search_weight": 0.35
  },
  "permissions": {
    "filesystem.read": [
      "./.tcode/plugins/paper_research_workflow",
      "./.tcode/plugins/paper_research_workflow/**",
      "./download",
      "./download/**"
    ],
    "filesystem.write": [
      "./.tcode/plugins/paper_research_workflow",
      "./.tcode/plugins/paper_research_workflow/**",
      "./download",
      "./download/**"
    ]
  }
}
```

## 工作目录与团队启动

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `workspace_dir` | string | `.tcode/plugins/paper_research_workflow` | Paper 插件在每个 Workspace 下的数据目录。相对路径以当前任务 Workspace 为基准。文献库位于该目录下的 `library/`。 |
| `auto_bootstrap` | boolean | `true` | 进入 Paper 管理 Agent 的 Session 时，自动创建并恢复固定 Paper Workers，同时同步运行策略 Memory。 |
| `default_max_results` | integer | `12` | `paper_literature_search` 未显式传入 `max_results` 时使用的默认候选数。运行时会限制在 1～30。 |

示例：当前任务 Workspace 为 `/workspace/user-a/project-1` 时，默认文献库目录为：

```text
/workspace/user-a/project-1/.tcode/plugins/paper_research_workflow/library
```

`workspace_dir` 是 Paper 插件状态目录，不是 TMCP 的 `workspace_root`，不要把两者混用。

## PDF 地址解析与下载

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `auto_resolve_pdf_links` | boolean | `true` | 搜索候选文献后，默认尝试通过统一 Paper MCP 解析可用 PDF 地址。工具调用参数可覆盖该默认行为。 |
| `auto_download_pdf_files` | boolean | `false` | 是否默认把解析出的 PDF 下载到当前 Workspace。即使为 `false`，用户明确要求下载时仍可按请求执行。 |
| `pdf_enrichment_limit` | integer | `8` | 一次搜索中最多对多少条候选执行 PDF 地址补全。初始化时限制在 0～30。0 表示不做补全。 |
| `pdf_download_subdir` | string | `download` | PDF 下载目录，建议使用 Workspace 内相对路径。浏览器兜底文件会写入其下的 `browser-fallback/`。 |

## 浏览器 PDF 兜底

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `browser_pdf_fallback_enabled` | boolean | `true` | 统一 Paper MCP 已穷尽下载来源后，是否允许进入浏览器兜底流程。 |
| `browser_pdf_fallback_limit` | integer | `2` | 单次搜索最多尝试的浏览器兜底数量。初始化时限制在 0～10。 |
| `browser_pdf_fallback_timeout_seconds` | integer | `90` | 单次浏览器兜底任务超时。初始化时限制在 15～300 秒。 |

浏览器兜底不是默认下载器，只应在统一下载链路返回 HTML/PDF Viewer、JS 跳转、403/429、超时或无开放 PDF 后使用。

## 文献库升级策略

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `library_auto_escalate_on_insufficient_evidence` | boolean | `false` | 本地文献库证据不足时，是否自动推动 Coordinator 继续进入外部搜索、阅读、综合和复核流程。`false` 时由 Coordinator 根据用户目标决定，不机械升级。 |

## 文本切块和摘要

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `library_chunk_max_chars` | integer | `900` | 文本 Chunk 的目标最大字符数。内部最低按 260 处理。 |
| `library_chunk_overlap_chars` | integer | `120` | 相邻文本单元形成 Chunk 时的重叠字符数，内部最低为 0。 |
| `library_allow_cross_page_chunks` | boolean | `true` | 是否允许同一个 Chunk 跨越 PDF 页边界。关闭后，换页会强制结束当前 Chunk。 |
| `library_paragraph_summary_max_chars` | integer | `220` | L1 段落摘要的最大字符数，内部最低按 120 处理。 |

### 修改切块参数后的影响

这些参数在 PDF 入库、重新解析或重建索引时使用。修改配置不会自动重写已经存在的 `l1_passages.json`、`l2_chunks.json` 和 FAISS 索引。

要让旧文献采用新参数，需要对目标文献执行 `force_reindex`，或使用后续提供的批量重建流程。

## 查询召回

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `library_query_candidate_doc_limit` | integer | `5` | 查询未指定 `limit_docs` 时，默认召回的候选文献数。 |
| `library_query_candidate_passage_limit` | integer | `8` | 每篇候选文献默认召回的段落数量。 |
| `library_query_evidence_limit` | integer | `8` | 查询未指定 `limit_evidence` 时，默认返回的证据数量。 |
| `library_evidence_context_max_records` | integer | `500` | 单篇论文最多同步到 EvidenceStore 的 L2 Context Chunk 数，必须为 1～2,000。表格 Chunk 会优先进入这个有界集合，避免位于文末的表格证据因简单截断而缺失。 |
| `library_evidence_ensure_max_references` | integer | `200` | 单次 Wiki Proposal 编译最多按 Claim 来源补齐的 Fact/Chunk Evidence 数，必须为 1～2,000。它只限制缺失证据的 Proposal 时补齐，不改变普通搜索、Wiki 打开或 Agent 查询。超出上限的 Claim 保持 `manual_unverified`，不会阻断 Proposal。 |
| `library_query_candidate_doc_score_threshold` | float | `0.0` | 相对相关性阈值。0 表示禁用；0.5 表示尝试保留得分至少为最高分 50% 的文献。 |
| `library_query_candidate_doc_max_hits` | integer | `20` | 开启阈值模式时，单次文献召回数量上限。 |
| `library_query_limit_docs_max` | integer | `10` | 工具调用参数 `limit_docs` 的安全上限，防止异常请求放大上下文。 |
| `library_query_limit_evidence_max` | integer | `12` | 工具调用参数 `limit_evidence` 的安全上限。 |

阈值模式的行为：

1. 按语义分数、关键词和问题类型综合排序；
2. 计算 `score / max_score`；
3. 保留达到阈值的文献；
4. 最多返回 `library_query_candidate_doc_max_hits`；
5. 如果阈值命中数量不足，仍保证 `library_query_candidate_doc_limit` 的基础召回数量。

## PDF 视觉产物

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `library_render_page_images` | boolean | `true` | 入库时是否生成 PDF 页图。 |
| `library_render_table_images` | boolean | `true` | 是否根据表格边界生成表格截图。 |
| `library_render_figure_images` | boolean | `true` | 是否提取可能的 Figure 图片。 |
| `library_visual_image_dpi` | integer | `200` | 页图渲染 DPI，运行时限制在 72～300。 |
| `library_visual_image_max_dim` | integer | `1000` | 视觉图片最长边最大像素，内部最低为 256。 |
| `library_visual_table_padding_px` | integer | `12` | 表格截图边界向外扩展的像素，内部最低为 0。 |
| `library_visual_figure_min_dim` | integer | `48` | Figure 候选的最小宽/高像素，内部最低为 1。 |
| `library_visual_figure_min_area` | integer | `4096` | Figure 候选的最小面积，内部最低为 1。 |
| `library_visual_max_pages` | integer | `0` | 最多渲染多少页；0 表示不设置页数上限。 |

关闭视觉产物可以降低入库时间和磁盘占用，但会削弱页图、表格截图、Figure 证据展示和视觉复核能力。

## Paper Wiki

所有字段都可以省略；代码默认值见下表。仓库根 `plugins.json` 和 `config/workdir_template/plugins.json` 已写入推荐示例，新建 Workspace 会继承模板中的设置。

| 配置项 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `library_wiki_enabled` | boolean | `true` | 是否注册并开放 Workspace 级 Paper Wiki 能力。新 Workspace 和未配置该字段的 Workspace 默认开启；可显式设为 `false` 关闭。 |
| `library_wiki_compile_after_ingest` | boolean | `true` | 新论文成功入库后，是否在后台从 L0/L2/EvidenceStore 编译 Wiki Proposal。 |
| `library_wiki_auto_approve_new_pages` | boolean | `true` | 入库 Proposal 只包含首次创建的页面时自动批准并应用；涉及任何已有页面时仍需人工审核。 |
| `library_wiki_require_human_review` | boolean | `true` | 已有 Wiki 页面修改的人工审核安全边界。当前版本必须为 `true`；设为 `false` 会导致插件配置校验失败。 |
| `library_wiki_auto_apply` | boolean | `false` | 是否允许编译器/Agent 不受限地自动应用 Proposal。当前版本必须为 `false`；入库新页面使用上面的窄开关。 |
| `library_wiki_require_evidence` | boolean | `true` | 提交 Proposal 时检查 Claim 是否绑定 Evidence。缺少证据的 Claim 会产生审核警告并标为未验证，不会伪装成原文事实。 |
| `library_wiki_max_pages_per_compile` | integer | `12` | 单篇论文一次自动编译最多生成或更新多少个页面，运行时限制为 1～50，避免实体爆炸和过大的审核单。 |
| `library_wiki_query_page_limit` | integer | `8` | Agent 单次 Wiki 搜索最多返回多少个页面摘要，运行时限制为 1～50。搜索结果不携带整页正文。 |
| `library_wiki_lint_after_apply` | boolean | `true` | Proposal 应用后是否执行完整性检查。Web 审核接口按服务端配置执行并返回 Lint 结果，客户端不能绕过。 |
| `library_wiki_regeneration_enabled` | boolean | `true` | 是否允许认证用户提交受限 LLM 重新生成 Job。新 Proposal 仍需人工审核。 |
| `library_wiki_markdown_sync_enabled` | boolean | `true` | 兼容配置名：是否周期校验 Agent 物化 Markdown，并把外部语义改写恢复为 SQLite 当前审核版。不再把外部编辑同步成 Revision。 |
| `library_wiki_markdown_sync_interval_seconds` | integer | `5` | Wiki 前端激活时的自动同步周期，必须为 2～300 秒；另有服务端节流和写入稳定检查。 |
| `library_wiki_vector_search_enabled` | boolean | `true` | 是否在 FTS5 上增加 FAISS 向量召回。FAISS 不可用时自动回退，不阻止启动。 |
| `library_wiki_vector_index_min_pages` | integer | `1` | 非归档页面达到多少页后建立向量索引，必须至少为 1；阈值以下继续用 FTS5。 |
| `library_wiki_vector_search_weight` | number | `0.35` | 混合排序中的向量权重，范围 0～1，推荐 0.25～0.4。 |

推荐生产配置：

```json
{
  "library_wiki_enabled": true,
  "library_wiki_compile_after_ingest": true,
  "library_wiki_auto_approve_new_pages": true,
  "library_wiki_require_human_review": true,
  "library_wiki_auto_apply": false,
  "library_wiki_require_evidence": true,
  "library_wiki_max_pages_per_compile": 12,
  "library_wiki_query_page_limit": 8,
  "library_wiki_lint_after_apply": true,
  "library_wiki_regeneration_enabled": true,
  "library_wiki_markdown_sync_enabled": true,
  "library_wiki_markdown_sync_interval_seconds": 5,
  "library_wiki_vector_search_enabled": true,
  "library_wiki_vector_index_min_pages": 1,
  "library_wiki_vector_search_weight": 0.35
}
```

配置项之所以保留 `require_human_review` 和 `auto_apply`，是为了让已有页面修改的安全边界在部署配置和审计中显式可见。唯一的自动应用路径是文献入库后首次创建页面，并且应用前会在事务内再次确认没有同 canonical key 的现有页面。

部分批准由服务端基于原 Proposal 和稳定 Change ID 推导；LLM 重新生成只替换审核单；Markdown 同步使用哈希、稳定 Marker 和 Revision 乐观锁；向量索引 dirty 时 Web 异步重建，本次搜索继续使用 FTS5。完整运维示例见 `docs/wiki/配置项说明.md`。

Wiki 向量复用当前 Workspace 顶层 `tcode.json.embeddings` 配置。Provider 禁用或不可用时会回退确定性 Hash Embedding；FAISS 本身不可用时再回退 FTS5/LIKE。Embedding 模型或端点变化会改变版本标识并触发重建，不会混用不兼容向量。

## 环境变量配置

以下配置目前通过环境变量控制，不属于 `plugins.json` 的 `config`：

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `TCODE_PAPER_LIBRARY_BACKGROUND_EVIDENCE_SYNC` | `true` | 是否在后台把 Paper Facts、Chunks 和 Visual Artifacts 同步到 EvidenceStore。 |
| `TCODE_PAPER_LIBRARY_EVIDENCE_SYNC_TIMEOUT_SECONDS` | `180` | Evidence 同步超时，限制在 10～1800 秒。 |

## 权限说明

根 `plugins.json` 当前包含 `filesystem.read` 和 `filesystem.write` 配置，但插件权限系统的工作方式是：

1. 插件通过 Manifest v2 的 `permissions` 声明最大申请；
2. `plugins.json.permissions` 只能保留、缩小或拒绝这些申请，不能扩展到 Manifest 未申请的路径；
3. 插件通过 `PluginRuntime` 访问受控路径。

`paper-research-workflow` 已在 Manifest v2 声明文件读写申请，但现有文献库实现仍主要直接使用 `Path` 访问 Workspace。因此当前 `permissions` 块不能被视为 Paper 文件访问的完整强制安全边界；实际 Workspace 隔离仍依赖 TCode Server、Session Workdir 和上层路径校验。

后续如果把 Paper 文件访问迁移到 `PluginRuntime`，必须同步更新本节和默认权限声明。

## 配置校验和重载

- 配置值会经过 `PaperResearchPluginConfig` 校验和类型转换；
- Python 入口导入前还会经过 `config.schema.json` 校验；
- 未配置字段使用代码中的默认值；
- `config.schema.json` 禁止未知字段，配置拼写错误会在插件代码导入前失败；
- 手工修改 `plugins.json` 后，已加载的插件不会自动重新执行 `initialize()`；
- 修改配置后应重启 TCode Server，或通过明确的插件卸载/重新加载流程生效。

## 推荐配置组合

### 只检索，不默认下载 PDF

```json
{
  "auto_resolve_pdf_links": true,
  "auto_download_pdf_files": false,
  "browser_pdf_fallback_enabled": true
}
```

### 降低入库磁盘占用

```json
{
  "library_render_page_images": false,
  "library_render_table_images": false,
  "library_render_figure_images": false
}
```

此组合会失去视觉证据产物，不建议用于需要表格、Figure 和页图复核的科研任务。

### 扩大候选文献召回

```json
{
  "library_query_candidate_doc_limit": 8,
  "library_query_candidate_doc_score_threshold": 0.5,
  "library_query_candidate_doc_max_hits": 30,
  "library_query_limit_docs_max": 20
}
```

扩大召回会增加排序、证据读取和模型上下文成本，应通过性能测试后再调整。
