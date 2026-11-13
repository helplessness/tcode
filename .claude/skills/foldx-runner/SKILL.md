---
name: foldx-runner
description: 使用 FoldX 5 对已有蛋白结构做 RepairPDB 或 PositionScan；按需选择独立操作，经本机 Job Protocol 异步提交，不自动修复或串成 Workflow。
routing:
  domains: [protein-research]
  intents: [protein-stability-scoring]
  aliases: [FoldX, FoldX位点扫描, FoldX结构修复]
  skill_role: runner
  priority: 80
---

# FoldX 稳定性分析

先确认用户授权和输入文件。RepairPDB 与 PositionScan 是独立计算，不自动先修复再扫描，不修改原始输入。已有修复或评分结果优先复用。

查询合同采用 JSON 对象，不使用顶层 arguments 字符串：

```json
{"input":{"target_server":"foldx-mcp","target_tool":"foldx_scan_mcp_server","detail":"compact"}}
```

通过现有 mcp_job-protocol-mcp_submit_job 提交，禁止计算直调。扫描示例：

```json
{"input_config":{"target_server":"foldx-mcp","target_tool":"foldx_scan_mcp_server","pdb":"<真实可移植文件引用>","positions":["LA43V"]}}
```

如暴露的是简化 Schema，按真实 Schema 传字段，不将整个对象编码为字符串。身份和工作区由宿主绑定，文件复用已有 ArtifactRef 等可移植引用和按需上传，不把客户端宿主路径当成服务器路径。

positions 使用 PDB 的原始氨基酸、链、残基编号和目标氨基酸，例如 LA43V 表示 A 链43位 L→V，末尾小写 a 表示20种标准氨基酸扫描。不是 FASTA 序号；原残基不一致、插入码或链有歧义时停止并澄清，不能猜测位点或自动重编号。最多500项，以实时合同为准。

仅当用户明确要求结构修复时选择 foldx_repair_mcp_server，领域参数仅 pdb；输出为派生产物，不覆盖原始结构。输入校验不是科学质量保证，未修复结构的扫描应披露限制。

每个授权子任务只提交一次。返回 LOCAL-* 或任务编号后本轮不轮询，等待既有异步恢复。状态不明确核查原任务，不重提；缺程序或所需rotabase就报告，不改远端、不暗中换工具。版本优先从实际执行日志识别，部署声明仅为补充；未配置版本不阻止计算，无法确认时披露“版本未确认”，不编造版本。当前评分归一化适配5系列；实际日志明确报告其他主版本，或无日志版本且回退声明明确指向其他主版本时，保留原始产物、停止评分归一化并请求核对，不猜测评分方向，也不把声明当成已证实的不兼容。日志确认5系列时不因旧声明拦截，仅披露冲突。未知或仅声明版本的成功不等于兼容性已验收。

回传后通过产物查询、引用解析及授权读取核对原始输出、scores.json、位点映射和版本/单位/分数方向；RepairPDB 则核对派生结构与运行清单。不能仅凭受理或目录存在宣称成功，不跳过缺失/失败项，也不把预测能量当作实验验证。

大产物包可能只回传清单，并非全部文件已下载。使用现有 `artifact_query` 按任务编号 list（跟随 next_offset），结合 size 和实际文件清单选择 resolve；只下载选中的文件，不需重新计算。评分比较通常需要完整 scores.json 和 provenance.json；定位归一化错误需原始 scanning_output、运行日志及运行清单；查看具体突变构象才需要对应 PDB，不必为评分比较下载全部突变结构。这是用途约定，不是文件存在或结果有效的证明；必要文件缺失、用途未知或超过下载/分析阈值时说明缺口，不将抽样冒充完整分析，也不自动重跑。清单中的远端引用须保留以供后续取用。

对大评分表，按需发现蛋白研究插件，用已检查输入和规范 scores.json 做程序全量分析，只把统计、少量候选和产物引用返回模型；不要逐页 read 全表。原始 FoldX 格式或 PDB 编号与目标序列编号不一致时，必须先确认支持的归一化和位点映射，不能猜测转换。插件不可用、缺输入或超限时报告，不把抽样/文本命中当作全量分析，也不重新提交计算。
