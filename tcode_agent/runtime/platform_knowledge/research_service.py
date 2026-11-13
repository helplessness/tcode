"""Owner-scoped read-only access to research projects and Research Packs."""

from __future__ import annotations

from typing import Any

from ...research.pack_store import ResearchPackStore
from ...research.topic_store import ResearchTopicStore
from .contracts import KnowledgeOwner, PlatformKnowledgeValidationError
from .policy import KnowledgeAccessPolicy


_PRIVATE_KEYS = {
    "archive_path",
    "database_path",
    "db_path",
    "file_path",
    "path",
    "staged_path",
    "storage_path",
}


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_public(item) for item in value[:200]]
    if isinstance(value, str):
        return value[:20_000]
    return value


class ResearchService:
    def __init__(self, owner: KnowledgeOwner) -> None:
        self.owner = owner
        self.owner_args = {
            "tenant_id": owner.tenant_id,
            "user_id": owner.user_id,
            "workspace_id": owner.workspace_id,
            "workdir": owner.workdir,
        }

    def list_projects(self, *, include_archived: bool = False, limit: int = 20) -> dict[str, Any]:
        projects = ResearchTopicStore.list_projects(
            **self.owner_args,
            include_archived=bool(include_archived),
            limit=max(1, min(int(limit or 20), 100)),
        )
        return {
            "count": len(projects),
            "projects": [_public(item.to_dict()) for item in projects],
        }

    def get_project(self, research_id: str) -> dict[str, Any]:
        normalized = KnowledgeAccessPolicy.bounded_text(
            research_id, maximum=200, field="research_id"
        )
        project = ResearchTopicStore.get_project(normalized, **self.owner_args)
        if project is None:
            raise PlatformKnowledgeValidationError("Research project was not found")
        return {"project": _public(project.to_dict())}

    def list_packs(self, *, research_id: str = "", limit: int = 20) -> dict[str, Any]:
        normalized = str(research_id or "").strip()
        if normalized:
            self.get_project(normalized)
        packs = ResearchPackStore.list_exports(
            research_id=normalized,
            **self.owner_args,
            limit=max(1, min(int(limit or 20), 100)),
        )
        return {
            "count": len(packs),
            "packs": [_public(item.to_dict()) for item in packs],
        }


__all__ = ["ResearchService"]
