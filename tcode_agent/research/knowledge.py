"""Stage 5 trusted links across Research, Paper Library, Evidence Brain, and Wiki."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from ..evidence import EvidenceStore, extract_evidence_ids
from .config import ResearchSettings, load_research_settings
from .episode_store import ResearchDerivationStore
from .knowledge_models import ResearchKnowledgeLintReport, ResearchKnowledgeLink
from .knowledge_store import ResearchKnowledgeStore
from .models import canonical_json, sha256_text, utc_now_iso


_SAFE_PAPER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")
_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+~-]{0,499}$")


def _unique(values: list[str] | None, limit: int = 5000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (values or [])
            if str(item or "").strip()
        )
    )[:limit]


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _evidence_ids_from_value(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in {"evidence_id", "citation_id"}:
                candidate = str(item or "").strip() if isinstance(item, str) else ""
                if _SAFE_EVIDENCE_ID.fullmatch(candidate):
                    found.append(candidate)
            elif normalized_key in {"evidence_ids", "evidence_refs", "citations"}:
                if isinstance(item, (list, tuple, set)):
                    found.extend(
                        candidate
                        for entry in item
                        if isinstance(entry, str)
                        and (candidate := str(entry or "").strip())
                        and _SAFE_EVIDENCE_ID.fullmatch(candidate)
                    )
                elif isinstance(item, str):
                    candidate = item.strip()
                    if _SAFE_EVIDENCE_ID.fullmatch(candidate):
                        found.append(candidate)
                found.extend(_evidence_ids_from_value(item))
            else:
                found.extend(_evidence_ids_from_value(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found.extend(_evidence_ids_from_value(item))
    elif isinstance(value, str):
        found.extend(extract_evidence_ids(value))
    return _unique(found, 1000)


class ResearchKnowledgeManager:
    """Build and validate a project-scoped, append-only knowledge projection."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str,
        settings: ResearchSettings | None = None,
    ):
        self.tenant_id = str(tenant_id or "").strip()
        self.user_id = str(user_id or "").strip()
        self.workspace_id = str(workspace_id or "").strip()
        self.workdir = Path(workdir).expanduser().resolve(strict=False)
        self.settings = settings or load_research_settings()
        self.library_root = (
            self.workdir
            / ".tcode"
            / "plugins"
            / "paper_research_workflow"
            / "library"
        )

    @property
    def owner(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
        }

    def _project(self, research_id: str):
        project = ResearchDerivationStore.get_project(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if project is None:
            raise ValueError("research project not found")
        return project

    def _wiki_store(self, *, create: bool = False):
        db_path = self.library_root / "wiki" / "state" / "wiki.sqlite3"
        if not create and not db_path.is_file():
            return None
        # Lazy import avoids making the research domain depend on server startup.
        from ..server.paper_wiki_loader import paper_wiki_module

        module = paper_wiki_module()
        return module.PaperWikiStore(
            library_root=self.library_root,
            workdir=self.workdir.as_posix(),
            vector_search_enabled=False,
        )

    def _wiki_snapshot(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        store = self._wiki_store()
        if store is None:
            return {}, {}
        pages: dict[str, dict[str, Any]] = {}
        claims: dict[str, dict[str, Any]] = {}
        offset = 0
        remaining = self.settings.knowledge_max_wiki_pages
        while remaining > 0:
            result = store.list_pages(
                include_archived=True,
                limit=min(200, remaining),
                offset=offset,
            )
            batch = list(result.get("pages") or [])
            if not batch:
                break
            for summary in batch:
                page_id = str(summary.get("page_id") or "")
                if not page_id:
                    continue
                page = store.get_page(page_id)
                pages[page_id] = page
                for claim in page.get("claims") or []:
                    claim_id = str(claim.get("claim_id") or "")
                    if claim_id:
                        claims[claim_id] = {**claim, "page_id": page_id, "page": page}
            offset += len(batch)
            remaining -= len(batch)
            if offset >= int(result.get("total") or 0):
                break
        return pages, claims

    def _paper_record_path(self, doc_id: str) -> Path | None:
        normalized = str(doc_id or "").strip()
        if not _SAFE_PAPER_ID.fullmatch(normalized):
            return None
        docs_root = (self.library_root / "docs").resolve(strict=False)
        candidates = []

        # New Paper Library ingests are published into an immutable version
        # directory.  registry.json is the authoritative pointer; the legacy
        # docs/<doc_id>/record.json path may intentionally not exist.
        registry_path = self.library_root / "registry.json"
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            registry = {}
        documents = registry.get("documents") if isinstance(registry, dict) else []
        documents = documents if isinstance(documents, list) else []
        entry = next(
            (
                dict(item)
                for item in documents
                if isinstance(item, dict) and str(item.get("doc_id") or "") == normalized
            ),
            {},
        )
        if entry:
            artifacts = entry.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            for raw in (
                artifacts.get("record_path"),
                entry.get("record_path"),
            ):
                value = str(raw or "").strip()
                if not value:
                    continue
                path = Path(value).expanduser()
                if path.is_absolute():
                    candidates.append(path.resolve(strict=False))
                    if value == "/workspace" or value.startswith("/workspace/"):
                        relative = value[len("/workspace") :].lstrip("/")
                        candidates.append((self.workdir / relative).resolve(strict=False))
                else:
                    candidates.extend(
                        (
                            (self.library_root / path).resolve(strict=False),
                            (self.workdir / path).resolve(strict=False),
                        )
                    )
            version_id = str(entry.get("version_id") or "").strip()
            if version_id and _SAFE_PAPER_ID.fullmatch(version_id):
                candidates.append(
                    (docs_root / normalized / "versions" / version_id / "record.json")
                    .resolve(strict=False)
                )

        candidates.append((docs_root / normalized / "record.json").resolve(strict=False))
        for path in candidates:
            try:
                path.relative_to(docs_root)
            except ValueError:
                continue
            if path.is_file():
                return path
        return None

    def _paper_record(self, doc_id: str) -> tuple[dict[str, Any], str]:
        path = self._paper_record_path(doc_id)
        if path is None or not path.is_file():
            return {}, ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}, _file_hash(path)
        return (dict(payload) if isinstance(payload, dict) else {}), _file_hash(path)

    @staticmethod
    def _evidence_doc_id(record: Any) -> str:
        candidates = [
            (record.metadata or {}).get("paper_doc_id"),
            (record.artifact_refs or {}).get("doc_id"),
        ]
        for citation in record.citations or []:
            candidates.append(citation.get("doc_id"))
        for value in candidates:
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _evidence_anchor_ids(record: Any) -> list[str]:
        values: list[str] = []
        for container in (record.artifact_refs or {}, record.metadata or {}):
            values.extend(_unique(container.get("anchor_ids") or []))
        for citation in record.citations or []:
            values.extend(_unique(citation.get("anchor_ids") or []))
            if citation.get("chunk_id"):
                values.append(str(citation["chunk_id"]))
        paper_anchor = dict((record.metadata or {}).get("paper_anchor") or {})
        for key in ("anchor_id", "chunk_id"):
            if paper_anchor.get(key):
                values.append(str(paper_anchor[key]))
        return _unique(values)

    @staticmethod
    def _passages_for_evidence(record: Any, paper_record: dict[str, Any]) -> list[dict[str, Any]]:
        anchor_ids = set(ResearchKnowledgeManager._evidence_anchor_ids(record))
        paper_chunk = dict((record.metadata or {}).get("paper_chunk") or {})
        if paper_chunk.get("chunk_id"):
            anchor_ids.add(str(paper_chunk["chunk_id"]))
        anchor_lookup = {
            str(item.get("anchor_id") or ""): dict(item)
            for item in paper_record.get("evidence_anchors") or []
            if isinstance(item, dict) and str(item.get("anchor_id") or "")
        }
        chunk_ids = set(anchor_ids)
        for anchor_id in anchor_ids:
            anchor = anchor_lookup.get(anchor_id) or {}
            if anchor.get("chunk_id"):
                chunk_ids.add(str(anchor["chunk_id"]))
        return [
            dict(passage)
            for passage in paper_record.get("l1_passage_summaries") or []
            if isinstance(passage, dict)
            and chunk_ids.intersection(
                str(item or "") for item in passage.get("source_chunk_ids") or []
            )
        ]

    def _event_snapshot(self, event_id: str) -> dict[str, Any] | None:
        connection = ResearchKnowledgeStore._connect(self.workdir.as_posix(), self.settings)
        try:
            row = connection.execute(
                "SELECT event_id,content_hash,event_type,source_kind,source_ref,occurred_at "
                "FROM research_events WHERE event_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
                (event_id, self.tenant_id, self.user_id, self.workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row is not None else None

    def _upsert(
        self,
        research_id: str,
        *,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        relation: str,
        side: str = "neutral",
        state: str = "active",
        source_revision: int = 0,
        source_hash: str = "",
        metadata: dict[str, Any] | None = None,
        discovered_by: str = "system",
    ) -> tuple[ResearchKnowledgeLink, bool]:
        return ResearchKnowledgeStore.upsert_link(
            research_id,
            **self.owner,
            subject_type=subject_type,
            subject_id=subject_id,
            object_type=object_type,
            object_id=object_id,
            relation=relation,
            side=side,
            state=state,
            source_revision=source_revision,
            source_hash=source_hash,
            metadata=metadata,
            discovered_by=discovered_by,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )

    def _link_evidence(
        self,
        research_id: str,
        *,
        subject_type: str,
        subject_id: str,
        evidence_id: str,
        side: str = "neutral",
    ) -> list[ResearchKnowledgeLink]:
        linked: list[ResearchKnowledgeLink] = []
        if evidence_id.startswith("event:"):
            event_id = evidence_id.split(":", 1)[1]
            event = self._event_snapshot(event_id)
            link, _ = self._upsert(
                research_id,
                subject_type=subject_type,
                subject_id=subject_id,
                object_type="evidence",
                object_id=evidence_id,
                relation="supports",
                side=side,
                state="active" if event else "missing",
                source_hash=str((event or {}).get("content_hash") or ""),
                metadata={"evidence_kind": "research_event", "event": event or {}},
            )
            return [link]

        record = EvidenceStore.get(evidence_id, workdir=self.workdir.as_posix())
        state = "missing" if record is None else (
            "active" if record.status == "active" else record.status
        )
        if state not in {"active", "stale", "missing", "retracted"}:
            state = "stale"
        evidence_link, _ = self._upsert(
            research_id,
            subject_type=subject_type,
            subject_id=subject_id,
            object_type="evidence",
            object_id=evidence_id,
            relation="supports",
            side=side,
            state=state,
            source_hash=str(record.content_hash if record else ""),
            metadata={
                "evidence_kind": str(record.kind if record else ""),
                "source_type": str(record.source_type if record else ""),
                "locator": str(record.locator if record else ""),
                "anchor_ids": self._evidence_anchor_ids(record) if record else [],
            },
        )
        linked.append(evidence_link)
        if record is None:
            return linked
        doc_id = self._evidence_doc_id(record)
        if not doc_id:
            return linked
        paper_record, paper_hash = self._paper_record(doc_id)
        paper_link, _ = self._upsert(
            research_id,
            subject_type="project",
            subject_id=research_id,
            object_type="paper",
            object_id=doc_id,
            relation="references",
            state="active" if paper_record else "missing",
            source_hash=paper_hash,
            metadata={
                "title": str((paper_record.get("metadata") or {}).get("title") or doc_id),
                "pdf_path": str(paper_record.get("pdf_path") or ""),
            },
        )
        linked.append(paper_link)
        for passage in self._passages_for_evidence(record, paper_record):
            passage_id = str(passage.get("passage_id") or "")
            if not passage_id:
                continue
            passage_link, _ = self._upsert(
                research_id,
                subject_type=subject_type,
                subject_id=subject_id,
                object_type="passage",
                object_id=passage_id,
                relation="references",
                side=side,
                state="active",
                source_hash=sha256_text(canonical_json(passage)),
                metadata={
                    "doc_id": doc_id,
                    "summary_text": str(passage.get("summary_text") or ""),
                    "source_chunk_ids": list(passage.get("source_chunk_ids") or []),
                    "page_span": list(passage.get("page_span") or []),
                },
            )
            linked.append(passage_link)
        return linked

    def refresh(self, research_id: str) -> dict[str, Any]:
        self._project(research_id)
        if not self.settings.knowledge_links_active:
            raise RuntimeError("Research Knowledge Links are not active")
        findings = ResearchDerivationStore.list_findings(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=1000,
        )
        conflicts = ResearchDerivationStore.list_conflicts(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=1000,
        )
        episodes = ResearchDerivationStore.list_episodes(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=1000,
        )
        project_events = ResearchDerivationStore.list_project_events(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=10000,
        )
        event_map = {item.event_id: item for item in project_events}
        touched: list[ResearchKnowledgeLink] = []
        evidence_subjects: dict[str, list[tuple[str, str, str]]] = {}
        for episode in episodes:
            evidence_ids: list[str] = []
            for event_id in episode.event_ids:
                event = event_map.get(event_id)
                if event is not None:
                    evidence_ids.extend(_evidence_ids_from_value(event.payload))
            evidence_ids.extend(_evidence_ids_from_value(episode.artifact_refs))
            for evidence_id in _unique(evidence_ids, 1000):
                touched.extend(
                    self._link_evidence(
                        research_id,
                        subject_type="episode",
                        subject_id=episode.episode_id,
                        evidence_id=evidence_id,
                    )
                )
                evidence_subjects.setdefault(evidence_id, []).append(
                    ("episode", episode.episode_id, "neutral")
                )
        for finding in findings:
            for evidence_id in _unique(finding.evidence_ids, 1000):
                touched.extend(
                    self._link_evidence(
                        research_id,
                        subject_type="finding",
                        subject_id=finding.finding_id,
                        evidence_id=evidence_id,
                    )
                )
                evidence_subjects.setdefault(evidence_id, []).append(
                    ("finding", finding.finding_id, "neutral")
                )
        for conflict in conflicts:
            for side, evidence_ids in (
                ("original", conflict.original_evidence_ids),
                ("challenging", conflict.challenging_evidence_ids),
            ):
                for evidence_id in _unique(evidence_ids, 1000):
                    touched.extend(
                        self._link_evidence(
                            research_id,
                            subject_type="conflict",
                            subject_id=conflict.conflict_id,
                            evidence_id=evidence_id,
                            side=side,
                        )
                    )
                    evidence_subjects.setdefault(evidence_id, []).append(
                        ("conflict", conflict.conflict_id, side)
                    )

        project_evidence_ids = set(evidence_subjects)
        pages, claims = self._wiki_snapshot() if project_evidence_ids else ({}, {})
        for claim_id, claim in claims.items():
            claim_evidence = set(_unique(claim.get("evidence_ids") or []))
            overlap = sorted(project_evidence_ids.intersection(claim_evidence))
            if not overlap:
                continue
            page = dict(claim.get("page") or {})
            page_id = str(claim.get("page_id") or "")
            page_link, _ = self._upsert(
                research_id,
                subject_type="project",
                subject_id=research_id,
                object_type="wiki_page",
                object_id=page_id,
                relation="references",
                state="active" if page.get("status") != "archived" else "stale",
                source_revision=int(page.get("revision") or 0),
                source_hash=str(page.get("content_hash") or ""),
                metadata={"title": page.get("title"), "status": page.get("status")},
            )
            touched.append(page_link)
            claim_hash = sha256_text(
                canonical_json(
                    {
                        "statement": claim.get("statement"),
                        "scope": claim.get("scope") or {},
                        "evidence_links": claim.get("evidence_links") or [],
                        "support_status": claim.get("support_status"),
                    }
                )
            )
            for subject_type, subject_id, side in {
                pair for evidence_id in overlap for pair in evidence_subjects[evidence_id]
            }:
                claim_link, _ = self._upsert(
                    research_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    object_type="wiki_claim",
                    object_id=claim_id,
                    relation="corresponds_to",
                    side=side,
                    state=(
                        "conflicting"
                        if claim.get("support_status") == "conflicting"
                        else "stale"
                        if claim.get("support_status") in {"stale", "retracted"}
                        else "active"
                    ),
                    source_revision=int(page.get("revision") or 0),
                    source_hash=claim_hash,
                    metadata={
                        "page_id": page_id,
                        "page_title": page.get("title"),
                        "statement": claim.get("statement"),
                        "support_status": claim.get("support_status"),
                        "evidence_ids": sorted(claim_evidence),
                    },
                )
                touched.append(claim_link)

        current = ResearchKnowledgeStore.list_links(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=10000,
        )
        touched_keys = {item.link_key for item in touched}
        retired_count = 0
        for link in current:
            if (
                link.discovered_by == "system"
                and link.link_key not in touched_keys
                and link.state != "removed"
            ):
                self._upsert(
                    research_id,
                    subject_type=link.subject_type,
                    subject_id=link.subject_id,
                    object_type=link.object_type,
                    object_id=link.object_id,
                    relation=link.relation,
                    side=link.side,
                    state="removed",
                    source_revision=link.source_revision,
                    source_hash=link.source_hash,
                    metadata={
                        **link.metadata,
                        "removed_reason": "system_source_no_longer_reachable",
                    },
                )
                retired_count += 1
        report = self.lint(research_id)
        links = ResearchKnowledgeStore.list_links(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=10000,
        )
        visible_links = [item for item in links if item.state != "removed"]
        return {
            "research_id": research_id,
            "link_count": len(visible_links),
            "created_or_refreshed_count": len(touched),
            # Keep stale_count for older API clients. System-discovered links
            # that disappear from the current projection are now retired from
            # the live snapshot instead of becoming permanent warnings.
            "stale_count": 0,
            "retired_count": retired_count,
            "object_counts": self._object_counts(visible_links),
            "lint": report.to_dict(),
        }

    @staticmethod
    def _object_counts(links: list[ResearchKnowledgeLink]) -> dict[str, int]:
        objects: dict[str, set[str]] = {}
        for link in links:
            if link.state == "removed":
                continue
            objects.setdefault(link.object_type, set()).add(link.object_id)
        return {key: len(values) for key, values in objects.items()}

    def create_manual_link(
        self,
        research_id: str,
        *,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        relation: str,
        side: str = "neutral",
        metadata: dict[str, Any] | None = None,
    ) -> ResearchKnowledgeLink:
        self._project(research_id)
        findings = {
            item.finding_id: item
            for item in ResearchDerivationStore.list_findings(
                research_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
                limit=1000,
            )
        }
        conflicts = {
            item.conflict_id: item
            for item in ResearchDerivationStore.list_conflicts(
                research_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
                limit=1000,
            )
        }
        episodes = {
            item.episode_id: item
            for item in ResearchDerivationStore.list_episodes(
                research_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
                limit=1000,
            )
        }
        needs_wiki = subject_type == "wiki_claim" or object_type in {
            "wiki_page",
            "wiki_claim",
        }
        pages, claims = self._wiki_snapshot() if needs_wiki else ({}, {})
        if subject_type == "project" and subject_id != research_id:
            raise ValueError("subject project does not match research project")
        if subject_type == "finding" and subject_id not in findings:
            raise ValueError("subject finding does not belong to research project")
        if subject_type == "episode" and subject_id not in episodes:
            raise ValueError("subject Episode does not belong to research project")
        if subject_type == "conflict" and subject_id not in conflicts:
            raise ValueError("subject conflict does not belong to research project")
        if subject_type == "wiki_claim" and subject_id not in claims:
            raise ValueError("subject Wiki claim was not found")
        if subject_type == "hypothesis":
            from .experiment_store import ResearchExperimentStore
            hypotheses = ResearchExperimentStore.list_hypotheses(research_id, **self.owner, workdir=self.workdir.as_posix())
            if not any(h.hypothesis_id == subject_id for h in hypotheses):
                raise ValueError("假说不属于当前课题")
        state = "active"
        source_revision = 0
        source_hash = ""
        snapshot: dict[str, Any] = dict(metadata or {})
        if object_type == "wiki_claim":
            claim = claims.get(object_id)
            if claim is None:
                state = "missing"
            else:
                page = dict(claim.get("page") or {})
                source_revision = int(page.get("revision") or 0)
                source_hash = sha256_text(canonical_json(claim))
                snapshot.update(
                    {
                        "page_id": claim.get("page_id"),
                        "page_title": page.get("title"),
                        "statement": claim.get("statement"),
                        "support_status": claim.get("support_status"),
                    }
                )
        elif object_type == "wiki_page":
            page = pages.get(object_id)
            if page is None:
                state = "missing"
            else:
                source_revision = int(page.get("revision") or 0)
                source_hash = str(page.get("content_hash") or "")
                snapshot.update({"title": page.get("title"), "status": page.get("status")})
        elif object_type == "evidence":
            if object_id.startswith("event:"):
                event = self._event_snapshot(object_id.split(":", 1)[1])
                state = "active" if event else "missing"
                source_hash = str((event or {}).get("content_hash") or "")
            else:
                evidence = EvidenceStore.get(object_id, workdir=self.workdir.as_posix())
                state = "missing" if evidence is None else evidence.status
                source_hash = str(evidence.content_hash if evidence else "")
                if evidence:
                    snapshot["title"] = evidence.title
        elif object_type == "paper":
            record, source_hash = self._paper_record(object_id)
            state = "active" if record else "missing"
            snapshot.update({"title": (record.get("metadata") or {}).get("title")})
        elif object_type == "passage":
            doc_id = object_id.split(":passage:", 1)[0]
            record, _ = self._paper_record(doc_id)
            passage = next(
                (
                    dict(item)
                    for item in record.get("l1_passage_summaries") or []
                    if isinstance(item, dict)
                    and str(item.get("passage_id") or "") == object_id
                ),
                None,
            )
            state = "active" if passage else "missing"
            source_hash = sha256_text(canonical_json(passage)) if passage else ""
            snapshot.update(
                {
                    "doc_id": doc_id,
                    "summary_text": str((passage or {}).get("summary_text") or ""),
                    "source_chunk_ids": list((passage or {}).get("source_chunk_ids") or []),
                }
            )
        elif object_type == "finding":
            target_finding = findings.get(object_id)
            state = "active" if target_finding else "missing"
            if target_finding:
                source_revision = target_finding.current_revision
                source_hash = sha256_text(
                    canonical_json(
                        {
                            "statement": target_finding.statement,
                            "scope": target_finding.scope,
                            "status": target_finding.status,
                            "evidence_ids": target_finding.evidence_ids,
                        }
                    )
                )
                snapshot.update(
                    {
                        "statement": target_finding.statement,
                        "status": target_finding.status,
                    }
                )
        link, _ = self._upsert(
            research_id,
            subject_type=subject_type,
            subject_id=subject_id,
            object_type=object_type,
            object_id=object_id,
            relation=relation,
            side=side,
            state=state,
            source_revision=source_revision,
            source_hash=source_hash,
            metadata=snapshot,
            discovered_by="user",
        )
        self.lint(research_id)
        return link

    def remove_link(self, link_id: str) -> ResearchKnowledgeLink:
        current = ResearchKnowledgeStore.get_link(
            link_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if current is None or current.superseded_by:
            raise ValueError("active knowledge link not found")
        link, _ = self._upsert(
            current.research_id,
            subject_type=current.subject_type,
            subject_id=current.subject_id,
            object_type=current.object_type,
            object_id=current.object_id,
            relation=current.relation,
            side=current.side,
            state="removed",
            source_revision=current.source_revision,
            source_hash=current.source_hash,
            metadata={**current.metadata, "removed_by": self.user_id},
            discovered_by="user",
        )
        self.lint(current.research_id)
        return link

    def lint(self, research_id: str) -> ResearchKnowledgeLintReport:
        self._project(research_id)
        started_at = utc_now_iso()
        links = ResearchKnowledgeStore.list_links(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=10000,
        )
        findings = ResearchDerivationStore.list_findings(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=1000,
        )
        conflicts = ResearchDerivationStore.list_conflicts(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=1000,
        )
        needs_wiki = any(
            item.object_type in {"wiki_page", "wiki_claim"}
            or item.subject_type == "wiki_claim"
            for item in links
            if item.state != "removed"
        )
        pages, claims = self._wiki_snapshot() if needs_wiki else ({}, {})
        issues: list[dict[str, Any]] = []

        def issue(severity: str, code: str, message: str, **refs: Any) -> None:
            issues.append({"severity": severity, "code": code, "message": message, **refs})

        active_links = [item for item in links if item.state != "removed"]
        linked_by_subject: dict[tuple[str, str], list[ResearchKnowledgeLink]] = {}
        for link in active_links:
            linked_by_subject.setdefault((link.subject_type, link.subject_id), []).append(link)
            if link.state == "missing":
                issue("error", "knowledge_object_missing", "关联的知识对象不存在。", link_id=link.link_id, object_type=link.object_type, object_id=link.object_id)
            elif link.state == "retracted":
                issue("error", "knowledge_evidence_retracted", "关联证据已经撤回。", link_id=link.link_id, object_id=link.object_id)
            elif link.state in {"stale", "conflicting"}:
                issue("warning", f"knowledge_link_{link.state}", "关联对象需要复核。", link_id=link.link_id, object_type=link.object_type, object_id=link.object_id)
            if link.object_type == "evidence":
                if link.object_id.startswith("event:"):
                    event = self._event_snapshot(link.object_id.split(":", 1)[1])
                    if event is None:
                        issue("error", "research_event_evidence_missing", "Finding 引用的原始 Event 已不存在。", link_id=link.link_id, object_id=link.object_id)
                    elif link.source_hash and link.source_hash != str(event.get("content_hash") or ""):
                        issue("error", "research_event_hash_changed", "原始 Event 哈希发生变化。", link_id=link.link_id, object_id=link.object_id)
                    continue
                evidence = EvidenceStore.get(link.object_id, workdir=self.workdir.as_posix())
                if evidence is None:
                    continue
                if link.source_hash and link.source_hash != evidence.content_hash:
                    issue("error", "evidence_hash_changed", "Evidence 内容哈希与链接快照不一致。", link_id=link.link_id, evidence_id=link.object_id)
                if evidence.source_type.lower() == "paper" and not (
                    evidence.locator or evidence.citations or (evidence.metadata or {}).get("paper_anchor")
                ):
                    issue("error", "paper_evidence_without_anchor", "Paper Evidence 缺少页码、段落或表格 Anchor。", link_id=link.link_id, evidence_id=link.object_id)
            elif link.object_type == "paper":
                record, current_hash = self._paper_record(link.object_id)
                if not record:
                    continue
                if link.source_hash and current_hash and link.source_hash != current_hash:
                    issue("warning", "paper_record_changed", "Paper 解析记录已变化，需要刷新可信链。", link_id=link.link_id, doc_id=link.object_id)
            elif link.object_type == "passage":
                doc_id = str(link.metadata.get("doc_id") or link.object_id.split(":passage:", 1)[0])
                record, _ = self._paper_record(doc_id)
                passages = {
                    str(item.get("passage_id") or ""): dict(item)
                    for item in record.get("l1_passage_summaries") or []
                    if isinstance(item, dict)
                }
                passage = passages.get(link.object_id)
                if passage is None:
                    issue("error", "paper_passage_missing", "稳定 L1 Passage 已不存在。", link_id=link.link_id, passage_id=link.object_id)
                    continue
                chunk_ids = {
                    str(item.get("chunk_id") or "")
                    for item in record.get("l2_chunks") or []
                    if isinstance(item, dict)
                }
                missing_chunks = [
                    item for item in passage.get("source_chunk_ids") or [] if str(item) not in chunk_ids
                ]
                if missing_chunks:
                    reference_only = bool(record.get("imported_reference_only")) and (
                        "chunks" in set(record.get("missing_layers") or [])
                    )
                    if reference_only:
                        issue(
                            "warning",
                            "passage_source_reference_only",
                            "导入包只保留 L1 Passage 引用，原始 Chunk 未随包分发。",
                            link_id=link.link_id,
                            passage_id=link.object_id,
                            missing_chunk_ids=missing_chunks,
                            degraded_reason=str(record.get("degraded_reason") or "reference_only_import"),
                        )
                    else:
                        issue("error", "passage_anchor_broken", "L1 Passage 的原始 Chunk Anchor 已断开。", link_id=link.link_id, passage_id=link.object_id, missing_chunk_ids=missing_chunks)
            elif link.object_type == "wiki_page":
                page = pages.get(link.object_id)
                if page is not None and int(page.get("revision") or 0) != link.source_revision:
                    issue("warning", "wiki_page_revision_changed", "Wiki 页面已有新 Revision，需要刷新可信链。", link_id=link.link_id, page_id=link.object_id)
            elif link.object_type == "wiki_claim":
                claim = claims.get(link.object_id)
                if claim is None:
                    continue
                page = dict(claim.get("page") or {})
                if int(page.get("revision") or 0) != link.source_revision:
                    issue("warning", "wiki_claim_revision_changed", "Wiki Claim 所在页面已有新 Revision。", link_id=link.link_id, claim_id=link.object_id)
                if claim.get("support_status") in {"manual_unverified", "stale", "retracted", "conflicting"}:
                    severity = "error" if claim.get("support_status") == "retracted" else "warning"
                    issue(severity, "wiki_claim_not_export_ready", "Wiki Claim 尚未达到导出准备状态。", link_id=link.link_id, claim_id=link.object_id, support_status=claim.get("support_status"))

        for finding in findings:
            if finding.status in {"superseded", "retracted"}:
                continue
            subject_links = linked_by_subject.get(("finding", finding.finding_id), [])
            durable = [
                link for link in subject_links
                if link.state == "active"
                and (
                    link.object_type in {"paper", "passage", "wiki_claim"}
                    or (link.object_type == "evidence" and not link.object_id.startswith("event:"))
                )
            ]
            if not finding.evidence_ids:
                issue("error", "finding_without_evidence", "当前 Finding 没有任何来源证据。", finding_id=finding.finding_id)
            elif not durable:
                issue("warning", "finding_only_session_evidence", "Finding 目前只能回到私人 Session Event，尚未连接可导出的 Evidence/Wiki/Paper。", finding_id=finding.finding_id)

        for conflict in conflicts:
            if conflict.status == "resolved":
                continue
            subject_links = linked_by_subject.get(("conflict", conflict.conflict_id), [])
            for side in ("original", "challenging"):
                if not any(link.side == side and link.state == "active" for link in subject_links):
                    issue("error", "conflict_side_without_evidence", "未解决 Conflict 的一侧缺少可追溯证据。", conflict_id=conflict.conflict_id, side=side)

        severity_counts = {
            "error": sum(1 for item in issues if item["severity"] == "error"),
            "warning": sum(1 for item in issues if item["severity"] == "warning"),
            "info": sum(1 for item in issues if item["severity"] == "info"),
        }
        status = "error" if severity_counts["error"] else "warning" if severity_counts["warning"] else "pass"
        completed_at = utc_now_iso()
        source_digest = sha256_text(
            canonical_json(
                {
                    "links": [
                        [item.link_id, item.revision, item.state, item.source_hash]
                        for item in active_links
                    ],
                    "issues": issues,
                }
            )
        )
        report = ResearchKnowledgeLintReport(
            run_id=f"knowledge-lint-{uuid.uuid4().hex}",
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            research_id=research_id,
            status=status,
            export_ready=severity_counts["error"] == 0,
            checked_link_count=len(active_links),
            counts=severity_counts,
            issues=issues,
            source_digest=source_digest,
            started_at=started_at,
            completed_at=completed_at,
            idempotency_key=f"knowledge-lint:{research_id}:{source_digest}",
        )
        return ResearchKnowledgeStore.save_lint_report(
            report,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )

    def snapshot(self, research_id: str) -> dict[str, Any]:
        self._project(research_id)
        links = ResearchKnowledgeStore.list_links(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
            limit=10000,
        )
        visible_links = [item for item in links if item.state != "removed"]
        lint = ResearchKnowledgeStore.latest_lint_report(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        evidence = EvidenceStore.get_many([item.object_id for item in visible_links if item.object_type == "evidence" and not item.object_id.startswith("event:")], workdir=self.workdir.as_posix())
        presented = []
        for item in visible_links:
            value = item.to_dict()
            record = evidence.get(item.object_id) if item.object_type == "evidence" else None
            if record and record.title:
                value["metadata"] = {**value["metadata"], "title": record.title}
            presented.append(value)
        return {
            "research_id": research_id,
            "count": len(visible_links),
            "object_counts": self._object_counts(visible_links),
            "links": presented,
            "lint": lint.to_dict() if lint else None,
        }
