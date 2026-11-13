"""Provider exposed through the tcode.harness_adapters entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from tcode_agent.adapters.external_harness import ExternalHarnessBackend
from tcode_agent.external_harness import HarnessProviderContext, HarnessRuntimeDef


class ExampleHarnessProvider:
    def get_runtime_definition(self) -> HarnessRuntimeDef:
        cli_path = Path(__file__).with_name("cli.py").resolve(strict=True)
        return HarnessRuntimeDef.model_validate(
            {
                "schema": "tcode.harness-runtime.v2",
                "id": "example-harness",
                "displayName": "Example Harness",
                "release": "experimental",
                "executables": {"primary": sys.executable},
                "probes": {
                    "versionArgs": ["--version"],
                    "authStrategy": "none",
                    "modelStrategy": "none",
                },
                "invocation": {
                    "args": [cli_path.as_posix()],
                    "promptMode": "stdin",
                    "streamFormat": "ndjson",
                },
                "session": {
                    "strategy": "none",
                    "resumeStrategy": "none",
                },
                "capabilities": {
                    "text": True,
                    "images": False,
                    "tools": False,
                    "permissionAuthority": "none",
                    "resume": False,
                    "cancel": True,
                    "guidanceMode": "queue_only",
                },
                "security": {
                    "workspaceMode": "configured",
                    "inheritEnvironment": "allowlist",
                    "publicServer": False,
                },
                "compatibility": {
                    "adapterApiVersion": "1",
                    "tcodeVersion": ">=0.0.1,<1",
                },
            }
        )

    def create_backend(
        self, context: HarnessProviderContext
    ) -> ExternalHarnessBackend:
        return ExternalHarnessBackend(
            runtime_id="example-harness",
            registry=context.runtime_registry,
            interaction_store=context.interaction_store,
        )

    def create_fixture_backend(
        self, context: HarnessProviderContext
    ) -> ExternalHarnessBackend:
        return self.create_backend(context)


provider = ExampleHarnessProvider()


__all__ = ["ExampleHarnessProvider", "provider"]
