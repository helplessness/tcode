---
name: physical-chemical-properties
description: 计算蛋白质的物理化学性质。当用户需要计算分子量、等电点、不稳定性指数、疏水性、二级结构比例、SASA、RSA 等理化参数时使用此技能。支持 FASTA 序列和 PDB 结构文件输入。
routing:
  domains: [protein-sequence]
  intents: [protein-physicochemical-properties]
  aliases: [蛋白理化性质, 分子量, 等电点, pI, 不稳定性指数, GRAVY, SASA, RSA]
  positive_examples:
    - 计算蛋白的分子量、等电点和不稳定性指数
    - 根据 PDB 计算 SASA 和 RSA
  negative_examples: [亚细胞定位, 信号肽, 活性位点, 催化残基]
  required_inputs: [fasta_or_pdb]
  conflicts_with: [protein-function-prediction, functional-residue-prediction]
  priority: 70
compatibility:
  python: "python3"
  dependencies:
    - biopython>=1.79
    - numpy>=1.20.0
  environment: "已配置：已安装项目依赖的 Python 环境"
---

# ⚗️ 蛋白质理化性质计算技能

## 功能概述
基于 VenusFactory 的理化性质计算功能，使用 BioPython 和结构分析工具进行全面的蛋白质特性分析。

## 触发条件
当用户提出以下类型的问题时使用：
- 计算蛋白质分子量
- 预测等电点 (pI)
- 计算不稳定性指数
- 分析疏水性 (GRAVY)
- 预测二级结构比例
- 计算溶剂可及表面积 (SASA)
- 计算相对溶剂可及性 (RSA)
- 上传 FASTA/PDB 文件进行性质分析

## 计算参数
### 基础理化性质
| 参数 | 描述 | 单位 |
|------|------|------|
| Molecular Weight | 分子量 | kDa |
| Theoretical pI | 理论等电点 | - |
| Aromaticity | 芳香性 | - |
| Instability Index | 不稳定性指数 | - |
| GRAVY | 疏水性平均值 | - |

### 二级结构
| 参数 | 描述 |
|------|------|
| Alpha Helix | α螺旋比例 |
| Beta Turn | β转角比例 |
| Beta Sheet | β折叠比例 |

### 结构性质 (需要 PDB 文件)
| 参数 | 描述 | 单位 |
|------|------|------|
| Total SASA | 总溶剂可及表面积 | Å² |
| Exposed Residues | 暴露残基数 | - |
| Buried Residues | 埋藏残基数 | - |

## 输入格式
- **FASTA 文件**: 蛋白质序列文件 (.fasta, .fa)
- **PDB 文件**: 蛋白质结构文件 (.pdb)
- **直接序列**: 氨基酸序列字符串

## 输出格式
- 基础理化性质报告
- 二级结构预测结果
- SASA/RSA 分析（PDB 输入）
- 氨基酸组成分析
- 格式化总结报告

## 工作流程
1. 检测输入类型（FASTA 序列或 PDB 结构）
2. 从 PDB 提取序列或直接使用 FASTA
3. 使用 BioPython 计算基础理化性质
4. 预测二级结构比例
5. （可选）从 PDB 计算 SASA/RSA
6. 生成综合报告

## 推荐工具
运行 bundled scripts 中的脚本：
```bash
# 从 FASTA 文件计算性质
python3 scripts/property_calculator.py --fasta_file input.fasta

# 从 PDB 文件计算（包含 SASA/RSA）
python3 scripts/property_calculator.py --pdb_file structure.pdb

# 指定输出文件
python3 scripts/property_calculator.py --fasta_file input.fasta --output results.json
```

## 稳定性判断
- **Instability Index < 40**: 预测为稳定蛋白 ✅
- **Instability Index > 40**: 预测为不稳定蛋白 ⚠️

## 注意事项
- PDB 文件需要包含完整的原子坐标
- SASA/RSA 计算仅适用于 PDB 输入
- 某些参数使用经验公式估算
- 结果仅供参考，需实验验证

## 相关脚本
- `scripts/property_calculator.py` - 主要性质计算脚本

## 依赖库
- biopython
- Bio.PDB
- numpy
- json
