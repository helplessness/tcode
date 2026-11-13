"""AG-UI、TCode 原生与外部 Harness 共用的运行时对象。"""

from __future__ import annotations

from ..adapters.direct_llm import DirectEchoBackend
from ..adapters.external_harness import ExternalHarnessBackend
from ..adapters.native import TCodeNativeBackend
from ..adapters.registry import AgentBackendRegistry
from ..external_harness.registry import runtime_registry
from ..external_harness.runtimes import builtin_adapter_types
from ..external_harness.provider import HarnessProviderContext
from ..runtime.interaction_store import InteractionStore
from ..runtime.run_coordinator import RunCoordinator


interaction_store = InteractionStore()
agent_backend_registry = AgentBackendRegistry()
agent_backend_registry.register(
    TCodeNativeBackend(interaction_store=interaction_store)
)
agent_backend_registry.register(DirectEchoBackend())
for _adapter_type in builtin_adapter_types():
    agent_backend_registry.register(
        _adapter_type(
            registry=runtime_registry,
            interaction_store=interaction_store,
        )
    )

_builtin_runtime_ids = {
    adapter_type.RUNTIME_ID for adapter_type in builtin_adapter_types()
}
for _runtime_definition in runtime_registry.definitions():
    if _runtime_definition.id in _builtin_runtime_ids:
        continue
    try:
        _provider = runtime_registry.provider_for(_runtime_definition.id)
        _factory = getattr(_provider, "create_backend", None)
        if callable(_factory):
            _backend = _factory(
                HarnessProviderContext(
                    runtime_registry=runtime_registry,
                    interaction_store=interaction_store,
                )
            )
        else:
            _backend = ExternalHarnessBackend(
                runtime_id=_runtime_definition.id,
                registry=runtime_registry,
                interaction_store=interaction_store,
            )
        if str(getattr(_backend, "backend_id", "") or "") != _runtime_definition.id:
            raise ValueError("provider backend_id does not match runtime definition")
        agent_backend_registry.register(_backend)
    except Exception as _exc:
        runtime_registry.record_backend_error(_runtime_definition.id, _exc)

run_coordinator = RunCoordinator(agent_backend_registry)


async def guidance_mode_for_runtime(runtime_id: str) -> str:
    """Return the platform guidance strategy declared by an AgentBackend.

    RuntimeDef-backed CLI adapters and third-party ``tcode.agent_backends``
    entry points are intentionally resolved through the same Backend contract.
    An absent or invalid declaration is treated as ``queue_only`` so adding a
    Harness cannot accidentally gain turn-cancellation semantics.
    """

    normalized = str(runtime_id or "").strip() or "tcode-native"
    backend = agent_backend_registry.get(normalized)
    if backend is None:
        return "queue_only"
    try:
        capabilities = await backend.get_capabilities()
    except Exception:
        return "queue_only"
    if not isinstance(capabilities, dict):
        return "queue_only"
    mode = str(
        capabilities.get("guidanceMode")
        or capabilities.get("guidance_mode")
        or ""
    ).strip()
    if mode in {"in_turn", "restart_turn", "queue_only", "unsupported"}:
        return mode
    return "queue_only"


__all__ = [
    "agent_backend_registry",
    "guidance_mode_for_runtime",
    "interaction_store",
    "run_coordinator",
    "runtime_registry",
]
