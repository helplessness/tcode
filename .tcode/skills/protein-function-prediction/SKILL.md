---
name: protein-function-prediction
description: 🔬 Protein Function Prediction - 预测蛋白质的整体功能和性质。当用户需要预测蛋白质溶解性、亚细胞定位、金属离子结合、热稳定性、信号肽或最适温度时使用此技能。支持 FASTA 序列输入。
routing:
  domains: [protein-sequence]
  intents: [protein-function-prediction]
  aliases: [蛋白功能预测, 溶解性预测, 亚细胞定位, 金属离子结合, 热稳定性, 信号肽, 最适温度]
  positive_examples:
    - 根据 FASTA 预测蛋白溶解性和亚细胞定位
    - 预测蛋白的信号肽和最适温度
  negative_examples: [分子量, 等电点, SASA, 活性位点, 催化残基]
  required_inputs: [fasta]
  conflicts_with: [physical-chemical-properties, functional-residue-prediction]
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

# 🔬 蛋白质功能预测技能

## 功能概述
基于 VenusFactory 的功能预测功能，使用 ESM2-650M 预训练模型和多种适配器进行多任务功能预测。

## 触发条件
当用户提出以下类型的问题时使用：
- 预测蛋白质的溶解性
- 预测亚细胞定位
- 预测金属离子结合能力
- 预测热稳定性
- 预测信号肽
- 预测最适温度
- 上传 FASTA 文件进行功能分析

## 预测任务
| 任务 | 描述 | 输出类型 |
|------|------|----------|
| Solubility | 溶解性预测 | 可溶/不可溶 |
| Subcellular Localization | 亚细胞定位 | 细胞核/细胞质/线粒体等 |
| Metal Ion Binding | 金属离子结合 | 结合/不结合 |
| Thermal Stability | 热稳定性 | 稳定/不稳定 |
| Signal Peptide | 信号肽预测 | 有/无 |
| Optimal Temperature | 最适温度 | 温度值 (°C) |

## 输入格式
- **FASTA 文件**: 蛋白质序列文件 (.fasta, .fa)
- **直接序列**: 氨基酸序列字符串

## 输出格式
- 表格形式的预测结果
- 每个任务的预测值和置信度
- 多任务综合分析

## 工作流程
1. 读取输入序列
2. 使用 ESM2 提取序列嵌入
3. 对每个功能任务使用相应的适配器进行预测
4. 整合所有任务的预测结果
5. 生成格式化报告

## 推荐工具
运行 bundled scripts 中的脚本：
```bash
# 预测所有功能
python3 scripts/function_predict.py --fasta_file input.fasta

# 预测特定任务
python3 scripts/function_predict.py --fasta_file input.fasta --tasks solubility,localization

# 指定输出文件
python3 scripts/function_predict.py --fasta_file input.fasta --output results.csv
```

## 依赖模型
- **ESM2-650M**: 基础序列嵌入模型
- **任务适配器**: 针对各功能任务微调的适配器

## 注意事项
- 序列长度应小于 4096 个氨基酸
- 确保序列只包含标准氨基酸
- 某些任务需要特定适配器文件
- 结果仅供参考，需实验验证

## 相关脚本
- `scripts/function_predict.py` - 主要功能预测脚本

## 依赖库
- torch
- esm
- pandas
- numpy
- biopython
