"""固定团队 Worker 的统一创建辅助。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..session.manager import SessionManager
from .registry import WorkerRegistry


@dataclass
class FixedWorkerSpec:
    name: str
    role: str
    agent_name: str
    team_id: str
    workdir: str
    session_prefix: str
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    permission_preset: str = "developer"
    deletable: bool = False
    memory_key: str = ""
    skill_tags: list[str] = field(default_factory=list)
    peer_whitelist: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        return f"{self.session_prefix}-{self.team_id[:12]}-{self.name}"

    @property
    def title(self) -> str:
        return f"[固定] {self.role}"


async def ensure_fixed_worker(spec: FixedWorkerSpec) -> bool:
    """确保固定 Worker 的 session 和注册表项存在。

    返回值：
    - True: 本次新创建了 WorkerRegistry 项
    - False: 已存在，仅做了对齐/补全
    """

    owner_kwargs = {
        key: value
        for key, value in {
            "tenant_id": spec.tenant_id,
            "user_id": spec.user_id,
            "workspace_id": spec.workspace_id,
        }.items()
        if str(value or "").strip()
    }
    session_metadata = {
        **dict(spec.metadata or {}),
        "team_worker_registration": {
            "name": spec.name,
            "role": spec.role,
            "permission_preset": spec.permission_preset,
            "deletable": spec.deletable,
            "memory_key": spec.memory_key,
            "skill_tags": list(spec.skill_tags or []),
            "peer_whitelist": list(spec.peer_whitelist or []),
        },
    }

    duplicates = [
        worker
        for worker in WorkerRegistry.list_for_team(spec.team_id)
        if worker.name == spec.name
    ]
    existing = next(
        (worker for worker in duplicates if worker.session_id == spec.session_id),
        None,
    )
    if existing is None and duplicates:
        existing = duplicates[0]

    for worker in duplicates:
        if existing and worker.session_id == existing.session_id:
            continue
        await WorkerRegistry.delete(worker.session_id)
        await SessionManager.delete(worker.session_id, **owner_kwargs)

    # Older builds could leave child Session rows without a WorkerRegistry row.
    # If left around, the UI may show two fixed workers with the same title.
    keep_session_id = existing.session_id if existing else spec.session_id
    try:
        child_sessions = await SessionManager.list(parent_id=spec.team_id, **owner_kwargs)
    except Exception:
        child_sessions = []
    for child in child_sessions:
        child_id = str(getattr(child, "id", "") or "").strip()
        if not child_id or child_id == keep_session_id:
            continue
        child_title = str(getattr(child, "title", "") or "").strip()
        child_agent = str(getattr(child, "agent_name", "") or "").strip()
        if child_title == spec.title or child_agent == spec.agent_name:
            await SessionManager.delete(child_id, **owner_kwargs)

    if existing:
        stale_busy = False
        if str(getattr(existing, "status", "") or "").strip().lower() in {
            "busy",
            "running",
        }:
            try:
                from .coordinator import worker_task_active

                stale_busy = not worker_task_active(existing.session_id)
            except Exception:
                stale_busy = False
        session = await SessionManager.get(existing.session_id, **owner_kwargs)
        if session:
            merged_metadata = dict(getattr(session, "metadata", {}) or {})
            merged_metadata.update(session_metadata)
            await SessionManager.update(
                existing.session_id,
                **owner_kwargs,
                agent_name=spec.agent_name,
                title=spec.title,
                workdir=spec.workdir,
                metadata=merged_metadata,
            )
        else:
            stale_session = await SessionManager.get(existing.session_id)
            if stale_session and str(getattr(stale_session, "parent_id", "") or "") == spec.team_id:
                await SessionManager.delete(existing.session_id)
            await SessionManager.create(
                session_id=existing.session_id,
                parent_id=spec.team_id,
                agent_name=spec.agent_name,
                title=spec.title,
                workdir=spec.workdir,
                **owner_kwargs,
                metadata=session_metadata,
            )
        await WorkerRegistry.update(
            existing.session_id,
            role=spec.role,
            permission_preset=spec.permission_preset,
            deletable=spec.deletable,
            memory_key=spec.memory_key or None,
            skill_tags=list(spec.skill_tags or []),
            peer_whitelist=list(spec.peer_whitelist or []),
            **(
                {"status": "idle", "current_task": None}
                if stale_busy
                else {}
            ),
        )
        return False

    session = await SessionManager.get(spec.session_id, **owner_kwargs)
    if not session:
        stale_session = await SessionManager.get(spec.session_id)
        if stale_session and str(getattr(stale_session, "parent_id", "") or "") == spec.team_id:
            await SessionManager.delete(spec.session_id)
        await SessionManager.create(
            session_id=spec.session_id,
            parent_id=spec.team_id,
            agent_name=spec.agent_name,
            title=spec.title,
            workdir=spec.workdir,
            **owner_kwargs,
            metadata=session_metadata,
        )
    else:
        merged_metadata = dict(getattr(session, "metadata", {}) or {})
        merged_metadata.update(session_metadata)
        await SessionManager.update(
            spec.session_id,
            **owner_kwargs,
            agent_name=spec.agent_name,
            title=spec.title,
            workdir=spec.workdir,
            metadata=merged_metadata,
        )

    await WorkerRegistry.create_worker(
        team_id=spec.team_id,
        name=spec.name,
        role=spec.role,
        session_id=spec.session_id,
        permission_preset=spec.permission_preset,
        deletable=spec.deletable,
        memory_key=spec.memory_key or None,
        skill_tags=list(spec.skill_tags or []),
        peer_whitelist=list(spec.peer_whitelist or []),
    )
    return True
