---
name: multi-model-sequence-mutation-scoring
description: 使用 VenusPLM、ESM2-650M、ESM-1v、ESM-1b 对蛋白序列突变进行 zero-shot 多模型评分、对比和共识排序。用户要求筛选单点或指定突变候选、比较多个序列模型、导出逐模型结果或生成聚合 Top-N 时使用；实际计算必须通过 job-protocol-mcp 异步提交到 mutation-scoring-mcp。
metadata:
  when_to_use: >-
    Use when the user wants multi-model zero-shot mutation scoring, consensus mutation ranking,
    candidate mutation screening, or per-model and aggregate score artifacts from a sequence or FASTA.
  routing:
    domains: [protein-mutation, protein-sequence]
    intents: [multi-model-mutation-scoring, zero-shot-mutation-ranking, consensus-mutation-ranking]
    aliases: [多模型突变评分, zero-shot mutation, VenusPLM, ESM-1v, 突变候选排序]
    positive_examples:
      - 用多个序列模型给这些突变打分
      - 对候选突变做共识排序
      - 生成 zero-shot mutation scoring 结果
    negative_examples: [结构预测, 化合物查询, 只筛选文献]
    required_inputs: [fasta_or_sequence]
    conflicts_with: [evolvepro-runner, ligandmpnn-runner]
    depends_on: [protein-job-protocol]
    skill_role: runner
    priority: 82
---

# Multi-model Sequence Mutation Scoring

用于实际提交多模型 zero-shot 突变评分；只解释方法时不要提交任务。

## 执行规则

1. 不直接调用目标计算工具，也不运行本地评分脚本；只调用 `mcp_job-protocol-mcp_submit_job`。
2. 固定使用 `target_server=mutation-scoring-mcp` 和 `target_tool=multi_model_sequence_mutation_scoring_mcp_server`。
3. 字段、类型或默认值不确定时，先调用 `mcp_job-protocol-mcp_describe_target_tool`；默认使用 `detail=compact`。
4. `sequence` 与 `fasta_path` 必须二选一；使用 FASTA 时，`fasta_path` 必须直接传前端提供的 ArtifactRef。不要用 `exec` / `glob` 查找 Workspace 文件，也不要转换成任何路径。
5. `candidate_mutants` 与 `positions` 互斥。两者都省略时，生成全序列所有单点替换。
6. 返回 `LOCAL-*` 后，本轮只汇报受理信息，不查询状态或结果。
7. 结果仅用于候选优先级排序，不得声称已经得到实验验证。

## 最小提交

直接序列：

```json
{
  "input_config": {
    "target_server": "mutation-scoring-mcp",
    "target_tool": "multi_model_sequence_mutation_scoring_mcp_server",
    "sequence": "MKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFPDWQ"
  }
}
```

FASTA：

```json
{
  "input_config": {
    "target_server": "mutation-scoring-mcp",
    "target_tool": "multi_model_sequence_mutation_scoring_mcp_server",
    "fasta_path": {"kind": "artifact", "artifact_id": "art_input_fasta"},
    "top_k": 50
  }
}
```

## 候选范围

只评分指定候选：

```json
{
  "input_config": {
    "target_server": "mutation-scoring-mcp",
    "target_tool": "multi_model_sequence_mutation_scoring_mcp_server",
    "sequence": "MKTAYIAKQRQISFVK",
    "candidate_mutants": ["M1A", "K2R", "M1A:K2R"],
    "models": ["venusplm", "esm2_650m", "esm1v", "esm1b"],
    "failure_mode": "partial"
  }
}
```

只扫描指定位置：

```json
{
  "input_config": {
    "target_server": "mutation-scoring-mcp",
    "target_tool": "multi_model_sequence_mutation_scoring_mcp_server",
    "sequence": "MKTAYIAKQRQISFVK",
    "positions": [2, 5, 9],
    "top_k": 20
  }
}
```

`models` 只能使用：`venusplm`、`esm2_650m`、`esm1v`、`esm1b`。

- `failure_mode=partial`：保留成功模型，失败模型明确列在 `failed_models`，聚合时排除失败模型。
- `failure_mode=fail_fast`：任一模型失败即终止任务。
- 输入含 `X` 时默认拒绝；只有用户明确接受旧版行为时才传 `unknown_residue_policy=replace_with_cysteine`。

## 按需查询参数

```json
{
  "input_config": {
    "target_server": "mutation-scoring-mcp",
    "target_tool": "multi_model_sequence_mutation_scoring_mcp_server",
    "detail": "compact",
    "fields": ["candidate_mutants", "positions", "models", "failure_mode", "top_k"]
  }
}
```

只有用户明确要求完整参数表时才使用 `detail=full`。

## 结果解释

后续用户明确要求查看结果时，按 `protein-job-protocol` 查询。完成结果重点包括：

- `all_models_detailed.csv`：每个成功模型的原始分和归一化分。
- `all_models_aggregated.csv`：平均归一化分、模型支持数和共识排名。
- `prediction_results.csv`：兼容旧流程的逐模型排名。
- `run_manifest.json`：模型成功/失败状态、警告和 artifact 路径。
- `top_mutations`：受 `top_k` 限制的聚合结果摘要。

若 `failed_models` 非空，必须明确说明这是部分模型结果；不得将失败模型替换为模拟值或随机值。

当前固定使用 wild-type marginal logit difference：每个位点在野生型上下文中计算突变残基与
野生型残基的 logit 差，多位点候选取各位点均值。这是候选排序信号，不建模多突变之间的显式上位性。
