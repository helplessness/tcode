# Community Smoke Plugin

这是从官方 `example_plugin` 中抽取并降权后的第三方插件 Fixture，用来验证 TCode Python Plugin SDK v1 的安装、Docker 隔离、Tool、声明式 Workflow 和卸载链路。

它故意不使用官方插件身份，不申请文件、网络、进程、MCP、Skill 或 Host Provider 权限。即使目录位于仓库 `plugins/` 下，宿主也会因为名称不在官方固定清单且 `requestedTrust` 为 `isolated`，将它作为未知社区插件送入 Docker Worker。

## 当前仓库中的装载方式

本 Fixture 已经位于：

```text
./plugins/third_party_fixtures/community_smoke_plugin
```

并已在仓库根 `plugins.json` 中预登记为默认禁用。使用时不需要自己写 Docker `-v` 参数；启动 Sandbox Runner 后，通过插件界面启用，或调用插件管理 API 即可。TCode 会校验目录是否位于 Runner 允许根下，并自动以只读方式挂载到隔离 Worker。

仅把一个目录复制进仓库并不等于已装载，因为当前 `plugins.json` 的 `auto_discover` 是 `false`。完整过程是：

1. 把包含 `plugin.json` 和入口文件的完整目录放入允许的插件根；
2. 在 `plugins.json` 中登记精确 `name`、`path`、`enabled` 和 `config`；
3. 启动 Docker 与 Sandbox Runner；
4. 显式启用插件或执行 `reload-config`；
5. 验证执行模式为 `isolated-worker`。

## 开发期检查

```bash
python scripts/plugin.py validate ./plugins/third_party_fixtures/community_smoke_plugin
python scripts/plugin.py test ./plugins/third_party_fixtures/community_smoke_plugin
python scripts/plugin.py doctor ./plugins/third_party_fixtures/community_smoke_plugin
python -m unittest discover -s ./plugins/third_party_fixtures/community_smoke_plugin -p "test_*.py"
```

## 启动隔离 Runner

```bash
scripts/start_sandbox_runner.sh start -d
scripts/start_sandbox_runner.sh status
```

本地必须已经构建 `tcode-plugin-runtime:py312` 镜像。未知第三方插件不会自动降级到 Web 主进程；Runner、镜像或目录白名单不满足时，加载会失败关闭。

## 通过 API 显式启用与卸载

以下命令假设本地 Web 运行在 `18080`，且当前开发环境不要求额外登录凭据：

```bash
curl -X POST http://127.0.0.1:18080/api/plugins/load \
  -H 'Content-Type: application/json' \
  -d '{"name":"community-smoke-plugin","path":"./plugins/third_party_fixtures/community_smoke_plugin","enabled":true,"config":{"prefix":"THIRD_PARTY"}}'

curl http://127.0.0.1:18080/api/plugins/tools

curl -X POST http://127.0.0.1:18080/api/plugins/unload \
  -H 'Content-Type: application/json' \
  -d '{"name":"community-smoke-plugin"}'
```

前端插件页走的是同一组 API。卸载会停止 Worker、撤销 Tool 和 Workflow 注册，并把 `plugins.json` 中该插件恢复为禁用；不会删除插件源码目录。

## 复制其他第三方插件时

项目开发期的第三方 Fixture 推荐归类到 `./plugins/third_party_fixtures/<插件目录>/`，避免被仓库官方插件门禁误判。真实部署可使用运维配置的任一受管插件根。然后增加类似配置：

```json
{
  "name": "my-community-plugin",
  "path": "./plugins/third_party_fixtures/my_community_plugin",
  "enabled": false,
  "config": {},
  "source": "plugins.json"
}
```

Manifest 的 `name` 必须与配置名称一致。第三方插件只允许可执行 Tool Contribution；Workflow 必须是宿主解释的声明式 JSON。插件不得通过自报 `trusted` 获得 Host 权限。
