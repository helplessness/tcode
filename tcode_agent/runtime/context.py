"""提供带 Scope、可逆所有权和类型约束的运行时服务组合能力。"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Literal, Optional, Protocol, TypeVar

from .effects import Disposer, EffectScope, EffectTransaction


T = TypeVar("T")
ServiceCardinality = Literal["one", "many"]
_SERVICE_KEY_PATTERN = re.compile(
    r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+@[1-9][0-9]*$"
)
_CLOSING_CONTEXT_IDS: ContextVar[frozenset[str]] = ContextVar(
    "closing-runtime-context-ids",
    default=frozenset(),
)


class RuntimeContextState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True)
class RuntimeIdentity:
    tenant_id: Optional[str] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None

    def overlay(self, child: Optional["RuntimeIdentity"] = None) -> "RuntimeIdentity":
        child = child or RuntimeIdentity()
        return RuntimeIdentity(
            tenant_id=child.tenant_id or self.tenant_id,
            workspace_id=child.workspace_id or self.workspace_id,
            session_id=child.session_id or self.session_id,
            agent_id=child.agent_id or self.agent_id,
        )

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True)
class ServiceKey(Generic[T]):
    name: str
    protocol: type[T]
    cardinality: ServiceCardinality = "one"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name != self.name.strip():
            raise ValueError("service key name must be a trimmed string")
        if not _SERVICE_KEY_PATTERN.fullmatch(self.name):
            raise ValueError(
                "service key must match '<namespace>.<name>@<major-version>'"
            )
        if not isinstance(self.protocol, type):
            raise TypeError("service key protocol must be a type or Protocol class")
        if self.cardinality not in {"one", "many"}:
            raise ValueError("service key cardinality must be 'one' or 'many'")


class RuntimeContextError(RuntimeError):
    """Base error for runtime context composition."""


class RuntimeContextClosedError(RuntimeContextError):
    def __init__(self, context_name: str, state: RuntimeContextState) -> None:
        self.context_name = context_name
        self.state = state
        super().__init__(
            f"runtime context {context_name!r} is not available while {state.value}"
        )


class ServiceDefinitionConflictError(RuntimeContextError):
    def __init__(self, current: ServiceKey[Any], requested: ServiceKey[Any]) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"conflicting definitions for service {requested.name!r}: "
            f"{current.protocol.__name__}/{current.cardinality} vs "
            f"{requested.protocol.__name__}/{requested.cardinality}"
        )


class ServiceNotFoundError(RuntimeContextError):
    def __init__(self, key: ServiceKey[Any]) -> None:
        self.key = key
        super().__init__(f"required runtime service is missing: {key.name}")


class DuplicateServiceError(RuntimeContextError):
    def __init__(self, key: ServiceKey[Any]) -> None:
        self.key = key
        super().__init__(
            f"service {key.name!r} has cardinality 'one'; use explicit shadow=True"
        )


class ServiceProviderTypeError(RuntimeContextError):
    def __init__(self, key: ServiceKey[Any], provider: Any) -> None:
        self.key = key
        self.provider = provider
        super().__init__(
            f"provider {type(provider).__name__!r} does not satisfy "
            f"service protocol {key.protocol.__name__!r} for {key.name!r}"
        )


class ServiceProviderPlanError(RuntimeContextError):
    """Raised before provider application when the dependency graph is invalid."""


class ServiceProviderContractError(RuntimeContextError):
    """Raised when apply() does not publish every service it declared."""


@dataclass(frozen=True)
class RuntimeContextCloseFailure:
    label: str
    error: BaseException


class RuntimeContextCloseError(RuntimeContextError):
    def __init__(
        self,
        context_name: str,
        failures: tuple[RuntimeContextCloseFailure, ...],
    ) -> None:
        self.context_name = context_name
        self.failures = failures
        detail = "; ".join(
            f"{failure.label}: {type(failure.error).__name__}: {failure.error}"
            for failure in failures
        )
        super().__init__(
            f"{len(failures)} failure(s) while closing runtime context "
            f"{context_name!r}: {detail}"
        )


@dataclass(frozen=True)
class ServiceRegistrationToken:
    registration_id: str
    context_id: str
    key_name: str
    source: str


@dataclass(frozen=True)
class _ServiceRegistration:
    key: ServiceKey[Any]
    provider: Any
    source: str
    registration_id: str
    shadow: bool
    context_id: str

    @property
    def token(self) -> ServiceRegistrationToken:
        return ServiceRegistrationToken(
            registration_id=self.registration_id,
            context_id=self.context_id,
            key_name=self.key.name,
            source=self.source,
        )


class ServiceRegistrationDisposer:
    """Idempotent callable that removes one exact service contribution."""

    def __init__(
        self,
        context: "RuntimeContext",
        token: ServiceRegistrationToken,
    ) -> None:
        self._context = context
        self._token = token
        self._disposed = False

    @property
    def token(self) -> ServiceRegistrationToken:
        return self._token

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __call__(self) -> None:
        if self._disposed:
            return
        self._context._unregister(self._token)
        self._disposed = True


class ServiceProvider(Protocol):
    name: str
    provides: tuple[ServiceKey[Any], ...]
    dependencies: tuple[ServiceKey[Any], ...]
    shadows: tuple[ServiceKey[Any], ...]

    async def apply(self, context: "RuntimeContext") -> None: ...


@dataclass(frozen=True)
class ServiceProviderPlan:
    ordered_names: tuple[str, ...]
    providers: tuple[ServiceProvider, ...]


class RuntimeContext:
    """Own typed services and inherit them through an explicit context tree.

    The object is an internal composition primitive, not the public Plugin API.
    All mutation is intended for one asyncio event loop; cross-thread access is
    deliberately unsupported.
    """

    def __init__(
        self,
        identity: Optional[RuntimeIdentity] = None,
        *,
        name: str = "runtime",
        effects: Optional[EffectScope] = None,
        parent: Optional["RuntimeContext"] = None,
    ) -> None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("runtime context name must not be empty")
        self._name = normalized_name
        self._context_id = uuid.uuid4().hex
        self._parent = parent
        parent_identity = parent.identity if parent is not None else RuntimeIdentity()
        self._identity = parent_identity.overlay(identity)
        self._effects = effects or EffectScope(f"runtime-context:{normalized_name}")
        self._state = RuntimeContextState.OPEN
        self._definitions: dict[str, ServiceKey[Any]] = {}
        self._registrations: dict[str, list[_ServiceRegistration]] = {}
        self._children: dict[str, RuntimeContext] = {}
        self._close_task: Optional[asyncio.Task[None]] = None
        self._active_effect_owner: ContextVar[
            Optional[EffectScope | EffectTransaction]
        ] = ContextVar(
            f"runtime-context-effect-owner:{self._context_id}",
            default=None,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def context_id(self) -> str:
        return self._context_id

    @property
    def identity(self) -> RuntimeIdentity:
        return self._identity

    @property
    def effects(self) -> EffectScope:
        return self._effects

    @property
    def state(self) -> RuntimeContextState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state is RuntimeContextState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state is RuntimeContextState.CLOSED

    def _ensure_open(self) -> None:
        if self._state is not RuntimeContextState.OPEN:
            raise RuntimeContextClosedError(self._name, self._state)

    def _definition(self, name: str) -> Optional[ServiceKey[Any]]:
        current = self._definitions.get(name)
        if current is not None:
            return current
        if self._parent is not None:
            return self._parent._definition(name)
        return None

    def _validate_definition(self, key: ServiceKey[Any]) -> None:
        current = self._definition(key.name)
        if current is not None and current != key:
            raise ServiceDefinitionConflictError(current, key)

    @staticmethod
    def _validate_provider_type(key: ServiceKey[Any], provider: Any) -> None:
        try:
            valid = isinstance(provider, key.protocol)
        except TypeError:
            # A Protocol without @runtime_checkable is documentation-only.
            return
        if not valid:
            raise ServiceProviderTypeError(key, provider)

    def _resolved_registrations(self, key_name: str) -> list[_ServiceRegistration]:
        inherited: list[_ServiceRegistration] = []
        if self._parent is not None:
            inherited = self._parent._resolved_registrations(key_name)
        return [*inherited, *self._registrations.get(key_name, ())]

    def _local_registration_count(self, key_name: str) -> int:
        return len(self._registrations.get(key_name, ()))

    def contains(self, key: ServiceKey[Any]) -> bool:
        self._validate_definition(key)
        return bool(self._resolved_registrations(key.name))

    def require(self, key: ServiceKey[T]) -> T:
        self._ensure_open()
        self._validate_definition(key)
        registrations = self._resolved_registrations(key.name)
        if not registrations:
            raise ServiceNotFoundError(key)
        if key.cardinality == "many":
            return tuple(item.provider for item in registrations)  # type: ignore[return-value]
        return registrations[-1].provider

    def optional(self, key: ServiceKey[T]) -> Optional[T]:
        self._ensure_open()
        self._validate_definition(key)
        registrations = self._resolved_registrations(key.name)
        if not registrations:
            return None
        if key.cardinality == "many":
            return tuple(item.provider for item in registrations)  # type: ignore[return-value]
        return registrations[-1].provider

    def add_effect(self, disposer: Disposer, *, label: Optional[str] = None) -> Disposer:
        self._ensure_open()
        owner = self._active_effect_owner.get() or self._effects
        return owner.add(disposer, label=label)

    def provide(
        self,
        key: ServiceKey[T],
        provider: T,
        *,
        source: str = "runtime",
        shadow: bool = False,
    ) -> ServiceRegistrationDisposer:
        self._ensure_open()
        self._validate_definition(key)
        self._validate_provider_type(key, provider)
        normalized_source = str(source or "").strip()
        if not normalized_source:
            raise ValueError("service provider source must not be empty")
        resolved = self._resolved_registrations(key.name)
        if key.cardinality == "one" and resolved and not shadow:
            raise DuplicateServiceError(key)
        if key.cardinality == "many" and shadow:
            raise ValueError("many-cardinality services cannot use shadow=True")

        registration = _ServiceRegistration(
            key=key,
            provider=provider,
            source=normalized_source,
            registration_id=uuid.uuid4().hex,
            shadow=bool(resolved and shadow),
            context_id=self._context_id,
        )
        self._definitions.setdefault(key.name, key)
        self._registrations.setdefault(key.name, []).append(registration)
        disposer = ServiceRegistrationDisposer(self, registration.token)
        try:
            self.add_effect(
                disposer,
                label=(
                    f"service:{key.name}:{normalized_source}:"
                    f"{registration.registration_id}"
                ),
            )
        except BaseException:
            self._unregister(registration.token)
            raise
        return disposer

    def _unregister(self, token: ServiceRegistrationToken) -> bool:
        if token.context_id != self._context_id:
            return False
        registrations = self._registrations.get(token.key_name)
        if not registrations:
            return False
        remaining = [item for item in registrations if item.token != token]
        if len(remaining) == len(registrations):
            return False
        if remaining:
            self._registrations[token.key_name] = remaining
        else:
            self._registrations.pop(token.key_name, None)
            self._definitions.pop(token.key_name, None)
        return True

    def child(
        self,
        identity: Optional[RuntimeIdentity] = None,
        *,
        name: Optional[str] = None,
    ) -> "RuntimeContext":
        self._ensure_open()
        child = RuntimeContext(
            identity,
            name=str(name or f"{self._name}.child"),
            parent=self,
        )
        self._children[child.context_id] = child
        return child

    def catalog(self) -> dict[str, Any]:
        names: set[str] = set(self._definitions)
        if self._parent is not None:
            names.update(item["key"] for item in self._parent.catalog()["services"])
        services: list[dict[str, Any]] = []
        for key_name in sorted(names):
            key = self._definition(key_name)
            if key is None:
                continue
            registrations = self._resolved_registrations(key_name)
            provider_records = [
                {
                    "registration_id": item.registration_id,
                    "source": item.source,
                    "context_id": item.context_id,
                    "inherited": item.context_id != self._context_id,
                    "shadow": item.shadow,
                }
                for item in registrations
            ]
            services.append(
                {
                    "key": key.name,
                    "protocol": f"{key.protocol.__module__}.{key.protocol.__qualname__}",
                    "cardinality": key.cardinality,
                    "provider_count": len(provider_records),
                    "selected_registration_id": (
                        provider_records[-1]["registration_id"]
                        if provider_records and key.cardinality == "one"
                        else None
                    ),
                    "providers": provider_records,
                }
            )
        return {
            "context_id": self._context_id,
            "name": self._name,
            "state": self._state.value,
            "identity": self._identity.to_dict(),
            "parent_context_id": self._parent.context_id if self._parent else None,
            "child_count": len(self._children),
            "services": services,
        }

    async def _close_impl(self) -> None:
        failures: list[RuntimeContextCloseFailure] = []
        closing_token = _CLOSING_CONTEXT_IDS.set(
            _CLOSING_CONTEXT_IDS.get() | {self._context_id}
        )
        try:
            children = tuple(reversed(tuple(self._children.values())))
            for child in children:
                try:
                    await child.close()
                except (Exception, asyncio.CancelledError) as exc:
                    failures.append(
                        RuntimeContextCloseFailure(
                            label=f"child:{child.name}:{child.context_id}",
                            error=exc,
                        )
                    )
            try:
                await self._effects.close()
            except (Exception, asyncio.CancelledError) as exc:
                failures.append(RuntimeContextCloseFailure("effects", exc))
        finally:
            self._children.clear()
            self._registrations.clear()
            self._definitions.clear()
            self._state = RuntimeContextState.CLOSED
            if self._parent is not None:
                self._parent._children.pop(self._context_id, None)
            _CLOSING_CONTEXT_IDS.reset(closing_token)
        if failures:
            raise RuntimeContextCloseError(self._name, tuple(failures))

    async def close(self) -> None:
        if self._context_id in _CLOSING_CONTEXT_IDS.get():
            return
        close_task = self._close_task
        if close_task is None:
            if self._state is RuntimeContextState.CLOSED:
                return
            self._ensure_open()
            self._state = RuntimeContextState.CLOSING
            close_task = asyncio.create_task(
                self._close_impl(),
                name=f"runtime-context-close:{self._name}",
            )
            self._close_task = close_task
        if asyncio.current_task() is close_task:
            return
        await asyncio.shield(close_task)


def _provider_name(provider: ServiceProvider) -> str:
    name = str(getattr(provider, "name", "") or "").strip()
    if not name:
        raise ServiceProviderPlanError("service provider name must not be empty")
    return name


def _provider_keys(
    provider: ServiceProvider,
    attribute: str,
) -> tuple[ServiceKey[Any], ...]:
    raw = getattr(provider, attribute, ())
    if not isinstance(raw, tuple) or any(not isinstance(item, ServiceKey) for item in raw):
        raise ServiceProviderPlanError(
            f"provider {_provider_name(provider)!r} {attribute} must be a tuple of ServiceKey"
        )
    return raw


def _find_provider_cycle(
    names: tuple[str, ...],
    adjacency: dict[str, set[str]],
) -> tuple[str, ...]:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> Optional[tuple[str, ...]]:
        state[name] = 1
        stack.append(name)
        for target in sorted(adjacency[name]):
            if state.get(target, 0) == 0:
                found = visit(target)
                if found:
                    return found
            elif state.get(target) == 1:
                index = stack.index(target)
                return tuple([*stack[index:], target])
        stack.pop()
        state[name] = 2
        return None

    for name in names:
        if state.get(name, 0) == 0:
            found = visit(name)
            if found:
                return found
    return ()


def plan_service_providers(
    context: RuntimeContext,
    providers: tuple[ServiceProvider, ...] | list[ServiceProvider],
) -> ServiceProviderPlan:
    """Validate definitions/dependencies and return deterministic apply order."""

    context._ensure_open()
    ordered_input = tuple(providers)
    names = tuple(_provider_name(provider) for provider in ordered_input)
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ServiceProviderPlanError(
            "duplicate service provider names: " + ", ".join(duplicates)
        )
    by_name = dict(zip(names, ordered_input))
    index = {name: position for position, name in enumerate(names)}
    provides_by_key: dict[str, list[str]] = {}
    key_definitions: dict[str, ServiceKey[Any]] = {}
    shadows_by_provider: dict[str, set[str]] = {}

    for name, provider in zip(names, ordered_input):
        provided = _provider_keys(provider, "provides")
        if not provided:
            raise ServiceProviderPlanError(f"provider {name!r} must declare provides")
        shadows = _provider_keys(provider, "shadows")
        shadow_names = {key.name for key in shadows}
        if not shadow_names.issubset({key.name for key in provided}):
            raise ServiceProviderPlanError(
                f"provider {name!r} shadows keys it does not provide"
            )
        shadows_by_provider[name] = shadow_names
        for key in (*provided, *_provider_keys(provider, "dependencies"), *shadows):
            current = key_definitions.get(key.name) or context._definition(key.name)
            if current is not None and current != key:
                raise ServiceDefinitionConflictError(current, key)
            key_definitions.setdefault(key.name, key)
        for key in provided:
            provides_by_key.setdefault(key.name, []).append(name)

    adjacency = {name: set() for name in names}
    indegree = {name: 0 for name in names}

    def add_edge(source: str, target: str) -> None:
        if target in adjacency[source]:
            return
        adjacency[source].add(target)
        indegree[target] += 1

    for key_name, provider_names in provides_by_key.items():
        key = key_definitions[key_name]
        if key.cardinality == "one":
            already_exists = context.contains(key)
            for position, provider_name in enumerate(provider_names):
                needs_shadow = already_exists or position > 0
                if needs_shadow and key_name not in shadows_by_provider[provider_name]:
                    raise ServiceProviderPlanError(
                        f"provider {provider_name!r} must explicitly shadow {key_name!r}"
                    )
                if position > 0:
                    add_edge(provider_names[position - 1], provider_name)

    for name, provider in zip(names, ordered_input):
        for dependency in _provider_keys(provider, "dependencies"):
            provider_names = provides_by_key.get(dependency.name, [])
            if provider_names:
                for provider_name in provider_names:
                    if (
                        provider_name == name
                        and context.contains(dependency)
                        and dependency.name in shadows_by_provider[name]
                    ):
                        continue
                    add_edge(provider_name, name)
            elif not context.contains(dependency):
                raise ServiceProviderPlanError(
                    f"provider {name!r} requires missing service {dependency.name!r}"
                )

    ready = sorted(
        (name for name in names if indegree[name] == 0),
        key=index.__getitem__,
    )
    result: list[str] = []
    while ready:
        name = ready.pop(0)
        result.append(name)
        for target in sorted(adjacency[name], key=index.__getitem__):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=index.__getitem__)

    if len(result) != len(names):
        cycle = _find_provider_cycle(names, adjacency)
        detail = " -> ".join(cycle) if cycle else ", ".join(
            name for name in names if indegree[name] > 0
        )
        raise ServiceProviderPlanError("service provider dependency cycle: " + detail)

    return ServiceProviderPlan(
        ordered_names=tuple(result),
        providers=tuple(by_name[name] for name in result),
    )


async def apply_service_providers(
    context: RuntimeContext,
    providers: tuple[ServiceProvider, ...] | list[ServiceProvider],
    *,
    transaction_name: Optional[str] = None,
) -> ServiceProviderPlan:
    """Validate and apply providers with rollback for context-owned effects.

    Registrations become visible during apply and commit into the context scope
    only after every provider succeeds. Staged publication to external
    consumers remains DSH-102 work.
    """

    plan = plan_service_providers(context, providers)
    name = str(transaction_name or "").strip() or f"{context.name}.providers"
    async with context.effects.transaction(name) as transaction:
        owner_token = context._active_effect_owner.set(transaction)
        try:
            for provider_name, provider in zip(plan.ordered_names, plan.providers):
                provided_keys = _provider_keys(provider, "provides")
                before = {
                    key.name: context._local_registration_count(key.name)
                    for key in provided_keys
                }
                result = provider.apply(context)
                if inspect.isawaitable(result):
                    await result
                missing = [
                    key.name
                    for key in provided_keys
                    if context._local_registration_count(key.name) <= before[key.name]
                ]
                if missing:
                    raise ServiceProviderContractError(
                        f"provider {provider_name!r} did not publish declared services: "
                        + ", ".join(missing)
                    )
        finally:
            context._active_effect_owner.reset(owner_token)
        transaction.commit()
    return plan


__all__ = [
    "DuplicateServiceError",
    "RuntimeContext",
    "RuntimeContextCloseError",
    "RuntimeContextCloseFailure",
    "RuntimeContextClosedError",
    "RuntimeContextError",
    "RuntimeContextState",
    "RuntimeIdentity",
    "ServiceCardinality",
    "ServiceDefinitionConflictError",
    "ServiceKey",
    "ServiceNotFoundError",
    "ServiceProvider",
    "ServiceProviderContractError",
    "ServiceProviderPlan",
    "ServiceProviderPlanError",
    "ServiceProviderTypeError",
    "ServiceRegistrationDisposer",
    "ServiceRegistrationToken",
    "apply_service_providers",
    "plan_service_providers",
]
