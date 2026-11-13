"""Owner-bound wrapper around the existing authoritative EvidenceStore."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ...evidence.store import EvidenceStore, build_evidence_record
from .contracts import KnowledgeOwner, PlatformKnowledgeValidationError
from .policy import KnowledgeAccessPolicy


class EvidenceService:
    def __init__(self, owner: KnowledgeOwner) -> None:
        self.owner = owner

    def search(self, query: str = "", *, limit: int = 8) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 8), 25))
        records = EvidenceStore.search(
            query=str(query or "")[:1000],
            workdir=self.owner.workdir,
            limit=safe_limit,
        )
        return {
            "query": str(query or "")[:1000],
            "items": [self._public_record(item.to_dict()) for item in records],
            "count": len(records),
            "retrieval": self.index_status(),
        }

    def index_status(self) -> dict[str, Any]:
        from ...evidence.maintenance import public_status
        return public_status(self.owner.workdir)

    def get(self, evidence_id: str) -> dict[str, Any]:
        normalized = KnowledgeAccessPolicy.bounded_text(
            evidence_id, maximum=200, field="evidence_id"
        )
        record = EvidenceStore.get(normalized, workdir=self.owner.workdir)
        if record is None:
            raise PlatformKnowledgeValidationError("Evidence record was not found")
        return self._public_record(record.to_dict())

    def graph(self, evidence_id: str, *, depth: int = 1, limit: int = 40) -> dict[str, Any]:
        normalized = KnowledgeAccessPolicy.bounded_text(
            evidence_id, maximum=200, field="evidence_id"
        )
        return EvidenceStore.graph_neighborhood(
            normalized,
            workdir=self.owner.workdir,
            depth=max(1, min(int(depth or 1), 2)),
            limit=max(5, min(int(limit or 40), 80)),
            include_semantic_hints=False,
        )

    def doctor(self, evidence_id: str = "", *, text: str = "", limit: int = 200) -> dict[str, Any]:
        if len(str(evidence_id or "")) > 200 or len(str(text or "")) > 20_000:
            raise PlatformKnowledgeValidationError("Evidence doctor input exceeds the size limit")
        report = EvidenceStore.doctor(
            workdir=self.owner.workdir,
            evidence_id=str(evidence_id or "").strip(),
            text=str(text or ""),
            record_limit=max(1, min(500, int(limit or 200))),
            persist=False,
        )
        report["issues"] = report.get("issues", [])[:100]
        report["issues_truncated"] = report["issue_count"] > len(report["issues"])
        return report

    def create_from_source(
        self,
        *,
        title: str,
        content: str,
        source: dict[str, Any],
        claims: list[str] | None = None,
        tags: list[str] | None = None,
        known_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create evidence only when TCode can verify the referenced source."""

        normalized_content = KnowledgeAccessPolicy.bounded_text(
            content, maximum=20_000, field="content"
        )
        if not isinstance(source, dict) or not source:
            multiline = "Free-form Harness text cannot be promoted to Evidence without a verified source"
            raise PlatformKnowledgeValidationError(multiline)
        source_type = str(source.get("type") or "").strip()
        source_id = ""
        source_path = ""
        artifact_refs: dict[str, Any] = {}
        source_digest = ""
        if source_type == "workspace_file":
            _target, source_path, source_digest = KnowledgeAccessPolicy.source_file(
                self.owner.workdir, source.get("path")
            )
            source_id = source_path
        elif source_type == "artifact":
            artifact_id = str(source.get("artifact_id") or "").strip()
            artifact = next(
                (
                    item
                    for item in list(known_artifacts or [])
                    if str(item.get("artifact_id") or "") == artifact_id
                ),
                None,
            )
            if artifact is None:
                raise PlatformKnowledgeValidationError("Artifact source is not known to this Run")
            source_id = artifact_id
            source_path = str(artifact.get("relative_path") or "")
            source_digest = str(artifact.get("sha256") or "")
            artifact_refs = {"artifact_id": artifact_id}
        elif source_type == "evidence":
            parent_id = str(source.get("evidence_id") or "").strip()
            parent = EvidenceStore.get(parent_id, workdir=self.owner.workdir)
            if parent is None:
                raise PlatformKnowledgeValidationError("Parent Evidence source was not found")
            source_id = parent.evidence_id
            source_digest = parent.content_hash
        else:
            raise PlatformKnowledgeValidationError(
                "source.type must be workspace_file, artifact, or evidence"
            )

        provenance = {
            "schema": "tcode.platform-evidence-provenance.v1",
            "tenant_id": self.owner.tenant_id,
            "user_id": self.owner.user_id,
            "workspace_id": self.owner.workspace_id,
            "session_id": self.owner.session_id,
            "run_id": self.owner.run_id,
            "runtime_id": self.owner.runtime_id,
            "source_digest": source_digest,
            "content_digest": hashlib.sha256(normalized_content.encode()).hexdigest(),
        }
        record = build_evidence_record(
            content=normalized_content,
            title=str(title or "")[:500],
            kind="external_harness_source",
            source_type=source_type,
            source_id=source_id,
            source_path=source_path,
            claims=[str(item)[:2000] for item in list(claims or [])[:50]],
            tags=[str(item)[:200] for item in list(tags or [])[:50]],
            artifact_refs=artifact_refs,
            source_agent=f"external-harness:{self.owner.runtime_id}",
            metadata={"platform_provenance": provenance},
            workdir=self.owner.workdir,
        )
        created = EvidenceStore.add(record, workdir=self.owner.workdir)
        return self._public_record(created.to_dict())

    @staticmethod
    def _public_record(value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value or {})
        result.pop("embedding", None)
        result.pop("search_text", None)
        result["citation_marker"] = f"[evidence:{result.get('evidence_id', '')}]"
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded) > 100_000:
            result["content"] = str(result.get("content") or "")[:20_000]
            result["truncated"] = True
        return result


__all__ = ["EvidenceService"]
