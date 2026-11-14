# BenchThread 第三方 Harness Adapter 示例

这个目录是一个独立 Python 分发包，演示第三方 CLI Harness 如何在不修改 BenchThread
源码的情况下被发现、启用并出现在 API 和前端中。

    python -m pip install ./examples/harness-adapter
    tcode runtimes discover --json
    tcode runtimes enable example-harness

enable 是对 Python entry point 的显式信任动作。BenchThread 服务已经运行时需要重启一次，
让新进程导入已信任的 Provider。随后可以执行：

    tcode runtimes inspect example-harness --json
    tcode runtimes doctor example-harness --json
    tcode runtimes conformance example-harness --fixture --json

示例由三个部分组成：

- provider.py 返回公开的 HarnessRuntimeDef，并创建复用 BenchThread NDJSON Driver 的 Backend。
- cli.py 是独立子进程，只读取 stdin 并输出有界 NDJSON。
- tcode.harness_adapters entry point 让 BenchThread 发现 Provider。

真实 Adapter 可以改用 ACP、Codex JSONL、Claude stream-json、通用 NDJSON 或纯文本输出。
Profile/Provider 中的能力必须与实际实现一致；通过 conformance 后才能提高发布等级。
