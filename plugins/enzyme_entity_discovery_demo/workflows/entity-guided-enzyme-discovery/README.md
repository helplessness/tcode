# 实体引导的未知反应酶发现 Demo

这个 Workflow 在独立插件中执行四个阶段：反应文本分析、机制与能力假说、候选基线冻结及实体扩展、有界通路组装与事实边界批判。路线搜索最多四步，并分别检查物料、方向、辅因子、能量、副产物、条件和中间体安全性。

运行产物位于当前 Workspace 的 `.tcode/plugins/enzyme_entity_discovery_demo/runs/<run_id>/`。本体事实包保持只读，Agent 提出的机制和候选只进入该运行目录的假说账本。
