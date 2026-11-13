---
name: paper-screening
description: 候选论文筛选与 paper card 结构化抽取准则。Use this skill when candidate papers must be filtered and normalized into paper cards before synthesis.
routing:
  domains:
  - literature
  intents:
  - paper-screening
  - paper-card-extraction
  - inclusion-exclusion
  aliases:
  - 文献筛选
  - paper screening
  - 纳排
  - paper cards
  - 候选论文过滤
  positive_examples:
  - 按纳排标准筛选这些候选论文
  - 把检索结果整理成 paper cards
  - 过滤不相关文献并说明原因
  negative_examples:
  - 设计检索式
  - 综合已经筛好的论文
  - 读取本地 PDF 做 OCR
  skill_role: processor
  priority: 66
---

# Paper Screening

Use this skill when candidate papers have already been retrieved and the next step is to decide what to keep, what to discard, and how to normalize each included paper into a paper card.

## Keep These Papers

- 与 research_question 直接相关
- 方法上具有代表性
- 结果或结论可比较
- 有足够元数据或摘要可供归纳

## Exclude These Papers

- 与主题弱相关的泛泛综述
- 重复报道或高度近似的镜像内容
- 无法识别研究对象、方法或结果的条目

## Required Paper Card Fields

- title
- year
- source
- authors
- url
- problem
- method
- dataset_or_setting
- main_results
- limitations
- why_relevant

## Workflow

1. Read candidate papers and compare them against the `research_question`.
2. Remove weakly relevant, duplicated, or low-information entries.
3. If metadata/abstract is too thin but the candidate has `pdf_download_path`, escalate to local PDF extraction before finalizing key fields.
4. If the candidate only has `pdf_status=resolved` plus `pdf_url` or `pdf_package_url`, request/download the local PDF first, then continue.
5. Normalize each kept paper into the required paper-card fields.
6. Persist results with `paper_save_cards`.

## Guardrails

- 筛选时优先考虑“能否支持后续归纳”，不是只看标题是否像相关。
- 若摘要信息不足，明确标记缺口，不要补写不存在的研究细节。
- 对排除理由尽量简洁且可解释。
- 不要把 publisher 挑战页、403 页面或 HTML 包装页误当作 PDF 证据。
- 若还没拿到本地 PDF，不要假装已经完成全文筛读；应明确标记为“待下载/待抽取”。
