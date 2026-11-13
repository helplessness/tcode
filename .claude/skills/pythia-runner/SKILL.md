---
name: pythia-runner
description: 使用 Pythia 对已有单链蛋白结构做突变评分；按需查询合同，通过本机 Job Protocol 异步提交，不直接调用远端计算。
routing:
  domains: [protein-research]
  intents: [protein-mutation-scoring]
  aliases: [Pythia, Pythia稳定性评分]
  skill_role: runner
  priority: 80
---

# Pythia 突变评分

只提供调用指导，不是自动 Workflow。已有评分先复用，不自行重算。

1. 确认用户已授权本次计算。输入是已有 PDB 的可移植引用：仅标准氨基酸、完整主链、单链且不超过500个残基。不自动选链、截断、修复或改写原文件；输入不满足则询问。
2. 查询现有合同，使用对象参数而非整个 JSON 字符串：

```json
{"input":{"target_server":"pythia-mcp","target_tool":"pythia_mcp_server","detail":"compact"}}
```

3. 加载现有 submit_job 工具，通过客户端 Job Protocol 提交，禁止计算直调。完整对象示例：

```json
{"input_config":{"target_server":"pythia-mcp","target_tool":"pythia_mcp_server","pdb":"<真实可移植文件引用>","device":"cpu"}}
```

如工具暴露的是简化参数 Schema，按其真实 Schema 传 target_server、target_tool 和领域字段，不臆造外层 arguments 字符串。身份、工作区由宿主绑定。工作区文件交给宿主已有按需上传机制，不把宿主路径当成远端路径。

device 只接受 cpu 或 cuda:0；默认 cpu，不猜测 GPU 可用。每个授权子任务只提交一次；收到 LOCAL-* 或任务编号后本轮不轮询，等待既有异步回传。提交状态不明确先核查原任务，不重提。程序或权重缺失时报告，不安装或修改远端服务，不切换其他计算。版本优先从实际执行日志识别，部署声明仅为补充；未配置版本不阻止计算，无法确认时披露“版本未确认”，不编造版本或承诺兼容性。

结果通过现有产物查询、引用解析与授权读取核对 scores.json、provenance.json 和原始评分；保留失败产物，不把受理当成功。Pythia 第一版为完整全位点扫描，不能承诺只计算一个位点。分数是 masked log probability ratio，无量纲，不是实验 Tm 或 kcal/mol；序号对应结构观察到的序列，解释前核对 PDB 映射及目标 FASTA，不能保证突变安全或活性保留。

大包可能只有清单已回传，文件内容尚在远端。用现有 `artifact_query` 的 list 查看实际文件和大小并跟随 next_offset，再 resolve 当前分析所需的单个引用。评分比较需完整 scores.json 与 provenance.json；排错时按需取原始输出和日志；仅核对评分时不必下载所有结构或中间文件。上述约定不保证本次文件存在；用途不明、必要文件缺失或超限时报告缺口，不默默跳过、不重算。保留未下载产物的引用。

大评分表不直接全文 read 或逐页送入模型。先查产物清单，按需发现蛋白研究插件的 `protein_inputs_check` / `protein_results_analyze`，通过已核对的 FASTA/PDB 检查记录对规范 scores.json 全量分析，只接收统计、少量候选和完整产物引用。插件不可用、输入不齐或文件超限时说明缺口，不将样本、清单行数或文本检索结果冒充全量验证；也不为此重新计算。
