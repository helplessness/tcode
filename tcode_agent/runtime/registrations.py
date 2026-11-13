"""Instance-owned registration tokens shared by process-wide registries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable
from .workspace_registry import plugin_workspace, registry_workspace


@dataclass(frozen=True)
class RegistrationToken:
    """Stable identity for one contribution to a process-wide registry."""

    registry: str
    key: str
    source: str
    instance_id: str = ""
    registration_id: str = ""

    @classmethod
    def create(
        cls,
        *,
        registry: str,
        key: str,
        source: str = "",
        instance_id: str = "",
    ) -> "RegistrationToken":
        return cls(
            registry=str(registry or "").strip(),
            key=str(key or "").strip(),
            source=str(source or "").strip(),
            instance_id=str(instance_id or "").strip(),
            registration_id=uuid.uuid4().hex,
        )


class RegistrationDisposer:
    """Callable, idempotent handle that removes only its own registration."""

    def __init__(
        self,
        token: RegistrationToken,
        unregister: Callable[[RegistrationToken], bool],
    ) -> None:
        self.token = token
        self._unregister = unregister
        self._disposed = False
        self._workspace = plugin_workspace.get()

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __call__(self) -> bool:
        if self._disposed:
            return False
        with registry_workspace(self._workspace):
            removed = bool(self._unregister(self.token))
        self._disposed = True
        return removed
