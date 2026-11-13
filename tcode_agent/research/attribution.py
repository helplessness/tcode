"""Deterministic, zero-model-token Attribution Shadow engine."""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .attribution_models import (
    ATTRIBUTION_DECISION_VERSION,
    AttributionCorrectionInput,
    ResearchEventLink,
    ResearchEventLinkInput,
)
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent
from .store import ResearchEventStore


_RESEARCH_SIGNAL_PATTERN = re.compile(
    r"(?i)(\b(?:research|paper|literature|citation|evidence|experiment|dataset|"
    r"benchmark|hypothesis|methodology|analysis|reproduce|replication|model|"
    r"algorithm|repository|implementation|debug|wiki|finding)\b|"
    r"研究|论文|文献|引用|证据|实验|数据集|基准|假设|方法|分析|复现|模型|算法|"
    r"代码|实现|调试|课题|结论|知识库|证据脑)"
)
_GREETING_PATTERN = re.compile(
    r"(?i)^\s*(?:hi|hello|hey|thanks|thank you|good morning|good evening|"
    r"你好|您好|嗨|哈喽|谢谢|早上好|晚上好)[!！,.，。?？\s]*$"
)
_RESEARCH_FILE_SUFFIXES = {
    ".pdf",
    ".csv",
    ".tsv",
    ".ipynb",
    ".bib",
    ".ris",
    ".tex",
    ".docx",
    ".xlsx",
    ".parquet",
}

_PROJECTION_CONTROL_EVENT_TYPES = {
    "session.created",
    "turn.started",
    "tool.started",
    "mcp.called",
    "agent.spawned",
    "workflow.started",
    "workflow.stage.started",
    "file.read",
}
_PROJECTION_READ_ONLY_TOOLS = {
    "read",
    "glob",
    "grep",
    "session_search",
    "memory_read",
    "evidence_search",
    "evidence_graph",
    "paper_library_get_record",
    "paper_library_lookup_pdf",
    "paper_library_query",
    "paper_library_stats",
    "paper_library_count",
    "research_read",
    "research_workspace",
    "research_action_commit",
}


def _event_requires_projection(event: ResearchEvent) -> bool:
    """Whether this event should trigger Topic/Activity/Episode recomputation."""

    if event.event_type in _PROJECTION_CONTROL_EVENT_TYPES:
        return False
    if event.event_type in {"tool.completed", "mcp.completed"}:
        tool_name = str(event.payload.get("tool_name") or "").strip()
        if tool_name in _PROJECTION_READ_ONLY_TOOLS:
            return False
    return True


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nested_value(value: Any, names: set[str], *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key or "").strip().lower()
            if normalized in names and item not in (None, "", [], {}):
                return item
        for item in value.values():
            found = _nested_value(item, names, depth=depth + 1)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, (list, tuple)):
        for item in value[:32]:
            found = _nested_value(item, names, depth=depth + 1)
            if found not in (None, "", [], {}):
                return found
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _event_text(event: ResearchEvent) -> str:
    parts: list[str] = []
    for key in (
        "content_preview",
        "output_preview",
        "title",
        "filename",
        "path",
        "tool_name",
        "mcp_tool_name",
    ):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)[:4000]


@dataclass
class AttributionRunReport:
    run_id: str
    status: str = "completed"
    scanned_count: int = 0
    decision_count: int = 0
    changed_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "scanned_count": self.scanned_count,
            "decision_count": self.decision_count,
            "changed_count": self.changed_count,
            "counts": dict(self.counts),
            "errors": list(self.errors),
        }


class ResearchAttributionEngine:
    """Create explainable shadow links without changing prompts or user output."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ):
        self.tenant_id = str(tenant_id or "").strip()
        self.user_id = str(user_id or "").strip()
        self.workspace_id = str(workspace_id or "").strip()
        self.workdir = workdir
        self.settings = settings or load_research_settings()

    @property
    def owner(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
        }

    def _decision(
        self,
        event: ResearchEvent,
        *,
        run_id: str,
        state: str,
        relevance: str,
        source: str,
        confidence: float,
        reason: str,
        candidate_id: str = "",
        research_id: str = "",
        correction_id: str = "",
        expires_at: str = "",
        detail: dict[str, Any] | None = None,
    ) -> ResearchEventLink:
        return ResearchEventLinkInput(
            **self.owner,
            event_id=event.event_id,
            state=state,
            relevance=relevance,
            decision_source=source,
            confidence=confidence,
            reason_code=reason,
            run_id=run_id,
            candidate_id=candidate_id,
            research_id=research_id,
            correction_id=correction_id,
            expires_at=expires_at,
            reason_detail=detail or {},
        ).build()

    def _pending_or_frozen(
        self,
        event: ResearchEvent,
        *,
        run_id: str,
        relevance: str,
        source: str,
        confidence: float,
        reason: str,
        correction_id: str = "",
        restored_at: str = "",
        candidate_id: str = "",
        research_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> ResearchEventLink:
        base_time = _parse_time(restored_at or event.occurred_at)
        expires = base_time + timedelta(days=self.settings.pending_ttl_days)
        if expires <= datetime.now(timezone.utc) and not restored_at:
            return self._decision(
                event,
                run_id=run_id,
                state="frozen",
                relevance=relevance,
                source=source,
                confidence=confidence,
                reason="pending_ttl_expired",
                correction_id=correction_id,
                expires_at=_iso(expires),
                candidate_id=candidate_id,
                research_id=research_id,
                detail={"previous_reason": reason, **dict(detail or {})},
            )
        return self._decision(
            event,
            run_id=run_id,
            state="pending",
            relevance=relevance,
            source=source,
            confidence=confidence,
            reason=reason,
            correction_id=correction_id,
            expires_at=_iso(expires),
            candidate_id=candidate_id,
            research_id=research_id,
            detail=detail,
        )

    def _from_correction(
        self,
        event: ResearchEvent,
        correction: dict[str, Any],
        *,
        run_id: str,
    ) -> ResearchEventLink:
        action = str(correction.get("action") or "").strip().lower()
        correction_id = str(correction.get("correction_id") or "")
        candidate_id = str(correction.get("candidate_id") or "")
        research_id = str(correction.get("research_id") or "")
        if action == "exclude":
            return self._decision(
                event,
                run_id=run_id,
                state="excluded",
                relevance="private_only",
                source="user",
                confidence=1.0,
                reason="user_excluded",
                correction_id=correction_id,
            )
        if action == "include" and (candidate_id or research_id):
            return self._decision(
                event,
                run_id=run_id,
                state="included",
                relevance="research",
                source="user",
                confidence=1.0,
                reason="user_included",
                candidate_id=candidate_id,
                research_id=research_id,
                correction_id=correction_id,
            )
        if action == "include":
            return self._pending_or_frozen(
                event,
                run_id=run_id,
                relevance="research",
                source="user",
                confidence=1.0,
                reason="user_marked_research_without_target",
                correction_id=correction_id,
                restored_at=str(correction.get("occurred_at") or ""),
            )
        return self._pending_or_frozen(
            event,
            run_id=run_id,
            relevance="uncertain",
            source="user",
            confidence=1.0,
            reason=(
                "user_restored_frozen" if action == "restore_frozen" else "user_pending"
            ),
            correction_id=correction_id,
            restored_at=str(correction.get("occurred_at") or ""),
        )

    def _inherit(
        self,
        event: ResearchEvent,
        link: ResearchEventLink,
        *,
        run_id: str,
        source: str,
        reason: str,
        confidence: float,
    ) -> ResearchEventLink:
        if (
            link.state == "included"
            and (link.candidate_id or link.research_id)
            and confidence >= self.settings.auto_include_threshold
        ):
            return self._decision(
                event,
                run_id=run_id,
                state="included",
                relevance="research",
                source=source,
                confidence=confidence,
                reason=reason,
                candidate_id=link.candidate_id,
                research_id=link.research_id,
                detail={"inherited_from_link_id": link.link_id},
            )
        if link.state == "included" and (link.candidate_id or link.research_id):
            return self._pending_or_frozen(
                event,
                run_id=run_id,
                relevance="research",
                source=source,
                confidence=confidence,
                reason="below_auto_include_threshold",
                candidate_id=link.candidate_id,
                research_id=link.research_id,
                detail={
                    "inherited_from_link_id": link.link_id,
                    "required_confidence": self.settings.auto_include_threshold,
                    "original_reason": reason,
                },
            )
        if link.state == "excluded":
            return self._decision(
                event,
                run_id=run_id,
                state="excluded",
                relevance="private_only",
                source=source,
                confidence=confidence,
                reason=reason,
                detail={"inherited_from_link_id": link.link_id},
            )
        return self._pending_or_frozen(
            event,
            run_id=run_id,
            relevance=("research" if link.relevance == "research" else "uncertain"),
            source=source,
            confidence=confidence,
            reason=reason,
            detail={"inherited_from_link_id": link.link_id},
        )

    def _classify(
        self,
        event: ResearchEvent,
        *,
        run_id: str,
        correction: dict[str, Any] | None,
        candidate_membership: tuple[Any, Any] | None,
        active_by_event: dict[str, ResearchEventLink],
        trace_links: dict[str, ResearchEventLink],
        source_links: dict[str, ResearchEventLink],
        session_links: dict[str, tuple[ResearchEventLink, datetime]],
    ) -> ResearchEventLink:
        if correction:
            return self._from_correction(event, correction, run_id=run_id)

        payload = event.payload
        explicit_private = _truthy(
            _nested_value(
                payload,
                {
                    "exclude_from_research",
                    "private_session_only",
                    "research_excluded",
                },
            )
        )
        explicit_state = str(
            _nested_value(payload, {"research_state", "attribution_state"}) or ""
        ).strip().lower()
        if explicit_private or explicit_state == "excluded":
            return self._decision(
                event,
                run_id=run_id,
                state="excluded",
                relevance="private_only",
                source="explicit",
                confidence=1.0,
                reason="explicit_private_only",
            )

        candidate_id = str(
            _nested_value(payload, {"candidate_id", "research_candidate_id"}) or ""
        ).strip()
        research_id = str(
            _nested_value(payload, {"research_id", "research_project_id"}) or ""
        ).strip()
        explicit_include = _truthy(
            _nested_value(payload, {"include_in_research", "research_relevant"})
        )
        if candidate_id or research_id:
            return self._decision(
                event,
                run_id=run_id,
                state="included",
                relevance="research",
                source="explicit",
                confidence=1.0,
                reason="explicit_research_context",
                candidate_id=candidate_id,
                research_id=research_id,
            )
        if candidate_membership is not None:
            membership, candidate = candidate_membership
            return self._decision(
                event,
                run_id=run_id,
                state="included",
                relevance="research",
                source="topic",
                confidence=float(getattr(membership, "confidence", 0.9) or 0.9),
                reason="topic_candidate_membership",
                candidate_id=str(getattr(candidate, "candidate_id", "") or ""),
                research_id=str(
                    getattr(candidate, "confirmed_research_id", "") or ""
                ),
                detail={
                    "membership_id": str(
                        getattr(membership, "membership_id", "") or ""
                    ),
                    "candidate_state": str(getattr(candidate, "state", "") or ""),
                },
            )
        if explicit_include:
            return self._pending_or_frozen(
                event,
                run_id=run_id,
                relevance="research",
                source="explicit",
                confidence=0.99,
                reason="explicit_research_without_target",
            )

        if event.parent_event_id and event.parent_event_id in active_by_event:
            return self._inherit(
                event,
                active_by_event[event.parent_event_id],
                run_id=run_id,
                source="lineage",
                reason="parent_event",
                confidence=0.99,
            )
        if event.trace_id and event.trace_id in trace_links:
            return self._inherit(
                event,
                trace_links[event.trace_id],
                run_id=run_id,
                source="lineage",
                reason="same_trace",
                confidence=0.97,
            )
        if event.source_ref and event.source_ref in source_links:
            return self._inherit(
                event,
                source_links[event.source_ref],
                run_id=run_id,
                source="lineage",
                reason="same_source_ref",
                confidence=0.95,
            )

        text = _event_text(event)
        if event.actor_type == "user" and text and _GREETING_PATTERN.fullmatch(text):
            return self._decision(
                event,
                run_id=run_id,
                state="excluded",
                relevance="private_only",
                source="rule",
                confidence=0.98,
                reason="standalone_greeting",
            )
        if text and _RESEARCH_SIGNAL_PATTERN.search(text):
            return self._pending_or_frozen(
                event,
                run_id=run_id,
                relevance="research",
                source="rule",
                confidence=0.78,
                reason="research_signal_without_target",
            )
        lowered_ref = str(event.source_ref or "").strip().lower()
        if any(lowered_ref.endswith(suffix) for suffix in _RESEARCH_FILE_SUFFIXES):
            return self._pending_or_frozen(
                event,
                run_id=run_id,
                relevance="research",
                source="rule",
                confidence=0.72,
                reason="research_file_type_without_target",
            )

        # A new user message is never silently assigned from old session context;
        # this is the primary guard against topic drift and unrelated conversation.
        if event.actor_type != "user" and event.session_id in session_links:
            previous_link, previous_time = session_links[event.session_id]
            age = _parse_time(event.occurred_at) - previous_time
            max_age = timedelta(
                minutes=self.settings.attribution_session_context_minutes
            )
            if timedelta(0) <= age <= max_age:
                return self._inherit(
                    event,
                    previous_link,
                    run_id=run_id,
                    source="session_context",
                    reason="recent_session_context",
                    confidence=0.91,
                )

        return self._pending_or_frozen(
            event,
            run_id=run_id,
            relevance="uncertain",
            source="rule",
            confidence=0.50,
            reason="insufficient_signal",
        )

    def run(
        self,
        *,
        trigger_kind: str = "manual",
        event_ids: list[str] | None = None,
        force: bool = False,
        limit: int | None = None,
    ) -> AttributionRunReport:
        if not self.settings.attribution_shadow_active:
            raise RuntimeError("Attribution Shadow is not active")
        if self.settings.attribution_model_enabled:
            raise RuntimeError(
                "Stage 2 only supports deterministic shadow decisions; model attribution is disabled"
            )
        bounded_limit = max(
            1,
            min(
                int(limit or self.settings.attribution_batch_size),
                5000,
            ),
        )
        run_id = ResearchEventStore.start_attribution_run(
            **self.owner,
            trigger_kind=trigger_kind,
            decision_version=ATTRIBUTION_DECISION_VERSION,
            workdir=self.workdir,
            settings=self.settings,
        )
        report = AttributionRunReport(run_id=run_id)
        counts: Counter[str] = Counter()
        try:
            all_events = ResearchEventStore.list_events_for_attribution(
                **self.owner,
                workdir=self.workdir,
                limit=5000,
                settings=self.settings,
            )
            requested = {
                str(item or "").strip()
                for item in (event_ids or [])
                if str(item or "").strip()
            }
            if requested:
                requested_events = ResearchEventStore.list_events_for_attribution(
                    **self.owner,
                    workdir=self.workdir,
                    event_ids=sorted(requested),
                    limit=min(len(requested), 1000),
                    settings=self.settings,
                )
                known_ids = {event.event_id for event in all_events}
                all_events.extend(
                    event for event in requested_events if event.event_id not in known_ids
                )
                all_events.sort(key=lambda item: (item.occurred_at, item.ingested_at))
            active_links = ResearchEventStore.list_attribution_links(
                **self.owner,
                workdir=self.workdir,
                active_only=True,
                limit=5000,
                settings=self.settings,
            )
            active_by_event = {link.event_id: link for link in active_links}
            corrections = ResearchEventStore.latest_attribution_corrections(
                **self.owner,
                workdir=self.workdir,
                event_ids=event_ids,
                settings=self.settings,
            )
            candidate_memberships: dict[str, tuple[Any, Any]] = {}
            if self.settings.topics_active:
                try:
                    from .topic_store import ResearchTopicStore

                    candidates = ResearchTopicStore.list_candidates(
                        **self.owner,
                        include_inactive=False,
                        limit=1000,
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    candidate_by_id = {
                        candidate.candidate_id: candidate for candidate in candidates
                    }
                    for membership in ResearchTopicStore.list_memberships(
                        **self.owner,
                        active_only=True,
                        limit=5000,
                        workdir=self.workdir,
                        settings=self.settings,
                    ):
                        candidate = candidate_by_id.get(membership.candidate_id)
                        if candidate is not None:
                            candidate_memberships[membership.event_id] = (
                                membership,
                                candidate,
                            )
                except Exception:
                    candidate_memberships = {}
            target_events: list[ResearchEvent] = []
            now_text = _iso(datetime.now(timezone.utc))
            for event in all_events:
                if requested and event.event_id not in requested:
                    continue
                active = active_by_event.get(event.event_id)
                must_refresh = bool(
                    force
                    or event.event_id in corrections
                    or active is None
                    or active.decision_version != ATTRIBUTION_DECISION_VERSION
                    or (
                        active.state == "pending"
                        and active.expires_at
                        and active.expires_at <= now_text
                    )
                )
                if must_refresh:
                    target_events.append(event)
                if len(target_events) >= bounded_limit:
                    break

            # Replay is causal: only decisions from events that occur earlier in the
            # stable event order may influence the current event. Pre-seeding these
            # maps with every active link would let a later correction leak backward.
            target_ids = {event.event_id for event in target_events}
            processed_by_event: dict[str, ResearchEventLink] = {}
            trace_links: dict[str, ResearchEventLink] = {}
            source_links: dict[str, ResearchEventLink] = {}
            session_links: dict[str, tuple[ResearchEventLink, datetime]] = {}
            for event in all_events:
                if event.event_id not in target_ids:
                    existing_link = active_by_event.get(event.event_id)
                    if existing_link is not None:
                        processed_by_event[event.event_id] = existing_link
                        if event.trace_id:
                            trace_links[event.trace_id] = existing_link
                        if event.source_ref:
                            source_links[event.source_ref] = existing_link
                        if event.session_id:
                            session_links[event.session_id] = (
                                existing_link,
                                _parse_time(event.occurred_at),
                            )
                    continue
                report.scanned_count += 1
                try:
                    link = self._classify(
                        event,
                        run_id=run_id,
                        correction=corrections.get(event.event_id),
                        candidate_membership=candidate_memberships.get(event.event_id),
                        active_by_event=processed_by_event,
                        trace_links=trace_links,
                        source_links=source_links,
                        session_links=session_links,
                    )
                    stored, changed = ResearchEventStore.append_attribution_link(
                        link,
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    report.decision_count += 1
                    report.changed_count += 1 if changed else 0
                    counts[stored.state] += 1
                    active_by_event[event.event_id] = stored
                    processed_by_event[event.event_id] = stored
                    if event.trace_id:
                        trace_links[event.trace_id] = stored
                    if event.source_ref:
                        source_links[event.source_ref] = stored
                    if event.session_id:
                        session_links[event.session_id] = (
                            stored,
                            _parse_time(event.occurred_at),
                        )
                except Exception as exc:
                    report.errors.append(
                        f"{event.event_id}: {type(exc).__name__}: {str(exc)[:300]}"
                    )
            report.counts = dict(counts)
            if report.errors:
                report.status = "partial"
        except Exception as exc:
            report.status = "failed"
            report.errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
        ResearchEventStore.finish_attribution_run(
            run_id,
            status=report.status,
            scanned_count=report.scanned_count,
            decision_count=report.decision_count,
            changed_count=report.changed_count,
            counts=report.counts,
            errors=report.errors,
            workdir=self.workdir,
            settings=self.settings,
        )
        return report

    def correct(
        self,
        correction: AttributionCorrectionInput,
    ) -> tuple[dict[str, Any], AttributionRunReport, ResearchEventLink | None]:
        stored = ResearchEventStore.append_attribution_correction(
            correction,
            workdir=self.workdir,
            settings=self.settings,
        )
        report = self.run(
            trigger_kind="user_correction",
            event_ids=[correction.event_id],
            force=True,
            limit=1,
        )
        links = ResearchEventStore.list_attribution_links(
            **self.owner,
            workdir=self.workdir,
            event_id=correction.event_id,
            active_only=True,
            limit=1,
            settings=self.settings,
        )
        return stored, report, links[0] if links else None


class AttributionShadowDispatcher:
    """Bounded daemon queue so Journal writes never wait on attribution."""

    _lock = threading.RLock()
    _queue: queue.Queue[tuple[ResearchEvent, str | None, ResearchSettings]] | None = None
    _thread: threading.Thread | None = None

    @classmethod
    def _ensure_started(cls, settings: ResearchSettings) -> None:
        with cls._lock:
            if cls._queue is None:
                cls._queue = queue.Queue(maxsize=settings.attribution_queue_size)
            if cls._thread is None or not cls._thread.is_alive():
                cls._thread = threading.Thread(
                    target=cls._worker,
                    name="research-attribution-shadow",
                    daemon=True,
                )
                cls._thread.start()

    @classmethod
    def submit(
        cls,
        event: ResearchEvent,
        *,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> bool:
        if not (
            settings.attribution_shadow_active
            and settings.attribution_auto_dispatch
            and not settings.attribution_model_enabled
        ):
            return False
        cls._ensure_started(settings)
        try:
            assert cls._queue is not None
            cls._queue.put_nowait((event, workdir, settings))
            return True
        except queue.Full:
            return False

    @classmethod
    def _worker(cls) -> None:
        while True:
            target_queue = cls._queue
            if target_queue is None:
                return
            first = target_queue.get()
            batch = [first]
            try:
                max_batch = max(1, int(first[2].attribution_batch_size))
                debounce_seconds = max(
                    0.0,
                    float(first[2].attribution_debounce_ms) / 1000.0,
                )
                quiet_deadline = time.monotonic() + debounce_seconds
                while len(batch) < max_batch:
                    try:
                        if debounce_seconds <= 0:
                            batch.append(target_queue.get_nowait())
                            continue
                        remaining = quiet_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        batch.append(target_queue.get(timeout=remaining))
                        quiet_deadline = time.monotonic() + debounce_seconds
                    except queue.Empty:
                        break
                grouped: dict[
                    tuple[str, str, str, str | None],
                    tuple[list[str], ResearchSettings],
                ] = {}
                for event, workdir, settings in batch:
                    key = (
                        event.tenant_id,
                        event.user_id,
                        event.workspace_id,
                        workdir,
                    )
                    ids, _ = grouped.setdefault(key, ([], settings))
                    ids.append(event.event_id)
                for (tenant_id, user_id, workspace_id, workdir), (
                    event_ids,
                    settings,
                ) in grouped.items():
                    try:
                        if workdir and not Path(workdir).exists():
                            continue
                        attribution_report = ResearchAttributionEngine(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            workdir=workdir,
                            settings=settings,
                        ).run(
                            trigger_kind="event_sink",
                            event_ids=event_ids,
                            limit=len(event_ids),
                        )
                        grouped_event_ids = set(event_ids)
                        should_refresh_projections = any(
                            event.event_id in grouped_event_ids
                            and _event_requires_projection(event)
                            for event, _, _ in batch
                        )
                        if (
                            attribution_report.status != "failed"
                            and should_refresh_projections
                            and settings.topics_active
                            and settings.topic_auto_discovery
                        ):
                            from .topics import ResearchTopicEngine

                            ResearchTopicEngine(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                workspace_id=workspace_id,
                                workdir=workdir,
                                settings=settings,
                            ).run(
                                trigger_kind="event_sink",
                                limit=max(100, len(event_ids) * 10),
                            )
                        activity_report = None
                        if (
                            attribution_report.status != "failed"
                            and should_refresh_projections
                            and settings.activities_active
                        ):
                            from .activities import ResearchActivityBuilder

                            activity_report = ResearchActivityBuilder(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                workspace_id=workspace_id,
                                workdir=workdir,
                                settings=settings,
                            ).apply_events(event_ids)
                        affected_research_ids = list(
                            getattr(activity_report, "affected_research_ids", []) or []
                        )
                        affected_activity_ids = list(
                            getattr(activity_report, "affected_activity_ids", []) or []
                        )
                        if (
                            activity_report is not None
                            and settings.experience_projection_active
                        ):
                            from .experience import ResearchExperienceProjector

                            projector = ResearchExperienceProjector(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                workspace_id=workspace_id,
                                workdir=workdir,
                                settings=settings,
                            )
                            if affected_activity_ids:
                                projector.apply_activities(affected_activity_ids)
                            projector.remove_activity_keys(
                                list(
                                    getattr(activity_report, "removed_activity_keys", [])
                                    or []
                                )
                            )
                        if (
                            attribution_report.status != "failed"
                            and should_refresh_projections
                            and settings.episodes_active
                            and settings.episode_auto_build
                            and affected_research_ids
                        ):
                            from .episodes import ResearchDerivationEngine

                            derivation_report = ResearchDerivationEngine(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                workspace_id=workspace_id,
                                workdir=workdir,
                                settings=settings,
                            ).run_projects(
                                affected_research_ids,
                                trigger_kind="event_sink",
                                force_open=False,
                            )
                            if settings.experience_projection_active:
                                from .experience import ResearchExperienceProjector

                                ResearchExperienceProjector(
                                    tenant_id=tenant_id,
                                    user_id=user_id,
                                    workspace_id=workspace_id,
                                    workdir=workdir,
                                    settings=settings,
                                ).apply_episodes(
                                    derivation_report.affected_episode_ids
                                )
                    except Exception:
                        pass
            finally:
                for _ in batch:
                    target_queue.task_done()

    @classmethod
    def wait_for_idle(cls, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            target_queue = cls._queue
            if target_queue is None or target_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        target_queue = cls._queue
        return target_queue is None or target_queue.unfinished_tasks == 0
