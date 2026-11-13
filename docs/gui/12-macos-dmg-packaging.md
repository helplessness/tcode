# macOS 桌面打包

在 Apple Silicon Mac 上，准备 Python 3.12+、Node.js 22.12+、npm 和 `uv`。从干净的仓库根目录运行（不需要自己的模型、账号配置或相邻 TMCP 仓库）：

```bash
python3 -m venv .desktop-venv
uv pip install --python .desktop-venv/bin/python -e '.[desktop]' -r packaging/requirements-macos.txt
npm --prefix web ci
npm --prefix desktop ci
npm --prefix desktop test
npm --prefix desktop run package:mac
npm --prefix desktop run smoke:backend:mac
```

后端位于 `build/release/backend/`，安装镜像位于 `desktop/release/`。PyInstaller 明确排除 `tmcp`，只内置 TCode 的通用 Job Protocol。打包时从校验过的 Cua 上游归档复制许可证和第三方声明到 `TCode.app/Contents/Resources/computer/`，不依赖开发机的本地文件。可用 `hdiutil verify` 检查镜像，再安装到独立目录检查许可证、启动、退出和本机数据目录。后端 smoke 不替代真实桌面验收。

当前命令生成的是未做 Developer ID 签名和公证的本地测试包，不应当作下载即用的发行包。只想运行源码版，请按[桌面首次启动](../../README.md#macos启动桌面端)操作。
