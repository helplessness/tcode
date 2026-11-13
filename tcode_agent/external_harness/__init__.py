"""声明式外部 Agent harness runtime。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .provider import HarnessProvider, HarnessProviderContext
from .runtime_def import (
    HarnessCompatibility,
    HarnessRuntimeDef,
    HarnessRuntimeRecord,
    RuntimeProbeResult,
)

if TYPE_CHECKING:
    from .registry import HarnessRuntimeRegistry


def __getattr__(name: str) -> Any:
    """延迟导入 Registry，避免 Provider 在发现阶段形成循环导入。"""

    if name in {"HarnessRuntimeRegistry", "runtime_registry"}:
        from .registry import HarnessRuntimeRegistry, runtime_registry

        return {
            "HarnessRuntimeRegistry": HarnessRuntimeRegistry,
            "runtime_registry": runtime_registry,
        }[name]
    raise AttributeError(name)

__all__ = [
    "HarnessCompatibility",
    "HarnessProvider",
    "HarnessProviderContext",
    "HarnessRuntimeDef",
    "HarnessRuntimeRecord",
    "HarnessRuntimeRegistry",
    "RuntimeProbeResult",
    "runtime_registry",
]
