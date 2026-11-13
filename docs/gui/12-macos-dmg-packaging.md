# macOS 桌面打包

先按[桌面说明](../../desktop/README.md)准备依赖，在 macOS 上从仓库根目录运行：

```bash
npm --prefix desktop run build:backend:mac
npm --prefix desktop run smoke:backend:mac
npm --prefix desktop run package:mac
```

后端位于 `build/release/backend/`，安装镜像位于 `desktop/release/`。构建不需要相邻 TMCP 仓库；PyInstaller 明确排除 `tmcp`，只内置 TCode 的通用 Job Protocol。开发构建不等同于已签名、公证的发行包。正式发布前在干净的 macOS 环境验证安装、启动、退出和本机数据目录；后端 smoke 不替代真实桌面验收。
