"""Complete Claude Code adapter composition entry point.

Claude-specific invocation and capabilities live in ``definition.py``.  The
controlled process lifecycle, handoff delivery and stream-json event mapping
are provided by ``ExternalHarnessBackend`` and its shared drivers.
"""

from __future__ import annotations

from tcode_agent.adapters.external_harness import ExternalHarnessBackend
from tcode_agent.external_harness.handoff_store import HarnessHandoffStore
from tcode_agent.external_harness.process_manager import HarnessProcessManager
from tcode_agent.external_harness.registry import HarnessRuntimeRegistry
from tcode_agent.external_harness.session_binding import HarnessSessionBindingStore
from tcode_agent.runtime.interaction_store import InteractionStore


class ClaudeCodeHarnessAdapter(ExternalHarnessBackend):
    """Claude stream-json adapter registered as the ``claude-code`` backend."""

    RUNTIME_ID = "claude-code"

    def __init__(
        self,
        *,
        registry: HarnessRuntimeRegistry,
        interaction_store: InteractionStore,
        process_manager: HarnessProcessManager | None = None,
        binding_store: HarnessSessionBindingStore | None = None,
        handoff_store: HarnessHandoffStore | None = None,
    ) -> None:
        super().__init__(
            runtime_id=self.RUNTIME_ID,
            registry=registry,
            interaction_store=interaction_store,
            process_manager=process_manager,
            binding_store=binding_store,
            handoff_store=handoff_store,
        )


__all__ = ["ClaudeCodeHarnessAdapter"]
