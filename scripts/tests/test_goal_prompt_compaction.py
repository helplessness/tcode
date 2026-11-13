import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tcode_agent.harness.prompt_builder import AgentPromptBuilder
from tcode_agent.providers.base import ChatMessage
from tcode_agent.tools.builtin.goal_checkpoint import goal_checkpoint_active


def test_goal_anchor_restores_exact_user_instructions_only_for_active_goal():
    objective = 'Compare the original records. Preserve the acceptance criteria and all constraints.'
    builder = AgentPromptBuilder(SimpleNamespace(_goal_tool_routing_prompt=objective))
    summary = ChatMessage(role='assistant', content='Summary: compare records.')
    assert not builder._missing_goal_anchor([summary])

    token = goal_checkpoint_active.set(True)
    try:
        anchor = builder._missing_goal_anchor([summary])
        assert [(msg.role, msg.content) for msg in anchor] == [('user', objective)]
        assert not builder._missing_goal_anchor([*anchor, summary])
        assert not AgentPromptBuilder(SimpleNamespace())._missing_goal_anchor([summary])
    finally:
        goal_checkpoint_active.reset(token)


def test_input_guard_retry_keeps_goal_even_outside_recent_tail(monkeypatch):
    objective = 'Goal instructions with required acceptance criteria. ' * 120
    builder = AgentPromptBuilder(SimpleNamespace(
        _goal_tool_routing_prompt=objective,
        agent_info=SimpleNamespace(temperature=0),
        _reasoning_effort=None,
    ))
    provider_chat = AsyncMock()
    monkeypatch.setattr(builder, '_provider_chat', provider_chat)
    monkeypatch.setattr(builder, '_configured_max_output_tokens', lambda: 0)
    monkeypatch.setattr(builder, '_append_runtime_system_notice', AsyncMock())
    history = [ChatMessage(role='user', content=objective)] + [
        ChatMessage(role='assistant', content='Recent context ' * 200) for _ in range(20)
    ]

    token = goal_checkpoint_active.set(True)
    try:
        asyncio.run(builder._call_degraded_llm(messages_for_api=history, max_input_chars=20_000))
        request = provider_chat.call_args.kwargs
        assert request['tools'] is None
        assert [msg.content for msg in request['messages'] if msg.role == 'user'] == [objective]
        assert sum(len(msg.content) for msg in request['messages']) < 20_000
        with pytest.raises(RuntimeError, match='cannot continue with partial instructions'):
            asyncio.run(builder._call_degraded_llm(messages_for_api=history, max_input_chars=5_000))
        provider_chat.assert_awaited_once()
    finally:
        goal_checkpoint_active.reset(token)
