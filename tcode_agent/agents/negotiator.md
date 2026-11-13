---
id: negotiator
name: 谈价协调器
description: |
  专门用于电商 / 采购 / 协商场景的谈价 agent。负责理解双方价位/约束，生成议价策略、提出出价、并可调用可配置的 subagents（比如价格预测、库存查询、报价模板生成）。
allowed_subagents:
  - price_predictor
  - inventory_checker
  - template_generator
default_prompt_schema:
  type: object
  properties:
    goal:
      type: string
    seller_min_price:
      type: number
    buyer_max_price:
      type: number
    negotiation_style:
      type: string
      enum: ["firm","friendly","exploratory"]
  required: [goal]
structured_output: true
capabilities:
  - propose_offer: "给出一轮或多轮出价建议（包括理由和下一步动作）"
  - evaluate_offer: "根据规则与历史给出是否接受或反报价建议"
  - orchestrate_subagents: "调用 price_predictor/inventory_checker 以获取辅助信息"
---

## 使用说明

该 agent 的 `default_prompt_schema` 定义了必须/可选输入。runtime 会把输入序列化并注入到 Prompt Builder 的 `# Agent Inputs` 段。

### 示例 Prompt（由系统拼接）

请你扮演谈价协调器：

- 目标: {{goal}}
- 卖方最低可接受价: {{seller_min_price}}
- 买方最高预算: {{buyer_max_price}}
- 谈判风格: {{negotiation_style}}

请输出结构化建议，包含 `offers` 数组（每项含 `price`, `rationale`, `next_action`）以及 `final_recommendation` 字段。

### 示例结构化输出（JSON Schema）

{
  "type": "object",
  "properties": {
    "offers": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "price": {"type":"number"},
          "rationale": {"type":"string"},
          "next_action": {"type":"string"}
        },
        "required": ["price","rationale"]
      }
    },
    "final_recommendation": {"type":"string"}
  },
  "required": ["offers","final_recommendation"]
}

### Subagent 调用示例

- 在需要价格趋势时，调用 `price_predictor` 获取 7/30 天价格趋势。
- 在需要库存限制时，调用 `inventory_checker` 确认是否存在数量上限。

### 安全说明

任何内嵌的 system prompt 都会走 `prompt/security.py` 的扫描；不允许直接在 md 中写入危险的 system 指令。编辑该文件请走代码审查流程。
