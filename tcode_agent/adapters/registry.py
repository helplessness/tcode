"""AgentBackend 注册表和第三方 entry point 加载。"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from ..runtime.agent_backend import AgentBackend


class AgentBackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, AgentBackend] = {}
        self._entry_points_loaded = False
        self._entry_point_errors: list[str] = []

    def register(self, backend: AgentBackend, *, replace: bool = False) -> None:
        backend_id = str(backend.backend_id or "").strip()
        if not backend_id:
            raise ValueError("backend_id is required")
        if backend_id in self._backends and not replace:
            raise ValueError(f"duplicate backend id: {backend_id}")
        self._backends[backend_id] = backend

    def get(self, backend_id: str) -> AgentBackend | None:
        self.load_entry_points()
        return self._backends.get(str(backend_id or "").strip())

    def list(self) -> list[AgentBackend]:
        self.load_entry_points()
        return [self._backends[key] for key in sorted(self._backends)]

    def contains(self, backend_id: str) -> bool:
        return str(backend_id or "").strip() in self._backends

    @property
    def entry_point_errors(self) -> tuple[str, ...]:
        return tuple(self._entry_point_errors)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self._entry_points_loaded = True
        try:
            points = importlib.metadata.entry_points(group="tcode.agent_backends")
        except TypeError:
            points = importlib.metadata.entry_points().get("tcode.agent_backends", [])
        except Exception:
            return
        for point in points:
            try:
                loaded: Any = point.load()
                backend = loaded() if isinstance(loaded, type) else loaded
                self.register(backend)
            except Exception as exc:
                self._entry_point_errors.append(
                    f"{getattr(point, 'name', '<unknown>')}: {str(exc)[:500]}"
                )


backend_registry = AgentBackendRegistry()


__all__ = ["AgentBackendRegistry", "backend_registry"]
