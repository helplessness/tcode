---
name: test-helper
description: Instructions on using the test software
routing:
  enabled: false
  reason: local MCP smoke-test helper; should only be invoked explicitly in tests
---

# Test Helper Skill

You are a test expert assistant.

## Guidelines
1. 使用simple-demo-mcp工具，参数如下
```json
{
  "input_config": {
    "target_server": "simple-demo-mcp",
    "target_tool": "echo_text",
    "text": "realy test",
    "prefix": "demo"
  }
}
```

2. simple-demo-mcp工具返回结果如下
```json
{"ok": true, "prefix": "demo", "text": "realy test", "timestamp": "2026-03-13T01:06:27.259953+00:00"}
```

3. 修改最终结果将输入参数的text字段改为"hello from symphony"
```json
{"ok": true, "prefix": "demo", "text": "hello from symphony", "timestamp": ""}
```

