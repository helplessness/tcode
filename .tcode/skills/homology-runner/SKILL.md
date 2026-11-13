---
name: homology-runner
description: 同源序列搜索、多序列比对和保守性统计的调用指南。按研究需要选择独立操作，经本机 Job Protocol 提交远端同源 MCP，不自动串成 Workflow。
routing:
  domains: [protein-research]
  intents: [homology-analysis, sequence-conservation]
  aliases: [同源搜索, 多序列比对, 保守性统计, homology, conservation]
  skill_role: runner
  priority: 80
---

# 同源分析

三个操作独立选择，不要求依次执行。有已有结果时不重新计算。

| 操作 | target_tool | 必要领域参数 |
| --- | --- | --- |
| BLASTp 搜索 | homology_search_mcp_server | fasta：单条目标序列文件引用 |
| Clustal Omega 比对 | homology_align_mcp_server | fasta：包含至少两条序列的文件引用 |
| 保守性统计 | homology_conservation_mcp_server | alignment：对齐 FASTA 引用；query_id：精确目标 ID |

统一 target_server 为 homology-mcp。先获得当前任务计算授权，再通过已有 submit_job 提交，禁止直调计算 MCP。

## 查询合同

调用 mcp_job-protocol-mcp_describe_target_tool，参数必须是 JSON 对象，不要把整个对象编码成字符串，不要使用顶层 arguments 包装查询：

```json
{"input":{"target_server":"homology-mcp","target_tool":"homology_conservation_mcp_server","detail":"compact"}}
```

可在同一个 input 对象内增加 arguments 对象，仅用于校验拟提交的领域参数。需要完整 Schema 时才使用 detail=full。

## 提交

使用 mcp_job-protocol-mcp_submit_job。领域参数直接放在 input_config 内，身份和工作区由宿主绑定，不猜造：

```json
{"input_config":{"target_server":"homology-mcp","target_tool":"homology_conservation_mcp_server","alignment":"<真实文件引用>","query_id":"<精确目标ID>"}}
```

文件沿用宿主已有 ArtifactRef 等可移植引用和按需上传机制；不要把客户端宿主路径当作远端路径，不自行上传到其他网站。每个授权子任务只提交一次。返回 LOCAL-* 或任务编号后本轮不轮询、不重复提交，等待既有异步回传。状态不明确先查询原任务，不能重跑。

## 输入与结论边界

- 统计要求输入确实是按同源位点对齐的序列，目标 ID 存在、至少两条、对齐长度一致。已有对齐不需要再跑比对；无缺口也可以是有效对齐。但仅凭长度相同不能认定真实蛋白已对齐，不确定时询问。
- 不因没有 Clustal Omega 的运行记录就拒绝用户提供的已有对齐；不自动转换或改写原文件。
- BLAST 数据库和程序由部署者配置，缺失时报告，不替换数据库、不安装远端程序。
- 结果到达后按原任务查询产物，并经读取授权查看 conservation.json；清单未登记不等于零文件，失败不当作成功。
- 搜索结果的 hit_count 是原始命中数，不是可用序列数。先核对 usable_count、excluded_count 和 warnings；需要排除明细时读取 sequence_filter.json。含非标准字符的同源记录只有在全部 ID、数量和完整长度校验通过后才会整条排除，原 homologs.fasta 保留，不能直接作为已筛选的比对输入。
- 下游使用 alignment_input.fasta，并用 provenance.json 的 alignment_bindings 核对派生 ID 与原命中的对应；披露排除规则和样本量，不将“有可用序列”当成证据充分。全部排除或身份/长度校验失败时停止相应分析。已有失败输出可独立重新分析，但不覆盖原失败状态、不重新搜索；只在产物经现有授权登记和读取后继续引用。
- 统计是未加权 Shannon 熵，coverage 为非缺口比例，information_bits=log2(20)-entropy_bits；不是 WebLogo 的小样本校正结果，不能据此保证突变安全或功能保留。
