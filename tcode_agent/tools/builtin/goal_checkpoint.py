from typing import Literal
from contextvars import ContextVar
from pydantic import Field
from ..base import ParameterSchema, Tool, ToolContext
from ...types import ToolResult

goal_checkpoint_active = ContextVar('goal_checkpoint_active', default=False)


class GoalCheckpointParameters(ParameterSchema):
    state: Literal['continue', 'wait', 'blocked', 'complete']
    progress: str = Field(min_length=1, max_length=3000)
    next_step: str = Field(default='', max_length=1500)


class GoalCheckpointTool(Tool):
    def available_to_agent(self, agent_name):
        return goal_checkpoint_active.get()
    @property
    def id(self): return 'goal_checkpoint'
    @property
    def description(self):
        return ('Report one goal-mode round checkpoint: progress, next_step, and state. '
                'A successful checkpoint ends the current round immediately; report it after finishing this round’s work. '
                'Use wait when only awaiting retained question answers or an existing async result; '
                'use blocked for an additional permission, capability or evidence blocker. '
                'Only available during an explicitly started goal-mode round. complete is a request '
                'for host verification, not proof of completion. Cannot start, resume, extend or stop a goal.')
    @property
    def parameters(self): return GoalCheckpointParameters
    async def execute(self, args, context: ToolContext):
        payload = GoalCheckpointParameters(**args).model_dump()
        callback = (context.extra or {}).get('__goal_checkpoint__')
        if not callable(callback):
            return ToolResult(title='目标进展', output='', error='当前不是目标模式运行，未修改任何目标')
        callback(payload)
        output = payload['progress']
        if payload['next_step']:
            output += '\n\n下一步：' + payload['next_step']
        if payload['state'] == 'complete':
            output += '\n\n已提交目标验收，是否完成由运行时校验决定。'
        return ToolResult(title='目标进展已记录', output=output,
                          metadata={'goal_checkpoint': payload, 'should_end_turn': True})
    @classmethod
    def register(cls):
        from ..registry import ToolRegistry
        ToolRegistry.register(cls())
