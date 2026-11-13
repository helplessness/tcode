"""Complete OpenCode adapter composition entry point.

OpenCode-specific invocation and capabilities live beside this class in
``definition.py``.  ACP framing, process lifecycle, durable session binding,
handoff delivery and normalized event mapping are shared by
``ExternalHarnessBackend``.
"""

from __future__ import annotations

from tcode_agent.adapters.external_harness import ExternalHarnessBackend
from tcode_agent.external_harness.handoff_store import HarnessHandoffStore
from tcode_agent.external_harness.process_manager import HarnessProcessManager
from tcode_agent.external_harness.registry import HarnessRuntimeRegistry
from tcode_agent.external_harness.session_binding import HarnessSessionBindingStore
from tcode_agent.runtime.interaction_store import InteractionStore


class OpenCodeHarnessAdapter(ExternalHarnessBackend):
    """OpenCode ACP adapter registered as the ``opencode`` backend."""

    RUNTIME_ID = "opencode"

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


__all__ = ["OpenCodeHarnessAdapter"]
