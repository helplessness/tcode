# 第三方来源与许可证

根目录的 MIT 许可证适用于项目贡献者有权授权的代码，不覆盖第三方包、模型、论文全文、数据集或用户导入的研究附件。外部资料保留其原有权利和使用条件。

## OpenCode 设计参考

Web README 原有声明记录了对 [OpenCode](https://github.com/anomalyco/opencode) 界面设计的参考。TCode 是独立项目，与其官方没有隶属关系。上游代码的许可证为 [MIT](https://github.com/anomalyco/opencode/blob/dev/LICENSE)，版权声明为 `Copyright (c) 2025 opencode`。设计参考不表示本仓库整体来自上游代码；如果引入具体上游实现，应在相应文件旁记录来源、版本和完整许可证。

## Paperclip 架构参考

Agent 控制面的目标层级、任务领取、Heartbeat 和预算治理参考了 [Paperclip](https://github.com/paperclipai/paperclip) 的公开概念与文档。TCode 保留自己的运行时和实现，没有复制 Paperclip 源代码。Paperclip 上游采用 MIT 许可证；若后续引入具体实现，应在对应文件旁记录来源、版本与许可证。

## 通过包管理器安装的依赖

Python 依赖见 `tcode_agent/requirements-core.txt` 与 `tcode_agent/requirements.txt`，Web 依赖见 `web/package.json`。其中包括 FastAPI、Pydantic、SolidJS、Vite 以及模型服务 SDK。安装包中的许可证与 NOTICE 是对应版本的权威声明；分发包含这些依赖的镜像或构建产物时，应保留其声明。

本文件是来源说明入口，不是对所有历史文件逐一完成的权属审计，也不是精确到依赖版本的许可证清单。

本次 Agent Gateway 与 MCP Apps Host 直接依赖的协议包固定为以下版本：

| 包 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| `@ag-ui/core` | `0.0.59` | MIT | AG-UI 输入与事件合同 |
| `@ag-ui/client` | `0.0.59` | MIT | 官方 `HttpAgent` 互操作验证 |
| `@modelcontextprotocol/ext-apps` | `1.7.5` | MIT | MCP Apps `AppBridge` 与 `PostMessageTransport` |
| `@mcp-ui/client` | `7.1.1` | Apache-2.0 | MCP Apps Host 设计与互操作参考 |

外部 Harness 不把 OpenCode、Codex CLI 或 Claude Code 打包进 TCode。用户自行安装的可执行文件继续受各自许可证和服务条款约束。

## 插件、素材与研究资料

- 仓库内独立第三方插件或素材若有自己的许可证，以其声明为准；引入外部实现时补充来源说明。
- `plugins/third_party_fixtures/community_smoke_plugin/` 是用于测试社区插件接入的 fixture，目录名称本身不是外部代码来源证明。
- 模型权重、论文、数据库记录与运行时下载内容不因被 Agent 使用而改为 MIT 授权。不要把运行目录中的资料作为项目源代码一并分发。

## Anatomy 绘图技能

`.claude/skills/anatomy/` 选择性捆绑 [Anatomy](https://github.com/wheresryan22/anatomy) 提交 `1482e8469b0a9e0d8cc18ea2740a01423d29ae80` 的几何工具和设计资料，MIT，`Copyright (c) 2026 Ryan`。完整声明在该目录 `LICENSE`，原版文件校验见 `upstream.json`。TCode 的技能入口和 `scripts/render.mjs` 为本地适配；原版几何文件保留。此引入独立于 `presentation/isometric.py` 的原有原创三维部件实现，不表示已接入上游完整网页、WebGL 或工程验证能力。
