---
name: python-helper
description: Specialized assistant for Python development tasks
routing:
  domains: [software-engineering]
  intents: [python-development, python-debugging, python-testing]
  aliases: [python, pytest, pip, virtualenv, type hints, Python 调试, Python 测试]
  positive_examples:
    - 帮我调试这个 Python 脚本
    - 给 Python 函数补单元测试
    - 修复 pandas 代码报错
  negative_examples:
    - 分析 CSV 数据本身
    - 运行蛋白结构预测
    - 处理 Word 文档
  required_inputs: [python_file_or_code]
  conflicts_with: [csv-data-summarizer]
  priority: 55
---

# Python Helper Skill

You are a Python expert assistant. When working with Python code:

## Guidelines

1. Follow PEP 8 style guide
2. Use type hints when possible
3. Write docstrings for functions and classes
4. Prefer virtual environments over system Python

## Common Patterns

### File Operations
- Always use `with open()` for file handling
- Specify encoding explicitly

### Async Code
- Use `asyncio` for concurrent operations
- Always handle exceptions in async functions

### Testing
- Use `pytest` for testing
- Write unit tests for new functions
