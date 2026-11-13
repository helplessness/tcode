"""MCP (Model Context Protocol) 挂载指南

MCP 允许你连接外部工具服务器，将它们的工具集成到 Agent 中。

## 支持的 MCP 类型

1. **Local (stdio)** - 本地进程通信
2. **Remote (HTTP/SSE)** - 远程 HTTP 服务器

## 配置方式

### 方式 1: 通过代码配置

```python
from tcode_agent.mcp.manager import MCPManager
from tcode_agent.mcp.config import MCPConfig, MCPType

# 配置本地 MCP 服务器
MCPManager.add(
    name="filesystem",
    config=MCPConfig(
        name="filesystem",
        type=MCPType.LOCAL,
        command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
        enabled=True,
        timeout=30000,
    )
)

# 配置远程 MCP 服务器
MCPManager.add(
    name="github",
    config=MCPConfig(
        name="github",
        type=MCPType.REMOTE,
        url="https://api.github-mcp-server.com",
        headers={"Authorization": "Bearer YOUR_TOKEN"},
        enabled=True,
    )
)

# 连接到 MCP 服务器
await MCPManager.connect("filesystem")
await MCPManager.connect("github")

# 获取所有 MCP 工具
mcp_tools = await MCPManager.get_tools_async()
```

### 方式 2: 通过配置文件

创建 `mcp_config.json`:

```json
{
  "filesystem": {
    "type": "local",
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
    "enabled": true,
    "timeout": 30000
  },
  "github": {
    "type": "remote",
    "url": "https://api.github-mcp-server.com",
    "headers": {
      "Authorization": "Bearer YOUR_TOKEN"
    },
    "enabled": true
  },
  "database": {
    "type": "local",
    "command": ["python", "-m", "mcp_server_database", "--dsn", "postgresql://localhost/mydb"],
    "enabled": true
  }
}
```

然后在代码中加载:

```python
import json
from tcode_agent.mcp.manager import MCPManager

with open("mcp_config.json") as f:
    configs = json.load(f)

MCPManager.configure(configs)

# 连接所有配置的 MCP
for name in configs:
    await MCPManager.connect(name)
```

## 在 Agent 中使用

```python
from tcode_agent import Agent
from tcode_agent.providers.ollama import OllamaProvider
from tcode_agent.mcp.manager import MCPManager

async def main():
    # 1. 配置并连接 MCP
    MCPManager.add(
        name="filesystem",
        config=MCPConfig(
            name="filesystem",
            type=MCPType.LOCAL,
            command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
        )
    )
    await MCPManager.connect("filesystem")
    
    # 2. 创建 Agent (MCP 工具会自动注册)
    provider = OllamaProvider()
    agent = Agent.create(
        name="build",
        provider=provider,
        session_id="mcp-test",
        workdir="/home/user",
    )
    
    # 3. 使用 MCP 工具
    result = await agent.run('用 filesystem 工具列出 /home/user 目录的文件')
    print(result.content)
    
    # 4. 清理
    await MCPManager.close_all()
    await provider.close()
```

## 常见的 MCP 服务器

### 文件系统
```bash
npx -y @modelcontextprotocol/server-filesystem /path/to/allow
```

### GitHub
```bash
npx -y @modelcontextprotocol/server-github
```

### 数据库 (PostgreSQL)
```bash
npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb
```

### Git
```bash
npx -y @modelcontextprotocol/server-git
```

### Puppeteer (浏览器自动化)
```bash
npx -y @modelcontextprotocol/server-puppeteer
```

### 自定义 Python MCP
```python
# 创建自己的 MCP 服务器
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server("my-server")

@server.tool()
async def hello(name: str) -> str:
    return f"Hello, {name}!"

async with stdio_server() as streams:
    await server.run(
        streams[0],
        streams[1],
        server.create_initialization_options(),
    )
```

## 状态管理

```python
# 检查连接状态
status = MCPManager.get_status("filesystem")
print(status)  # MCPStatus.CONNECTED, MCPStatus.FAILED, etc.

# 获取所有状态
all_status = MCPManager.get_all_status()

# 重新连接
await MCPManager.reconnect("filesystem")

# 断开
await MCPManager.disconnect("filesystem")
```

## 错误处理

```python
from tcode_agent.mcp.config import MCPStatus

try:
    status = await MCPManager.connect("my-server")
    if status == MCPStatus.FAILED:
        print("连接失败，检查配置")
    elif status == MCPStatus.CONNECTED:
        print("连接成功")
except Exception as e:
    print(f"连接错误：{e}")
```

## 完整示例

```python
#!/usr/bin/env python3
import asyncio
import sys
sys.path.insert(0, '.')

from tcode_agent import Agent
from tcode_agent.providers.ollama import OllamaProvider
from tcode_agent.mcp.manager import MCPManager
from tcode_agent.mcp.config import MCPConfig, MCPType

async def main():
    print("=" * 60)
    print("MCP 挂载示例")
    print("=" * 60)
    
    # 1. 配置 MCP
    print("\n1. 配置 MCP 服务器...")
    MCPManager.add(
        name="filesystem",
        config=MCPConfig(
            name="filesystem",
            type=MCPType.LOCAL,
            command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            enabled=True,
        )
    )
    
    # 2. 连接 MCP
    print("2. 连接到 MCP 服务器...")
    status = await MCPManager.connect("filesystem")
    print(f"   状态：{status}")
    
    # 3. 列出可用的 MCP 工具
    print("3. 获取 MCP 工具列表...")
    mcp_tools = await MCPManager.get_tools_async()
    print(f"   可用工具：{list(mcp_tools.keys())}")
    
    # 4. 创建 Agent
    print("4. 创建 Agent...")
    provider = OllamaProvider()
    agent = Agent.create(
        name="build",
        provider=provider,
        session_id="mcp-demo",
        workdir="/tmp",
    )
    
    # 5. 使用 MCP 工具
    print("5. 执行任务...")
    result = await agent.run('使用 filesystem 工具列出 /tmp 目录的文件')
    print(f"\n结果:\n{result.content[:500]}")
    
    # 6. 清理
    print("\n6. 清理资源...")
    await MCPManager.close_all()
    await provider.close()
    
    print("完成!")

if __name__ == "__main__":
    asyncio.run(main())
```

## 注意事项

1. **安全性**: Local MCP 会启动本地进程，确保命令来源可信
2. **权限**: 文件系统 MCP 只能访问允许的目录
3. **超时**: 设置合理的 timeout 避免长时间等待
4. **认证**: Remote MCP 可能需要 OAuth 或 API Key
5. **资源清理**: 使用完毕后调用 `close_all()` 释放资源
"""
