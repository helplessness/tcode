# Windows 兼容第三批：本地进程与取消

本批接入 `bash`、`exec` / `process` 托管命令以及旧版 `StdioMCPClient` 的本地进程生命周期。远端 MCP 请求、任务 ID、异步恢复和远端取消协议不变；停止本地进程不会取消已提交的远端任务。没有新增依赖、CI 或本地配置项。

- Windows 用独立、不可继承的 Job Object 管理每次启动。受信任的 CPython 引导进程在解释器目录等待，加入 Job 后才放行命令。若绑定失败，命令不会启动；不根据记录中的 PID 查找或终止进程。
- 停止时终止对应 Job 并检查活跃进程数。直接进程正常退出时也清理仍在 Job 内的后代，避免后代持有输出管道导致等待不结束。Job 最后一个句柄关闭时触发内核清理。参考 [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 和 [进程记账信息](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)。
- 托管停止状态先为 `terminating`，确认后才写入 `terminated`；超时或系统调用失败返回未确认。POSIX 保留独立进程组和信号机制，只以直接子进程退出为等待依据，不承诺终止主动脱离组的进程。Bash 执行超时会清理；Exec 的等待超时仍保留可继续管理的 `process_id`，不改变后台任务语义。
- Job Object 不是沙箱。Windows 原生命令使用 `cmd.exe`，现有 POSIX 解析器不对它做安全审批。默认 `enforce` 模式拒绝非沙箱 Windows shell；如果管理员原先显式使用 `audit` / `disabled`，仅标记未分类，不宣称审批通过。不自动修改策略或回退到不受管控的执行。已配置本地 stdio 命令用参数数组启动，不经过 shell。

当前 Windows 后端限源码或虚拟环境中的 CPython（使用支持 subprocess 的默认 Proactor 事件循环），依赖其原始进程句柄；不可用时拒绝启动。此改动不实现完整 Windows 桌面沙箱，不改变现有桌面隔离要求，也不覆盖外部 Harness、插件 Worker 等独立启动器。旧版 stdio MCP 的协议实现本身未扩展。

## 手动测试

在 Windows 上，从已有 TCode 开发虚拟环境的 PowerShell 运行：

```powershell
python -m pytest scripts/tests/test_local_process.py scripts/tests/test_platform_compat.py -q
```

用例只启动临时本地 Python 子进程。Windows 专项检查 Job 绑定失败不执行命令、显式停止清理子进程、父进程先退出仍清理子进程；不连接模型、远端服务或共享 MCP。

本机 macOS：公开兼容回归 36 项通过，3 个 Windows 专项未执行；宿主回归 381 项通过、3 项已知的 Harness 版本契约失败、1 项原有 opt-in 跳过。原生行为仍待 Windows 实机验收。
