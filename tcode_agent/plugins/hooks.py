"""Hook registry for plugin lifecycle and execution events."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


HookHandlerResult = dict[str, Any] | None
HookHandler = Callable[
    [dict[str, Any], dict[str, Any]],
    Awaitable[HookHandlerResult] | HookHandlerResult,
]


class HookDispatchMode(str, Enum):
    """Explicit hook execution strategies.

    ``serial`` is the legacy Tcode behavior: handlers run by priority, await
    completion, share one output payload, and never stop the remaining chain.
    ``parallel`` is reserved for observer-style notifications: handlers run
    concurrently against isolated payload copies and only their errors are
    merged into the returned output.
    """

    SERIAL = "serial"
    PARALLEL = "parallel"


class HookCategory(str, Enum):
    PLUGIN = "plugin"
    CONFIG = "config"
    SYSTEM = "system"
    AGENT = "agent"
    SESSION = "session"
    MEMORY = "memory"
    MESSAGE = "message"
    TOOL = "tool"
    MCP = "mcp"
    TASK = "task"


class HookEvent(str, Enum):
    """Stable hook names exposed to plugins.

    Plugins may still register custom dotted event names. This catalog defines
    the runtime-owned contract and prevents event names from drifting between
    emitters, documentation, and the management UI.
    """

    PLUGIN_INIT = "plugin.init"
    PLUGIN_INIT_COMPLETE = "plugin.init.complete"
    PLUGIN_SHUTDOWN = "plugin.shutdown"
    PLUGIN_SHUTDOWN_COMPLETE = "plugin.shutdown.complete"
    PLUGIN_ERROR = "plugin.error"
    CONFIG_LOADED = "config.loaded"
    CONFIG_RELOAD = "config.reload"
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    MEMORY_LOW = "system.memory.low"
    DISK_LOW = "system.disk.low"

    AGENT_CREATE = "agent.create"
    AGENT_CREATE_COMPLETE = "agent.create.complete"
    AGENT_START = "agent.start"
    AGENT_STOP = "agent.stop"
    AGENT_PAUSE = "agent.pause"
    AGENT_RESUME = "agent.resume"
    AGENT_ERROR = "agent.error"
    AGENT_THINKING = "agent.thinking"
    AGENT_RESPONSE = "agent.response"

    SESSION_BOOTSTRAP = "session.bootstrap"
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_SAVE = "session.save"
    SESSION_LOAD = "session.load"
    SESSION_COMPACTION_BEFORE = "session.compaction.before"
    SESSION_COMPACTION_AFTER = "session.compaction.after"
    SESSION_COMPACTION_ERROR = "session.compaction.error"
    SESSION_DELETE_BEFORE = "session.delete.before"
    SESSION_DELETE_AFTER = "session.delete.after"
    SESSION_DELETE_ERROR = "session.delete.error"
    # Deprecated compatibility event. New plugins must use session.delete.before.
    SESSION_DELETE = "session.delete"

    MEMORY_RECALL_BEFORE = "memory.recall.before"
    MEMORY_RECALL_AFTER = "memory.recall.after"
    MEMORY_WRITE_BEFORE = "memory.write.before"
    MEMORY_WRITE_AFTER = "memory.write.after"
    MEMORY_DELETE_BEFORE = "memory.delete.before"
    MEMORY_DELETE_AFTER = "memory.delete.after"
    MEMORY_PROMOTE_BEFORE = "memory.promote.before"
    MEMORY_PROMOTE_AFTER = "memory.promote.after"
    MEMORY_OPERATION_ERROR = "memory.operation.error"

    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"
    MESSAGE_STREAM_START = "message.stream.start"
    MESSAGE_STREAM_CHUNK = "message.stream.chunk"
    MESSAGE_STREAM_END = "message.stream.end"
    MESSAGE_EDIT = "message.edit"
    MESSAGE_DELETE = "message.delete"

    TOOL_REGISTER = "tool.register"
    TOOL_UNREGISTER = "tool.unregister"
    TOOL_DEFINITION = "tool.definition"
    TOOL_EXECUTE_BEFORE = "tool.execute.before"
    TOOL_EXECUTE_AFTER = "tool.execute.after"
    TOOL_EXECUTE_ERROR = "tool.execute.error"
    TOOL_VALIDATE_BEFORE = "tool.validate.before"
    TOOL_RATE_LIMIT = "tool.rate.limit"
    TOOL_RESULT_SYNTHETIC = "tool.result.synthetic"

    MCP_CONNECT = "mcp.connect"
    MCP_DISCONNECT = "mcp.disconnect"
    MCP_TOOL_CALL_BEFORE = "mcp.tool.call.before"
    MCP_TOOL_CALL_AFTER = "mcp.tool.call.after"
    MCP_TOOL_CALL_ERROR = "mcp.tool.call.error"
    MCP_RESOURCE_READ = "mcp.resource.read"
    MCP_PROMPT_LOAD = "mcp.prompt.load"

    TASK_CREATE = "task.create"
    TASK_START = "task.start"
    TASK_COMPLETE = "task.complete"
    TASK_FAIL = "task.fail"
    TASK_PROGRESS = "task.progress"
    TASK_CANCEL = "task.cancel"


@dataclass(frozen=True)
class HookEventSpec:
    event: HookEvent
    category: HookCategory
    description: str
    required_input: tuple[str, ...] = ()
    allowed_modes: tuple[HookDispatchMode, ...] = (HookDispatchMode.SERIAL,)
    recommended_mode: HookDispatchMode = HookDispatchMode.SERIAL

    def supports(self, mode: HookDispatchMode) -> bool:
        return mode in self.allowed_modes

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.value,
            "category": self.category.value,
            "description": self.description,
            "required_input": list(self.required_input),
            "allowed_modes": [mode.value for mode in self.allowed_modes],
            "recommended_mode": self.recommended_mode.value,
        }


def _spec(
    event: HookEvent,
    category: HookCategory,
    description: str,
    *required_input: str,
    allowed_modes: tuple[HookDispatchMode, ...] = (HookDispatchMode.SERIAL,),
    recommended_mode: HookDispatchMode = HookDispatchMode.SERIAL,
) -> HookEventSpec:
    return HookEventSpec(
        event,
        category,
        description,
        tuple(required_input),
        allowed_modes,
        recommended_mode,
    )


_EVENT_DESCRIPTIONS: dict[HookEvent, str] = {
    HookEvent.CONFIG_LOADED: "Runtime configuration and configured plugins were loaded.",
    HookEvent.SYSTEM_STARTUP: "The web runtime finished startup.",
    HookEvent.SYSTEM_SHUTDOWN: "The web runtime is shutting down.",
    HookEvent.AGENT_CREATE: "Agent construction started.",
    HookEvent.AGENT_CREATE_COMPLETE: "Agent construction completed.",
    HookEvent.AGENT_START: "An agent turn started.",
    HookEvent.AGENT_STOP: "An agent was asked to stop.",
    HookEvent.AGENT_ERROR: "An agent turn failed.",
    HookEvent.AGENT_THINKING: "The provider started a reasoning step.",
    HookEvent.AGENT_RESPONSE: "An agent response was finalized.",
    HookEvent.SESSION_BOOTSTRAP: "A session context was bootstrapped.",
    HookEvent.SESSION_START: "A server session was registered.",
    HookEvent.SESSION_END: "A server session was cleaned up.",
    HookEvent.SESSION_COMPACTION_BEFORE: "A session compaction transaction is about to capture and replace history.",
    HookEvent.SESSION_COMPACTION_AFTER: "A session compaction transaction completed.",
    HookEvent.SESSION_COMPACTION_ERROR: "A session compaction transaction failed.",
    HookEvent.SESSION_DELETE_BEFORE: "A persisted session is about to be deleted.",
    HookEvent.SESSION_DELETE_AFTER: "A persisted session and its owned memory were deleted.",
    HookEvent.SESSION_DELETE_ERROR: "A persisted session deletion failed.",
    HookEvent.SESSION_DELETE: "Deprecated compatibility notice emitted before session deletion.",
    HookEvent.MEMORY_RECALL_BEFORE: "A scoped semantic-memory recall is about to run.",
    HookEvent.MEMORY_RECALL_AFTER: "A scoped semantic-memory recall completed.",
    HookEvent.MEMORY_WRITE_BEFORE: "A scoped semantic-memory write is about to run.",
    HookEvent.MEMORY_WRITE_AFTER: "A scoped semantic-memory write completed.",
    HookEvent.MEMORY_DELETE_BEFORE: "A scoped semantic-memory deletion is about to run.",
    HookEvent.MEMORY_DELETE_AFTER: "A scoped semantic-memory deletion completed.",
    HookEvent.MEMORY_PROMOTE_BEFORE: "A semantic-memory promotion between scopes is about to run.",
    HookEvent.MEMORY_PROMOTE_AFTER: "A semantic-memory promotion between scopes completed.",
    HookEvent.MEMORY_OPERATION_ERROR: "A semantic-memory operation failed.",
    HookEvent.MESSAGE_STREAM_END: "Streaming output for a message ended.",
    HookEvent.TOOL_DEFINITION: "A tool definition is being exposed to a model.",
    HookEvent.TOOL_EXECUTE_BEFORE: "A tool call is about to execute.",
    HookEvent.TOOL_EXECUTE_AFTER: "A tool call completed.",
    HookEvent.TOOL_EXECUTE_ERROR: "A tool call raised an execution error.",
    HookEvent.TOOL_VALIDATE_BEFORE: "A tool call passed permission checks and will be validated.",
    HookEvent.TOOL_RATE_LIMIT: "A rate-limited tool was invoked.",
    HookEvent.TOOL_RESULT_SYNTHETIC: "A synthetic tool result was injected into the turn.",
    HookEvent.MCP_DISCONNECT: "MCP clients were disconnected.",
    HookEvent.MCP_TOOL_CALL_BEFORE: "An MCP tool call is about to execute.",
    HookEvent.MCP_TOOL_CALL_AFTER: "An MCP tool call completed.",
    HookEvent.MCP_TOOL_CALL_ERROR: "An MCP tool call failed.",
    HookEvent.TASK_CANCEL: "A subtask was cancelled or cleaned up.",
}


def _event_category(event: HookEvent) -> HookCategory:
    prefix = event.value.split(".", 1)[0]
    if prefix == "config":
        return HookCategory.CONFIG
    return HookCategory(prefix)


def _required_input(event: HookEvent) -> tuple[str, ...]:
    value = event.value
    if value.startswith("agent."):
        return ("agent", "session_id")
    if value.startswith("session."):
        return ("session_id",)
    if value.startswith("memory."):
        return ("operation_id", "scope")
    if value.startswith("mcp.tool.call."):
        return ("server", "tool")
    if value.startswith("task."):
        return ("task_id",)
    if value.startswith("tool.") and value not in {
        HookEvent.TOOL_REGISTER.value,
        HookEvent.TOOL_UNREGISTER.value,
        HookEvent.TOOL_DEFINITION.value,
        HookEvent.TOOL_RESULT_SYNTHETIC.value,
    }:
        return ("tool",)
    return ()


# Parallel eligibility is deliberately opt-in. These events are observation-only
# candidates; they still recommend and currently dispatch as serial. Adding an
# event here requires a separate semantic review and does not migrate call sites.
_PARALLEL_ALLOWED_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.MEMORY_LOW,
        HookEvent.DISK_LOW,
        HookEvent.AGENT_THINKING,
        HookEvent.SESSION_COMPACTION_AFTER,
        HookEvent.SESSION_COMPACTION_ERROR,
        HookEvent.SESSION_DELETE_AFTER,
        HookEvent.SESSION_DELETE_ERROR,
        HookEvent.MEMORY_RECALL_AFTER,
        HookEvent.MEMORY_WRITE_AFTER,
        HookEvent.MEMORY_DELETE_AFTER,
        HookEvent.MEMORY_PROMOTE_AFTER,
        HookEvent.MEMORY_OPERATION_ERROR,
    }
)


def _allowed_modes(event: HookEvent) -> tuple[HookDispatchMode, ...]:
    if event in _PARALLEL_ALLOWED_EVENTS:
        return (HookDispatchMode.SERIAL, HookDispatchMode.PARALLEL)
    return (HookDispatchMode.SERIAL,)


def _recommended_mode(_event: HookEvent) -> HookDispatchMode:
    # DSH-104B declares eligibility only. A later canary must explicitly opt in
    # before any runtime-owned event can recommend parallel dispatch.
    return HookDispatchMode.SERIAL


HOOK_EVENT_SPECS: dict[str, HookEventSpec] = {
    event.value: _spec(
        event,
        _event_category(event),
        _EVENT_DESCRIPTIONS.get(event, event.value.replace(".", " ").capitalize() + "."),
        *_required_input(event),
        allowed_modes=_allowed_modes(event),
        recommended_mode=_recommended_mode(event),
    )
    for event in HookEvent
}


class HookEventSpecError(ValueError):
    """Raised when the runtime-owned HookEventSpec catalog is inconsistent."""


def validate_hook_event_specs(
    specs: Mapping[str, HookEventSpec],
) -> None:
    """Fail deterministically when event identity or dispatch modes conflict."""

    errors: list[str] = []
    expected_keys = {event.value for event in HookEvent}
    actual_keys = set(specs)
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    if missing:
        errors.append("missing events: " + ", ".join(missing))
    if unexpected:
        errors.append("unexpected events: " + ", ".join(unexpected))

    for key in sorted(expected_keys & actual_keys):
        spec = specs[key]
        if not isinstance(spec, HookEventSpec):
            errors.append(f"{key}: expected HookEventSpec")
            continue
        if not isinstance(spec.event, HookEvent):
            errors.append(f"{key}: event must be a HookEvent")
            continue
        if spec.event.value != key:
            errors.append(f"{key}: spec declares conflicting event {spec.event.value}")
        expected_category = _event_category(spec.event)
        if not isinstance(spec.category, HookCategory):
            errors.append(f"{key}: category must be a HookCategory")
        elif spec.category is not expected_category:
            errors.append(
                f"{key}: category {spec.category.value} conflicts with "
                f"{expected_category.value}"
            )
        if not isinstance(spec.description, str) or not spec.description.strip():
            errors.append(f"{key}: description must not be empty")
        if not spec.allowed_modes:
            errors.append(f"{key}: allowed_modes must not be empty")
            continue
        if any(not isinstance(mode, HookDispatchMode) for mode in spec.allowed_modes):
            errors.append(f"{key}: allowed_modes contains an unsupported value")
            continue
        if len(set(spec.allowed_modes)) != len(spec.allowed_modes):
            errors.append(f"{key}: allowed_modes contains duplicates")
        if not isinstance(spec.recommended_mode, HookDispatchMode):
            errors.append(f"{key}: recommended_mode contains an unsupported value")
        elif spec.recommended_mode not in spec.allowed_modes:
            errors.append(
                f"{key}: recommended mode {spec.recommended_mode.value} is not allowed"
            )
        if (
            HookDispatchMode.PARALLEL in spec.allowed_modes
            and spec.event not in _PARALLEL_ALLOWED_EVENTS
        ):
            errors.append(f"{key}: parallel mode was not approved for this event")

    if errors:
        raise HookEventSpecError("invalid HookEventSpec catalog: " + "; ".join(errors))


# Import/startup-time validation ensures a bad static catalog cannot reach a
# running PluginManager. Runtime dispatch still validates the selected mode.
validate_hook_event_specs(HOOK_EVENT_SPECS)


def normalize_hook_event(event: str | HookEvent) -> str:
    value = event.value if isinstance(event, HookEvent) else str(event or "")
    normalized = value.strip().lower()
    if not normalized or "." not in normalized:
        raise ValueError("hook event must be a non-empty dotted name")
    return normalized


@dataclass(frozen=True)
class HookRegistrationToken:
    """Opaque identity for exactly one hook registration."""

    registration_id: str
    event: str
    name: str
    source: str
    instance_id: Optional[str] = None


@dataclass(frozen=True)
class HookRegistration:
    event: str
    name: str
    source: str
    handler: HookHandler
    priority: int = 0
    registration_id: str = ""
    instance_id: Optional[str] = None

    @property
    def token(self) -> HookRegistrationToken:
        return HookRegistrationToken(
            registration_id=self.registration_id,
            event=self.event,
            name=self.name,
            source=self.source,
            instance_id=self.instance_id,
        )


class HookRegistrationDisposer:
    """Idempotent callable that removes only its own registration token."""

    def __init__(self, token: HookRegistrationToken) -> None:
        from ..runtime.workspace_registry import plugin_workspace
        self._workspace = plugin_workspace.get()
        self._token = token
        self._disposed = False

    @property
    def token(self) -> HookRegistrationToken:
        return self._token

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __call__(self) -> None:
        if self._disposed:
            return
        from ..runtime.workspace_registry import registry_workspace
        with registry_workspace(self._workspace):
            HookRegistry.unregister(self._token)
        self._disposed = True


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class HookRegistry(metaclass=WorkspaceRegistryMeta):
    _workspace_fields = ('_hooks',)

    _hooks: dict[str, list[HookRegistration]] = {}

    @classmethod
    def register(
        cls,
        event: str | HookEvent,
        name: str,
        handler: HookHandler,
        source: str,
        priority: int = 0,
        *,
        instance_id: Optional[str] = None,
    ) -> HookRegistrationDisposer:
        event_name = normalize_hook_event(event)
        normalized_instance_id = str(instance_id or "").strip() or None
        registration_id = uuid.uuid4().hex
        registrations = cls._hooks.setdefault(event_name, [])
        registration = HookRegistration(
            event=event_name,
            name=name,
            source=source,
            handler=handler,
            priority=priority,
            registration_id=registration_id,
            instance_id=normalized_instance_id,
        )
        registrations.append(registration)
        registrations.sort(key=lambda item: item.priority, reverse=True)
        return HookRegistrationDisposer(registration.token)

    @classmethod
    def unregister(cls, token: HookRegistrationToken) -> bool:
        """Remove one exact registration and leave lookalike entries intact."""

        if not isinstance(token, HookRegistrationToken):
            raise TypeError("hook unregister requires a HookRegistrationToken")
        registrations = cls._hooks.get(token.event)
        if not registrations:
            return False
        remaining = [item for item in registrations if item.token != token]
        if len(remaining) == len(registrations):
            return False
        if remaining:
            cls._hooks[token.event] = remaining
        else:
            del cls._hooks[token.event]
        return True

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> int:
        """Remove registrations owned by one plugin instance identity."""

        normalized = str(instance_id or "").strip()
        if not normalized:
            return 0
        removed = 0
        for event, registrations in list(cls._hooks.items()):
            remaining = [
                item for item in registrations if item.instance_id != normalized
            ]
            removed += len(registrations) - len(remaining)
            if remaining:
                cls._hooks[event] = remaining
            else:
                del cls._hooks[event]
        return removed

    @classmethod
    def unregister_by_source(cls, source: str, *, legacy_only: bool = False) -> int:
        """Compatibility cleanup by source.

        The historical one-argument call still removes every matching entry.
        New instance-owned paths use ``legacy_only=True`` so this fallback can
        never remove a tokenized registration belonging to a newer instance.
        """

        removed = 0
        for event, registrations in list(cls._hooks.items()):
            remaining = [
                item
                for item in registrations
                if not (
                    item.source == source
                    and (not legacy_only or item.instance_id is None)
                )
            ]
            removed += len(registrations) - len(remaining)
            if remaining:
                cls._hooks[event] = remaining
            else:
                del cls._hooks[event]
        return removed

    @classmethod
    def _prepare_dispatch(
        cls,
        event: str | HookEvent,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        *,
        mode: HookDispatchMode,
    ) -> tuple[str, dict[str, Any], dict[str, Any], tuple[HookRegistration, ...]]:
        event_name = normalize_hook_event(event)
        payload_in = dict(input_data or {})
        payload_out = dict(output_data or {})
        spec = HOOK_EVENT_SPECS.get(event_name)
        if spec:
            if not spec.supports(mode):
                allowed = ", ".join(item.value for item in spec.allowed_modes)
                raise HookEventSpecError(
                    f"hook event {event_name!r} does not allow dispatch mode "
                    f"{mode.value!r}; allowed: {allowed}; recommended: "
                    f"{spec.recommended_mode.value}"
                )
            missing = [key for key in spec.required_input if key not in payload_in]
            if missing:
                payload_out.setdefault("__hook_contract_errors__", []).append(
                    {
                        "event": event_name,
                        "missing_input": missing,
                    }
                )
        registrations = tuple(cls._hooks.get(event_name, ()))
        return event_name, payload_in, payload_out, registrations

    @staticmethod
    def _timeout_seconds() -> float:
        try:
            timeout_seconds = float(
                os.environ.get("TCODE_PLUGIN_HOOK_TIMEOUT_SECONDS", "10")
            )
        except Exception:
            timeout_seconds = 10.0
        if timeout_seconds <= 0:
            return 0.0
        return max(0.1, min(timeout_seconds, 300.0))

    @staticmethod
    def _isolated_payload(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return deepcopy(payload)
        except Exception:
            # Runtime payloads may contain live objects that cannot be copied.
            # A fresh top-level mapping still prevents ordinary hook writes
            # from leaking into another observer or the caller.
            return dict(payload)

    @classmethod
    async def _invoke(
        cls,
        event_name: str,
        registration: HookRegistration,
        payload_in: dict[str, Any],
        payload_out: dict[str, Any],
    ) -> Optional[dict[str, str]]:
        try:
            result = registration.handler(payload_in, payload_out)
            if inspect.isawaitable(result):
                timeout_seconds = cls._timeout_seconds()
                if timeout_seconds > 0:
                    await asyncio.wait_for(result, timeout=timeout_seconds)
                else:
                    await result
        except asyncio.TimeoutError:
            return {
                "event": event_name,
                "source": registration.source,
                "name": registration.name,
                "error": "plugin hook timed out",
            }
        except Exception as exc:
            return {
                "event": event_name,
                "source": registration.source,
                "name": registration.name,
                "error": str(exc),
            }
        return None

    @staticmethod
    def _append_errors(
        payload_out: dict[str, Any],
        errors_to_add: list[dict[str, str]],
    ) -> None:
        if not errors_to_add:
            return
        errors = payload_out.setdefault("__hook_errors__", [])
        if isinstance(errors, list):
            errors.extend(errors_to_add)

    @classmethod
    async def emit_serial(
        cls,
        event: str | HookEvent,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run hooks in priority order using the legacy shared-output contract."""

        event_name, payload_in, payload_out, registrations = cls._prepare_dispatch(
            event,
            input_data,
            output_data,
            mode=HookDispatchMode.SERIAL,
        )
        errors: list[dict[str, str]] = []
        for registration in registrations:
            error = await cls._invoke(
                event_name,
                registration,
                payload_in,
                payload_out,
            )
            if error is not None:
                errors.append(error)
        cls._append_errors(payload_out, errors)
        return payload_out

    @classmethod
    async def emit_parallel(
        cls,
        event: str | HookEvent,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run observer hooks concurrently without merging their mutations.

        Each handler receives isolated input/output mappings. This keeps the
        result deterministic and makes the mode suitable for telemetry,
        lifecycle notices, and cache invalidation. Handler errors are appended
        in registration order after every handler has settled.
        """

        event_name, payload_in, payload_out, registrations = cls._prepare_dispatch(
            event,
            input_data,
            output_data,
            mode=HookDispatchMode.PARALLEL,
        )
        results = await asyncio.gather(
            *(
                cls._invoke(
                    event_name,
                    registration,
                    cls._isolated_payload(payload_in),
                    cls._isolated_payload(payload_out),
                )
                for registration in registrations
            )
        )
        cls._append_errors(
            payload_out,
            [error for error in results if error is not None],
        )
        return payload_out

    @classmethod
    async def dispatch(
        cls,
        event: str | HookEvent,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        *,
        mode: str | HookDispatchMode = HookDispatchMode.SERIAL,
    ) -> dict[str, Any]:
        """Dispatch a hook event with an explicit execution strategy."""

        if isinstance(mode, HookDispatchMode):
            dispatch_mode = mode
        else:
            try:
                dispatch_mode = HookDispatchMode(str(mode or "").strip().lower())
            except ValueError as exc:
                supported = ", ".join(item.value for item in HookDispatchMode)
                raise ValueError(
                    f"unsupported hook dispatch mode: {mode!r}; expected one of: {supported}"
                ) from exc

        if dispatch_mode is HookDispatchMode.PARALLEL:
            return await cls.emit_parallel(event, input_data, output_data)
        return await cls.emit_serial(event, input_data, output_data)

    @classmethod
    async def emit(
        cls,
        event: str | HookEvent,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Backward-compatible alias for serial hook dispatch."""

        return await cls.emit_serial(event, input_data, output_data)

    @classmethod
    def list_hooks(cls, event: Optional[str] = None) -> list[str]:
        items: list[str] = []
        for event_name, registrations in cls._hooks.items():
            if event and event != event_name:
                continue
            for registration in registrations:
                items.append(
                    f"{event_name}:{registration.source}:{registration.name}"
                )
        return items

    @classmethod
    def catalog(cls) -> list[dict[str, Any]]:
        registered_counts = {
            event: len(registrations) for event, registrations in cls._hooks.items()
        }
        return [
            {
                **spec.to_dict(),
                "registered_handlers": registered_counts.get(spec.event.value, 0),
            }
            for spec in sorted(HOOK_EVENT_SPECS.values(), key=lambda item: item.event.value)
        ]

    @classmethod
    def clear(cls):
        cls._hooks.clear()
