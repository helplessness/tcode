"""Bootstrap fixed team workers for every session execution entry point."""

from __future__ import annotations

from typing import Any, Optional

from .catalog import TeamCatalog, TeamCatalogEntry
from .fixed_workers import FixedWorkerSpec, ensure_fixed_worker


def team_worker_session_prefix(team_entry: TeamCatalogEntry) -> str:
    for prefix in team_entry.session_prefixes or ():
        value = str(prefix or "").strip().strip("-")
        if value:
            return value
    return (
        str(team_entry.slug or team_entry.key or "team").strip().strip("-")
        or "team"
    )


async def _ensure_catalog_workers(
    *,
    session_id: str,
    workdir: str,
    team_entry: TeamCatalogEntry,
    owner: Optional[dict[str, Any]] = None,
) -> list[str]:
    from ..agent.agents import BuiltinAgents

    workers = list(team_entry.workers or ())
    if not workers:
        return []
    created: list[str] = []
    session_prefix = team_worker_session_prefix(team_entry)
    team_key = str(team_entry.key or "").strip() or session_prefix
    runtime_agent = str(team_entry.runtime_agent or team_key).strip()

    for worker in workers:
        worker_name = str(worker.name or "").strip().lower()
        if not worker_name or not bool(worker.enabled):
            continue
        agent_name = str(worker.agent_name or "").strip() or worker_name
        if (
            BuiltinAgents.get(agent_name) is None
            and BuiltinAgents.get(f"worker_{worker_name}") is not None
        ):
            agent_name = f"worker_{worker_name}"
        if BuiltinAgents.get(agent_name) is None:
            continue

        metadata = dict(worker.metadata or {})
        metadata.setdefault("team_key", team_key)
        metadata.setdefault("team_worker_name", worker_name)
        metadata.setdefault("resident_worker", bool(worker.resident))
        metadata.setdefault(
            "execution_domain",
            str(metadata.get("domain") or team_key).strip(),
        )
        metadata.setdefault(
            "workspace_strategy",
            str(metadata.get("strategy") or "isolated").strip(),
        )
        created_now = await ensure_fixed_worker(
            FixedWorkerSpec(
                name=worker_name,
                role=str(worker.role or worker_name),
                agent_name=agent_name,
                team_id=session_id,
                workdir=workdir,
                session_prefix=session_prefix,
                tenant_id=str((owner or {}).get("tenant_id") or ""),
                user_id=str((owner or {}).get("user_id") or ""),
                workspace_id=str((owner or {}).get("workspace_id") or ""),
                permission_preset=str(worker.permission_preset or "developer"),
                deletable=bool(worker.deletable),
                memory_key=f"{runtime_agent}:{worker_name}",
                skill_tags=[team_key, worker_name, *list(worker.skill_tags or ())],
                peer_whitelist=list(worker.peer_whitelist or ()),
                metadata=metadata,
            )
        )
        if created_now:
            created.append(f"{session_prefix}-{session_id[:12]}-{worker_name}")
    return created


async def ensure_session_team_workers(
    *,
    session_id: str,
    agent_name: str,
    workdir: str,
    owner: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Idempotently initialize a team's workers regardless of launch surface."""

    entry = TeamCatalog.get_by_agent_name(agent_name, available_only=False)
    if entry is None:
        return []
    if entry.key == "protein":
        from .bootstrapper import ProteinTeamBootstrapper

        return await ProteinTeamBootstrapper.ensure_workers(
            session_id,
            workdir,
            owner=dict(owner or {}),
        )
    return await _ensure_catalog_workers(
        session_id=session_id,
        workdir=workdir,
        team_entry=entry,
        owner=owner,
    )
