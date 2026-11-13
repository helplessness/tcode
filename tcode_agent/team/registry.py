"""Worker registry - manages TeamMode worker lifecycle"""

import asyncio
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator, Optional

from ..file_lock import lock_state_file as lock_file, unlock_file


@dataclass
class WorkerInfo:
    """Represents a persistent Worker Agent in a team"""

    worker_id: str
    name: str                # human-readable lowercase name, e.g. "alice"
    role: str                # role description, e.g. "前端工程师"
    session_id: str          # backing tcode Session ID
    team_id: str             # Manager's session_id (the team owner)
    status: str              # "idle" | "busy" | "terminated"
    permission_preset: str   # "readonly" | "developer" | "full"
    created_at: float
    current_task: Optional[str] = None
    result_summary: Optional[str] = None
    deletable: bool = True   # False = fixed worker (e.g. protein team), cannot be removed
    last_task: Optional[str] = None
    task_count: int = 0
    memory_key: Optional[str] = None  # Fixed key for cross-session worker memory, e.g. 'protein_orchestrator:requirements'
    success_count: int = 0
    failure_count: int = 0
    avg_latency_sec: float = 0.0
    common_tools: list[str] = field(default_factory=list)
    skill_tags: list[str] = field(default_factory=list)
    recent_failure_reason: Optional[str] = None
    peer_whitelist: list[str] = field(default_factory=list)


class WorkerRegistry:
    """Disk-persisted registry of all Workers across all teams"""

    _workers: dict[str, WorkerInfo] = {}
    _lock: Optional[asyncio.Lock] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def _storage_path(cls) -> Path:
        base = os.environ.get("TCODE_WORKDIR", os.getcwd())
        return Path(base) / ".tcode" / "teams" / "workers.json"

    @classmethod
    @contextmanager
    def _process_file_lock(cls) -> Iterator[None]:
        """Serialize registry mutations across server/worker processes."""

        path = cls._storage_path().with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        acquired = False
        try:
            lock_file(handle)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    unlock_file(handle)
            finally:
                handle.close()

    @classmethod
    def _save(cls) -> None:
        path = cls._storage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in cls._workers.items()}
        temp_name = ""
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".workers-",
                suffix=".tmp",
                dir=str(path.parent),
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            temp_name = ""
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    @classmethod
    def _load(cls) -> None:
        path = cls._storage_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            field_names = set(WorkerInfo.__dataclass_fields__)
            loaded: dict[str, WorkerInfo] = {}
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                try:
                    filtered = {
                        name: item
                        for name, item in value.items()
                        if name in field_names
                    }
                    info = WorkerInfo(**filtered)
                except (TypeError, ValueError):
                    continue
                loaded[str(key)] = info
            cls._workers = loaded
        except Exception:
            pass

    @classmethod
    async def create_worker(
        cls,
        team_id: str,
        name: str,
        role: str,
        session_id: str,
        permission_preset: str = "developer",
        deletable: bool = True,
        memory_key: Optional[str] = None,
        skill_tags: Optional[list[str]] = None,
        peer_whitelist: Optional[list[str]] = None,
    ) -> WorkerInfo:
        async with cls._get_lock():
            with cls._process_file_lock():
                cls._load()
                existing = next(
                    (
                        worker
                        for worker in cls._workers.values()
                        if worker.team_id == team_id
                        and worker.name == str(name or "").strip().lower()
                    ),
                    None,
                )
                if existing is not None:
                    return existing
                info = WorkerInfo(
                    worker_id=session_id,
                    name=str(name or "").strip().lower(),
                    role=role,
                    session_id=session_id,
                    team_id=team_id,
                    status="idle",
                    permission_preset=permission_preset,
                    created_at=time.time(),
                    deletable=deletable,
                    memory_key=memory_key,
                    skill_tags=[str(tag).strip() for tag in (skill_tags or []) if str(tag).strip()],
                    peer_whitelist=[
                        str(name).strip().lower()
                        for name in (peer_whitelist or [])
                        if str(name).strip()
                    ],
                )
                cls._workers[session_id] = info
                cls._save()
                return info

    @classmethod
    def get_by_name(cls, team_id: str, name: str) -> Optional[WorkerInfo]:
        cls._load()
        identifier = name.strip()
        normalized_name = identifier.lower()
        for w in cls._workers.values():
            if w.team_id == team_id and (
                w.name == normalized_name or w.session_id == identifier
            ):
                return w
        return None

    @classmethod
    async def reconcile_team_sessions(
        cls,
        team_id: str,
        *,
        owner_kwargs: Optional[dict[str, str]] = None,
    ) -> list[str]:
        """Rebuild missing registry rows from durable child-session metadata."""

        from ..session.manager import SessionManager

        owner = dict(owner_kwargs or {})
        try:
            sessions = await SessionManager.list(parent_id=team_id, **owner)
        except Exception:
            return []
        recovered: list[str] = []
        for session in sessions:
            session_id = str(getattr(session, "id", "") or "").strip()
            if not session_id or cls.get(session_id) is not None:
                continue
            metadata = dict(getattr(session, "metadata", {}) or {})
            registration = metadata.get("team_worker_registration")
            registration = dict(registration) if isinstance(registration, dict) else {}
            agent_name = str(getattr(session, "agent_name", "") or "").strip()
            name = str(registration.get("name") or "").strip().lower()
            if not name and agent_name.startswith("worker_"):
                name = agent_name.removeprefix("worker_").strip().lower()
            if not name:
                continue
            role = str(
                registration.get("role")
                or metadata.get("worker_role")
                or getattr(session, "title", "")
                or name
            ).strip()
            recovered_worker = await cls.create_worker(
                team_id=team_id,
                name=name,
                role=role,
                session_id=session_id,
                permission_preset=str(
                    registration.get("permission_preset") or "developer"
                ).strip(),
                deletable=bool(registration.get("deletable", True)),
                memory_key=(
                    str(registration.get("memory_key") or "").strip() or None
                ),
                skill_tags=[
                    str(item).strip()
                    for item in registration.get("skill_tags", [])
                    if str(item).strip()
                ],
                peer_whitelist=[
                    str(item).strip().lower()
                    for item in registration.get("peer_whitelist", [])
                    if str(item).strip()
                ],
            )
            if recovered_worker.session_id == session_id:
                recovered.append(session_id)
        return recovered

    @classmethod
    async def resolve_for_session_tree(
        cls,
        requester_session_id: str,
        identifier: str,
        *,
        owner_kwargs: Optional[dict[str, str]] = None,
    ) -> tuple[Optional[WorkerInfo], list[WorkerInfo]]:
        """Resolve a worker inside the caller's owned session tree.

        The current team wins.  Otherwise the nearest unambiguous worker may be
        addressed by name or by its durable session id.  This lets a manager route
        work into a delegated team without making the registry globally visible.
        """

        normalized_identifier = str(identifier or "").strip()
        if not normalized_identifier:
            return None, []
        direct = cls.get_by_name(requester_session_id, normalized_identifier)
        if direct is not None:
            return direct, [direct]

        from ..session.manager import SessionManager

        owner = dict(owner_kwargs or {})
        sessions: dict[str, Any] = {}
        current_id = requester_session_id
        seen_ancestors: set[str] = set()
        while current_id and current_id not in seen_ancestors:
            seen_ancestors.add(current_id)
            current = await SessionManager.get(current_id, **owner)
            if current is None:
                break
            sessions[current_id] = current
            parent_id = str(getattr(current, "parent_id", "") or "").strip()
            if not parent_id:
                break
            current_id = parent_id
        root_id = current_id or requester_session_id

        queue = [root_id]
        expanded: set[str] = set()
        while queue:
            parent_id = queue.pop(0)
            if parent_id in expanded:
                continue
            expanded.add(parent_id)
            try:
                children = await SessionManager.list(parent_id=parent_id, **owner)
            except Exception:
                children = []
            for child in children:
                child_id = str(getattr(child, "id", "") or "").strip()
                if not child_id:
                    continue
                sessions.setdefault(child_id, child)
                if child_id not in expanded:
                    queue.append(child_id)

        for team_id in [requester_session_id, *sessions.keys()]:
            await cls.reconcile_team_sessions(team_id, owner_kwargs=owner)

        cls._load()
        tree_ids = set(sessions) | {requester_session_id, root_id}
        normalized_name = normalized_identifier.lower()
        candidates = [
            worker
            for worker in cls._workers.values()
            if worker.team_id in tree_ids
            and (
                worker.session_id == normalized_identifier
                or worker.name == normalized_name
            )
        ]
        if not candidates:
            return None, []
        exact_session = [
            worker
            for worker in candidates
            if worker.session_id == normalized_identifier
        ]
        if len(exact_session) == 1:
            return exact_session[0], candidates

        parent_by_id = {
            session_id: str(getattr(session, "parent_id", "") or "").strip()
            for session_id, session in sessions.items()
        }
        adjacency: dict[str, set[str]] = {session_id: set() for session_id in tree_ids}
        for session_id, parent_id in parent_by_id.items():
            if parent_id and parent_id in adjacency:
                adjacency.setdefault(session_id, set()).add(parent_id)
                adjacency.setdefault(parent_id, set()).add(session_id)
        distances = {requester_session_id: 0}
        distance_queue = [requester_session_id]
        while distance_queue:
            item = distance_queue.pop(0)
            for neighbor in adjacency.get(item, set()):
                if neighbor in distances:
                    continue
                distances[neighbor] = distances[item] + 1
                distance_queue.append(neighbor)
        minimum = min(distances.get(worker.team_id, 10**9) for worker in candidates)
        nearest = [
            worker
            for worker in candidates
            if distances.get(worker.team_id, 10**9) == minimum
        ]
        return (nearest[0], candidates) if len(nearest) == 1 else (None, candidates)

    @classmethod
    def get(cls, session_id: str) -> Optional[WorkerInfo]:
        cls._load()
        return cls._workers.get(session_id)

    @classmethod
    def list_for_team(cls, team_id: str) -> list[WorkerInfo]:
        cls._load()
        return sorted(
            [w for w in cls._workers.values() if w.team_id == team_id],
            key=lambda w: w.created_at,
        )

    @classmethod
    async def update(cls, session_id: str, **kwargs) -> None:
        async with cls._get_lock():
            with cls._process_file_lock():
                cls._load()
                if session_id in cls._workers:
                    for k, v in kwargs.items():
                        if hasattr(cls._workers[session_id], k):
                            setattr(cls._workers[session_id], k, v)
                    cls._save()

    @classmethod
    async def record_execution(
        cls,
        session_id: str,
        *,
        success: bool,
        latency_sec: float,
        tools_used: Optional[list[str]] = None,
        skill_tags: Optional[list[str]] = None,
        failure_reason: Optional[str] = None,
    ) -> None:
        async with cls._get_lock():
            with cls._process_file_lock():
                cls._load()
                worker = cls._workers.get(session_id)
                if not worker:
                    return

                if success:
                    worker.success_count += 1
                else:
                    worker.failure_count += 1
                    worker.recent_failure_reason = (failure_reason or "").strip() or None

                total_runs = max(1, worker.success_count + worker.failure_count)
                previous_weight = max(0, total_runs - 1)
                worker.avg_latency_sec = (
                    (worker.avg_latency_sec * previous_weight) + max(0.0, float(latency_sec or 0.0))
                ) / total_runs

                merged_tools = list(worker.common_tools)
                for tool in tools_used or []:
                    value = str(tool or "").strip()
                    if value and value not in merged_tools:
                        merged_tools.append(value)
                worker.common_tools = merged_tools[-20:]

                merged_tags = list(worker.skill_tags)
                for tag in skill_tags or []:
                    value = str(tag or "").strip()
                    if value and value not in merged_tags:
                        merged_tags.append(value)
                worker.skill_tags = merged_tags[-20:]
                cls._save()

    @classmethod
    async def delete(cls, session_id: str) -> bool:
        async with cls._get_lock():
            with cls._process_file_lock():
                cls._load()
                if session_id not in cls._workers:
                    return False
                del cls._workers[session_id]
                cls._save()
                return True

    @classmethod
    async def delete_for_team(cls, team_id: str) -> list[str]:
        async with cls._get_lock():
            with cls._process_file_lock():
                cls._load()
                removed: list[str] = []
                for session_id, worker in list(cls._workers.items()):
                    if worker.team_id != team_id:
                        continue
                    removed.append(session_id)
                    del cls._workers[session_id]
                if removed:
                    cls._save()
                return removed
