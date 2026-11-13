"""OpenCode ACP runtime definition."""

from __future__ import annotations

from ...runtime_def import HarnessRuntimeDef


def runtime_definition() -> HarnessRuntimeDef:
    return HarnessRuntimeDef.model_validate(
        {
            "id": "opencode",
            "displayName": "OpenCode",
            "release": "beta",
            "executables": {"primary": "opencode"},
            "probes": {
                "versionArgs": ["--version"],
                "authStrategy": "opencode-acp",
                "modelStrategy": "opencode-models",
                "timeoutSeconds": 5,
            },
            "invocation": {
                # Workspace is set by the shared process cwd and ACP session
                # params. CLI-only convenience flags aren't an ACP contract.
                "args": ["acp"],
                "promptMode": "acp",
                "streamFormat": "acp-json-rpc",
            },
            "session": {"strategy": "acp", "resumeStrategy": "acp-load"},
            "capabilities": {
                "text": True,
                "images": True,
                "tools": True,
                "permissionAuthority": "shared",
                "resume": True,
                "cancel": True,
                "mcpInjection": "acp",
                "negotiated": True,
                "guidanceMode": "restart_turn",
            },
            "security": {"publicServer": True},
        }
    )


__all__ = ["runtime_definition"]
