# TCode Agent 能力测试用例

## 1. 用途

这里是 T01～T12 的统一测试数据入口。以后需要复测时，请把图片、PDF、代码 Fixture、MCP 输入文件或待验收的第三方插件复制到 `inputs` 下对应的测试目录，不再依赖桌面散落路径。仓库自带的可提交第三方 Fixture 位于 `plugins/third_party_fixtures/community_smoke_plugin/`。

测试步骤、推荐提示词和通过条件见[测试用例清单](./测试用例清单.md)。如果文件包含必须核对的事实、预期产物或 MCP 参数，请先复制[测试数据登记模板](./测试数据登记模板.yaml.example)，填写为本地登记文件。

## 2. 目录结构

```text
能力测试用例/
├── README.md
├── 测试用例清单.md
├── 测试数据登记模板.yaml.example
├── fixtures/
│   └── T08-plugin-workflow/
│       └── community_archive_test_plugin/
└── inputs/
    ├── README.md
    ├── T01-runtime-baseline/
    ├── T02-code-and-files/
    ├── T03-session-lifecycle/
    ├── T04-image-understanding/
    ├── T05-document-pdf/
    ├── T06-mcp-synchronous/
    ├── T07-mcp-async-artifact/
    ├── T08-plugin-workflow/
    ├── T09-paper-research/
    ├── T10-security-and-failure/
    ├── T11-browser-ui/
    └── T12-stability/
```

## 3. 你应该把哪些文件复制到哪里

| 数据 | 复制目标 |
| --- | --- |
| 待修复的小型代码项目 | `inputs/T02-code-and-files/` |
| Session 附件、上下文锚点文件 | `inputs/T03-session-lifecycle/` |
| JPG、PNG、WebP 图片 | `inputs/T04-image-understanding/` |
| 普通 PDF、扫描 PDF、大型 PDF | `inputs/T05-document-pdf/` |
| 同步 MCP 的输入文件 | `inputs/T06-mcp-synchronous/` |
| PDB、SDF、任务配置等异步 MCP 输入 | `inputs/T07-mcp-async-artifact/` |
| 待验收的插件目录或压缩包 | `inputs/T08-plugin-workflow/` |
| 论文种子、研究问题、关键词文件 | `inputs/T09-paper-research/` |
| Prompt Injection、非法路径等安全 Fixture | `inputs/T10-security-and-failure/` |
| UI 专用图片、PDF、二进制文件 | `inputs/T11-browser-ui/` |
| 并发任务列表和稳定性参数 | `inputs/T12-stability/` |

T01 一般不需要输入文件；如需对比特定部署配置，可将脱敏后的配置副本放入 `inputs/T01-runtime-baseline/`。不要复制真实密码、Token、Cookie、私钥或完整生产环境配置。

## 4. 当前已经存在的历史测试数据

以下数据已经实际用于此前测试，但尚未复制到这个统一目录：

| 测试 | 当前来源 |
| --- | --- |
| T02 代码 Fixture | `workdirs/admin_tenant/admin/ws1/t02_agent_fixture/` |
| T03 Session 文件 | `workdirs/admin_tenant/admin/ws1/t03_session_fixture/tool_note.txt` |
| T03 附件 | `build/test-fixtures/agent-e2e/T03-session-lifecycle/attachment_note.txt` |
| T04 猫咪图片 | `inputs/T04-image-understanding/example.jpg` |
| T05 两篇论文 | `inputs/T05-document-pdf/` |
| T05 损坏 PDF | `build/test-fixtures/agent-e2e/T05-document-pdf/corrupted.pdf` |
| T06/T07 蛋白质与小分子 | `inputs/T07-mcp-async-artifact/example.pdb`、`pqq_pose2.sdf` |
| T08 官方示例插件 | `plugins/example_plugin/` |
| T08 第三方隔离插件 | `plugins/third_party_fixtures/community_smoke_plugin/` |
| T08 待安装 ZIP 插件 | `inputs/T08-plugin-workflow/community-archive-test-plugin-0.1.0.zip` |
| T08 ZIP 可复现源码 | `fixtures/T08-plugin-workflow/community_archive_test_plugin/` |

这些路径继续保留在历史报告中，方便追溯。以后新一轮测试优先使用本目录中的副本。

## 5. Git 与隐私规则

`inputs` 下的用户测试数据默认被本目录的 `.gitignore` 排除，原因是：

- 避免 PDF、图片、模型文件增大 Git 仓库；
- 避免业务数据或受版权保护的论文被意外提交；
- 避免测试 Token、内部地址和任务参数进入版本历史。

Markdown 文档和登记模板可以提交；实际用户输入文件只保留在本机。T08 的 ZIP 是不含用户数据和 Secret 的小型公开 Fixture，已作为唯一例外显式纳入版本化。

## 6. 报告位置

测试输入放在本目录，测试结果仍统一写到：

```text
build/test-reports/agent-e2e/T01-.../
...
build/test-reports/agent-e2e/T12-.../
```

完整规划见[Agent 完整能力测试规划](../Agent完整能力测试规划.md)。
