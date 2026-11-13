"""Protein team bootstrapper - auto-creates fixed specialist workers for protein_orchestrator."""
from pathlib import Path
from typing import Any

from .worker_scoper import WorkerScoper
from .fixed_workers import FixedWorkerSpec, ensure_fixed_worker
from .protein_team_config import load_protein_team_config
from ..agent.agents import BuiltinAgents


class ProteinTeamBootstrapper:
    """Idempotently creates the fixed protein team workers when a protein_orchestrator session starts."""

    CONFIG_PATH = Path(__file__).parent / "config" / "protein_team.json"

    @classmethod
    def _load_config(cls) -> dict:
        return load_protein_team_config()

    @classmethod
    async def ensure_workers(
        cls,
        team_id: str,
        workdir: str,
        *,
        owner: dict[str, Any] | None = None,
    ) -> list[str]:
        """Create fixed protein workers for the given team_id if not already present.

        Returns list of worker session_ids that were newly created.
        """
        config = cls._load_config()
        created = []
        execution_domain = str(config.get("execution_domain") or "protein").strip()
        workspace_strategy = str(
            config.get("workspace_strategy") or "protein_run"
        ).strip()

        for w_cfg in config.get("workers", []):
            name = str(w_cfg.get("name", "")).strip().lower()
            if not name:
                continue
            if not bool(w_cfg.get("enabled", True)):
                continue

            scoped_worker_config = {
                **dict(w_cfg),
                "execution_domain": str(
                    w_cfg.get("execution_domain") or execution_domain
                ).strip(),
                "workspace_strategy": str(
                    w_cfg.get("workspace_strategy") or workspace_strategy
                ).strip(),
            }
            agent_info = WorkerScoper.build_from_config(
                scoped_worker_config,
                workdir=workdir,
            )
            # 自定义 protein worker 的 AgentInfo 是进程内注册。
            # 服务重启后即使 WorkerRegistry/Session 还在，也需要重新注册，
            # 否则 `/api/subagents/chat`、`/api/team/{id}/chat` 会找不到 agent。
            BuiltinAgents.register_custom(agent_info, source="protein_team")

            role = str(w_cfg.get("role", name))
            preset = str(w_cfg.get("permission_preset", "developer"))
            deletable = bool(w_cfg.get("deletable", False))
            memory_key = f"protein_orchestrator:{name}"
            metadata = {
                "execution_domain": scoped_worker_config["execution_domain"],
                "workspace_strategy": scoped_worker_config["workspace_strategy"],
            }
            if bool(w_cfg.get("resident_generalist", False)):
                metadata.update({
                    "shared_memory_key": memory_key,
                    "shared_memory_source": f"worker:{name}",
                    "task_child_policy": "ephemeral",
                    "resident_generalist": True,
                })

            created_now = await ensure_fixed_worker(
                FixedWorkerSpec(
                    name=name,
                    role=role,
                    agent_name=agent_info.name,
                    team_id=team_id,
                    workdir=workdir,
                    session_prefix="protein",
                    tenant_id=str((owner or {}).get("tenant_id") or ""),
                    user_id=str((owner or {}).get("user_id") or ""),
                    workspace_id=str((owner or {}).get("workspace_id") or ""),
                    permission_preset=preset,
                    deletable=deletable,
                    memory_key=memory_key,
                    skill_tags=[name, role, "protein"],
                    peer_whitelist=w_cfg.get("peer_whitelist") or [],
                    metadata=metadata,
                )
            )

            session_id = f"protein-{team_id[:12]}-{name}"
            if created_now:
                created.append(session_id)

        return created
