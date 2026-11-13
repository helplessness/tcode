# 贡献指南

先按 [快速开始](README.md#快速开始) 建立独立环境，再查看 [维护地图](docs/MAINTENANCE.md) 选择修改入口。真实凭据、用户附件与运行目录不应进入补丁。

## 修改约定

- 优先修改负责该功能的模块，避免把新的领域 API 或完整交互面板继续加入主入口。
- 跨会话、工作区和租户操作须保留原请求的身份；异步返回后不要用当前选中的会话代替原会话。
- 新插件使用 [SDK](docs/plugin-sdk/README.md)，并声明权限和执行方式。
- 行为修复附上能复现问题的测试；纯文档修改检查命令和链接即可。

## 本地检查

```bash
python -m pip install '.[dev]'
python -m pytest -q scripts/tests
npm --prefix web test
npm --prefix web run build
```

涉及入口或跨模块集成时，运行快速开始中的 smoke 命令。工具、Hook 和插件定义变化时，更新生成目录：

```bash
python scripts/dsh.py catalog
python scripts/dsh.py catalog --check
```

公开测试范围见 [Alpha 验证说明](docs/PUBLIC-ALPHA.md)。网络、容器和真实模型测试需要额外环境；请在提交中区分已验证、未运行和环境阻塞。

提交说明应写明用户遇到的问题、修复后的行为及验证结果。重构应说明模块职责和兼容性，不以减少行数作为唯一理由。

发布前同步 `VERSION`、`pyproject.toml`、`tcode_agent.__version__` 和 `web/package.json`。提交并保持工作树干净后运行 `./scripts/tag_release.sh` 创建带注释的本地标签；脚本不会推送标签。

构建 Python 发布包前先在干净目录准备 Mol*：运行 `npm --prefix web ci` 和 `npm --prefix web run prebuild`，再执行 `cp -R web/public/vendor tcode_agent/server/static/`。这些文件由锁定的 npm 依赖生成，不提交到 Git；CI 和 `scripts/validate_agui_release.sh` 会在打包前准备它们。
