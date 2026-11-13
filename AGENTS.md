# Project Rules

## Tool Usage
- Use specialized tools over bash: `read` not `cat`, `edit` not `sed`, `write` not `echo >`
- Use relative paths like `src/main.py`, NOT `workdir/` prefix
- Complete tasks efficiently - stop when done
- 对远程论文或网页 PDF 下载：默认加载 `paper-literature-search` skill 做检索；检索统一使用 `paper-search-mcp`，下载统一使用 `paper-download-mcp`，不要直接选择底层文献数据源。只有统一下载 MCP 已穷尽内部 provider，仍返回 HTML/PDF viewer、JS 跳转、403/429、超时或无开放 PDF 后，才允许调用 `task` 并显式设置 `subagent_type="agent-browser"` 委派浏览器子代理作为最后兜底。主代理不得直接加载 `agent-browser` skill，也不得把 `webfetch`、直接 `curl` 或主代理直接调用 `browser` 当成默认下载器。
- 对远程论文或网页 PDF 下载：只有当本地文件已经落地，且运行 `python3 .claude/skills/paper-literature-search/scripts/validate_downloaded_pdf.py <文件路径>` 返回 `ok=true` 时，才允许宣称“PDF 下载成功”。

## Async Domain MCP
计算型 MCP 禁止直调；先加载对应 runner skill，再通过 `mcp_job-protocol-mcp_submit_job` 提交：

```json
{"input_config":{"target_server":"<server>","target_tool":"<tool>","...":"target args"}}
```

映射：
- `boltz-mcp` → `boltz_mcp_server` → `boltz-runner`
- `gnina-mcp` → `gnina_mcp_server` → `gnina-runner`
- `ligandmpnn-mcp` → `ligandmpnn_run_mcp_server` → `ligandmpnn-runner`
- `placer-mcp` → `placer_mcp_server` → `placer-runner`
- `plip-mcp` → `plip_mcp_server` → `plip-runner`
- `rfdiffusion-mcp` → `rfdiffusion_mcp_server` → `rfdiffusion-runner`
- `evolvepro-mcp` → `evolvepro_single_round_mcp_server` / `evolvepro_multi_round_mcp_server` → `evolvepro-runner`
- `mutation-scoring-mcp` → `multi_model_sequence_mutation_scoring_mcp_server` → `multi-model-sequence-mutation-scoring`
- `simulation-mcp` → `simulation_run_mcp_server` → `simulation-runner`
- `simple-demo-mcp` → `echo_text` / `sleep_then_echo`
- `homology-mcp` → `homology_search_mcp_server` / `homology_align_mcp_server` / `homology_conservation_mcp_server` → `homology-runner`
- `pythia-mcp` → `pythia_mcp_server` → `pythia-runner`
- `foldx-mcp` → `foldx_repair_mcp_server` / `foldx_scan_mcp_server` → `foldx-runner`

参数不确定时调用 `mcp_job-protocol-mcp_describe_target_tool`；默认用 `detail=compact` 或指定 `fields`，只有确需完整 Schema 时才用 `detail=full`。目标参数与 `target_server/target_tool` 放在同一个 `input_config`，不要额外嵌套。每个子任务提交一次；返回 `LOCAL-*` 后本轮不调用状态/结果工具。无专用 skill 时加载 `protein-job-protocol`。

## Workflow
1. Understand the request
2. Use tools to complete it directly
3. Verify results work
4. Stop - no unnecessary steps
