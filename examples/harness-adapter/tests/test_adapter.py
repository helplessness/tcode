from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from example_harness.provider import provider
from tcode_agent.external_harness.provider import HarnessProviderContext
from tcode_agent.external_harness.registry import HarnessRuntimeRegistry
from tcode_agent.external_harness.trust_store import HarnessTrustStore
from tcode_agent.runtime.agent_backend import (
    NormalizedMessage,
    NormalizedRunInput,
    RunContext,
)
from tcode_agent.runtime.interaction_store import InteractionStore


class ExampleHarnessAdapterTests(unittest.TestCase):
    def test_definition_matches_entry_point_name(self) -> None:
        definition = provider.get_runtime_definition()
        self.assertEqual("example-harness", definition.id)
        self.assertEqual("tcode.harness-runtime.v2", definition.schema_id)

    def test_backend_runs_as_external_process(self) -> None:
        async def exercise() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                trust_store = HarnessTrustStore(Path(tmp) / "settings.json")
                registry = HarnessRuntimeRegistry(
                    discover_external=False,
                    trust_store=trust_store,
                )
                definition = provider.get_runtime_definition()
                registry.register(
                    definition,
                    source_type="entrypoint",
                    source_name="example",
                    trust_state="trusted",
                )
                trust_store.update(runtime_id=definition.id, enabled=True)
                store = InteractionStore(Path(tmp) / "runtime.sqlite")
                backend = provider.create_backend(
                    HarnessProviderContext(
                        runtime_registry=registry,
                        interaction_store=store,
                    )
                )
                run_input = NormalizedRunInput(
                    thread_id="thread",
                    run_id="run",
                    runtime_id=definition.id,
                    messages=[
                        NormalizedMessage(
                            id="user",
                            role="user",
                            content="UNICODE-你好-🧪",
                        )
                    ],
                )
                context = RunContext(
                    tenant_id="tenant",
                    user_id="user",
                    workspace_id="workspace",
                    workdir=tmp,
                    session_id="thread",
                    run_id="run",
                    agent_id="default",
                    runtime_id=definition.id,
                    request_id="request",
                )
                return [
                    event.model_dump_json()
                    async for event in backend.run(run_input, context)
                ]

        events = asyncio.run(exercise())
        self.assertTrue(any("UNICODE-你好-🧪" in item for item in events))


if __name__ == "__main__":
    unittest.main()
