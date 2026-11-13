"""第三方 Python Harness Adapter 的公开 Provider 合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..runtime.agent_backend import AgentBackend
from ..runtime.interaction_store import InteractionStore
from .runtime_def import HarnessRuntimeDef


@dataclass(frozen=True)
class HarnessProviderContext:
    runtime_registry: Any
    interaction_store: InteractionStore


@runtime_checkable
class HarnessProvider(Protocol):
    def get_runtime_definition(self) -> HarnessRuntimeDef | dict[str, Any]: ...

    def create_backend(self, context: HarnessProviderContext) -> AgentBackend: ...


def instantiate_provider(value: Any) -> Any:
    if isinstance(value, type):
        return value()
    if callable(value) and not hasattr(value, "get_runtime_definition"):
        return value()
    return value


def provider_definition(provider: Any) -> HarnessRuntimeDef:
    getter = getattr(provider, "get_runtime_definition", None)
    if not callable(getter):
        raise TypeError("Harness provider must define get_runtime_definition()")
    value = getter()
    return (
        value
        if isinstance(value, HarnessRuntimeDef)
        else HarnessRuntimeDef.model_validate(value)
    )


__all__ = [
    "HarnessProvider",
    "HarnessProviderContext",
    "instantiate_provider",
    "provider_definition",
]
