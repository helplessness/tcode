"""Host-owned semantic-memory capability used by plugins and lifecycle code.

The service is the only plugin-facing path to the existing global, team, worker,
and session stores.  It keeps filesystem paths private, emits typed lifecycle
hooks, and returns JSON-compatible projections without embeddings.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..plugins.hooks import HookDispatchMode, HookEvent, HookRegistry
from ..team.memory import TeamMemoryManager
from ..team.worker_memory import WorkerMemoryManager
from .manager import MemoryManager
from .session_memory import SessionMemoryManager


MEMORY_SCOPES = frozenset({"global", "team", "worker", "session"})


class MemoryServiceError(RuntimeError):
    """Base failure raised by the host memory capability."""

    code = "memory.service"


class MemoryOperationRejected(MemoryServiceError):
    """A before hook explicitly rejected a memory operation."""

    code = "memory.operation.rejected"


def memory_target(scope: str, scope_key: str = "") -> str:
    """Return the canonical permission target for one memory namespace."""

    normalized_scope = str(scope or "").strip().lower()
    normalized_key = str(scope_key or "").strip()
    if normalized_scope not in MEMORY_SCOPES:
        raise ValueError(f"unsupported memory scope: {scope!r}")
    if normalized_scope == "global":
        if normalized_key:
            raise ValueError("global memory does not accept a scope_key")
        return "global"
    if not normalized_key:
        raise ValueError(f"{normalized_scope} memory requires a scope_key")
    if (
        len(normalized_key) > 240
        or normalized_key in {".", ".."}
        or any(token in normalized_key for token in ("/", "\\", "\x00"))
    ):
        raise ValueError("memory scope_key must be an opaque identifier, not a path")
    return f"{normalized_scope}:{normalized_key}"


def _entry_dict(entry: Any) -> dict[str, Any]:
    if is_dataclass(entry):
        raw = asdict(entry)
    elif isinstance(entry, Mapping):
        raw = dict(entry)
    else:
        raw = dict(vars(entry))
    raw.pop("embedding", None)
    raw.pop("search_text", None)
    return raw


class ScopedMemoryService:
    """Versioned, hook-aware adapter over the existing scoped memory stores."""

    def __init__(self, workdir: str) -> None:
        resolved = Path(str(workdir or ".")).expanduser().resolve()
        self.workdir = str(resolved)

    @staticmethod
    def _normalize_scope(scope: str, scope_key: str = "") -> tuple[str, str]:
        target = memory_target(scope, scope_key)
        normalized_scope, _, normalized_key = target.partition(":")
        return normalized_scope, normalized_key

    @staticmethod
    def _manager(scope: str):
        return {
            "global": MemoryManager,
            "team": TeamMemoryManager,
            "worker": WorkerMemoryManager,
            "session": SessionMemoryManager,
        }[scope]

    async def _before(
        self,
        event: HookEvent,
        *,
        operation_id: str,
        scope: str,
        scope_key: str,
        request: dict[str, Any],
        extra: Optional[dict[str, Any]] = None,
        allow_rejection: bool = True,
    ) -> dict[str, Any]:
        payload = {
            "operation_id": operation_id,
            "scope": scope,
            "scope_key": scope_key,
            "__workspace_path": self.workdir,
            **dict(extra or {}),
        }
        output = await HookRegistry.dispatch(
            event,
            input_data=payload,
            output_data={"action": "continue", "request": dict(request)},
            mode=HookDispatchMode.SERIAL,
        )
        if (
            allow_rejection
            and str(output.get("action") or "continue").strip().lower() == "deny"
        ):
            reason = str(output.get("reason") or "memory operation rejected by plugin")
            raise MemoryOperationRejected(reason[:500])
        patched = output.get("request")
        if not isinstance(patched, Mapping):
            raise MemoryServiceError("memory before hook returned an invalid request patch")
        return dict(patched)

    async def _after(self, event: HookEvent, payload: dict[str, Any]) -> None:
        payload = {"__workspace_path": self.workdir, **payload}
        await HookRegistry.dispatch(
            event,
            input_data=payload,
            mode=HookDispatchMode.PARALLEL,
        )

    async def _error(
        self,
        *,
        operation: str,
        operation_id: str,
        scope: str,
        scope_key: str,
        error: BaseException,
    ) -> None:
        try:
            await HookRegistry.dispatch(
                HookEvent.MEMORY_OPERATION_ERROR,
                input_data={
                    "operation_id": operation_id,
                    "operation": operation,
                    "scope": scope,
                    "scope_key": scope_key,
                    "__workspace_path": self.workdir,
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                },
                mode=HookDispatchMode.PARALLEL,
            )
        except Exception:
            pass

    def _load_raw(self, scope: str, scope_key: str) -> list[Any]:
        manager = self._manager(scope)
        if scope == "global":
            return list(manager.load(self.workdir))
        return list(manager.load(scope_key, self.workdir))

    def _query_raw(
        self,
        scope: str,
        scope_key: str,
        *,
        query: Optional[str],
        limit: int,
        level: str,
        filters: Optional[dict[str, Any]],
    ) -> list[Any]:
        manager = self._manager(scope)
        if scope == "global":
            return list(
                manager.query(
                    workdir=self.workdir,
                    query=query,
                    limit=limit,
                    level=level,
                    filters=filters,
                )
            )
        return list(
            manager.query(
                scope_key,
                self.workdir,
                query=query,
                limit=limit,
                level=level,
                filters=filters,
            )
        )

    def _get_raw(self, scope: str, scope_key: str, memory_id: str) -> Any:
        manager = self._manager(scope)
        if scope == "global":
            return manager.get(memory_id, workdir=self.workdir, level="l2")
        return manager.get(scope_key, self.workdir, memory_id, level="l2")

    def _put_raw(
        self,
        scope: str,
        scope_key: str,
        *,
        content: str,
        tags: list[str],
        source: str,
        structured: Optional[dict[str, Any]],
    ) -> Any:
        manager = self._manager(scope)
        if scope == "global":
            return manager.add(
                content=content,
                tags=tags,
                workdir=self.workdir,
                source=source,
                structured=structured,
            )
        return manager.add(
            scope_key,
            self.workdir,
            content=content,
            tags=tags,
            source=source,
            structured=structured,
        )

    def _delete_raw(self, scope: str, scope_key: str, memory_id: str) -> bool:
        manager = self._manager(scope)
        if scope == "global":
            return bool(manager.delete(memory_id, workdir=self.workdir))
        return bool(manager.delete(scope_key, self.workdir, memory_id))

    @staticmethod
    def _idempotency_tag(value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"tcode:idempotency:{digest}"

    async def query(
        self,
        *,
        scope: str,
        scope_key: str = "",
        query: Optional[str] = None,
        limit: int = 20,
        level: str = "l1",
        filters: Optional[dict[str, Any]] = None,
        source: str = "host",
    ) -> dict[str, Any]:
        scope, scope_key = self._normalize_scope(scope, scope_key)
        operation_id = uuid.uuid4().hex
        try:
            request = await self._before(
                HookEvent.MEMORY_RECALL_BEFORE,
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                request={
                    "query": str(query or "")[:4000],
                    "limit": max(1, min(int(limit or 20), 200)),
                    "level": str(level or "l1").lower(),
                    "filters": dict(filters or {}),
                },
                extra={"source": source},
            )
            safe_limit = max(1, min(int(request.get("limit") or 20), 200))
            safe_level = str(request.get("level") or "l1").lower()
            if safe_level not in {"l0", "l1", "l2"}:
                safe_level = "l1"
            safe_filters = request.get("filters")
            if safe_filters is not None and not isinstance(safe_filters, Mapping):
                raise ValueError("memory filters must be an object")
            items = self._query_raw(
                scope,
                scope_key,
                query=str(request.get("query") or "") or None,
                limit=safe_limit,
                level=safe_level,
                filters=dict(safe_filters or {}),
            )
            result = {
                "operation_id": operation_id,
                "scope": scope,
                "scope_key": scope_key,
                "items": [_entry_dict(item) for item in items],
            }
            await self._after(
                HookEvent.MEMORY_RECALL_AFTER,
                {
                    "operation_id": operation_id,
                    "scope": scope,
                    "scope_key": scope_key,
                    "source": source,
                    "count": len(items),
                    "memory_ids": [str(getattr(item, "id", "")) for item in items],
                },
            )
            return result
        except BaseException as exc:
            await self._error(
                operation="recall",
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                error=exc,
            )
            raise

    async def put(
        self,
        *,
        scope: str,
        scope_key: str = "",
        content: str,
        tags: Optional[list[str]] = None,
        structured: Optional[dict[str, Any]] = None,
        source: str = "host",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        scope, scope_key = self._normalize_scope(scope, scope_key)
        operation_id = uuid.uuid4().hex
        try:
            request = await self._before(
                HookEvent.MEMORY_WRITE_BEFORE,
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                request={
                    "content": str(content or "").strip(),
                    "tags": [str(item).strip() for item in (tags or []) if str(item).strip()],
                    "structured": dict(structured or {}),
                    "idempotency_key": str(idempotency_key or "").strip()[:500],
                },
                extra={"source": source},
            )
            safe_content = str(request.get("content") or "").strip()
            if not safe_content:
                raise ValueError("Memory content cannot be empty")
            safe_tags = request.get("tags") or []
            if not isinstance(safe_tags, (list, tuple)):
                raise ValueError("memory tags must be an array")
            normalized_tags = [
                str(item).strip()[:200]
                for item in safe_tags
                if str(item).strip()
            ][:64]
            key = str(request.get("idempotency_key") or "").strip()
            structured_value = request.get("structured")
            if structured_value is not None and not isinstance(structured_value, Mapping):
                raise ValueError("structured memory fields must be an object")
            # Keep the idempotency lookup and publication in one cross-process
            # critical section. The data-file lock is independently acquired by
            # the managers, so no async hooks or nested data locks run here.
            from contextlib import nullcontext
            from ..atomic_storage import file_transaction
            base = self._scope_paths(scope, scope_key)[0]
            guard = file_transaction(str(base) + ".idempotency") if key else nullcontext()
            with guard:
                entry = None
                if key:
                    marker = self._idempotency_tag(key)
                    normalized_tags.append(marker)
                    entry = next((candidate for candidate in self._load_raw(scope, scope_key)
                        if marker in list(getattr(candidate, "tags", []) or [])), None)
                created = entry is None
                if created:
                    entry = self._put_raw(
                        scope, scope_key, content=safe_content, tags=normalized_tags,
                        source=str(source or "host")[:200], structured=dict(structured_value or {}),
                    )
            result = {
                "operation_id": operation_id,
                "scope": scope,
                "scope_key": scope_key,
                "created": created,
                "entry": _entry_dict(entry),
            }
            await self._after(
                HookEvent.MEMORY_WRITE_AFTER,
                {
                    "operation_id": operation_id,
                    "scope": scope,
                    "scope_key": scope_key,
                    "source": source,
                    "created": created,
                    "memory_id": str(getattr(entry, "id", "")),
                },
            )
            return result
        except BaseException as exc:
            await self._error(
                operation="write",
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                error=exc,
            )
            raise

    async def delete(
        self,
        *,
        scope: str,
        scope_key: str = "",
        memory_id: str,
        source: str = "host",
    ) -> dict[str, Any]:
        scope, scope_key = self._normalize_scope(scope, scope_key)
        operation_id = uuid.uuid4().hex
        normalized_id = str(memory_id or "").strip()
        if not normalized_id:
            raise ValueError("memory_id is required")
        try:
            request = await self._before(
                HookEvent.MEMORY_DELETE_BEFORE,
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                request={"memory_id": normalized_id},
                extra={"source": source, "cleanup_scope": False},
            )
            normalized_id = str(request.get("memory_id") or "").strip()
            if not normalized_id:
                raise ValueError("memory_id is required")
            deleted = self._delete_raw(scope, scope_key, normalized_id)
            result = {
                "operation_id": operation_id,
                "scope": scope,
                "scope_key": scope_key,
                "memory_id": normalized_id,
                "deleted": deleted,
            }
            await self._after(
                HookEvent.MEMORY_DELETE_AFTER,
                {**result, "source": source, "cleanup_scope": False},
            )
            return result
        except BaseException as exc:
            await self._error(
                operation="delete",
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                error=exc,
            )
            raise

    async def promote(
        self,
        *,
        source_scope: str,
        source_scope_key: str = "",
        memory_id: str,
        target_scope: str,
        target_scope_key: str = "",
        move: bool = False,
        source: str = "host",
    ) -> dict[str, Any]:
        source_scope, source_scope_key = self._normalize_scope(
            source_scope, source_scope_key
        )
        target_scope, target_scope_key = self._normalize_scope(
            target_scope, target_scope_key
        )
        operation_id = uuid.uuid4().hex
        try:
            request = await self._before(
                HookEvent.MEMORY_PROMOTE_BEFORE,
                operation_id=operation_id,
                scope=source_scope,
                scope_key=source_scope_key,
                request={"memory_id": str(memory_id or "").strip(), "move": bool(move)},
                extra={
                    "source": source,
                    "target_scope": target_scope,
                    "target_scope_key": target_scope_key,
                },
            )
            normalized_id = str(request.get("memory_id") or "").strip()
            entry = self._get_raw(source_scope, source_scope_key, normalized_id)
            if entry is None:
                raise KeyError(f"memory not found: {normalized_id}")
            entry_data = _entry_dict(entry)
            promoted = self._put_raw(
                target_scope,
                target_scope_key,
                content=str(entry_data.get("l2_detail") or entry_data.get("content") or ""),
                tags=[
                    *[str(item) for item in entry_data.get("tags", [])],
                    f"promoted-from:{source_scope}",
                ],
                source=f"{source}:promote"[:200],
                structured={
                    key: entry_data.get(key)
                    for key in (
                        "task_type",
                        "goal",
                        "constraints",
                        "decision",
                        "outcome",
                        "confidence",
                        "toolchain",
                        "failure_reason",
                    )
                },
            )
            moved = bool(request.get("move"))
            if moved:
                self._delete_raw(source_scope, source_scope_key, normalized_id)
            result = {
                "operation_id": operation_id,
                "scope": source_scope,
                "scope_key": source_scope_key,
                "memory_id": normalized_id,
                "target_scope": target_scope,
                "target_scope_key": target_scope_key,
                "promoted_memory_id": str(getattr(promoted, "id", "")),
                "moved": moved,
            }
            await self._after(
                HookEvent.MEMORY_PROMOTE_AFTER,
                {**result, "source": source},
            )
            return result
        except BaseException as exc:
            await self._error(
                operation="promote",
                operation_id=operation_id,
                scope=source_scope,
                scope_key=source_scope_key,
                error=exc,
            )
            raise

    def _scope_paths(self, scope: str, scope_key: str) -> list[Path]:
        manager = self._manager(scope)
        if scope == "global":
            base = Path(manager._memory_path(self.workdir))
        else:
            base = Path(manager._path(scope_key, self.workdir))
        return [
            base,
            base.with_suffix(base.suffix + ".faiss"),
            base.with_suffix(base.suffix + ".faiss.meta.json"),
            base.with_suffix(".faiss"),
            base.with_suffix(".faiss.meta.json"),
            Path(str(base) + ".archive"),
        ]

    async def cleanup_scope(
        self,
        *,
        scope: str,
        scope_key: str = "",
        source: str = "host",
        allow_rejection: bool = True,
    ) -> dict[str, Any]:
        scope, scope_key = self._normalize_scope(scope, scope_key)
        operation_id = uuid.uuid4().hex
        try:
            entries = self._load_raw(scope, scope_key)
            await self._before(
                HookEvent.MEMORY_DELETE_BEFORE,
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                request={"count": len(entries)},
                extra={"source": source, "cleanup_scope": True},
                allow_rejection=allow_rejection,
            )
            removed_files: list[str] = []
            for path in dict.fromkeys(self._scope_paths(scope, scope_key)):
                if not path.exists():
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed_files.append(path.name)
            result = {
                "operation_id": operation_id,
                "scope": scope,
                "scope_key": scope_key,
                "deleted_count": len(entries),
                "removed_files": removed_files,
            }
            await self._after(
                HookEvent.MEMORY_DELETE_AFTER,
                {**result, "source": source, "cleanup_scope": True},
            )
            return result
        except BaseException as exc:
            await self._error(
                operation="cleanup_scope",
                operation_id=operation_id,
                scope=scope,
                scope_key=scope_key,
                error=exc,
            )
            raise


__all__ = [
    "MEMORY_SCOPES",
    "MemoryOperationRejected",
    "MemoryServiceError",
    "ScopedMemoryService",
    "memory_target",
]
