# T08 第三方插件压缩包输入

本目录提供一个真实 ZIP 插件包，供 AI 自动完成“安全解压、定位插件根、SDK 门禁、Docker 隔离加载、Tool/Workflow 验证和卸载清理”。

## 给 AI 的直接任务

```text
请执行 T08 第三方插件压缩包测试。使用 inputs/T08-plugin-workflow 中的 ZIP 和 expected.json；先安全检查压缩包，再解压到 plugins/third_party_test_runs 下，不得覆盖已有目录。自动识别 plugin.json 所在的真实插件根，依次执行 validate、test、doctor 和插件自测；启动 Docker Sandbox Runner，以未知第三方身份加载插件，调用 expected.json 中的全部 Tool，检查 Workflow 阶段依赖、Tool 白名单、Review Gate 和命名空间；连续加载/卸载三轮，确认 Tool、Workflow、Registry 和容器无残留。测试结束后恢复 plugins.json 原内容并写报告，不得将插件加入 trusted allowlist，不得降级为 Host 运行。
```

## 输入文件

| 文件 | 用途 |
| --- | --- |
| `community-archive-test-plugin-0.1.0.zip` | 待安装的真实第三方插件压缩包 |
| `expected.json` | Manifest、Tool、Workflow 和期望输出 |

## 解压规则

1. 拒绝绝对路径、`..` 路径逃逸、符号链接、重复成员和异常大的压缩包；
2. 先解压到独立暂存目录；
3. 如果暂存根直接包含 `plugin.json`，暂存根就是插件根；
4. 如果只有一个一级目录且其中直接包含 `plugin.json`，该一级目录才是插件根；
5. 其他层级视为歧义并失败关闭，不得猜测；
6. 最终测试安装位置必须位于 `plugins/third_party_test_runs/`；
7. 不得覆盖已有目录，使用插件名与压缩包 SHA-256 前 12 位组成唯一目录名。

## 通过条件

- `validate`、`test`、`doctor` 和插件自测全部通过；
- 执行模式严格等于 `isolated-worker`；
- Docker Worker 无网络、只读、非 root、丢弃全部 capabilities；
- 两个 Tool 的原始输出与 `expected.json` 一致；
- Workflow 完整键、阶段依赖和 Review Gate 与预期一致；
- 连续三轮装载和卸载成功；
- 卸载后无 Tool、Workflow、Plugin Registry 或插件容器残留；
- 根 `plugins.json` 在测试结束后逐字节恢复。

Fixture 可复现源码位于 `exmples/能力测试用例/fixtures/T08-plugin-workflow/community_archive_test_plugin/`。修改源码后必须重新生成 ZIP，并同步更新 `expected.json` 的 `archive_sha256`。
