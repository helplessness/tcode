"""随 Schema 发布的最小 JSON 示例目录。"""

from __future__ import annotations

from typing import Any

from .envelope import PROTOCOL_ID, PROTOCOL_VERSION


def protocol_event_examples() -> dict[str, dict[str, Any]]:
    common = {
        "protocol": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "event_id": "evt_example",
        "session_id": "session_example",
        "run_id": "run_example",
        "sequence": 1,
        "timestamp": "2026-09-07T12:00:00.000Z",
        "causation_id": "message_user",
        "metadata": {},
    }

    def event(event_type: str, **payload: Any) -> dict[str, Any]:
        return {**common, "type": event_type, **payload}

    activity = {
        "activity_id": "activity_1",
        "phase": "tool_progress",
        "status": "running",
        "message": "已处理 1/2 项",
        "current": 1,
        "total": 2,
        "unit": "items",
    }
    return {
        "run.started": event("run.started", agent_id="build"),
        "run.finished": event("run.finished", outcome="completed", result={}),
        "run.error": event("run.error", code="run_failed", message="执行失败"),
        "run.cancelled": event("run.cancelled", reason="用户取消"),
        "run.interrupted": event(
            "run.interrupted",
            reason="interaction_required",
            interaction_id="interaction_1",
        ),
        "message.started": event(
            "message.started", message_id="message_1", role="assistant"
        ),
        "message.delta": event(
            "message.delta", message_id="message_1", delta="你好", stream_epoch=1
        ),
        "message.finished": event(
            "message.finished",
            message={
                "message_id": "message_1",
                "role": "assistant",
                "content": "你好",
            },
        ),
        "tool.started": event(
            "tool.started",
            tool_call_id="tool_1",
            tool_name="read",
            arguments={"path": "README.md"},
        ),
        "tool.args": event("tool.args", tool_call_id="tool_1", delta='{"path":'),
        "tool.args_finished": event(
            "tool.args_finished",
            tool_call_id="tool_1",
            arguments={"path": "README.md"},
            arguments_digest="sha256:example",
        ),
        "tool.result": event(
            "tool.result",
            tool_call_id="tool_1",
            tool_name="read",
            result="content",
            state="completed",
        ),
        "activity.snapshot": event("activity.snapshot", **activity),
        "activity.delta": event("activity.delta", **{**activity, "current": 2}),
        "activity.finished": event(
            "activity.finished", **{**activity, "status": "completed", "current": 2}
        ),
        "interaction.requested": event(
            "interaction.requested",
            interaction_id="interaction_1",
            kind="question",
            prompt="是否继续？",
            payload={"options": ["继续", "停止"]},
        ),
        "interaction.resolved": event(
            "interaction.resolved",
            interaction_id="interaction_1",
            kind="question",
            resolution={"answer": "继续"},
        ),
        "interaction.cancelled": event(
            "interaction.cancelled",
            interaction_id="interaction_1",
            kind="question",
            reason="run_cancelled",
        ),
        "interaction.expired": event(
            "interaction.expired",
            interaction_id="interaction_1",
            kind="question",
            resolution={"answer": "默认值"},
        ),
        "subagent.started": event(
            "subagent.started", subagent_run_id="subrun_1", agent_id="researcher"
        ),
        "subagent.finished": event(
            "subagent.finished", subagent_run_id="subrun_1", result={"summary": "完成"}
        ),
        "subagent.error": event(
            "subagent.error", subagent_run_id="subrun_1", message="子任务失败"
        ),
        "state.snapshot": event("state.snapshot", state={"phase": "running"}),
        "state.delta": event(
            "state.delta",
            patch=[{"op": "replace", "path": "/phase", "value": "completed"}],
        ),
        "messages.snapshot": event(
            "messages.snapshot",
            messages=[
                {"message_id": "message_1", "role": "assistant", "content": "你好"}
            ],
        ),
    }


__all__ = ["protocol_event_examples"]
