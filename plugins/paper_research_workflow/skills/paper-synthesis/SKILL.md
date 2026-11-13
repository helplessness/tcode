---
name: paper-synthesis
description: 将多篇论文卡片归纳为证据导向的研究综述。Use this skill when multiple paper cards must be merged into themes, consensus, disagreements, and research gaps.
routing:
  domains:
  - literature
  intents:
  - evidence-synthesis
  - literature-review
  - research-gap-analysis
  aliases:
  - 文献综述
  - 证据综合
  - paper synthesis
  - research gaps
  - 主题归纳
  positive_examples:
  - 把这些 paper cards 综合成证据报告
  - 总结多篇论文的一致结论、分歧和空白
  - 写一个 evidence-oriented synthesis
  negative_examples:
  - 规划检索式
  - 筛选候选论文
  - 单篇 PDF 摘要
  skill_role: reporting
  priority: 67
---

# Paper Synthesis

Use this skill when paper cards are ready and the task is to produce an evidence-oriented synthesis rather than a paper-by-paper summary dump.

## Workflow

1. Read the paper cards.
2. Group them by theme, method family, dataset/setting, or claim cluster.
3. Write a synthesis that emphasizes evidence patterns:
   - what the papers agree on
   - where they disagree
   - what remains open
4. Persist the final synthesis with `paper_write_artifact(artifact_type="synthesis_report")`.

## Required Output

- research_question
- search_strategy
- included_papers
- theme_summary
- consensus
- disagreements
- open_gaps
- recommended_next_queries

## Guidelines

- 每个共识/分歧都应可回溯到 paper_cards
- 对证据不足的地方明确标注
- 对下一步检索建议尽量具体

## Guardrails

- 不要按论文顺序机械罗列摘要。
- 不要把薄弱证据包装成强结论。
- 若证据冲突，先描述冲突来源，再给出你的判断。
