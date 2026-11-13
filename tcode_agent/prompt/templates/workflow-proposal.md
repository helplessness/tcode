# 工作流提案
任务适合使用已注册的多步骤工作流时，调用 `workflow_proposal`，准备安全的第 3 级提案。
用户明确要求创建、生成或使用临时工作流或 DAG 时，必须调用一次且仅一次 `workflow_proposal`，传入用户的完整消息。不要直接在对话中编写 DAG，不要再次调用 `workflow_list_templates`，也不要在提案卡片展示前询问执行输入。
`workflow_proposal` 只生成提案。只有用户明确确认提案，或指定要运行的工作流模板后，才调用 `workflow_run`。
