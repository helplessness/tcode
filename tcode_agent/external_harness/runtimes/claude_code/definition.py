"""Claude Code stream-json runtime definition."""

from __future__ import annotations

from ...runtime_def import HarnessRuntimeDef


def runtime_definition() -> HarnessRuntimeDef:
    return HarnessRuntimeDef.model_validate(
        {
            "id": "claude-code",
            "displayName": "Claude Code",
            "release": "beta",
            "executables": {"primary": "claude"},
            "probes": {"authStrategy": "claude", "modelStrategy": "none"},
            "invocation": {
                "args": ["-p", "--output-format", "stream-json", "--verbose"],
                "promptMode": "stdin",
                "streamFormat": "claude-stream-json",
            },
            "session": {"strategy": "native", "resumeStrategy": "claude-session"},
            "capabilities": {
                "text": True,
                "images": True,
                "tools": True,
                "permissionAuthority": "runtime",
                "resume": False,
                "cancel": True,
                "guidanceMode": "queue_only",
            },
        }
    )


__all__ = ["runtime_definition"]
