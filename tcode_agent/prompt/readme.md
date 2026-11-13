固定系统提示词正文位于 `templates/*.md`，由 `templates.py` 使用包资源加载。
模型选择与动态环境仍由 `agent/agent.py` 和 `harness/prompt_builder.py` 组装。
修改方式、打包规则和验收边界见 [系统提示词Markdown迁移.md](../../docs/上下文/系统提示词Markdown迁移.md)。

```mermaid
flowchart TD
    A["Agent.run / Agent.run_stream"] --> B["Agent._call_llm()"]
    B --> C["AgentPromptBuilder.call_llm()"]

    C --> D["读取基础 System Prompt<br/>Agent._get_system_prompt()"]
    D --> E["追加环境信息<br/>Agent._get_environment_prompt()"]
    E --> F["追加 Agent 自定义 Prompt<br/>agent_info.prompt"]

    F --> G{"direct_execution_mode?"}
    G -- "yes" --> H["追加 Direct Execution Mode 规则"]
    G -- "no" --> I["跳过"]
    H --> J["ProjectPromptAssembler"]
    I --> J["ProjectPromptAssembler"]

    J --> K["build_project_rules_section()"]
    K --> L["RuleManager.load() / get_rules()"]

    L --> M["扫描规则文本<br/>scan_prompt_text()"]
    M --> N{"命中危险模式?"}
    N -- "yes" --> O["阻断注入<br/>sanitized_content = 空<br/>记录 diagnostics"]
    N -- "no" --> P["保留安全规则文本"]
    O --> Q["返回安全规则集合"]
    P --> Q["返回安全规则集合"]

    J --> R["build_memory_sections(goal)"]
    R --> S["MemoryProviderManager.from_agent()"]

    S --> T["GlobalMemoryProvider"]
    S --> U{"Agent 是 Manager?"}
    U -- "yes" --> V["TeamMemoryProvider"]
    U -- "no" --> W["跳过 Team Memory"]

    S --> X{"存在 shared_memory_key?"}
    X -- "yes" --> Y["SharedWorkerMemoryProvider"]
    X -- "no" --> Z["跳过 Shared Worker Memory"]

    T --> AA["生成 Persistent Memory 段"]
    V --> AB["生成 Team Long-term Memory 段"]
    Y --> AC["生成 Shared Worker Memory 段"]

    Q --> AD["拼入 # Project Rules"]
    AA --> AE["拼入 # Persistent Memory"]
    AB --> AF["拼入 # Team Long-term Memory"]
    AC --> AG["拼入 # Shared Worker Memory"]

    AD --> AH{"是否要求 JSON Schema 输出?"}
    AE --> AH
    AF --> AH
    AG --> AH

    AH -- "yes" --> AI["追加 Structured Output Requirement"]
    AH -- "no" --> AJ["跳过"]

    AI --> AK["组装 messages_for_api"]
    AJ --> AK["组装 messages_for_api"]

    AK --> AL["注入 system 消息"]
    AL --> AM["追加历史 user / assistant / tool 消息"]
    AM --> AN["可选：dump prompt 到 .tcode/debug/prompts/<session_id>/"]

    AN --> AO["provider.chat(messages, tools, temperature, reasoning_effort)"]
    AO --> AP["解析 response.content / tool_calls / usage"]
    AP --> AQ["返回给 Agent 主循环"]

```



| 维度 | 原先的 Prompt 组装 | 现在的 Prompt 组装 |
|---|---|---|
| 组装入口 | 主要分散在 `Agent` 本体里，边判断边拼接 | 收口到 `harness/prompt_builder.py`，`Agent` 只做委托 |
| 核心职责划分 | `Agent` 同时管 prompt、工具、执行流程、部分运行时规则 | Prompt 组装、工具执行、主循环开始分层 |
| 项目规则注入 | 读取规则文件后基本直接使用 | 先经过 `prompt/security.py` 扫描，再由 `RuleManager` 注入 |
| 规则文件安全性 | 默认信任本地规则文本 | 命中注入/覆盖/欺骗模式会被 block，不进入 prompt |
| Rule 文件来源 | 规则来源存在，但管理较松散 | 统一由 `RuleManager` 处理 `AGENTS.md / CLAUDE.md / .cursor/rules.md` |
| Memory 注入方式 | 更偏零散、调用处驱动 | 统一走 `MemoryProviderManager`，按 provider 注入 |
| 支持的 Memory 类型 | 主要是“有 memory 就加” | 明确区分全局 memory、team memory、shared worker memory |
| 运行时模式注入 | 多数靠 Agent 内部条件分支 | 在 Prompt Builder 里有正式注入位，例如 `direct_execution_mode` |
| JSON Schema 输出要求 | 可能散在调用点附近 | 统一在 Prompt Builder 里追加 `Structured Output Requirement` |
| 历史消息格式化 | 更贴近 Agent 内部逻辑 | 统一在 Prompt Builder 中转成 provider 需要的消息格式 |
| Provider 调用前的最终收口 | 不够集中，调试时不容易看全量输入 | 统一在 `call_llm()` 里形成完整 `messages_for_api` |
| 调试能力 | 能看，但不够体系化 | Prompt dump 对应最终组装结果，更适合排查问题 |
| 可测试性 | 改一处 prompt 容易影响 Agent 其他行为 | 可以单测规则加载、规则扫描、memory section、prompt 组装 |
| 可扩展性 | 新加一类 prompt 规则常要改 `Agent` 多处 | 新增规则层/运行模式/记忆段更容易挂进统一入口 |
| 风险控制 | 本地规则文件可能直接污染 system prompt | 增加了 prompt/rule 安全扫描这一道闸门 |
| 维护成本 | 随着团队、插件、模式变多，会越来越粘 | 更适合继续扩展团队、execution profile、worker 模式 |
| 最本质变化 | “Agent 自己拼 prompt” | “Prompt 组装成为独立层，并加了安全边界” |
