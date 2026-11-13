"""Proposal-only external Harness access to the platform-owned Paper Wiki."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...evidence.store import EvidenceStore
from .contracts import KnowledgeOwner, PlatformKnowledgeValidationError
from .policy import KnowledgeAccessPolicy


_STORAGE_KEYS = {
    "path",
    "file_path",
    "storage_path",
    "library_root",
    "database_path",
    "db_path",
}


def _strip_storage(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_storage(item)
            for key, item in value.items()
            if str(key).lower() not in _STORAGE_KEYS
        }
    if isinstance(value, list):
        return [_strip_storage(item) for item in value]
    return value


def _evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) == "evidence_id" and str(item or "").strip():
                found.add(str(item).strip())
            elif str(key) == "evidence_ids" and isinstance(item, list):
                found.update(str(entry).strip() for entry in item if str(entry).strip())
            else:
                found.update(_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_evidence_ids(item))
    return found


class WikiService:
    def __init__(self, owner: KnowledgeOwner) -> None:
        self.owner = owner

    def _store(self):
        library_root = (
            Path(self.owner.workdir)
            / ".tcode"
            / "plugins"
            / "paper_research_workflow"
            / "library"
        )
        state_db = library_root / "wiki" / "state" / "wiki.sqlite3"
        if not state_db.is_file():
            raise PlatformKnowledgeValidationError("Workspace Wiki is unavailable")
        from ...server.paper_wiki_loader import paper_wiki_module

        return paper_wiki_module().PaperWikiStore(
            library_root=library_root,
            workdir=self.owner.workdir,
            vector_search_enabled=True,
        )

    def search(self, query: str = "", *, limit: int = 8) -> dict[str, Any]:
        result = self._store().search(
            str(query or "")[:1000],
            limit=max(1, min(int(limit or 8), 25)),
            include_archived=False,
        )
        return _strip_storage(result)

    def read(self, page_id: str) -> dict[str, Any]:
        normalized = KnowledgeAccessPolicy.bounded_text(
            page_id, maximum=200, field="page_id"
        )
        return _strip_storage(self._store().get_page(normalized, include_history=False))

    def propose(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(operations, list) or not operations or len(operations) > 20:
            raise PlatformKnowledgeValidationError("Wiki proposal requires 1 to 20 operations")
        clean_operations = [dict(item) for item in operations if isinstance(item, dict)]
        if len(clean_operations) != len(operations):
            raise PlatformKnowledgeValidationError("Every Wiki proposal operation must be an object")
        missing = [
            evidence_id
            for evidence_id in sorted(_evidence_ids(clean_operations))
            if not EvidenceStore.exists(evidence_id, workdir=self.owner.workdir)
        ]
        if missing:
            raise PlatformKnowledgeValidationError(
                "Wiki proposal references unknown Evidence IDs: " + ", ".join(missing[:10])
            )
        proposal = self._store().stage_proposal(
            operations=clean_operations,
            trigger_kind="external_harness",
            trigger_doc_ids=[],
            created_by=f"external-harness:{self.owner.runtime_id}",
            compiler_version="tcode-platform-bridge-1",
            require_evidence=True,
        )
        result = _strip_storage(proposal)
        result["apply_policy"] = "proposal_only"
        result["applied"] = False
        return result


__all__ = ["WikiService"]
