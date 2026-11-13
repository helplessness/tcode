"""Bounded startup maintenance for Journal reconciliation and Shadow catch-up."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .attribution import AttributionShadowDispatcher, ResearchAttributionEngine
from .config import ResearchSettings, load_research_settings
from .reconcile import ResearchJournalReconciler


@dataclass
class ResearchMaintenanceReport:
    sessions_seen: int = 0
    sessions_reconciled: int = 0
    sessions_skipped_old: int = 0
    event_attempts: int = 0
    event_results: int = 0
    events_created: int = 0
    attribution_runs: int = 0
    attribution_decisions: int = 0
    topic_runs: int = 0
    topic_candidates_created: int = 0
    topic_memberships_changed: int = 0
    activity_runs: int = 0
    activities_built: int = 0
    experience_runs: int = 0
    experiences_changed: int = 0
    derivation_runs: int = 0
    episodes_created: int = 0
    checkpoints_created: int = 0
    pack_expired_inspections: int = 0
    pack_interrupted_imports: int = 0
    pack_orphan_objects_removed: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class ResearchMaintenanceRunner:
    @classmethod
    def run(
        cls,
        sessions: Iterable[Any],
        *,
        settings: ResearchSettings | None = None,
    ) -> ResearchMaintenanceReport:
        resolved = settings or load_research_settings()
        report = ResearchMaintenanceReport()
        if not resolved.journal_active:
            return report
        cutoff = time.time() - resolved.reconcile_recent_days * 24 * 60 * 60
        groups: dict[tuple[str, str, str, str], list[Any]] = {}
        for session in list(sessions or []):
            report.sessions_seen += 1
            last_active = float(
                getattr(session, "last_active_at", 0)
                or getattr(session, "updated_at", 0)
                or getattr(session, "created_at", 0)
                or 0
            )
            if last_active and last_active < cutoff:
                # Activity age limits expensive reconstruction, not delivery of
                # already committed projection work after a process restart.
                workdir = str(getattr(session, "workdir", "") or "")
                if resolved.routing_active and workdir and resolved.database_path(workdir).is_file():
                    try:
                        from .journal import ResearchEventSink
                        ResearchEventSink.retry_pending_routing(workdir=workdir, settings=resolved)
                    except Exception:
                        report.errors += 1
                report.sessions_skipped_old += 1
                continue
            metadata = dict(getattr(session, "metadata", {}) or {})
            key = (
                str(getattr(session, "tenant_id", "") or metadata.get("tenant_id") or ""),
                str(getattr(session, "user_id", "") or metadata.get("user_id") or ""),
                str(
                    getattr(session, "workspace_id", "")
                    or metadata.get("workspace_id")
                    or ""
                ),
                str(getattr(session, "workdir", "") or ""),
            )
            groups.setdefault(key, []).append(session)

        for (tenant_id, user_id, workspace_id, workdir), items in groups.items():
            owner_has_changes = False
            try:
                from .journal import ResearchEventSink
                owner_has_changes = ResearchEventSink.retry_pending_routing(
                    workdir=workdir or None, settings=resolved,
                    owner=(tenant_id, user_id, workspace_id), limit=100,
                ) > 0
            except Exception:
                report.errors += 1
            try:
                reconciler = ResearchJournalReconciler(workdir=workdir or None)
                reconcile_report = reconciler.reconcile(items)
                report.sessions_reconciled += reconcile_report.sessions_scanned
                report.event_attempts += reconcile_report.event_attempts
                report.event_results += reconcile_report.event_results
                report.events_created += reconcile_report.events_created
                owner_has_changes = owner_has_changes or reconcile_report.events_created > 0
                report.errors += reconcile_report.errors
            except Exception:
                report.errors += 1
            if resolved.research_pack_import_recovery_enabled and all(
                (tenant_id, user_id, workspace_id, workdir)
            ):
                try:
                    from .pack_importer import ResearchPackRecoveryManager

                    recovery = ResearchPackRecoveryManager(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        workdir=workdir,
                        settings=resolved,
                    ).run(recover_importing=True)
                    report.pack_expired_inspections += recovery.expired_inspections
                    report.pack_interrupted_imports += recovery.interrupted_imports
                    report.pack_orphan_objects_removed += (
                        recovery.evidence_removed
                        + recovery.wiki_pages_removed
                        + recovery.papers_removed
                    )
                    report.errors += len(recovery.errors or [])
                except Exception:
                    report.errors += 1
            if not resolved.attribution_shadow_active:
                continue
            try:
                AttributionShadowDispatcher.wait_for_idle(timeout=30)
                attribution_report = ResearchAttributionEngine(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    workdir=workdir or None,
                    settings=resolved,
                ).run(
                    trigger_kind="startup_reconcile",
                    force=False,
                    limit=resolved.attribution_batch_size,
                )
                report.attribution_runs += 1
                report.attribution_decisions += attribution_report.decision_count
                report.errors += len(attribution_report.errors)
                owner_has_changes = bool(
                    owner_has_changes or attribution_report.changed_count > 0
                )
                # A normal restart replays canonical Session facts with stable
                # idempotency keys.  If neither the Journal nor Attribution
                # changed, all downstream projections are already current; a
                # full Topic/Activity/Episode rebuild only burns CPU and makes
                # the freshly started Web service appear frozen.
                if not owner_has_changes:
                    continue
                if resolved.topics_active and resolved.topic_auto_discovery:
                    from .topics import ResearchTopicEngine

                    topic_report = ResearchTopicEngine(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        workdir=workdir or None,
                        settings=resolved,
                    ).run(
                        trigger_kind="startup_reconcile",
                        limit=max(100, resolved.attribution_batch_size * 10),
                    )
                    report.topic_runs += 1
                    report.topic_candidates_created += (
                        topic_report.candidate_created_count
                    )
                    report.topic_memberships_changed += (
                        topic_report.membership_changed_count
                    )
                    report.errors += len(topic_report.errors)
                if resolved.activities_active:
                    from .activities import ResearchActivityBuilder

                    activities = ResearchActivityBuilder(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        workdir=workdir or None,
                        settings=resolved,
                    ).rebuild_owner(limit=5000)
                    report.activity_runs += 1
                    report.activities_built += len(activities)
                    if resolved.experience_projection_active:
                        from .experience import ResearchExperienceProjector

                        experience_report = ResearchExperienceProjector(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            workdir=workdir or None,
                            settings=resolved,
                        ).apply_activities([item.activity_id for item in activities])
                        report.experience_runs += 1
                        report.experiences_changed += (
                            experience_report.created_count
                            + experience_report.revised_count
                        )
                if resolved.episodes_active and resolved.episode_auto_build:
                    from .episodes import ResearchDerivationEngine

                    derivation_report = ResearchDerivationEngine(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        workdir=workdir or None,
                        settings=resolved,
                    ).run_all(
                        trigger_kind="startup_reconcile",
                        force_open=False,
                    )
                    report.derivation_runs += derivation_report.project_count
                    report.episodes_created += (
                        derivation_report.episode_created_count
                    )
                    report.checkpoints_created += (
                        derivation_report.checkpoint_created_count
                    )
                    report.errors += len(derivation_report.errors)
                    if resolved.experience_projection_active:
                        from .experience import ResearchExperienceProjector
                        from .topic_store import ResearchTopicStore

                        projects = ResearchTopicStore.list_projects(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            include_archived=False,
                            limit=3,
                            workdir=workdir or None,
                            settings=resolved,
                        )
                        experience_report = ResearchExperienceProjector(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            workdir=workdir or None,
                            settings=resolved,
                        ).apply_projects([item.research_id for item in projects])
                        report.experience_runs += 1
                        report.experiences_changed += (
                            experience_report.created_count
                            + experience_report.revised_count
                        )
            except Exception:
                report.errors += 1
        return report
