---
name: paper-query-planner
description: 将研究问题改写为结构化论文检索计划。Use this skill when a literature task needs search queries, source selection, time range, or screening rules before retrieval.
routing:
  domains:
  - literature
  intents:
  - query-planning
  - literature-search-planning
  - screening-rule-design
  aliases:
  - 文献检索计划
  - 检索式
  - query planner
  - search strategy
  - 纳排标准
  positive_examples:
  - 把这个研究问题转成论文检索计划
  - 帮我设计 PubMed/arXiv 检索式和筛选规则
  - 先不要下载论文，先规划检索策略
  negative_examples:
  - 总结单篇 PDF
  - 综合已经筛选好的 paper cards
  - 运行 docking
  skill_role: planner
  priority: 66
---

# Paper Query Planner

Use this skill when the task is to turn a natural-language research question into a structured literature search plan before retrieving papers.

## Workflow

1. Rewrite the user request into a clear `research_question`.
2. Expand the topic into search-oriented terms:
   - topic keywords
   - synonyms
   - optional exclude terms
   - date range
   - preferred sources
3. Define practical screening rules before retrieval starts.
4. For `paper_auto_coordinator` or `paper_search`, prefer:
   - `paper_prepare_workspace`
   - then `paper_literature_search`
5. If the task includes “下载 PDF / 获取全文”, plan retrieval in this order:
   - 先定位 `PMID` / `DOI` / `PMCID`
   - 生物医学论文优先 `PubMed/PMC`
   - 再用 `Crossref` / `Semantic Scholar` 做 DOI 补全和召回扩展
   - publisher 落地页只作为最后兜底，不要把“抓网页 PDF 按钮”当默认方案

## Output Expectations

- research_question
- search_queries
- exclude_terms
- date_range
- preferred_sources
- screening_rules

## Guidelines

- 同时给出主题词和同义词
- 若用户要求“最新进展”，优先近 3-5 年
- 若用户要求“经典工作”，可放宽时间范围
- 若问题范围过大，应主动收窄检索主题
- 若用户目标包含“稳定下载全文”，在 `preferred_sources` 中优先写 `pubmed`、`crossref`、`semanticscholar`
- 对生命科学/医学主题，优先考虑能映射到 `PMCID` 的来源，因为后续可走 PMC 官方 OA 分发链路

## Guardrails

- 先产出检索计划，再进入检索，不要直接跳到结论。
- 若用户问题本身不清晰，应先收窄范围，再生成 query。
- 不要把检索计划写成泛泛建议，尽量给出可直接执行的 query。
- 不要默认把 publisher PDF 直链当成稳定入口；尤其 `PMC` / `MDPI` 可能返回挑战页、403 或 HTML 包装页。
- 当任务明确要求下载时，检索计划里应包含“先拿标识符，再走官方开放获取分发或 MCP 下载工具”的步骤。
