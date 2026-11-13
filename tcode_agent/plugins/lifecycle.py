"""Instance lifecycle and health primitives for the experimental Plugin API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PluginInstanceState(str, Enum):
    DISCOVERED = "discovered"
    VALIDATING = "validating"
    RESOLVED = "resolved"
    LOADING = "loading"
    STARTING = "starting"
    WARMING = "warming"
    ACTIVE = "active"
    DEGRADED = "degraded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    DRAINING = "draining"
    STOPPED = "stopped"
    UNLOADED = "unloaded"


class PluginHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    NOT_DECLARED = "not_declared"


def _safe_health_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 32:
                output["<truncated>"] = True
                break
            key = str(raw_key)[:100]
            lowered = key.lower()
            if any(token in lowered for token in ("secret", "token", "password", "authorization")):
                output[key] = "<redacted>"
            else:
                output[key] = _safe_health_value(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe_health_value(item, depth=depth + 1)
            for item in list(value)[:32]
        ]
    return f"<{type(value).__name__}>"


@dataclass(frozen=True, slots=True)
class PluginHealth:
    status: PluginHealthStatus
    summary: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "details": dict(self.details),
            "checked_at": self.checked_at,
        }


def normalize_plugin_health(value: Any) -> PluginHealth:
    if isinstance(value, PluginHealth):
        return PluginHealth(
            value.status,
            str(value.summary or "")[:500],
            _safe_health_value(value.details),
        )
    if value is None or value is True:
        return PluginHealth(PluginHealthStatus.HEALTHY, "plugin health check passed")
    if value is False:
        return PluginHealth(PluginHealthStatus.UNHEALTHY, "plugin health check failed")
    if not isinstance(value, Mapping) and all(
        hasattr(value, field) for field in ("status", "summary", "details")
    ):
        value = {
            "status": getattr(value, "status"),
            "summary": getattr(value, "summary"),
            "details": getattr(value, "details"),
        }
    if not isinstance(value, Mapping):
        raise TypeError("health() must return PluginHealth, mapping, bool, or None")
    raw_status = value.get("status")
    raw_status = getattr(raw_status, "value", raw_status)
    try:
        status = PluginHealthStatus(str(raw_status or "").strip().lower())
    except ValueError as exc:
        raise ValueError(
            "health status must be healthy, degraded, unhealthy, or not_declared"
        ) from exc
    summary = str(value.get("summary") or value.get("message") or "")[:500]
    details = value.get("details") or {}
    if not isinstance(details, Mapping):
        raise TypeError("health details must be a mapping")
    return PluginHealth(status, summary, _safe_health_value(details))


@dataclass(frozen=True, slots=True)
class PluginStateTransition:
    state: PluginInstanceState
    at: str = field(default_factory=_utc_now)
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state.value, "at": self.at, "reason": self.reason}


@dataclass(slots=True)
class PluginInstanceStatus:
    name: str
    instance_id: str
    state: PluginInstanceState = PluginInstanceState.DISCOVERED
    history: list[PluginStateTransition] = field(default_factory=list)
    health: PluginHealth = field(
        default_factory=lambda: PluginHealth(
            PluginHealthStatus.NOT_DECLARED,
            "plugin does not declare a health callback",
        )
    )
    last_error: str = ""

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(PluginStateTransition(self.state))

    def transition(
        self,
        state: PluginInstanceState,
        *,
        reason: str = "",
        error: Optional[BaseException | str] = None,
    ) -> None:
        if self.state is state and not reason and error is None:
            return
        self.state = state
        if error is not None:
            self.last_error = str(error)
        self.history.append(PluginStateTransition(state, reason=str(reason or "")))
        if len(self.history) > 64:
            self.history[:] = [self.history[0], *self.history[-63:]]

    def set_health(self, health: PluginHealth) -> None:
        self.health = health

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "instance_id": self.instance_id,
            "state": self.state.value,
            "history": [item.to_dict() for item in self.history],
            "health": self.health.to_dict(),
            "last_error": self.last_error,
        }


__all__ = [
    "PluginHealth",
    "PluginHealthStatus",
    "PluginInstanceState",
    "PluginInstanceStatus",
    "PluginStateTransition",
    "normalize_plugin_health",
]
