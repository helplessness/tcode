"""Lifecycle ownership primitives for reversible runtime effects.

This module is intentionally independent from plugins, registries, and service
composition. DSH-101A establishes the contract; later work packages can adapt
existing registrations to it without changing their current default path.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional


Disposer = Callable[[], Awaitable[None] | None]


class EffectScopeState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class EffectTransactionState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    COMMIT_REQUESTED = "commit_requested"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class EffectScopeClosedError(RuntimeError):
    """Raised when a closing or closed scope receives a new effect."""

    def __init__(self, scope_name: str, state: EffectScopeState) -> None:
        self.scope_name = scope_name
        self.state = state
        super().__init__(
            f"effect scope {scope_name!r} does not accept new effects while {state.value}"
        )


class EffectTransactionStateError(RuntimeError):
    """Raised when a transaction operation is invalid for its current state."""

    def __init__(
        self,
        transaction_name: str,
        operation: str,
        state: EffectTransactionState,
    ) -> None:
        self.transaction_name = transaction_name
        self.operation = operation
        self.state = state
        super().__init__(
            f"cannot {operation} effect transaction {transaction_name!r} "
            f"while {state.value}"
        )


@dataclass(frozen=True)
class EffectDisposalFailure:
    """One failed disposer, ordered by actual LIFO disposal order."""

    label: str
    registration_index: int
    error: BaseException


class EffectDisposalError(Exception):
    """Aggregate raised after every disposer has been attempted."""

    def __init__(
        self,
        scope_name: str,
        failures: tuple[EffectDisposalFailure, ...],
    ) -> None:
        self.scope_name = scope_name
        self.failures = failures
        detail = "; ".join(
            f"{failure.label}: {type(failure.error).__name__}: {failure.error}"
            for failure in failures
        )
        super().__init__(
            f"{len(failures)} disposer(s) failed while closing "
            f"effect scope {scope_name!r}: {detail}"
        )


class EffectTransactionError(EffectDisposalError):
    """Rollback failures combined with the error that triggered rollback."""

    def __init__(
        self,
        transaction_name: str,
        cause: BaseException,
        failures: tuple[EffectDisposalFailure, ...],
    ) -> None:
        self.transaction_name = transaction_name
        self.cause = cause
        super().__init__(transaction_name, failures)
        detail = Exception.__str__(self)
        self.args = (
            f"effect transaction {transaction_name!r} failed with "
            f"{type(cause).__name__}: {cause}; rollback also failed: {detail}",
        )


@dataclass(frozen=True)
class _RegisteredEffect:
    disposer: Disposer
    label: str
    registration_index: int


class EffectScope:
    """Own a LIFO stack of reversible effects.

    Scope methods are safe for concurrent tasks on one asyncio event loop.
    Cross-thread mutation is intentionally unsupported.
    """

    def __init__(self, name: str = "scope") -> None:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("effect scope name must not be empty")
        self._name = normalized
        self._state = EffectScopeState.OPEN
        self._effects: list[_RegisteredEffect] = []
        self._next_registration_index = 0
        self._close_task: Optional[asyncio.Task[None]] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> EffectScopeState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state is EffectScopeState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state is EffectScopeState.CLOSED

    @property
    def effect_count(self) -> int:
        return len(self._effects)

    def _ensure_open(self) -> None:
        if self._state is not EffectScopeState.OPEN:
            raise EffectScopeClosedError(self._name, self._state)

    def add(self, disposer: Disposer, *, label: Optional[str] = None) -> Disposer:
        """Register a disposer and return the same callable for composition."""

        self._ensure_open()
        if not callable(disposer):
            raise TypeError("effect disposer must be callable")
        index = self._next_registration_index
        self._next_registration_index += 1
        resolved_label = str(label or "").strip()
        if not resolved_label:
            resolved_label = str(
                getattr(disposer, "__qualname__", None)
                or getattr(disposer, "__name__", None)
                or f"effect-{index}"
            )
        self._effects.append(
            _RegisteredEffect(
                disposer=disposer,
                label=resolved_label,
                registration_index=index,
            )
        )
        return disposer

    def transaction(self, name: Optional[str] = None) -> "EffectTransaction":
        """Create an isolated transaction that must explicitly commit."""

        self._ensure_open()
        transaction_name = str(name or "").strip()
        if not transaction_name:
            transaction_name = f"{self._name}.transaction"
        return EffectTransaction(self, transaction_name)

    def _adopt(self, child: "EffectScope") -> None:
        """Atomically transfer an open child stack into this open scope."""

        self._ensure_open()
        child._ensure_open()
        for effect in child._detach_for_commit():
            self._effects.append(
                _RegisteredEffect(
                    disposer=effect.disposer,
                    label=effect.label,
                    registration_index=self._next_registration_index,
                )
            )
            self._next_registration_index += 1

    def _detach_for_commit(self) -> tuple[_RegisteredEffect, ...]:
        self._ensure_open()
        effects = tuple(self._effects)
        self._effects.clear()
        self._state = EffectScopeState.CLOSED
        return effects

    async def _dispose_all(
        self,
        effects: tuple[_RegisteredEffect, ...],
    ) -> None:
        failures: list[EffectDisposalFailure] = []
        try:
            for effect in reversed(effects):
                try:
                    result = effect.disposer()
                    if inspect.isawaitable(result):
                        await result
                except (Exception, asyncio.CancelledError) as exc:
                    failures.append(
                        EffectDisposalFailure(
                            label=effect.label,
                            registration_index=effect.registration_index,
                            error=exc,
                        )
                    )
        finally:
            self._state = EffectScopeState.CLOSED
        if failures:
            raise EffectDisposalError(self._name, tuple(failures))

    async def close(self) -> None:
        """Dispose every effect once, preserving LIFO and aggregate failures.

        Concurrent callers await the same close task. Cancelling one caller does
        not cancel cleanup; another close call can await the converged outcome.
        """

        close_task = self._close_task
        if close_task is None:
            if self._state is EffectScopeState.CLOSED:
                return
            self._ensure_open()
            self._state = EffectScopeState.CLOSING
            effects = tuple(self._effects)
            self._effects.clear()
            close_task = asyncio.create_task(
                self._dispose_all(effects),
                name=f"effect-scope-close:{self._name}",
            )
            self._close_task = close_task

        # A disposer may defensively call close() on its own scope. Treat that
        # re-entrant call as an idempotent no-op instead of awaiting itself.
        if asyncio.current_task() is close_task:
            return
        await asyncio.shield(close_task)


class EffectTransaction:
    """An isolated effect stack that rolls back unless explicitly committed."""

    def __init__(self, parent: EffectScope, name: str) -> None:
        self._parent = parent
        self._name = name
        self._scope = EffectScope(name)
        self._state = EffectTransactionState.NEW

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> EffectTransactionState:
        return self._state

    @property
    def effect_count(self) -> int:
        return self._scope.effect_count

    def _require_state(
        self,
        operation: str,
        *allowed: EffectTransactionState,
    ) -> None:
        if self._state not in allowed:
            raise EffectTransactionStateError(self._name, operation, self._state)

    async def __aenter__(self) -> "EffectTransaction":
        self._require_state("enter", EffectTransactionState.NEW)
        self._parent._ensure_open()
        self._state = EffectTransactionState.ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type,
        exc: Optional[BaseException],
        traceback,
    ) -> bool:
        del exc_type, traceback
        self._require_state(
            "exit",
            EffectTransactionState.ACTIVE,
            EffectTransactionState.COMMIT_REQUESTED,
            EffectTransactionState.ROLLED_BACK,
        )
        if self._state is EffectTransactionState.ROLLED_BACK:
            return False
        if exc is not None:
            await self._rollback(trigger=exc)
            return False
        if self._state is EffectTransactionState.COMMIT_REQUESTED:
            try:
                self._parent._adopt(self._scope)
            except BaseException as commit_error:
                await self._rollback(trigger=commit_error)
                raise
            self._state = EffectTransactionState.COMMITTED
            return False
        await self._rollback()
        return False

    def add(self, disposer: Disposer, *, label: Optional[str] = None) -> Disposer:
        self._require_state("add", EffectTransactionState.ACTIVE)
        return self._scope.add(disposer, label=label)

    def commit(self) -> None:
        """Request commit; transfer occurs only after a clean context exit."""

        self._require_state("commit", EffectTransactionState.ACTIVE)
        self._state = EffectTransactionState.COMMIT_REQUESTED

    async def rollback(self) -> None:
        self._require_state(
            "rollback",
            EffectTransactionState.ACTIVE,
            EffectTransactionState.COMMIT_REQUESTED,
            EffectTransactionState.ROLLED_BACK,
        )
        if self._state is EffectTransactionState.ROLLED_BACK:
            return
        await self._rollback()

    async def _rollback(self, trigger: Optional[BaseException] = None) -> None:
        self._state = EffectTransactionState.ROLLING_BACK
        try:
            await self._scope.close()
        except EffectDisposalError as disposal_error:
            self._state = EffectTransactionState.ROLLED_BACK
            if trigger is not None:
                raise EffectTransactionError(
                    self._name,
                    trigger,
                    disposal_error.failures,
                ) from trigger
            raise
        self._state = EffectTransactionState.ROLLED_BACK
