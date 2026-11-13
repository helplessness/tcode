# Windows 兼容第二批：Artifact SDK 文件传输

## 范围与当前状态

新增 Windows 10+、本地固定 NTFS 磁盘的原生句柄后端。当前为实验性实现：macOS 契约测试通过，Windows 原生验收尚未完成。CI 不是开发前提，也不新增自动工作流；可直接在 Windows 手动执行测试。

本批仅适配独立的 `tcode-artifact-sdk`，不改 TCode 普通模式、目标模式、审核、Protein Run 或异步恢复状态机。Windows 远端 MCP 与 Linux/macOS TCode 可以沿用同一 Artifact HTTP API，不传跨机器绝对路径。Job Protocol、Artifact 服务端本身的 Windows 部署不属于本批。

## 实现

- `windows_paths.py`：拒绝 UNC/设备路径、ADS、保留设备名、尾随点/空格、短名别名及 `~` 名称；预检清单大小写和文件/目录冲突。
- `windows_io.py`：通过 Windows 系统 DLL 提供句柄相对读写、目录枚举和失败删除。没有添加 Python 第三方依赖。
- 父目录逐层打开且保留句柄，使用 `NtCreateFile` 的 `RootDirectory` 和 `OBJ_DONT_REPARSE`，不依赖一次 `resolve()` 后再普通 `open()`。目录和文件句柄校验重解析属性，拒绝硬链接上传源。
- 新对象显式设置当前进程用户和 SYSTEM 的保护 DACL；不修改既有用户目录权限。
- 下载先独占创建，不覆盖旧文件；字节数和 SHA-256 检查仍在返回前完成。失败通过原句柄标记删除，不删除同名替换对象。
- `ArtifactIO` 公共接口、引用格式、服务凭据隔离、不自动重试和不跟随重定向的约束不变。Linux/macOS 保留 POSIX 描述符实现。
- 远端示例报告显式使用 UTF-8，避免 Windows 默认编码导致中文报告失败。示例仍不是生产队列，本批不扩大其并发保证。

原生调用依据：[NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)、[句柄目录查询](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)、[FILE_FULL_DIR_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_full_dir_info)。

## 手动验收

Windows 安装、配置和命令见 [SDK README](../packages/artifact-sdk/README.md)。无需启动真实 MCP、模型、GPU 或任何正式服务即可执行测试。

```powershell
py -3.12 -m venv .venv-artifact-sdk
.\.venv-artifact-sdk\Scripts\python.exe -m pip install -e "./packages/artifact-sdk[test,example]"
.\.venv-artifact-sdk\Scripts\python.exe -m pytest -c packages/artifact-sdk/pyproject.toml packages/artifact-sdk/tests packages/artifact-sdk/examples/test_mcp_server.py -q
```

测试包括文件/目录与本地 HTTP 往返、MCP 文件回显、远端任务重复提交返回同一 ID、服务重新加载后复用已有结果引用而不重复上传。原生专项包含失败清理、不可覆盖、父目录/文件不可替换、junction 拒绝、硬链接拒绝、多页目录枚举与固定根目录内的嵌套写入。

2026-11-12 本机 macOS 结果：72 项通过，6 项 Windows 原生专项明确跳过。跳过不是通过；尚未验证真实 Windows 的 ACL、句柄共享及重解析防护。不要据此宣称 Windows 全功能可用。

宿主既有目标模式、普通执行、Protein Run、异步恢复及文件传输回归：375 项通过；3 项之前已在原始 HEAD 复现的 Harness 版本契约失败保持不变；1 项真实 OpenCode 测试按原 opt-in 规则未运行。独立 sdist/wheel 在隔离构建环境构建成功，Windows 模块已包含在分发包中，没有向现有运行环境安装新依赖。

本批没有更改 `.tcode-local`，没有提交远程计算，没有修改第三方服务，没有推送仓库。
