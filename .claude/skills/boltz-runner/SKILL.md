---
name: boltz-runner
description: Boltz 结构预测任务的异步提交指南，覆盖输入路径、配体数组、按需参数查询和结果约定。
metadata:
  when_to_use: >-
    Use when the user wants Boltz structure prediction or sends fasta, fa, pep, yaml, yml, or a compatible input directory for Boltz. Examples: “跑 boltz”, “做结构预测”, “把 fasta 送进 boltz”, “用 boltz 跑 yaml 输入”.
  routing:
    domains: [protein-structure]
    intents: [protein-structure-prediction]
    aliases: [Boltz, 结构预测, 蛋白结构预测, 用 FASTA 预测结构, Boltz YAML]
    positive_examples:
      - 把 FASTA 输入 Boltz 做结构预测
      - 使用 Boltz 预测蛋白配体结构
    negative_examples: [蛋白结构生成, motif scaffolding, 序列设计]
    required_inputs: [fasta_or_yaml]
    conflicts_with: [rfdiffusion-runner, ligandmpnn-runner]
    skill_role: runner
    priority: 80
---

# Boltz Runner

用于实际运行 Boltz；仅解释原理时不要提交任务。执行规则：

1. 不直接调用 `boltz_mcp_server`，只用 `mcp_job-protocol-mcp_submit_job`。
2. `tools_input` 为单个 fasta/fa/pep/yaml/yml 文件时传 ArtifactRef；为输入目录时传 BundleRef。收到 Portable reference 后原样提交，不要调用 `exec` / `glob` 查找本地文件，也不要转换成路径。
3. 提交前核对输入内容和有效默认值是否在本次授权范围内；字段、默认值或约束不确定时调用 `mcp_job-protocol-mcp_describe_target_tool`。不得把省略参数解释为关闭功能。
4. 返回 `LOCAL-*` 后本轮只报告受理信息，不查询状态或结果。

## 最小提交

```json
{
  "input_config": {
    "target_server": "boltz-mcp",
    "target_tool": "boltz_mcp_server",
    "tools_input": {"kind": "artifact", "artifact_id": "art_input_fasta"}
  }
}
```

`project_id/task_id/user_id/workspace_root` 由运行上下文补齐；不要猜造身份字段。

## 配体输入

`ligand_smiles` 必须是 JSON 字符串数组：

```json
{
  "input_config": {
    "target_server": "boltz-mcp",
    "target_tool": "boltz_mcp_server",
    "tools_input": {"kind": "artifact", "artifact_id": "art_protein_fasta"},
    "ligand_smiles": ["CCO", "CCC"],
    "affinity_ligand_index": null
  }
}
```

```json
{"ligand_smiles": "CCO,CCC"}
```

上面的字符串写法不会自动拆分，应拒绝提交。单配体也使用数组。`affinity_ligand_index` 是零基索引；不需要亲和力预测时必须显式设为 `null`，不能省略（旧部署可能默认开启）。只有获准亲和力预测才传具体索引。YAML 输入不会被 `ligand_smiles` 重写，`affinity_ligand_index: null` 也不会删除原 YAML 的 `properties.affinity`；直接提交 YAML 前必须读取该部分，若超出授权则停止澄清，不修改原件。

## 按需查询

只查所需字段：

```json
{"input_config":{"target_server":"boltz-mcp","target_tool":"boltz_mcp_server","detail":"compact","fields":["sampling_steps","diffusion_samples","devices"]}}
```

仅当用户要求完整字段清单时使用 `detail=full`。字段类型和默认值以该工具返回的 `schema_hash` 对应契约为准。

## References

- 部署默认值、固定 MSA 和布尔开关：`references/runtime_defaults.md`
- 完整字段、官方到本地参数映射：`references/official_prediction_mapping.md`
- 输出目录和下游文件选择：`references/output_layout.md`

结果查询应放到后续回合。汇报只给状态、`run_id`、`output_path` 和必要结构文件；不要根据目录名臆造结论，也不要展开整棵目录树。
