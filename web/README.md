# BenchThread Agent Web UI

BenchThread 的现代化 Web 界面，使用 SolidJS 构建。

## 特性

- 🎨 **深色主题** - 与 BenchThread TUI 一致的深色终端风格界面
- 💬 **实时聊天** - WebSocket 支持，流式输出
- 🔌 **MCP 集成** - 自动显示 MCP 服务器状态和工具数量
- 📊 **会话管理** - 多会话支持，左侧边栏切换
- 📈 **使用统计** - 实时显示 token 使用情况
- 📱 **响应式设计** - 适配不同屏幕尺寸
- ⚡ **高性能** - 虚拟滚动支持大量消息

## 快速开始

首次运行请从仓库根目录按 [本地快速开始](../README.md#快速开始) 安装后端、建立独立配置并启动。不要直接沿用开发环境中的模型或 MCP 配置。

前端命令（从仓库根目录运行）：

```bash
npm --prefix web install
npm --prefix web run dev
npm --prefix web test
npm --prefix web run build
```

开发服务器将 API/WebSocket 请求代理到本机 8080 端口；以 Vite 输出的地址为准。构建后的 `web/dist` 由 Python 后端提供。

输入区拆分后的维护入口见 [维护地图](../docs/MAINTENANCE.md)。

## 项目结构

```
web/
├── src/
│   ├── components/
│   │   ├── ChatView.tsx      # 聊天视图
│   │   ├── MessageInput.tsx  # 消息输入框
│   │   ├── MessageItem.tsx   # 消息项
│   │   ├── SessionList.tsx   # 会话列表
│   │   └── MCPAndSkillsPanel.tsx # MCP 与技能面板
│   ├── stores/
│   │   └── session.ts        # 状态管理
│   ├── api/
│   │   ├── client.ts         # API 公共导出入口
│   │   ├── chat.ts           # 聊天连接、流式消息和交互
│   │   └── *.ts              # 按领域组织的 API
│   ├── App.tsx               # 主应用组件
│   ├── index.tsx             # 入口文件
│   └── index.css             # 样式
├── package.json
├── vite.config.ts
└── tsconfig.json
```

## API 接口

后端提供以下 API 端点：

- `GET /api/health` - 健康检查
- `GET /api/mcp/status` - MCP 服务器状态
- `GET /api/mcp/tools` - MCP 工具列表
- `GET /api/skills` - 技能列表
- `GET /api/files` - 文件列表
- `GET /api/files/content` - 文件内容
- `POST /api/chat/cancel` - 取消聊天
- `WS /ws/chat` - WebSocket 聊天

## 配置

### MCP 配置

编辑当前配置目录中的 `mcp.json` 添加你实际部署的 MCP 服务器。下面仅是格式示例：

```json
{
  "my-fastmcp": {
    "url": "http://127.0.0.1:9999/mcp",
    "transport": "streamable-http",
    "timeout": 30000
  }
}
```

### 环境变量

```bash
export TCODE_WORKDIR=/path/to/work  # 工作目录
export PORT=9000                        # 自定义端口
```

## 技术栈

- **前端**: SolidJS, TailwindCSS, Vite
- **后端**: FastAPI, WebSocket
- **AI**: Ollama (qwen3-coder-next)
- **MCP**: Model Context Protocol

## 开发

```bash
# 安装依赖
cd web
npm install

# 开发模式 (热重载)
npm run dev

# 构建生产版本
npm run build

# 预览生产构建
npm run preview
```

## 截图

界面采用与 BenchThread TUI 一致的深色主题：

- 左侧边栏：会话列表 + MCP 状态 + 使用统计
- 中间：聊天对话区域
- 底部：消息输入框

## 许可证

[MIT License](../LICENSE)。第三方组件沿用各自的许可证及版权声明。
