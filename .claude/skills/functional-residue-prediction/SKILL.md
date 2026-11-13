---
name: functional-residue-prediction
description: 🎯 Functional Residue Prediction - 识别蛋白质序列中的关键功能残基。当用户需要预测活性位点、结合位点、催化残基、DNA/RNA 结合位点或保守残基时使用此技能。支持 FASTA 序列输入。
routing:
  domains: [protein-sequence]
  intents: [functional-residue-prediction]
  aliases: [功能残基预测, 活性位点, 结合位点, 催化残基, 保守残基, DNA 结合位点, RNA 结合位点]
  positive_examples:
    - 预测蛋白序列中的催化残基和活性位点
    - 定位 DNA 或 RNA 结合残基
  negative_examples: [整体功能预测, 亚细胞定位, 分子量, 等电点]
  required_inputs: [fasta]
  conflicts_with: [protein-function-prediction, physical-chemical-properties]
  priority: 70
compatibility:
  python: "python3"
  dependencies:
    - torch
    - pandas
    - numpy
    - biopython
    - fair-esm
  environment: "已配置：已安装项目依赖的 Python 环境"
---

# 🎯 功能残基预测技能

## 功能概述
基于 VenusFactory 的残基级功能预测功能，使用 ESM2-650M 模型对每个氨基酸残基进行功能注释。

## 触发条件
当用户提出以下类型的问题时使用：
- 识别蛋白质的活性位点
- 预测底物结合残基
- 定位催化残基
- 预测 DNA/RNA 结合位点
- 识别蛋白质相互作用界面
- 查找功能保守残基
- 上传 FASTA 文件进行残基分析

## 预测任务
| 任务 | 描述 | 输出 |
|------|------|------|
| Catalytic Sites | 催化位点预测 | 功能/非功能残基 |
| Binding Sites | 结合位点预测 | 功能/非功能残基 |
| Protein-Protein Interface | 蛋白相互作用界面 | 界面/非界面残基 |
| DNA/RNA Binding | 核酸结合位点 | 结合/非结合残基 |
| Conserved Residues | 保守残基预测 | 保守/非保守残基 |

## 输入格式
- **FASTA 文件**: 蛋白质序列文件 (.fasta, .fa)
- **直接序列**: 氨基酸序列字符串

## 输出格式
- 每个任务的详细残基列表
- 功能残基位置（1-based）
- 功能残基的氨基酸类型
- 预测置信度
- 功能残基比例统计

## 工作流程
1. 读取输入序列
2. 使用 ESM2 提取每个残基的嵌入
3. 对每个功能任务使用相应适配器进行预测
4. 逐残基分类（功能/非功能）
5. 生成详细的功能残基报告

## 推荐工具
运行 bundled scripts 中的脚本：
```bash
# 预测所有功能残基
python3 scripts/residue_predict.py --fasta_file input.fasta

# 预测特定任务
python3 scripts/residue_predict.py --fasta_file input.fasta --tasks catalytic,binding

# 指定输出文件
python3 scripts/residue_predict.py --fasta_file input.fasta --output results.csv
```

## 输出示例
```
## Catalytic Sites
- Functional residues: 8 (6.2%)
- Non-functional residues: 121 (93.8%)

D12, K45, S78, H89, E102, Y156, R189, C201
```

## 注意事项
- 序列长度应小于 4096 个氨基酸
- 结果位置使用 1-based 索引
- 功能残基比例因任务而异
- 结果仅供参考，需实验验证

## 相关脚本
- `scripts/residue_predict.py` - 主要残基预测脚本
- `scripts/batch_residue_analysis.py` - 批量残基分析脚本

## 依赖库
- torch
- esm
- pandas
- numpy
- biopython
- json
