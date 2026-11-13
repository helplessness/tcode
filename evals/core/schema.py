from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalTask:
    id: str
    category: str
    prompt: str
    input: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    metrics: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalTask":
        return cls(
            id=str(data.get("id") or "").strip(),
            category=str(data.get("category") or "general").strip(),
            prompt=str(data.get("prompt") or ""),
            input=dict(data.get("input") or {}),
            expected=dict(data.get("expected") or {}),
            metrics=[str(item) for item in data.get("metrics", [])],
            raw=dict(data),
        )

    def render_prompt(self) -> str:
        input_json = json.dumps(self.input, ensure_ascii=False, sort_keys=True)
        return self.prompt.replace("{{input_json}}", input_json).replace(
            "{input_json}",
            input_json,
        )


@dataclass
class AgentRunResult:
    final_output: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    subagent_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    stderr: str = ""
    returncode: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_output": self.final_output,
            "tool_calls": self.tool_calls,
            "subagent_calls": self.subagent_calls,
            "usage": self.usage,
            "artifacts": self.artifacts,
            "messages": self.messages,
            "duration_s": self.duration_s,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "error": self.error,
        }
