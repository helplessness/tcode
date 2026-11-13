"""把 RuntimeDef 驱动的本地 Agent CLI 暴露为 AgentBackend。"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from ..external_harness.process_manager import HarnessProcessManager
from ..external_harness.handoff_store import HarnessHandoffStore
from ..external_harness.session_binding import HarnessSessionBindingStore
from ..external_harness.transports.acp_stdio import AcpStdioDriver
from ..external_harness.transports.process_jsonl import ProcessJsonlDriver
from ..protocol.v2 import RuntimeEventValue, parse_runtime_event
from ..runtime.agent_backend import NormalizedRunInput, RunContext
from ..runtime.interaction_store import InteractionStore

if TYPE_CHECKING:
    from ..external_harness.registry import HarnessRuntimeRegistry


class ExternalHarnessBackend:
    """一个实例对应一个外部 runtime id。"""

    def __init__(
        self,
        *,
        runtime_id: str,
        registry: "HarnessRuntimeRegistry",
        interaction_store: InteractionStore,
        process_manager: HarnessProcessManager | None = None,
        binding_store: HarnessSessionBindingStore | None = None,
        handoff_store: HarnessHandoffStore | None = None,
    ) -> None:
        self.backend_id = runtime_id
        self.registry = registry
        self.interaction_store = interaction_store
        self.process_manager = process_manager or HarnessProcessManager()
        self.binding_store = binding_store or HarnessSessionBindingStore()
        self.handoff_store = handoff_store or HarnessHandoffStore()
        self.acp = AcpStdioDriver(
            process_manager=self.process_manager,
            interaction_store=self.interaction_store,
            binding_store=self.binding_store,
            handoff_store=self.handoff_store,
        )
        self.jsonl = ProcessJsonlDriver(
            process_manager=self.process_manager,
            handoff_store=self.handoff_store,
        )

    async def get_capabilities(self) -> dict[str, object]:
        definition = self.registry.get(self.backend_id)
        if definition is None:
            return {"available": False}
        effective = self.registry.effective_capabilities(self.backend_id)
        from ..runtime.platform_capabilities.policy import (
            runtime_platform_capability_summary,
        )

        return effective | {
            "release": definition.release,
            "platformBridge": runtime_platform_capability_summary(
                self.backend_id, effective
            ),
        }

    @staticmethod
    def _should_forward_event(event: RuntimeEventValue) -> bool:
        """Keep external harness private reasoning out of the public timeline.

        ACP/JSONL runtimes do not share TCode's public-commentary contract.
        Their activity deltas can be token-sized private thought fragments, so
        only normalized messages, tools, interactions and terminal events are
        forwarded. Tool lifecycle events still provide an accurate process
        summary without publishing unstable reasoning text.
        """

        return not str(getattr(event, "type", "") or "").startswith("activity.")

    @classmethod
    def _public_event(cls, event: RuntimeEventValue) -> RuntimeEventValue:
        """Hide private activity text without creating a sequence gap.

        Runtime event sequences are contiguous.  Dropping an ACP activity
        event outright makes the coordinator reject the next public event.
        An empty state delta preserves the sequence position while producing
        no legacy UI event and no visible process note.
        """

        if cls._should_forward_event(event):
            return event
        return parse_runtime_event(
            {
                "protocol": event.protocol,
                "protocol_version": event.protocol_version,
                "type": "state.delta",
                "event_id": event.event_id,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "sequence": event.sequence,
                "timestamp": event.timestamp,
                "causation_id": event.causation_id,
                "metadata": event.metadata,
                "patch": [],
            }
        )

    async def run(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]:
        definition = self.registry.get(self.backend_id)
        if definition is None:
            raise RuntimeError(f"runtime is not registered: {self.backend_id}")
        forwarded = run_input.forwarded_props.get("tcode", {})
        public_server = bool(
            isinstance(forwarded, dict) and forwarded.get("publicServer", False)
        )
        allowed, reason = self.registry.allowed_for_request(
            self.backend_id, public_server=public_server
        )
        if not allowed:
            raise RuntimeError(reason)
        executable = ""
        for candidate in [
            definition.executables.primary,
            *definition.executables.fallbacks,
        ]:
            found = shutil.which(candidate)
            if found:
                executable = Path(found).resolve(strict=True).as_posix()
                break
        if not executable:
            raise RuntimeError(
                f"harness executable is unavailable: {definition.executables.primary}"
            )
        if definition.invocation.stream_format == "acp-json-rpc":
            async for event in self.acp.run(
                definition, executable, run_input, context
            ):
                yield self._public_event(event)
        else:
            async for event in self.jsonl.run(
                definition, executable, run_input, context
            ):
                yield self._public_event(event)

    async def cancel(self, run_id: str, context: RunContext) -> None:
        context.cancelled.set()
        self.interaction_store.cancel_pending_for_run(
            owner_key=f"{context.tenant_id}:{context.user_id}",
            origin_run_id=run_id,
        )
        await self.process_manager.cancel(run_id)


__all__ = ["ExternalHarnessBackend"]
