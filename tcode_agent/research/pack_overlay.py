"""Append local continuation projections to imported ResearchPack Overlays.

The Parent snapshot is immutable.  This projector only copies already-created,
sanitised local research objects into the owner-scoped Overlay selected by the
main turn route.  It is deliberately deterministic and never calls a model.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..evidence import EvidenceStore
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent, canonical_json, sha256_text
from .pack_asset_store import ResearchPackAssetStore
from .routing_models import ResearchTurnRoute
from .store import ResearchEventStore
from .topic_store import ResearchTopicStore


_DURABILITY_RANK = {"transient": 0, "supporting": 1, "durable": 2}
_OWNER_FIELDS = {"tenant_id", "user_id", "workspace_id", "idempotency_key"}


def _portable(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        raw = value.to_dict()
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        raw = asdict(value)
    return {key: item for key, item in raw.items() if key not in _OWNER_FIELDS}


def _strings(values: Iterable[Any], *, limit: int = 1000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in values
            if str(item or "").strip()
        )
    )[:limit]


@dataclass
class ResearchPackOverlayProjectionReport:
    attempted_count: int = 0
    created_count: int = 0
    unchanged_count: int = 0
    blocked_count: int = 0
    errors: list[str] = field(default_factory=list)
    model_call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchPackOverlayProjector:
    """Copy routed local objects into append-only imported Pack Overlays."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ):
        self.owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if not all(self.owner.values()):
            raise ValueError("complete owner context is required")
        self.workdir = workdir
        self.settings = settings or load_research_settings()

    @staticmethod
    def _canonical_refs(routes: Iterable[ResearchTurnRoute]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for route in routes:
            if route.routing_state != "resolved" or route.durability == "transient":
                continue
            for raw in route.imported_scope_refs:
                ref = {
                    key: str(raw.get(key) or "").strip()
                    for key in ("asset_id", "scope_id", "pack_id", "source_research_id")
                }
                identity = (
                    ref["asset_id"],
                    ref["scope_id"],
                    ref["pack_id"],
                    ref["source_research_id"],
                )
                if not ref["asset_id"] or identity in seen:
                    continue
                seen.add(identity)
                result.append(ref)
        return result[:100]

    def _routes_for_event_ids(self, event_ids: Iterable[str]) -> list[ResearchTurnRoute]:
        identifiers = _strings(event_ids, limit=5000)
        if not identifiers:
            return []
        connection = ResearchEventStore._connect(self.workdir, settings=self.settings)
        result: dict[str, ResearchTurnRoute] = {}
        try:
            for offset in range(0, len(identifiers), 400):
                batch = identifiers[offset : offset + 400]
                rows = connection.execute(
                    "SELECT DISTINCT r.* FROM research_turn_routes r "
                    "JOIN research_events e ON e.turn_id=r.turn_id "
                    "AND e.tenant_id=r.tenant_id AND e.user_id=r.user_id "
                    "AND e.workspace_id=r.workspace_id "
                    "WHERE r.tenant_id=? AND r.user_id=? AND r.workspace_id=? "
                    "AND r.superseded_by='' AND e.event_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    (*self.owner.values(), *batch),
                ).fetchall()
                for row in rows:
                    route = ResearchTurnRoute.from_row(row)
                    result[route.route_id] = route
        finally:
            connection.close()
        return list(result.values())

    def refs_for_event_ids(self, event_ids: Iterable[str]) -> list[dict[str, str]]:
        return self._canonical_refs(self._routes_for_event_ids(event_ids))

    @staticmethod
    def _by_asset(refs: Iterable[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
        result: dict[str, list[dict[str, str]]] = {}
        for ref in refs:
            asset_id = str(ref.get("asset_id") or "").strip()
            if asset_id:
                result.setdefault(asset_id, []).append(dict(ref))
        return result

    def _append(
        self,
        report: ResearchPackOverlayProjectionReport,
        *,
        object_type: str,
        object_key: str,
        local_object_id: str,
        payload: dict[str, Any],
        refs: list[dict[str, str]],
        state: str = "active",
    ) -> None:
        for asset_id, asset_refs in self._by_asset(refs).items():
            report.attempted_count += 1
            origins = _strings(
                (ref.get("pack_id") for ref in asset_refs),
                limit=100,
            )
            try:
                _, created = ResearchPackAssetStore.append_overlay_object(
                    asset_id,
                    **self.owner,
                    object_type=object_type,
                    object_key=object_key,
                    local_object_id=local_object_id,
                    payload=payload,
                    imported_scope_refs=asset_refs,
                    origin_pack_ids=origins,
                    state=state,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                if created:
                    report.created_count += 1
                else:
                    report.unchanged_count += 1
            except ValueError as exc:
                message = str(exc)
                if "blocked" in message or "conflict" in message:
                    report.blocked_count += 1
                report.errors.append(message[:500])

    @staticmethod
    def _durability(routes: Iterable[ResearchTurnRoute]) -> str:
        return max(
            (route.durability for route in routes),
            key=lambda value: _DURABILITY_RANK.get(value, -1),
            default="transient",
        )

    def project_event(
        self,
        event: ResearchEvent,
        route: ResearchTurnRoute,
    ) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        refs = self._canonical_refs([route])
        if not refs:
            return report
        payload = _portable(event)
        payload.update(
            {
                "source_research_ids": _strings(
                    [
                        *route.research_ids,
                        *(ref.get("source_research_id") for ref in refs),
                    ]
                ),
                "origin_pack_ids": _strings(ref.get("pack_id") for ref in refs),
                "continuation_layer": "overlay",
                "included_reason": "routed_continuation_trace",
            }
        )
        self._append(
            report,
            object_type="event",
            object_key=f"event:{event.event_id}",
            local_object_id=event.event_id,
            payload=payload,
            refs=refs,
        )
        return report

    def project_activity(self, activity: Any) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        routes = self._routes_for_event_ids(list(getattr(activity, "event_ids", []) or []))
        refs = self._canonical_refs(routes)
        if not refs:
            return report
        local_research_ids = _strings(
            [
                *(route_id for route in routes for route_id in route.research_ids),
                getattr(activity, "research_id", ""),
            ],
            limit=3,
        )
        source_research_ids = _strings(
            [
                *(ref.get("source_research_id") for ref in refs),
                *local_research_ids,
            ]
        )
        origin_pack_ids = _strings(ref.get("pack_id") for ref in refs)
        payload = _portable(activity)
        payload.update(
            {
                "source_research_ids": source_research_ids,
                "durability": self._durability(routes),
                "routing_state": "resolved",
                "origin_pack_ids": origin_pack_ids,
                "continuation_of_ids": [],
                "source_event_ids": list(getattr(activity, "event_ids", []) or []),
                "continuation_layer": "overlay",
            }
        )
        self._append(
            report,
            object_type="activity",
            object_key=f"activity:{activity.activity_key}",
            local_object_id=activity.activity_id,
            payload=payload,
            refs=refs,
        )
        for research_id in local_research_ids:
            project = ResearchTopicStore.get_project(
                research_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if project is None:
                continue
            project_payload = _portable(project)
            project_payload.update(
                {
                    "source_research_ids": [research_id],
                    "origin_pack_ids": origin_pack_ids,
                    "continuation_layer": "overlay",
                }
            )
            self._append(
                report,
                object_type="research",
                object_key=f"research:{research_id}",
                local_object_id=research_id,
                payload=project_payload,
                refs=refs,
            )

        # Imported-only continuation cannot create a local Project Episode.  A
        # deterministic Overlay Episode keeps the v2 record shape uniform.
        episode_id = "overlay-episode-" + sha256_text(
            f"{activity.activity_key}:{','.join(sorted(origin_pack_ids))}"
        ).split(":", 1)[-1][:32]
        episode = {
            "episode_id": episode_id,
            "episode_key": f"overlay-activity:{activity.activity_key}",
            "schema_version": 1,
            "research_id": source_research_ids[0] if source_research_ids else "",
            "source_research_ids": source_research_ids,
            "title": str(getattr(activity, "title", "") or "研究活动"),
            "goal": {"value": str(getattr(activity, "title", "") or "")},
            "summary": {"value": str(getattr(activity, "summary", "") or "")},
            "outcome_status": str(getattr(activity, "status", "") or "unknown"),
            "event_ids": list(getattr(activity, "event_ids", []) or []),
            "session_ids": list(getattr(activity, "session_ids", []) or []),
            "trace_ids": list(getattr(activity, "trace_ids", []) or []),
            "artifact_refs": [
                *list(getattr(activity, "artifact_refs", []) or []),
                *list(getattr(activity, "artifact_groups", []) or []),
            ][:1000],
            "tool_calls": list(getattr(activity, "tool_calls", []) or []),
            "decisions": [],
            "failed_paths": [],
            "open_questions": [],
            "next_actions": [],
            "started_at": str(getattr(activity, "started_at", "") or ""),
            "ended_at": str(getattr(activity, "ended_at", "") or ""),
            "boundary_reason": "overlay_activity_projection",
            "derivation_version": "researchpack-overlay-1",
            "source_digest": str(getattr(activity, "source_digest", "") or ""),
            "state": "active",
            "origin_pack_ids": origin_pack_ids,
            "continuation_layer": "overlay",
        }
        if not getattr(activity, "research_id", "") and not any(
            route.research_ids for route in routes
        ):
            self._append(
                report,
                object_type="episode",
                object_key=f"episode:{episode['episode_key']}",
                local_object_id=episode_id,
                payload=episode,
                refs=refs,
            )
        for raw in (
            [
                *list(getattr(activity, "artifact_refs", []) or []),
                *list(getattr(activity, "artifact_groups", []) or []),
            ][:1000]
        ):
            if not isinstance(raw, dict):
                continue
            identity = str(
                raw.get("artifact_id")
                or raw.get("ref")
                or raw.get("path")
                or raw.get("uri")
                or sha256_text(canonical_json(raw))
            ).strip()
            artifact_id = str(raw.get("artifact_id") or sha256_text(identity)).strip()
            artifact = {
                **dict(raw),
                "artifact_id": artifact_id,
                "source_activity_id": activity.activity_id,
                "source_research_ids": source_research_ids,
                "origin_pack_ids": origin_pack_ids,
                "distribution": str(raw.get("distribution") or "reference_only"),
                "original_file_embedded": False,
                "continuation_layer": "overlay",
            }
            self._append(
                report,
                object_type="artifact",
                object_key=(
                    f"artifact:{activity.activity_key}:"
                    f"{sha256_text(identity).split(':', 1)[-1]}"
                ),
                local_object_id=artifact_id,
                payload=artifact,
                refs=refs,
            )
        return report

    def project_experience(self, item: Any) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        event_ids = list(getattr(item, "source_event_ids", []) or [])
        if not event_ids:
            event_ids = self._event_ids_for_dependencies(
                list(getattr(item, "source_activity_ids", []) or []),
                list(getattr(item, "source_episode_ids", []) or []),
            )
        refs = self.refs_for_event_ids(event_ids)
        if not refs:
            return report
        payload = _portable(item)
        payload.update(
            {
                "source_research_ids": _strings(
                    [
                        *list(getattr(item, "source_research_ids", []) or []),
                        *(ref.get("source_research_id") for ref in refs),
                    ]
                ),
                "origin_pack_ids": _strings(ref.get("pack_id") for ref in refs),
                "continuation_layer": "overlay",
            }
        )
        self._append(
            report,
            object_type="experience",
            object_key=f"experience:{item.experience_key}",
            local_object_id=item.experience_id,
            payload=payload,
            refs=refs,
            state="active" if str(getattr(item, "state", "active")) == "active" else "removed",
        )
        self._project_evidence_ids(
            report,
            list(getattr(item, "evidence_ids", []) or []),
            refs,
            list(payload.get("source_research_ids") or []),
        )
        return report

    def _project_evidence_ids(
        self,
        report: ResearchPackOverlayProjectionReport,
        evidence_ids: Iterable[str],
        refs: list[dict[str, str]],
        source_research_ids: list[str],
    ) -> None:
        identifiers = _strings(
            (
                evidence_id
                for evidence_id in evidence_ids
                if not str(evidence_id or "").startswith("event:")
            ),
            limit=1000,
        )
        if not identifiers:
            return
        records = EvidenceStore.get_many(
            identifiers,
            workdir=self.workdir,
        )
        origins = _strings(ref.get("pack_id") for ref in refs)
        for evidence_id in identifiers:
            record = records.get(evidence_id)
            if record is None:
                continue
            raw = record.to_dict()
            payload = {
                key: value
                for key, value in raw.items()
                if key
                not in {
                    "source_path",
                    "workflow_run_id",
                    "stage_id",
                    "source_agent",
                    "search_text",
                    "embedding",
                    "embedding_version",
                }
            }
            payload.update(
                {
                    "source_research_ids": source_research_ids,
                    "origin_pack_ids": origins,
                    "included_as_dependency": True,
                    "included_reason": "overlay_object_evidence",
                    "continuation_layer": "overlay",
                }
            )
            self._append(
                report,
                object_type="evidence",
                object_key=f"evidence:{evidence_id}",
                local_object_id=evidence_id,
                payload=payload,
                refs=refs,
                state="active" if record.status == "active" else "removed",
            )

    def _event_ids_for_dependencies(
        self,
        activity_ids: list[str],
        episode_ids: list[str],
    ) -> list[str]:
        activity_ids = _strings(activity_ids, limit=1000)
        episode_ids = _strings(episode_ids, limit=1000)
        result: list[str] = []
        connection = ResearchEventStore._connect(self.workdir, settings=self.settings)
        try:
            for table, column, identifiers in (
                ("research_activities", "activity_id", activity_ids),
                ("research_episodes", "episode_id", episode_ids),
            ):
                for offset in range(0, len(identifiers), 400):
                    batch = identifiers[offset : offset + 400]
                    rows = connection.execute(
                        f"SELECT event_ids_json FROM {table} WHERE tenant_id=? AND user_id=? "
                        f"AND workspace_id=? AND {column} IN ("
                        + ",".join("?" for _ in batch)
                        + ")",
                        (*self.owner.values(), *batch),
                    ).fetchall()
                    for row in rows:
                        try:
                            values = json.loads(str(row["event_ids_json"] or "[]"))
                        except Exception:
                            values = []
                        result.extend(values if isinstance(values, list) else [])
        finally:
            connection.close()
        return _strings(result, limit=5000)

    def project_episode(self, episode: Any) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        refs = self.refs_for_event_ids(list(getattr(episode, "event_ids", []) or []))
        if not refs:
            return report
        payload = _portable(episode)
        payload.update(
            {
                "source_research_ids": _strings(
                    [
                        getattr(episode, "research_id", ""),
                        *(ref.get("source_research_id") for ref in refs),
                    ]
                ),
                "origin_pack_ids": _strings(ref.get("pack_id") for ref in refs),
                "continuation_layer": "overlay",
            }
        )
        self._append(
            report,
            object_type="episode",
            object_key=f"episode:{episode.episode_key}",
            local_object_id=episode.episode_id,
            payload=payload,
            refs=refs,
            state="active" if str(getattr(episode, "state", "active")) == "active" else "removed",
        )
        return report

    def project_finding(self, finding: Any) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        refs = self.refs_for_event_ids(
            list(getattr(finding, "source_event_ids", []) or [])
        )
        if not refs:
            return report
        payload = _portable(finding)
        payload.update(
            {
                "source_research_ids": _strings(
                    [
                        getattr(finding, "research_id", ""),
                        *(ref.get("source_research_id") for ref in refs),
                    ]
                ),
                "origin_pack_ids": _strings(ref.get("pack_id") for ref in refs),
                "continuation_layer": "overlay",
            }
        )
        self._append(
            report,
            object_type="finding",
            object_key=f"finding:{finding.finding_id}",
            local_object_id=finding.finding_id,
            payload=payload,
            refs=refs,
            state=(
                "active"
                if str(getattr(finding, "status", "")) in {"active", "context_limited", "disputed"}
                else "removed"
            ),
        )
        self._project_evidence_ids(
            report,
            list(getattr(finding, "evidence_ids", []) or []),
            refs,
            list(payload.get("source_research_ids") or []),
        )
        return report

    def _event_ids_for_finding_refs(self, finding_ids: Iterable[str]) -> list[str]:
        identifiers = _strings(finding_ids, limit=1000)
        if not identifiers:
            return []
        result: list[str] = []
        connection = ResearchEventStore._connect(self.workdir, settings=self.settings)
        try:
            for offset in range(0, len(identifiers), 400):
                batch = identifiers[offset : offset + 400]
                rows = connection.execute(
                    "SELECT source_event_ids_json FROM research_findings WHERE tenant_id=? "
                    "AND user_id=? AND workspace_id=? AND finding_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    (*self.owner.values(), *batch),
                ).fetchall()
                for row in rows:
                    try:
                        values = json.loads(str(row["source_event_ids_json"] or "[]"))
                    except Exception:
                        values = []
                    result.extend(values if isinstance(values, list) else [])
        finally:
            connection.close()
        return _strings(result, limit=5000)

    def project_conflict(self, conflict: Any) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        finding_ids = [
            *list(getattr(conflict, "original_refs", []) or []),
            *list(getattr(conflict, "challenging_refs", []) or []),
        ]
        refs = self.refs_for_event_ids(self._event_ids_for_finding_refs(finding_ids))
        if not refs:
            return report
        source_research_ids = _strings(
            [
                getattr(conflict, "research_id", ""),
                *(ref.get("source_research_id") for ref in refs),
            ]
        )
        payload = _portable(conflict)
        payload.update(
            {
                "source_research_ids": source_research_ids,
                "origin_pack_ids": _strings(ref.get("pack_id") for ref in refs),
                "continuation_layer": "overlay",
            }
        )
        self._append(
            report,
            object_type="conflict",
            object_key=f"conflict:{conflict.conflict_id}",
            local_object_id=conflict.conflict_id,
            payload=payload,
            refs=refs,
        )
        self._project_evidence_ids(
            report,
            [
                *list(getattr(conflict, "original_evidence_ids", []) or []),
                *list(getattr(conflict, "challenging_evidence_ids", []) or []),
            ],
            refs,
            source_research_ids,
        )
        return report

    def project_resolution(
        self,
        resolution: Any,
        conflict: Any,
    ) -> ResearchPackOverlayProjectionReport:
        report = ResearchPackOverlayProjectionReport()
        finding_ids = [
            *list(getattr(conflict, "original_refs", []) or []),
            *list(getattr(conflict, "challenging_refs", []) or []),
        ]
        refs = self.refs_for_event_ids(self._event_ids_for_finding_refs(finding_ids))
        if not refs:
            return report
        source_research_ids = _strings(
            [
                getattr(resolution, "research_id", ""),
                *(ref.get("source_research_id") for ref in refs),
            ]
        )
        payload = _portable(resolution)
        payload.update(
            {
                "source_research_ids": source_research_ids,
                "origin_pack_ids": _strings(ref.get("pack_id") for ref in refs),
                "continuation_layer": "overlay",
            }
        )
        self._append(
            report,
            object_type="resolution",
            object_key=f"resolution:{resolution.resolution_id}",
            local_object_id=resolution.resolution_id,
            payload=payload,
            refs=refs,
        )
        self._project_evidence_ids(
            report,
            list(getattr(resolution, "evidence_ids", []) or []),
            refs,
            source_research_ids,
        )
        return report


__all__ = [
    "ResearchPackOverlayProjectionReport",
    "ResearchPackOverlayProjector",
]
