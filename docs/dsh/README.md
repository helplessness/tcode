# DSH 开发验证

公开快照从仓库根目录运行 `python scripts/dsh.py smoke` 验证入口。该命令使用本机协议桩，不需要模型密钥。可用 `python scripts/dsh.py --help` 查看分项命令。依赖私有测试材料的完整门禁不属于本快照的验证范围。

改动工具、事件或配置后，可运行 `python scripts/dsh.py catalog` 在本地生成目录，再用 `catalog --check` 检查是否漂移。生成文件不是用户运行配置。第一次安装与运行从[仓库首页](../../README.md#快速开始)开始。
