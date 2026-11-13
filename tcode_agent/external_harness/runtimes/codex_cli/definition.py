"""Codex CLI JSONL runtime definition."""

from __future__ import annotations

from ...runtime_def import HarnessRuntimeDef


def runtime_definition() -> HarnessRuntimeDef:
    return HarnessRuntimeDef.model_validate(
        {
            "id": "codex-cli",
            "displayName": "Codex CLI",
            "release": "beta",
            "executables": {"primary": "codex"},
            "probes": {"authStrategy": "codex", "modelStrategy": "none"},
            "invocation": {
                "args": [
                    "exec",
                    "--json",
                    "--ephemeral",
                    "-C",
                    "${TCODE_RESOLVED_WORKSPACE}",
                    "-",
                ],
                "promptMode": "stdin",
                "streamFormat": "codex-jsonl",
            },
            "session": {"strategy": "native", "resumeStrategy": "codex-session"},
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
