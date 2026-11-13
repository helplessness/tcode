# Question 工具 - 用户交互

## 功能

Question 工具允许 Agent 向用户提问并获取回答，支持三种类型：

1. **custom** - 自由文本输入（默认）
2. **single** - 单选
3. **multiple** - 多选

一次需要确认多项独立信息时，可通过 `questions` 批量提问，数量不设上限。Web/桌面端会在滚动区域中同时展示并统一提交；原有单问题参数继续兼容。

目标模式会把完整问题组（包括每题 id、正文、类型和选项）保存到会话。重新加载会话后可继续统一回答；后端逐题校验，必填题未答或封闭选项无效时整组不提交。后续轮次会读取问题及答案；相同标题但题目或选项变化时会创建新问题组。问题组不会因外层偏好设置而超时默认。旧版本已经遗漏内部题目的记录无法还原，需要让 Agent 重新提出完整问题组。

Web/桌面端会将同一会话当前待答的多个独立请求合成一个分页表单。翻页、选择和输入只更新表单草稿；点击“提交全部回答”才发送一个包含 `answers: {request_id: answer}` 的请求。普通聊天会先校验整组再同步唤醒全部 Future，旧的逐题回答接口无法绕过必填题检查；刷新后的状态接口返回全部存活请求。目标模式会在同一次会话更新中校验并保存整组答案，只生成一次继续指导。可选题允许留空；新出现的必填请求会使不完整的旧提交被拒绝，用户补齐后再提交。

这项合组针对已经发出的待答问题。若 Agent 在收到答案后才生成下一道问题，它属于下一轮澄清；模型应将同一轮已知问题放在一次 `questions` 调用中。普通聊天的多请求问题组超时后不会用部分默认答案恢复执行，而是全部返回未回答的等待检查点；目标模式仍按持久化问题和预算恢复。

填写时可前后翻页修改。选项显示高亮、勾选和当前答案；自定义输入有独立选中状态。已有自定义文字只是草稿，点击选项后以选项为最终答案；切回自定义输入可继续编辑保留的文字。真实 Electron 交互回归命令：`desktop/node_modules/.bin/electron desktop/scripts/smoke-question-ui.cjs`。

## 使用方式

### 在 Agent 中使用

```python
from tcode_agent import Agent
from tcode_agent.providers.ollama import OllamaProvider

provider = OllamaProvider()
agent = Agent.create(
    name='build',
    provider=provider,
    session_id='test',
)

# Agent 会自动使用 question 工具
result = await agent.run('请问我一个问题，比如喜欢的编程语言')
```

### 直接使用工具

```python
from tcode_agent.tools.builtin.question import QuestionTool

tool = QuestionTool()

# 自定义问题
result = await tool.execute({
    'question': '你叫什么名字？',
    'type': 'custom',
}, context)

# 单选问题
result = await tool.execute({
    'question': '你喜欢的编辑器是？',
    'type': 'single',
    'options': ['VS Code', 'Vim', 'Emacs', 'PyCharm'],
    'allow_custom': True,
}, context)

# 多选问题
result = await tool.execute({
    'question': '你需要哪些功能？',
    'type': 'multiple',
    'options': ['代码补全', 'Linting', '调试', 'Git 集成'],
    'allow_custom': True,
}, context)

# 一次询问多个独立问题
result = await tool.execute({
    'question': '发布前确认',
    'questions': [
        {'id': 'platform', 'header': '平台', 'question': '目标平台？',
         'type': 'single', 'options': ['macOS', 'Windows'], 'allow_custom': False},
        {'id': 'features', 'header': '功能', 'question': '需要哪些功能？',
         'type': 'multiple', 'options': ['文件预览', '远程 MCP']},
    ],
}, context)
```

## 参数说明

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| question | str | 要问的问题 | 必填 |
| type | str | 问题类型：custom/single/multiple | custom |
| options | list[str] | 选项列表（单选/多选需要） | None |
| allow_custom | bool | 是否允许自定义回答 | True |
| questions | list | 多个独立问题；每项包含唯一 `id` | None |

## 返回结果

```python
{
    "type": "single_choice",  # 或 multiple_choice/custom
    "answer": "用户回答",
    "selected_option": "选中的选项",
    "selected_index": 0,  # 选项索引
    "formatted": "格式化后的回答"
}
```

## 示例输出

### 自定义问题
```
============================================================
❓ 你叫什么名字？

Type your answer below:
============================================================

Your answer: 张三
```

### 单选问题
```
============================================================
❓ 你喜欢的编辑器是？

Options (select one):
  1. VS Code
  2. Vim
  3. Emacs
  4. PyCharm
  Or type your own answer

Enter the number of your choice, or type your answer
============================================================

Your answer: 1
```

### 多选问题
```
============================================================
❓ 你需要哪些功能？

Options (select multiple, comma-separated):
  1. 代码补全
  2. Linting
  3. 调试
  4. Git 集成

Example: 1,3,5 or type your own answer
============================================================

Your answer: 1,3
```

## 注意事项

1. **阻塞输入**: question 工具使用 `input()` 会阻塞等待用户输入
2. **异步兼容**: 内部使用 `run_in_executor` 避免阻塞事件循环
3. **取消**: 按 Ctrl+C 可以取消问题
4. **空回答**: 直接回车会返回空回答

## 运行示例

```bash
# 运行交互式测试
python tcode_agent/test/test_question.py
```

如果只想快速体验，直接复用上面的 `直接使用工具` 代码片段即可，不再单独维护额外的 example 脚本。

## 与原始 tcode 对比

| 功能 | 原始 tcode | 我们的实现 |
|------|--------------|-----------|
| 自定义问题 | ✓ | ✓ |
| 单选 | ✓ | ✓ |
| 多选 | ✓ | ✓ |
| 允许自定义答案 | ✓ | ✓ |
| 前端集成 | ✓ (React) | ✓ (CLI input) |
| 回调系统 | ✓ | 可选 |

我们的实现使用 Python 的 `input()` 适合 CLI 环境，原始版本使用 React 前端。
