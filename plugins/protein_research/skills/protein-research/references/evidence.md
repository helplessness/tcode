# 证据整理接口

这是可选数据接口，不约束研究顺序或方法选择。两入口均复用宿主逐次读取授权与不可覆盖产物发布。

## 输入与结构上下文

```json
{"fasta_path":"upload/target.fasta","pdb_path":"structures/complex.pdb","chain":"A","protected_positions":[15],"ligands":[{"chain":"B","residue_number":1,"name":"LIG"}],"pocket_radius":6}
```

只有FASTA时仅传fasta_path。返回snapshot（path、sha256），有PDB时检查所选完整观测链并保留原编号和插入码。ligands是明确的HETATM残基标识，可加insertion_code；半径单位Å，只影响距离标注。距离不是结合相互作用或催化位点的证明。多个复合物可各自产生snapshot，不强求链名/PDB编号相同，但目标序列须相同。

有口袋时，snapshot是小型输入索引，关联完整输入快照和每页最多100项的口袋清单；仍原样作为input_snapshot传给分析入口，无须自行展开大文件。恢复会重新核对原始来源和完整映射/距离。若只有口袋清单页缺失，分析可继续，摘要和分析产物以recovery_warnings列出缺页，不能宣称交付物全部完整；读取拒绝、指纹或归属不符不降级为缺页警告。

## 单份评分兼容接口

```json
{"input_snapshot":{"path":"<输入记录>","sha256":"<指纹>"},"results_path":"results/scores.json","file_format":"json"}
```

JSON为metadata与rows；CSV至少mutation、score，metadata另传。元数据需method/version/metric/units/direction/target_sequence_sha256。未知版本可明确写unknown。mutation使用目标序列1起始编号；可附chain、pdb_residue_number、insertion_code，插件将核对结构映射。分数阈值可省略，不自动推断。

## 多来源联合表

```json
{
  "input_snapshot":{"path":"<输入记录>","sha256":"<指纹>"},
  "score_sources":[
    {"label":"Pythia-apo","path":"results/pythia/scores.json","file_format":"json"},
    {"label":"FoldX-complex","path":"results/foldx/scores.json","file_format":"json"}
  ],
  "conservation_path":"results/homology/conservation.json",
  "context_snapshots":[{"path":"<复合物输入记录>","sha256":"<指纹>"}]
}
```

每份来源可单独指定threshold，按其direction筛选；标签应唯一。不同结构的链号或PDB编号不同时，给该评分来源指定其自己的input_snapshot，程序核对同一目标序列并按该结构映射检查，保护位点仍沿用主研究输入。score_sources和旧results_path/file_format互斥。可选条件：

- required_sources：必须有有效评分的来源标签；未提供时不默认取双工具交集。
- conservation_max：当前同源MCP unweighted_shannon的information_bits上限。
- min_coverage：同源比对非缺口比例下限（0–1）。
- pocket_only：只保留明确配体距离半径内的位点，多结构取并集；默认false。

无条件时只是联结与有效性检查，不替Agent选择科学阈值。失败、缺失、重复、自突变和保护位点均保留理由；缺少一来源不是另一来源的零分。联合表按突变名排列，不是综合排名，各方法指标不相加。

返回全量处理计数、各来源计数/指标、最多20项证据预览和snapshot。联合counts.total是突变并集数，analyzed_rows是各评分输入行数之和。大表保存在row_pages；每条记录保留来源原行、缺失来源、保守性和距离证据，不需要将全表灌入上下文。Agent可据此写TSV/报告并引用分析根记录；使用宿主既有文件写入授权，不增加插件文件写权限。
