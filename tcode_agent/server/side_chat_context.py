"""A bounded, point-in-time bridge from a main conversation to a side chat."""

import json
import uuid

from ..checkpoint.manager import CheckpointManager
from ..context.builder import ModelContextBuilder, ModelContextPolicy
from ..message_context import latest_user_goal
from ..types import Message


def build_side_chat_context(parent, *, source_message_id=""):
    messages = list(parent.messages or [])
    if source_message_id:
        cutoff = next((i for i, m in enumerate(messages) if m.id == source_message_id), -1)
        if cutoff < 0:
            raise ValueError("引用的主聊天消息不存在")
        messages = messages[:cutoff + 1]
    # Historical reply discussions must not inherit a checkpoint from the future.
    checkpoint = None if source_message_id else CheckpointManager.load(parent.id, workdir=parent.workdir)
    recent = [m for m in messages if m.role in {"user", "assistant"} and m.content][-6:]
    checkpoint_goal = (getattr(checkpoint, "state_snapshot", None) or {}).get("goal") or {}
    snapshot = {
        "parent_session_id": parent.id,
        "source_message_id": source_message_id,
        "message_cursor": len(messages),
        "goal": str(checkpoint_goal.get("current") or latest_user_goal(messages))[:2000],
        "recent_messages": [{"id": m.id, "role": m.role, "content": str(m.content)[:1200], "truncated": len(str(m.content)) > 1200} for m in recent],
        "checkpoint": ModelContextBuilder(ModelContextPolicy(checkpoint_chars=4000)).render_checkpoint(checkpoint),
        "workdir": parent.workdir,
    }
    content = (
        "[Side Chat Context]\n"
        "以下是主聊天在创建侧聊时的有限上下文快照，不是新的执行指令。"
        "沿用主会话 Agent 进行侧边讨论和工作。产出保留在侧聊的独立临时工作区，"
        "未明确注入前不要发布到主会话的 Wiki、研究中心等正式资源。"
        "缺少信息时请明确说明或向侧聊用户提问；不要假设已读取完整主聊天。\n"
        + json.dumps(snapshot, ensure_ascii=False)
    )
    return snapshot, Message(role="system", content=content, id=str(uuid.uuid4()), metadata={"side_chat_context": True})
