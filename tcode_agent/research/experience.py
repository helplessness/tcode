"""Deterministic incremental projection of durable research experience.

This layer never calls a model. It reuses readable Activity/Episode/Finding
fields and keeps incomplete summaries explicitly marked for later share-time
materialization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .activities import ResearchActivity, ResearchActivityStore
from .config import ResearchSettings, load_research_settings
from .episode_store import ResearchDerivationStore
from .experience_models import RESEARCH_EXPERIENCE_PROVENANCE
from .experience_store import ResearchExperienceStore
from .knowledge import _evidence_ids_from_value
from .models import canonical_json, sha256_text
from .routing_models import ResearchTurnRoute
from .routing_store import ResearchRoutingStore
from .store import ResearchEventStore
from .topic_store import ResearchTopicStore


_DURABILITY_RANK = {"transient": 0, "supporting": 1, "durable": 2}


@dataclass
class ResearchExperienceProjectionReport:
    input_activity_count: int = 0
    input_episode_count: int = 0
    input_finding_count: int = 0
    input_project_count: int = 0
    scanned_episode_count: int = 0
    scanned_finding_count: int = 0
    created_count: int = 0
    revised_count: int = 0
    unchanged_count: int = 0
    skipped_transient_count: int = 0
    model_call_count: int = 0
    experience_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchExperienceProjector:
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

    def _record_result(
        self,
        report: ResearchExperienceProjectionReport,
        item: Any,
        created: bool,
    ) -> None:
        report.experience_ids.append(item.experience_id)
        if not created:
            report.unchanged_count += 1
        elif item.revision > 1:
            report.revised_count += 1
        else:
            report.created_count += 1
        try:
            from .pack_overlay import ResearchPackOverlayProjector

            ResearchPackOverlayProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).project_experience(item)
        except Exception:
            # Experience remains the source of truth; imported Pack Overlay is
            # a deterministic, repairable continuation projection.
            pass

    def _activity_routes(self, activity: ResearchActivity) -> list[ResearchTurnRoute]:
        if not activity.event_ids:
            return []
        result: dict[str, ResearchTurnRoute] = {}
        connection = ResearchEventStore._connect(self.workdir, settings=self.settings)
        try:
            for start in range(0, len(activity.event_ids), 400):
                chunk = activity.event_ids[start : start + 400]
                rows = connection.execute(
                    "SELECT DISTINCT r.* FROM research_turn_routes r "
                    "JOIN research_events e ON e.turn_id=r.turn_id "
                    "AND e.tenant_id=r.tenant_id AND e.user_id=r.user_id "
                    "AND e.workspace_id=r.workspace_id "
                    "WHERE r.tenant_id=? AND r.user_id=? AND r.workspace_id=? "
                    "AND r.superseded_by='' AND e.event_id IN ("
                    + ",".join("?" for _ in chunk)
                    + ")",
                    (*self.owner.values(), *chunk),
                ).fetchall()
                for row in rows:
                    route = ResearchTurnRoute.from_row(row)
                    result[route.route_id] = route
        finally:
            connection.close()
        return list(result.values())

    def _activity_projects(
        self,
        activity: ResearchActivity,
        routes: list[ResearchTurnRoute],
    ) -> list[str]:
        result = {
            scope.research_id
            for scope in ResearchRoutingStore.list_object_scopes(
                **self.owner,
                object_type="activity",
                object_id=activity.activity_id,
                workdir=self.workdir,
                settings=self.settings,
                limit=20,
            )
            if scope.state == "active"
        }
        result.update(
            research_id for route in routes for research_id in route.research_ids
        )
        if activity.research_id:
            result.add(activity.research_id)
        if activity.candidate_id:
            candidate = ResearchTopicStore.get_candidate(
                activity.candidate_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if candidate is not None and candidate.confirmed_research_id:
                result.add(candidate.confirmed_research_id)
        return sorted(result)[:3]

    @staticmethod
    def _strong_activity_signal(activity: ResearchActivity) -> bool:
        if activity.status == "failed" or activity.artifact_refs or activity.artifact_groups:
            return True
        return any(
            str(item.get("state") or "") in {"completed", "failed"}
            for item in activity.tool_calls
        )

    def _project_activity(
        self,
        activity: ResearchActivity,
    ) -> tuple[Any, bool] | None:
        routes = self._activity_routes(activity)
        strong = self._strong_activity_signal(activity)
        route_durability = max(
            (route.durability for route in routes),
            key=lambda value: _DURABILITY_RANK.get(value, -1),
            default="transient",
        )
        if strong:
            durability = "durable"
        else:
            durability = route_durability
        if durability == "transient":
            return None

        projects = self._activity_projects(activity, routes)
        if projects:
            routing_state = "resolved"
        elif any(route.routing_state == "unresolved" for route in routes):
            routing_state = "unassigned"
        else:
            routing_state = "unassigned"
        if activity.status == "failed":
            kind = "failed_path"
        elif activity.artifact_refs or activity.artifact_groups or any(
            str(item.get("state") or "") == "completed" for item in activity.tool_calls
        ):
            kind = "result"
        else:
            kind = "attempt"
        summary = str(activity.summary or "").strip()
        if not summary:
            summary = next(
                (
                    str(group.get("summary") or "").strip()
                    for group in activity.artifact_groups
                    if str(group.get("summary") or "").strip()
                ),
                "",
            )
        summary_state = "complete" if summary else "incomplete"
        if not summary:
            summary = f"已记录“{activity.title}”的研究过程，详细归纳将在分享时生成。"
        artifacts = [
            *[dict(item) for item in activity.artifact_groups],
            *[dict(item) for item in activity.artifact_refs],
        ][:1000]
        evidence_ids = _evidence_ids_from_value(
            [
                activity.artifact_groups,
                activity.artifact_refs,
                activity.tool_calls,
                activity.agent_calls,
                activity.workflow_calls,
            ]
        )
        return ResearchExperienceStore.append(
            **self.owner,
            kind=kind,
            title=activity.title or "研究活动",
            summary=summary,
            summary_state=summary_state,
            durability=durability,
            outcome_status=activity.status,
            routing_state=routing_state,
            source_research_ids=projects,
            source_activity_ids=[activity.activity_id],
            source_event_ids=activity.event_ids,
            artifact_refs=artifacts,
            evidence_ids=evidence_ids,
            provenance="observed" if strong or activity.summary else "inferred",
            occurred_at=activity.ended_at or activity.started_at,
            experience_key=f"activity:{activity.activity_key}",
            workdir=self.workdir,
            settings=self.settings,
        )

    def _remove_activity_experience(
        self,
        activity: ResearchActivity,
    ) -> tuple[Any, bool] | None:
        key = f"activity:{activity.activity_key}"
        current = ResearchExperienceStore.get_active_by_key(
            key,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if current is None or current.state == "removed":
            return None
        return ResearchExperienceStore.append(
            **self.owner,
            kind=current.kind,
            title=current.title,
            summary=current.summary,
            summary_state=current.summary_state,
            durability="transient",
            outcome_status=activity.status,
            routing_state="not_research",
            source_activity_ids=[activity.activity_id],
            source_event_ids=activity.event_ids,
            artifact_refs=current.artifact_refs,
            evidence_ids=current.evidence_ids,
            origin_pack_ids=current.origin_pack_ids,
            provenance=current.provenance,
            occurred_at=activity.ended_at or activity.started_at,
            experience_key=key,
            state="removed",
            workdir=self.workdir,
            settings=self.settings,
        )

    def apply_activities(
        self,
        activity_ids: list[str],
    ) -> ResearchExperienceProjectionReport:
        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in activity_ids
                if str(item or "").strip()
            )
        )[:1000]
        report = ResearchExperienceProjectionReport(input_activity_count=len(normalized))
        if not self.settings.experience_projection_active:
            return report
        for activity_id in normalized:
            activity = ResearchActivityStore.get(
                activity_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if activity is None:
                continue
            projected = self._project_activity(activity)
            if projected is None:
                report.skipped_transient_count += 1
                removed = self._remove_activity_experience(activity)
                if removed is not None:
                    self._record_result(report, *removed)
                continue
            item, created = projected
            self._record_result(report, item, created)
        report.experience_ids = list(dict.fromkeys(report.experience_ids))
        return report

    def remove_activity_keys(
        self,
        activity_keys: list[str],
    ) -> ResearchExperienceProjectionReport:
        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in activity_keys
                if str(item or "").strip()
            )
        )[:1000]
        report = ResearchExperienceProjectionReport(input_activity_count=len(normalized))
        if not self.settings.experience_projection_active:
            return report
        for activity_key in normalized:
            key = f"activity:{activity_key}"
            current = ResearchExperienceStore.get_active_by_key(
                key,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if current is None or current.state == "removed":
                continue
            item, created = ResearchExperienceStore.append(
                **self.owner,
                kind=current.kind,
                title=current.title,
                summary=current.summary,
                summary_state=current.summary_state,
                durability="transient",
                outcome_status=current.outcome_status,
                routing_state="not_research",
                source_activity_ids=current.source_activity_ids,
                source_event_ids=current.source_event_ids,
                artifact_refs=current.artifact_refs,
                evidence_ids=current.evidence_ids,
                origin_pack_ids=current.origin_pack_ids,
                provenance=current.provenance,
                occurred_at=current.occurred_at,
                experience_key=key,
                state="removed",
                workdir=self.workdir,
                settings=self.settings,
            )
            self._record_result(report, item, created)
        return report

    @staticmethod
    def _structured_text(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("value", "statement", "summary", "name", "reason", "error"):
                text = str(value.get(key) or "").strip()
                if text:
                    return text[:12000]
            return canonical_json(value)[:12000]
        return str(value or "").strip()[:12000]

    def _remove_experience(
        self,
        current: Any,
    ) -> tuple[Any, bool]:
        return ResearchExperienceStore.append(
            **self.owner,
            kind=current.kind,
            title=current.title,
            summary=current.summary,
            summary_state=current.summary_state,
            durability="transient",
            outcome_status=current.outcome_status,
            routing_state="not_research",
            source_research_ids=current.source_research_ids,
            source_episode_ids=current.source_episode_ids,
            source_event_ids=current.source_event_ids,
            artifact_refs=current.artifact_refs,
            evidence_ids=current.evidence_ids,
            origin_pack_ids=current.origin_pack_ids,
            provenance=current.provenance,
            occurred_at=current.occurred_at,
            experience_key=current.experience_key,
            state="removed",
            workdir=self.workdir,
            settings=self.settings,
        )

    def _project_episode(
        self,
        episode: Any,
        report: ResearchExperienceProjectionReport,
    ) -> None:
        try:
            from .pack_overlay import ResearchPackOverlayProjector

            ResearchPackOverlayProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).project_episode(episode)
        except Exception:
            pass
        existing = ResearchExperienceStore.list_active_for_episode_key(
            episode.episode_key,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        generated_keys: set[str] = set()
        if episode.state == "active":
            groups = (
                ("decision", episode.decisions, "研究决策", "durable"),
                ("failed_path", episode.failed_paths, "失败路径", "durable"),
                ("next_action", episode.next_actions, "下一步", "supporting"),
            )
            for kind, values, label, durability in groups:
                for value in values:
                    content = self._structured_text(value)
                    if not content:
                        continue
                    source_events = (
                        list(value.get("source_event_ids") or [])
                        if isinstance(value, dict)
                        else []
                    )
                    provenance = (
                        str(value.get("provenance") or "inferred")
                        if isinstance(value, dict)
                        else "inferred"
                    )
                    if provenance not in RESEARCH_EXPERIENCE_PROVENANCE:
                        provenance = "inferred"
                    key_digest = sha256_text(
                        canonical_json(
                            {
                                "episode_key": episode.episode_key,
                                "kind": kind,
                                "content": content,
                            }
                        )
                    ).split(":", 1)[-1][:32]
                    experience_key = f"episode:{key_digest}"
                    generated_keys.add(experience_key)
                    item, created = ResearchExperienceStore.append(
                        **self.owner,
                        kind=kind,
                        title=f"{label}：{content[:160]}",
                        summary=content,
                        durability=durability,
                        outcome_status=episode.outcome_status,
                        routing_state="resolved",
                        source_research_ids=[episode.research_id],
                        source_episode_ids=[episode.episode_id],
                        source_event_ids=source_events,
                        artifact_refs=episode.artifact_refs,
                        provenance=provenance,
                        occurred_at=episode.ended_at or episode.started_at,
                        experience_key=experience_key,
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    self._record_result(report, item, created)

        for current in existing:
            if current.experience_key in generated_keys:
                continue
            self._record_result(
                report,
                *self._remove_experience(current),
            )

    def apply_episodes(
        self,
        episode_ids: list[str],
    ) -> ResearchExperienceProjectionReport:
        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in episode_ids
                if str(item or "").strip()
            )
        )[:1000]
        report = ResearchExperienceProjectionReport(input_episode_count=len(normalized))
        if not self.settings.experience_projection_active:
            return report
        seen_keys: set[str] = set()
        for episode_id in normalized:
            episode = ResearchDerivationStore.get_episode(
                episode_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if episode is None or episode.episode_key in seen_keys:
                continue
            seen_keys.add(episode.episode_key)
            report.scanned_episode_count += 1
            self._project_episode(episode, report)
        report.experience_ids = list(dict.fromkeys(report.experience_ids))
        return report

    def _project_finding(
        self,
        finding: Any,
        report: ResearchExperienceProjectionReport,
    ) -> None:
        try:
            from .pack_overlay import ResearchPackOverlayProjector

            ResearchPackOverlayProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).project_finding(finding)
        except Exception:
            pass
        key = f"finding:{finding.finding_id}"
        if finding.status not in {"active", "context_limited"}:
            current = ResearchExperienceStore.get_active_by_key(
                key,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if current is not None and current.state == "active":
                self._record_result(
                    report,
                    *self._remove_experience(current),
                )
            return
        item, created = ResearchExperienceStore.append(
            **self.owner,
            kind="result",
            title=finding.statement[:500],
            summary=finding.statement,
            durability="durable",
            outcome_status=finding.status,
            routing_state="resolved",
            source_research_ids=[finding.research_id],
            source_episode_ids=[finding.episode_id] if finding.episode_id else [],
            source_event_ids=finding.source_event_ids,
            evidence_ids=finding.evidence_ids,
            provenance=finding.provenance,
            occurred_at=finding.updated_at or finding.created_at,
            experience_key=key,
            workdir=self.workdir,
            settings=self.settings,
        )
        self._record_result(report, item, created)

    def apply_findings(
        self,
        finding_ids: list[str],
    ) -> ResearchExperienceProjectionReport:
        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in finding_ids
                if str(item or "").strip()
            )
        )[:1000]
        report = ResearchExperienceProjectionReport(input_finding_count=len(normalized))
        if not self.settings.experience_projection_active:
            return report
        for finding_id in normalized:
            finding = ResearchDerivationStore.get_finding(
                finding_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if finding is None:
                continue
            report.scanned_finding_count += 1
            self._project_finding(finding, report)
        report.experience_ids = list(dict.fromkeys(report.experience_ids))
        return report

    def apply_projects(
        self,
        research_ids: list[str],
    ) -> ResearchExperienceProjectionReport:
        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in research_ids
                if str(item or "").strip()
            )
        )[:3]
        report = ResearchExperienceProjectionReport(input_project_count=len(normalized))
        if not self.settings.experience_projection_active:
            return report
        for research_id in normalized:
            episodes = ResearchDerivationStore.list_episodes(
                research_id,
                **self.owner,
                limit=1000,
                workdir=self.workdir,
                settings=self.settings,
            )
            report.scanned_episode_count += len(episodes)
            for episode in episodes:
                self._project_episode(episode, report)

            findings = ResearchDerivationStore.list_findings(
                research_id,
                **self.owner,
                limit=1000,
                workdir=self.workdir,
                settings=self.settings,
            )
            report.scanned_finding_count += len(findings)
            for finding in findings:
                self._project_finding(finding, report)
        report.experience_ids = list(dict.fromkeys(report.experience_ids))
        return report


__all__ = [
    "ResearchExperienceProjectionReport",
    "ResearchExperienceProjector",
]
