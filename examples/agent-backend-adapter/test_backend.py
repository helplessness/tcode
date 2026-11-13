from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tcode_agent.adapters.conformance import collect_backend_events
from tcode_agent.runtime.agent_backend import NormalizedMessage, NormalizedRunInput, RunContext

from tcode_sample_backend import SampleAgentBackend


class SampleBackendContractTests(unittest.TestCase):
    def test_backend_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_input = NormalizedRunInput(
                thread_id="thread-1",
                run_id="run-1",
                runtime_id="sample-agent",
                messages=[NormalizedMessage(id="user-1", role="user", content="hello")],
            )
            context = RunContext(
                tenant_id="tenant",
                user_id="user",
                workspace_id="workspace",
                workdir=str(Path(temporary)),
                session_id="thread-1",
                run_id="run-1",
                agent_id="default",
                runtime_id="sample-agent",
                request_id="request-1",
            )
            events = asyncio.run(
                collect_backend_events(SampleAgentBackend(), run_input, context)
            )
        self.assertEqual(
            ["message.started", "message.delta", "message.finished"],
            [event["type"] for event in events],
        )
        self.assertEqual(
            "Sample backend received: hello", events[-1]["message"]["content"]
        )


if __name__ == "__main__":
    unittest.main()
