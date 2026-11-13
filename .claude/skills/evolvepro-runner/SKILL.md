---
name: evolvepro-runner
description: 准备并运行 EvolvePro 实验进化任务。用户要求运行 EvolvePro、处理 Round1 或后续轮次、上传 WT FASTA 与实验活性 CSV/Excel、修正 Variant/activity 格式、进行单突变或多突变主动学习预测时使用。负责安全规范化实验数据，并严格通过 job-protocol-mcp 异步提交到 evolvepro-mcp。
metadata:
  when_to_use: >-
    Use when the user wants EVOLVEpro directed evolution, asks to repair Round1 or later-round measurement files, or provides WT FASTA plus CSV/Excel activity data for single- or multi-mutant prediction.
  routing:
    domains: [protein-mutation, protein-sequence]
    intents: [directed-evolution, evolvepro-prediction, experimental-round-normalization]
    aliases: [EvolvePro, EVOLVEpro, 定向进化, 实验进化, Round1, 单突变进化, 多突变进化]
    positive_examples:
      - 帮我检查 Round1 Excel 并运行 EvolvePro
      - 根据 WT 序列和实验活性预测下一轮突变
      - 把这些多突变实验结果提交给 EvolvePro
    negative_examples: [只做零样本突变打分, 仅计算理化性质, 结构预测]
    required_inputs: [wt_sequence_or_fasta, round_measurements]
    conflicts_with: [multi-model-sequence-mutation-scoring, protein-mutation-prediction]
    skill_role: runner
    priority: 85
---

# EvolvePro Runner

用于准备和提交 EvolvePro 长任务；只解释原理时不要提交任务。

## 核心规则

1. 不直接调用 `evolvepro_single_round_mcp_server` 或 `evolvepro_multi_round_mcp_server`。
2. 实际运行时只调用 `mcp_job-protocol-mcp_submit_job`。
3. 已经符合 EvolvePro 输入约定的实验文件直接以 ArtifactRef 提交；不要为了预检把 Artifact 下载到 Agent Workspace。
4. 字段、默认值或约束不确定时，调用 `mcp_job-protocol-mcp_describe_target_tool`。
5. 返回 `LOCAL-*` 后，本轮只报告受理信息，不查询状态或结果。
6. `.xlsx`、`.xls`、`.csv`、FASTA 和预计算 embedding 等文件输入必须使用 ArtifactRef；文件数组使用 ArtifactRef 数组。
7. 收到 Portable reference 后禁止使用 `exec`、`glob`、`find` 或 `read` 在本地查找文件，也不要安装依赖或把二进制附件内容塞进消息。
8. 不要把 ArtifactRef 转成 `/workspace/...`、宿主机路径或相对路径；目标 EvolvePro MCP 会在服务器任务目录中物化文件。

## 准备实验数据

需要详细输入约定时读取 `references/input-formats.md`。

目标文件必须包含可明确识别的 `Variant` 和 `activity` 列。当前 Agent 到 MCP 的公共边界只接受 Portable reference，因此默认不在 Agent 本地执行 `normalize_round_data.py`。如果输入需要 CSV→XLSX、列名修正或突变格式归一化，应先通过独立预处理流程生成并上传新文件，再把新 ArtifactRef 交给本 skill；不要把旧 ArtifactRef 当作本地路径处理。

允许自动执行的确定性修正：

- 将明确的列别名规范为 `Variant` 和 `activity`。
- 将 CSV/TSV 转为 XLSX，并清理全空行和格式空白。
- 根据 WT 校验突变位置和原始氨基酸。
- 单突变将 `G25A` 规范为官方轮次格式 `25A`。
- 多突变规范为 `G25A_V40L`。

预处理工具仍可供开发者离线使用，但它的输入输出不属于 MCP 公共参数。只有预处理产物已经上传并获得新的 ArtifactRef 后才能提交；旧版 `.xls` 无法解析时，优先让用户转换为 `.xlsx` 后重新上传。

不得猜测实验值、活性方向、WT 序列、错误位置或有歧义的列映射。

## 单突变提交

固定路由：

- `target_server: "evolvepro-mcp"`
- `target_tool: "evolvepro_single_round_mcp_server"`

```json
{
  "input_config": {
    "target_server": "evolvepro-mcp",
    "target_tool": "evolvepro_single_round_mcp_server",
    "protein_name": "K4_pol",
    "round_name": "Round1",
    "wt_fasta_path": {"kind": "artifact", "artifact_id": "art_wt_fasta"},
    "excel_files": [{"kind": "artifact", "artifact_id": "art_round1_xlsx"}],
    "number_of_variants": 12
  }
}
```

`sequence` 与 `wt_fasta_path` 至少提供一个。所有 Portable reference 直接提交；不要做 Workspace 或宿主机路径预检。

## 多突变提交

固定路由：

- `target_server: "evolvepro-mcp"`
- `target_tool: "evolvepro_multi_round_mcp_server"`

```json
{
  "input_config": {
    "target_server": "evolvepro-mcp",
    "target_tool": "evolvepro_multi_round_mcp_server",
    "protein_name": "K4_pol",
    "round_name": "Round3",
    "wt_fasta_path": {"kind": "artifact", "artifact_id": "art_wt_fasta"},
    "excel_files": [
      {"kind": "artifact", "artifact_id": "art_round1_xlsx"},
      {"kind": "artifact", "artifact_id": "art_round2_xlsx"}
    ],
    "excel_file_multi": [{"kind": "artifact", "artifact_id": "art_round2_multi_xlsx"}],
    "mult_file": {"kind": "artifact", "artifact_id": "art_round2_xlsx"},
    "variant_get_list": [2],
    "number_of_variants": 12
  }
}
```

- 历史单突变轮次放入 `excel_files`。
- 历史多突变轮次放入 `excel_file_multi`。
- 需要从高活性单突变生成组合时，传入 `mult_file` 和 `variant_get_list`。
- 用户已有 embedding 时使用 `precomputed_embedding_csv` / `precomputed_embedding_csvs`。

## 按需查询参数

只查询当前需要的字段：

```json
{
  "input_config": {
    "target_server": "evolvepro-mcp",
    "target_tool": "evolvepro_single_round_mcp_server",
    "detail": "compact",
    "fields": ["excel_files", "sequence", "wt_fasta_path", "model_location"]
  }
}
```

只有用户要求完整参数列表时才使用 `detail=full`。

## 结果汇报

首轮提交只报告目标服务、目标工具和真实受理标识。后续用户明确要求查进度或结果时，再按 `protein-job-protocol` 使用状态与结果工具。

完成后优先报告 `run_id`、`output_path`、`manifest_path` 和 `top_variants`。`top_variants` 是模型预测候选，不是已测实验活性。
