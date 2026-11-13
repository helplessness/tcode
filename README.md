# TCode

TCode 是一个可自托管的 AI 研究工作台，重点面向蛋白质研究。你可以在同一个工作区里整理文献、调用研究工具、跟踪远程计算，并把返回的文件和结果留给下一步分析。

这是首版开源 Alpha 的候选快照，面向本机和自托管研究，不代表生产多租户部署已经验收。仓库保留研究工具和插件代码，不附带私有配置、真实验收记录、未确认可分发的知识库或研究样本。依赖这些数据的插件需由部署方提供有权使用的数据，基础聊天不需要它们。

`v0.0.2` 是固定的源码快照；`main` 包含其后的开发改动。复现问题时请注明使用的标签或提交，不要只看应用显示的版本号。

## 为什么用 TCode？

如果你的任务需要在聊天、计算队列和结果文件之间来回切换，TCode 可以把这些步骤连起来。它尤其适合**不止一轮对话、需要等结果回来再做判断**的研究任务。

- **不只说「做完了」，还告诉你哪些要求已经验证。** TCode 可以按任务的验收条件检查工具结果、交付文件和输出格式；证据不足时，指出还缺什么。你可以据此决定继续补查、人工复核，还是先接受当前结果。接受结果不会把未验证的检查标成通过。
- **长计算不用守在聊天窗口。** 接入支持异步任务的 MCP 服务后，TCode 会保留提交回执，后续按原任务查询进度、恢复跟踪并取回结果。不确定是否提交成功时，会保留待核对状态，而不是把超时当成失败后盲目重提。
- **任务可以持续推进，人仍能决定何时介入。** 一次性问题用普通对话即可；多步骤任务可以设定目标和预算，运行中补充说明、暂停或恢复。需要把关的工具可配置人工审核，重复的步骤可以整理成 Workflow。
- **结果留在工作区，方便复查和接着做。** 会话、输入文件、运行记录和生成的产物按工作区管理。对蛋白质计算，Protein Run 提供独立的记录入口；复盘时可以查看当时用了什么输入、任务跑到哪一步、返回了哪些文件。
- **复用已有模型和工具。** 可以先接一个模型跑通基础对话，再按需连接实验室已有的 MCP 服务，或用插件扩展工作流。不必为了使用工作台，把现有计算程序重新实现一遍。

例如，接入结构预测服务后，可以从会话提交预测，稍后回到工作区查看任务状态和返回的结构文件，再决定是调整输入、追加分析，还是请同事复核。**TCode 负责组织和跟踪过程，不替代预测服务或科研判断。** 领域计算、异步回传和知识库需要按场景另行配置；自托管工作台也不等于所接入的模型或工具全部在本机运行。

想先看有哪些功能，见[功能进度](#功能进度)。

## 快速开始

从一个刚克隆的仓库开始，进入包含 `pyproject.toml` 的 TCode 根目录。不需要复制别人的配置、聊天记录或工作区。

### 先选择启动方式

| 平台 | Alpha 支持范围 | 配置完成后的日常启动 |
| --- | --- | --- |
| [Linux Web](#linux安装并启动-web) | 主要 Web 部署入口；本机试用和按配置部署的启动方式不同。 | 本机体验用 `tcode-server`；按配置启动用 `./scripts/start_web.sh` |
| [macOS 桌面源码版](#macos启动桌面端) | Apple Silicon（arm64）；不提供已签名安装包，不支持 Intel Mac 桌面。 | `./scripts/start_desktop.sh`，不需要另启 Web |
| [Windows Web](docs/Windows-Web快速启动.md) | 原生 PowerShell 源码启动，实验性支持，尚待 Windows 实机验收；不提供 Windows 桌面版。 | 在 PowerShell 用 `tcode-server.exe` 启动，浏览器访问 |

以上命令从项目根目录执行，首次使用前都需要安装依赖和配置模型。Windows 和 Linux 的本机最小 Web 启动方式都不要求 Docker；连接普通远程 MCP 也不要求本机安装 Docker。浏览器接管、电脑控制和本机隔离等能力不承诺跨平台对等。异步任务、文件传输和需要隔离的工具按使用场景另行配置。

### 依赖与必填配置

**当前源码启动不能在空白环境中直接运行。** 用户需先准备下面标为“必须”的基础环境，再安装项目依赖、填写模型配置。安装脚本不会自动安装系统 Python、Node.js、Docker 或远程计算服务。首次下载项目依赖需要联网。

| 依赖或配置 | 是否必须 | 用户需要准备什么 |
| --- | --- | --- |
| Git | 按需 | 使用 `git clone` / `git pull` 获取和更新源码时需要；下载源码压缩包不需要 Git 来解压。 |
| Python | 源码启动必须 | Windows / Linux Web 准备 Python 3.12（Windows 指南使用 `py -3.12`）；macOS 桌面准备 Python 3.12+，确保 `python3` 指向符合要求的版本。 |
| Python venv / pip | 源码启动必须 | 需能创建虚拟环境并安装包。Linux 发行版可能需另装与 Python 版本对应的 venv / pip 软件包。 |
| Node.js 与 npm | 源码启动必须 | 准备 Node.js 22.12+ 和 npm，用于构建前端；桌面源码启动还用它安装和启动 Electron。 |
| Apple Silicon 与系统工具 | macOS 桌面源码版必须 | 当前驱动使用 arm64 版本，Intel Mac 不在支持范围。需有 `curl`、`tar`、`shasum` 等系统命令；首次准备依赖需能访问 Python 包源、npm、Electron 和 GitHub 发布下载地址。即使不启用 Computer Use，启动脚本也会检查并按需准备驱动。 |
| 项目 Python / npm 依赖 | 源码启动必须 | Windows 按 [PowerShell 指南](docs/Windows-Web快速启动.md)用 pip 和 npm 安装；Linux 运行 `./scripts/install_tcode.sh`；macOS 的 `./scripts/start_desktop.sh` 按需安装桌面依赖。 |
| 模型接口与配置 | 实际聊天必须 | 准备可访问的模型服务，填写模型 ID、接口地址和服务所需的密钥。可使用远程 API，不要求本机部署模型或安装 GPU 驱动。 |
| Docker 与 Sandbox Runner | 按需 | Linux 使用容器隔离插件、容器执行能力时，按部署方式准备 Docker 和对应 Runner；Runner 可部署在独立主机。下文最小 Web 聊天体验不需要 Docker。选择 Docker 部署 Web 本身时也需要 Docker，但这与工具沙箱是两回事。 |
| macOS 原生隔离 | 桌面本地隔离功能需要 | 已接入的桌面本地执行使用 macOS 原生隔离，不要求安装 Docker；启动时进行隔离自检。电脑控制等能力还需按提示授予系统权限。 |
| 普通 MCP 服务 | 按需 | 使用对应工具时自行部署或连接已有服务；只连接远程 MCP 不要求本机安装 Docker，也不要求部署 TMCP。 |
| 异步任务服务与执行端 | 按需 | 桌面和单机 Web 可以托管内置 Job Protocol，或继续用已有网关。真正执行计算的 MCP 服务仍需单独提供；Windows Web 的内置网关尚待实机验证。 |
| Artifact 文件服务 | 按需 | 使用远程文件引用传输输入或取回产物时配置服务地址和认证。文本工具不需要；安装文件 SDK 不会自动部署文件服务。 |
| 领域工具、GPU、模型权重 | 按需 | Boltz 等计算工具按各自要求部署在实际执行主机，不是基础 Web / 桌面启动依赖。 |
| 扩展功能依赖 | 按需 | swarm、向量检索、Redis 相关 Python 依赖可用 `./scripts/install_tcode.sh --full` 安装；Redis 服务、检索模型及插件自身依赖仍需按所用功能另外准备。 |

**未配置沙箱时，需要隔离的功能不应绕过隔离在宿主执行。** 基础聊天可用不代表所有工具可用；缺少对应服务时，请配置服务或保持该功能关闭。详细服务入口见[需要哪些服务？](#需要哪些服务)和[运行指南](docs/RUNNING.md)。

### Windows：在 PowerShell 启动 Web

按 [Windows Web 快速启动](docs/Windows-Web快速启动.md)安装、一次补齐本机配置并启动。不要执行下面的 Linux Bash 脚本或 macOS 桌面命令。指南先跑通基础聊天，再说明如何接普通 MCP、远程异步任务和文件服务。

### macOS：启动桌面端

不需要先运行 Web 安装脚本，也不需要另外启动 Web 后端。首次执行：

```bash
./scripts/init_local_config.sh
```

参考[模型配置](#2-填写新生成的配置)填写模型 ID。源码桌面默认要求登录：**首次启动前**，在私有的 `.tcode-local/desktop.env` 中、继承 `local.env` 的 `source` 行之后设置 `TCODE_BOOTSTRAP_PASSWORD` 为自己的强密码。初始邮箱默认是 `admin@example.com`，可在同处通过 `TCODE_BOOTSTRAP_EMAIL` 更改。启动后用该邮箱和密码登录；这些设置只用于空认证库的首次初始化，不会重置已有账号的密码。

在项目根目录执行（如用终端临时设置了模型凭据，请在同一终端执行）：

```bash
./scripts/start_desktop.sh
```

启动脚本会按需创建 `.desktop-venv`、安装桌面 Python 与 npm 依赖并构建前端；后续仍使用同一条命令。桌面使用独立后端与数据目录，不会自动复用 Web 的会话。这里是**源码开发版**启动方式，不是已签名安装包；登录、可选服务和打包说明见[桌面 README](desktop/README.md)。只使用桌面端可跳过下面 Web 专用的安装与启动步骤。

### Linux：安装并启动 Web

首次使用建议选方式 A：只监听 `127.0.0.1`，不要求登录，也不要求插件、远端 MCP 或容器沙箱。内置工具仍可操作本机文件；请使用专用工作区，不要把此配置用于共享部署。方式 B 会按本机配置启动所需服务。

#### 1. 安装与构建

```bash
./scripts/install_tcode.sh
source .venv/bin/activate
```

脚本使用锁定的前端依赖构建界面，再从 `pyproject.toml` 安装 Python 包。需要 swarm、向量检索和 Redis 时运行 `./scripts/install_tcode.sh --full`。Python 的跨平台解析结果保存在 `uv.lock`，可供 pip 直接安装的带哈希版本分别保存在 `requirements.lock` 和 `requirements-full.lock`；领域插件还可能有各自依赖。
安装脚本最后会调用 `scripts/init_local_config.sh`，一次把全部公开配置模板补齐到 `.tcode-local/`，不覆盖已有配置。

该脚本安装的是项目依赖，不会替你安装系统 Python、Node.js 或 npm。可先用 `python3.12 --version`、`node --version`、`npm --version` 检查；若创建虚拟环境时提示缺少 `venv` / `ensurepip`，需先安装发行版中与该 Python 版本对应的 venv 软件包。

#### 2. 填写新生成的配置

如果想在网页配置模型，可以先按下面的「方式 A」启动，再打开 **设置 → 模型与执行 → Web 模型服务**。添加服务后可分别测试文本和图片能力，并选择默认模型。这样不需要先修改 JSON 或设置模型环境变量。已有部署也可继续用下面的文件配置；共享部署的网页配置须由实例管理员通过 HTTPS 操作。

安装结束后，`.tcode-local/` 已有全部 13 份本机配置。若已安装依赖但配置目录还不存在，可单独运行 `./scripts/init_local_config.sh`。首次聊天只需编辑 `.tcode-local/tcode.json`：把 `model` 中的 `your-model-id` 与 `provider.demo.models` 下的同名键都换成服务商实际提供的模型 ID。模型服务应支持 OpenAI-compatible Chat Completions；要使用工具时还需支持工具调用。

在当前终端填写模型地址与凭据（本地服务不要求密钥时可用 `local`）：

```bash
export TCODE_DEMO_BASE_URL='https://your-provider.example/v1'
export TCODE_DEMO_API_KEY='replace-with-your-key'
```

公开模板通过 `baseURLEnv` 和 `apiKeyEnv` 读取这两个变量。终端中的 `export` 只对当前终端及其子进程有效；从 Finder 启动桌面时不要依赖它。需要持久配置时，可将这两个变量写入私有的 `.tcode-local/desktop.env`（桌面）或 `.tcode-local/local.env`（下方方式 B）。方式 A 不加载这些环境文件，需在启动终端中设置变量。不要提交 `.tcode-local/` 或真实凭据。未配置实际视觉模型时先用纯文本聊天；要识图，再将 `vision_model` 指向实际支持图片输入的模型。其他设置见[配置指南](docs/CONFIGURATION.md)。

#### 3. 启动

##### 方式 A：本机最小体验（新用户推荐）

```bash
mkdir -p workdirs/local
TCODE_CONFIG_ROOT="$PWD/.tcode-local" \
TCODE_MCP_CONFIG="$PWD/.tcode-local/mcp.json" \
tcode-server --local \
  --host 127.0.0.1 --port 8080 --workdir "$PWD/workdirs/local"
```

打开 [Web 界面](http://127.0.0.1:8080)，先选择一个工作区（也可在侧栏新建），再创建会话并发送一条消息。命令中的 `workdirs/local` 是服务的运行数据目录，不是界面中的工作区名称。可用 [健康检查](http://127.0.0.1:8080/api/health) 检查后端。按 `Ctrl+C` 停止；重启时重新激活 `.venv`、设置模型环境变量并使用同一个运行数据目录，即可保留工作区和会话。桌面端和 Docker 的启动方式见[运行指南](docs/RUNNING.md)。

##### 方式 B：按本机配置启动服务

已有部署继续使用原启动命令，不要加 `--local`。桌面和单机 Web 启用通用异步 MCP 后重启，可自动启动内置 Job Protocol；已有外部网关优先，旧任务不会自动改投。启用方式见[第三方异步 MCP 指南](docs/服务隔离/第三方MCP插件接入与运行.md#1-先确认-tcode-侧的服务就绪)。

使用高级功能前先准备对应服务：Web 浏览器需要 Sandbox Runner 和 Playwright 镜像；隔离执行需要支持的桌面隔离后端或 Linux 沙箱；远程计算需要已启用的 Job Protocol 和已注册的执行端；远程文件传输还需要 Artifact 服务和工具绑定。缺少这些服务不妨碍基础聊天。

依赖已安装且 `.tcode-local/local.env` 已按用途配置后，执行：

```bash
PYTHON_BIN="$PWD/.venv/bin/python" ./scripts/start_web.sh --workdir "$PWD/workdirs/local"
```

这个脚本加载本机运行配置，认证、沙箱和后台服务以实际配置为准。若启用了认证或强制 Runner，先准备登录和隔离服务。它不是方式 A 的缩写；首次只想试聊、没有配置沙箱时，继续使用方式 A。不要同时用两条命令启动同一个运行目录。

前端开发时保持选定的后端运行，在另一个终端执行 `npm --prefix web run dev`，打开终端显示的 Vite 地址。默认代理目标为后端 8080 端口。

#### 4. 不连接远端服务的验证

暂时没有可用模型或 MCP 服务，也可以验证真实入口：

```bash
python scripts/dsh.py smoke \
  --scenario-timeout-seconds 45 --total-timeout-seconds 120 \
  --report build/test-reports/real-entry-smoke.json
```

该命令使用临时目录和本机 LLM/MCP 协议桩，检查 WebSocket 对话、插件、CLI 与 Worker 入口。它验证集成链路，不代表真实模型效果或远端计算服务可用。

### 常见问题

| 现象 | 处理 |
| --- | --- |
| 界面可打开，但消息失败 | 检查模型 ID、服务地址、密钥及工具调用支持；健康检查不验证模型服务 |
| 页面提示未构建或显示旧界面 | 重新运行 `npm --prefix web run build` 并刷新 |
| 8080 已被占用 | 更改 `--port`；开发模式同时调整 `web/vite.config.ts` 的代理目标 |
| 找不到插件或 MCP 工具 | 本示例主动关闭它们；按插件 SDK、MCP 指南在体验目录中单独配置 |
| 沙箱执行器不可用 | 本地体验不要求执行器；启用插件隔离时需按部署文档配置真实 runner |

## 需要哪些服务？

### 网页搜索（可选）

保存配置后，可在 **设置 → 网页搜索** 点击「测试连接」。测试只向当前已保存的服务发送一次固定查询，不会自动切换；Brave 可能消耗一次请求额度。通过后显示状态和测试时间，修改配置后需重新验证。

基础聊天不需要搜索服务。内置 `websearch` 默认使用 SearXNG：部署或连接允许 JSON 搜索的实例，按需设置 `TCODE_SEARXNG_BASE_URL`（默认 `http://127.0.0.1:8888`）。地址必须从 TCode 后端可达。

不想部署 SearXNG，可从 [Brave Search API](https://api-dashboard.search.brave.com/documentation/guides/authentication) 获取自己的 Key，再在启动后端的环境中设置：

```bash
export TCODE_WEBSEARCH_PROVIDER=brave
export TCODE_BRAVE_SEARCH_API_KEY='replace-with-your-key'
```

重启后端后，原来的 `websearch` 会用 Brave 搜索普通网页，每次最多请求一页。专用图片、视频、新闻等分类仍用 SearXNG。切回 SearXNG 时将后端设为 `searxng` 或取消该变量。服务失败不会自动切换；选 Brave 后，搜索关键词会发送给 Brave。

也可以在 Web 或桌面打开 **设置 → 网页搜索**。实例管理员选择 SearXNG 并填写实例地址，或选择 Brave 并填写 Key。保存后立即用于后续搜索，不修改已有环境文件；未保存过的字段继续使用启动配置。Key 保存在后端运行数据目录的私有设置文件，页面只显示是否已配置，留空保留原 Key。Web 与桌面使用不同运行目录时分别配置。不要提交真实凭据或将运行数据目录公开。

**装好 TCode 后，第一次聊天只需配置一个可用的模型接口。** 不需要单独部署数据库、Redis、TMCP 或沙箱。首次初始化生成的 MCP 服务清单是空的，不会自动连接外部工具。

| 组件 | 默认 | 何时需要额外配置 |
| --- | --- | --- |
| TCode 后端与模型 API | 后端随 TCode；桌面不需要另启 Web。 | 聊天需配置本地或远端模型；图片输入另需可用的视觉模型。 |
| SQLite | 内置，持久化会话、异步跟踪等本地状态。 | 不需要部署数据库服务；重启后应继续使用同一运行数据目录。 |
| 证据图与本体 | 使用本地 SQLite 和图文件等存储，不依赖外部图数据库。 | 特定研究插件可能需要另配有权使用的数据；无需部署 Neo4j。 |
| Redis | 不需要；运行指导通知和 Swarm 消息传递默认不依赖 Redis。 | 按需配置部分通知或 Swarm 消息后端。Redis 不替代 SQLite，也不等于支持高可用部署。 |
| FAISS 与 Embedding | 基础聊天不需要。 | 启用证据语义召回时安装本地 FAISS 并配置 Embedding；没有可用语义索引时，证据搜索仍可用关键词。 |
| 普通 MCP | 不预连任何服务。 | 工具直接返回结果时加入 [MCP 配置](tcode_agent/MCP_QUICKSTART.md)，不需要 Job Protocol。 |
| Job Protocol 与计算执行端 | 基础聊天不启动网关或计算服务。 | 需要远程异步任务时，启用内置网关或连接外部兼容网关，并配置实际执行端。 |
| Artifact 文件服务 | 基础聊天和纯文本工具不需要。 | 远程工具需要通过文件引用收发输入或产物时，配置 Artifact 地址和认证；安装 SDK 不会部署服务。 |
| 领域工具与 Sandbox Runner | 基础聊天不需要。 | 领域工具（例如 Boltz）由实际执行端提供；需要隔离的工具须配置对应隔离后端，不应绕过隔离。 |

`--full` 安装的是 Redis 客户端、FAISS 等可选 Python 依赖，不会部署 Redis 或下载 Embedding 模型。接入配置见 [MCP 服务示例](config/examples/README.md)和[配置指南](docs/CONFIGURATION.md)；共享部署前请先读[安全说明](SECURITY.md)。

### 开发自己的 MCP

- **普通或远程异步工具**：[第三方 MCP 接入与运行](docs/服务隔离/第三方MCP插件接入与运行.md)，包含可运行的 remote-job 示例及 TCode 端配置步骤。
- **工具要收发文件**：[Artifact Python SDK](packages/artifact-sdk/README.md)，包含安装、文件与目录 API、最小回显示例和契约测试。SDK 负责文件传输，不负责执行和恢复任务。
- **在 TCode 里声明异步目标或扩展插件**：[TCode Plugin SDK](docs/plugin-sdk/README.md)。这是 TCode 侧的 SDK，不需要装到远端计算服务上。

## 进一步阅读

- [文档目录](docs/README.md)：Windows Web、Linux Web、macOS 桌面与 Docker 的运行入口。
- [配置指南](docs/CONFIGURATION.md)：必填项、公开模板和本机私有配置。
- [公开快照范围与验证](docs/PUBLIC-ALPHA.md)：本版包含什么、如何验证、不承诺什么。
- [项目架构](docs/MAINTENANCE.md)：前后端职责、异步任务和数据边界。
- [贡献指南](CONTRIBUTING.md)。
- [安全说明与漏洞报告](SECURITY.md)。

## 功能进度

这里列的是用户可以使用的功能。部分功能需要先连接服务或开启权限，见[配置指南](docs/CONFIGURATION.md)。

### 已完成（20 项）

**对话与交互**

- [x] **对话与工作区**：流式对话、多会话、附件上传、文件预览和历史记录。
- [x] **全局搜索**：在一个入口查找会话、资料和任务记录，并跳转到对应页面。
- [x] **智能 UI（Intelligent UI）**：在对话中生成可交互页面和图解，点击部件看说明，也可继续修改图解。
- [x] **三维结构查看与 Agent 操作**：用 Mol* 打开蛋白质或分子结构文件；授权后，Agent 可以打开工作区里的结构、操作视图和测距。
- [x] **浏览器接管**：桌面端可以接管 Agent 正在使用的浏览器，手动操作后再交还控制权。
- [x] **电脑控制（Computer Use）**：macOS 桌面端可在用户授权下观察和操作本机界面，用户也可暂停或接管。

**任务与协作**

- [x] **目标模式**：设定目标和预算，在聊天中补充指导，按需暂停和恢复。
- [x] **任务结果验收**：按条件核对工具结果、交付文件和输出格式，指出未通过或缺少证据的地方。
- [x] **人工审核与权限确认**：查看待审核项及操作参数，由用户决定是否放行。
- [x] **工作流（Workflow）**：用模板复用多步骤任务，查看各步骤的进度和结果。
- [x] **多 Agent 协作与协作办公室**：查看成员分工、运行状态和协作消息，点选成员查看任务。
- [x] **远程计算与 Protein Run**：查询远程任务、恢复跟踪、取回产物，并查看蛋白质计算记录；需要连接相应服务。
- [x] **定时任务**：安排一次或重复执行，支持暂停、恢复、立即执行和查看历史。
- [x] **任务看板**：集中查看任务状态和待处理事项，并进入相应审核。

**研究与扩展**

- [x] **研究工作区**：记录假说、实验和证据，查看研究变化，也可分享或导入研究资料包。
- [x] **文献资料库与 Paper Wiki**：整理、检索和查看文献资料，按需启用相关插件与索引。
- [x] **本体查询与关系探索**：在公共或工作区私有知识中查询实体与关系，查看来源证据。
- [x] **从文档抽取本体知识**：上传 PDF、TXT 或 Markdown 生成候选，由人审核后发布到工作区私有知识库。
- [x] **模型、MCP、Skill 与插件**：在工作台配置模型、连接工具服务、管理 Skill 和插件，按需扩展功能。
- [x] **飞书接入**：配置并绑定飞书后，可通过飞书与 TCode 交互。

### 尚未支持（2 项）

- [ ] **直接导入外部本体**：目前不能在工作台直接导入现成的 OWL/RDF 文件、预览与现有知识的差异，再按审核结果合并。这与上传文档抽取候选是两种功能。
- [ ] **扫描件直接进入本体抽取**：当前文档导入不包含扫描件 OCR；图片型 PDF 需要先转成可提取文字的文档。

## Roadmap

- **近期**：补做 Windows Web 实机验收，持续验证干净环境的安装、前端构建和安装包。
- **接下来**：串起已有的第三方 MCP 示例，提供从上传文件、提交异步任务，到服务重启后恢复并取回产物的完整演练。
- **按需求评估**：多实例部署和外部图数据库。先确认单实例或本地图存储确实遇到瓶颈，不把新服务变成基础使用门槛。

这是开发优先级，不是发布日程或已支持功能列表。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `tcode_agent/server/` | HTTP、WebSocket、身份与工作区访问 |
| `tcode_agent/runtime/`、`harness/` | 运行状态与任务执行 |
| `tcode_agent/context/`、`memory/` | 上下文与记忆 |
| `tcode_agent/plugins/`、`plugins/` | 插件机制与仓库插件 |
| `tcode_agent/research/`、`mcp/` | 研究资产与外部工具集成 |
| `web/src/` | Web 界面、会话和输入状态 |
| `scripts/tests/`、`scripts/dsh.py` | 公开回归测试与本地入口验证 |

## 许可证与来源

项目代码按 [MIT License](LICENSE) 提供。第三方组件和外部资料沿用各自的许可证及版权声明。
