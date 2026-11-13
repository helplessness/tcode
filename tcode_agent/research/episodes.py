"""Deterministic Episode/Checkpoint builders and append-only research evolution."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import ResearchSettings, load_research_settings
from .episode_models import (
    CHECKPOINT_GENERATOR_VERSION,
    EPISODE_DERIVATION_VERSION,
    DerivationRunReport,
    ResearchConflict,
    ResearchEpisode,
)
from .episode_store import ResearchDerivationStore
from .models import ResearchEvent, canonical_json, sha256_text
from .store import ResearchEventStore
from .topic_store import ResearchTopicStore


_GOAL_PATTERN = re.compile(
    r"(?i)(?:研究|实验|实现|分析|复现|开发|优化)(?:目标|目的)|"
    r"(?:目标|目的|课题)(?:是|为|：|:)|"
    r"\b(?:research goal|goal is|objective|we aim)\b"
)
_DECISION_PATTERN = re.compile(
    r"(?i)(?:决定|选择|改用|采用|不再使用|最终使用|decision|decided|switch(?:ed)? to)"
)
_NEXT_PATTERN = re.compile(
    r"(?i)(?:下一步|后续(?:需要|计划)|待办|TODO|next step|next action)"
)
_OPEN_PATTERN = re.compile(
    r"(?i)(?:待解决|开放问题|尚不清楚|仍需(?:.{0,30})?验证|无法确定|open question|unresolved)"
)
_COMPLETED_PATTERN = re.compile(
    r"(?i)(?:任务|研究|实验|复现|实现)(?:已经|已)?完成|"
    r"(?:完成了|验证通过|成功复现)|\b(?:task|research|experiment) completed\b"
)

_FAILURE_TYPES = {
    "tool.failed",
    "mcp.failed",
    "workflow.failed",
    "agent.failed",
    "task.failed",
    "turn.failed",
}
_ABANDONED_TYPES = {
    "tool.cancelled",
    "workflow.cancelled",
    "agent.cancelled",
    "task.cancelled",
    "turn.cancelled",
}
_COMPLETION_TYPES = {
    "workflow.completed",
    "agent.completed",
    "task.completed",
    "job.completed",
}
_TOOL_PREFIXES = ("tool.", "mcp.", "workflow.", "agent.", "task.", "job.")
_ARTIFACT_TYPES = {
    "artifact.created",
    "artifact.updated",
    "file.uploaded",
    "file.changed",
    "document.created",
}
_TECHNICAL_LINE_PATTERN = re.compile(
    r"(?i)^(?:session|trace|event|turn|call|episode|checkpoint|run|task)"
    r"(?:\s*[:#-]|_[a-z]+\s*[:=])"
)
_MECHANICAL_SUMMARY_PATTERN = re.compile(
    r"^记录\s*\d+\s*条研究事件，涉及\s*\d+\s*个\s*Session、"
)
_MARKDOWN_PREFIX_PATTERN = re.compile(r"^\s{0,3}(?:#{1,6}|[-*+] |\d+[.)] )\s*")


def _parse_time(value: str) -> datetime:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_text(event: ResearchEvent) -> str:
    payload = event.payload
    values: list[str] = []
    for key in (
        "content_preview",
        "output_preview",
        "error_preview",
        "message",
        "title",
        "summary",
        "preview",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "\n".join(values)[:8000]


def _readable_event_text(event: ResearchEvent, *, limit: int = 600) -> str:
    """Return user-facing prose and reject control, identifier, and tool payload noise."""

    if event.source_kind in {"tool_call", "mcp_tool", "workflow", "tool_artifact"}:
        return ""
    if event.event_type.startswith(("tool.", "mcp.", "workflow.")):
        return ""
    value = str(
        event.payload.get("content_preview")
        or event.payload.get("message")
        or event.payload.get("summary")
        or ""
    ).strip()
    if not value or value.startswith("# Task Contract"):
        return ""

    lines: list[str] = []
    in_code_block = False
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not line:
            continue
        line = _MARKDOWN_PREFIX_PATTERN.sub("", line).strip()
        if not line or _TECHNICAL_LINE_PATTERN.search(line):
            continue
        if line.startswith(("{", "[")) and line.endswith(("}", "]")):
            continue
        lines.append(line)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not text or _MECHANICAL_SUMMARY_PATTERN.search(text):
        return ""
    if len(text) <= limit:
        return text
    shortened = text[:limit]
    boundary = max(shortened.rfind(mark) for mark in ("。", "！", "？", ".", "!", "?", "；", ";"))
    return (shortened[: boundary + 1] if boundary >= max(80, limit // 2) else shortened.rstrip()) + "…"


def _episode_title(project: Any, events: list[ResearchEvent], goal: dict[str, Any]) -> str:
    for event in events:
        if event.actor_type != "user":
            continue
        text = _readable_event_text(event, limit=180)
        if not text:
            continue
        first = re.split(r"[。！？!?\n]", text, maxsplit=1)[0].strip()
        first = re.sub(r"^(?:请|麻烦)?(?:帮我|协助我)?", "", first).strip(" ：:")
        if first:
            return first[:160]
    return str(goal.get("value") or project.title or "研究进展")[:160]


def _episode_summary(
    project: Any,
    events: list[ResearchEvent],
    *,
    tools: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    outcome: str,
) -> tuple[str, str, str]:
    """Build readable progress prose without an LLM or additional token cost."""

    agent_event = next(
        (
            event
            for event in reversed(events)
            if event.actor_type == "agent" and _readable_event_text(event)
        ),
        None,
    )
    if agent_event is not None:
        return _readable_event_text(agent_event), "observed", agent_event.event_id

    user_event = next(
        (
            event
            for event in events
            if event.actor_type == "user" and _readable_event_text(event, limit=260)
        ),
        None,
    )
    subject = (
        _readable_event_text(user_event, limit=260)
        if user_event is not None
        else str(project.title or "当前课题")
    )
    if artifacts:
        value = f"围绕“{subject}”完成了一轮资料处理，并形成 {len(artifacts)} 项可追溯产物。"
    elif tools:
        state_text = "遇到执行问题" if outcome == "failed" else "完成了一轮分析"
        value = f"围绕“{subject}”{state_text}，相关过程与工具结果已保留，可继续核对或推进。"
    else:
        value = f"围绕“{subject}”记录了新的研究讨论，尚未形成明确结论。"
    return value[:600], "inferred", user_event.event_id if user_event is not None else ""


def _provenance(value: str, provenance: str, event_id: str = "") -> dict[str, Any]:
    return {
        "value": str(value or "").strip()[:4000],
        "provenance": provenance,
        "source_event_ids": [event_id] if event_id else [],
    }


def _dedupe_dicts(items: list[dict[str, Any]], key: str = "value") -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        marker = str(item.get(key) or canonical_json(item)).strip()
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


@dataclass
class _EpisodeGroup:
    events: list[ResearchEvent]
    boundary_reason: str


class ResearchDerivationEngine:
    """Deterministic readable derivation; never calls a model or mutates prompts."""

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
            "tenant_id": tenant_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        }
        self.workdir = workdir
        self.settings = settings or load_research_settings()

    def _project(self, research_id: str, *, require_active: bool = False):
        project = ResearchDerivationStore.get_project(
            research_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if project is None:
            raise ValueError("project does not belong to owner")
        if require_active and project.status != "active":
            raise ValueError("project must be active for research state changes")
        return project

    def _groups(
        self,
        events: list[ResearchEvent],
        active_episodes: list[ResearchEpisode],
        *,
        force_open: bool,
        force_reason: str,
    ) -> list[_EpisodeGroup]:
        if not events:
            return []
        existing_boundaries = {
            episode.event_ids[-1]: episode.boundary_reason
            for episode in active_episodes
            if episode.event_ids
        }
        idle_seconds = self.settings.episode_idle_candidate_minutes * 60
        force_seconds = self.settings.episode_idle_force_partial_minutes * 60
        groups: list[_EpisodeGroup] = []
        current: list[ResearchEvent] = []

        for event in events:
            if current:
                previous = current[-1]
                gap = max(
                    0.0,
                    (_parse_time(event.occurred_at) - _parse_time(previous.occurred_at)).total_seconds(),
                )
                reason = ""
                if previous.event_id in existing_boundaries:
                    reason = existing_boundaries[previous.event_id]
                elif event.session_id != previous.session_id:
                    reason = "session_changed"
                elif gap >= force_seconds:
                    reason = "idle_force_partial"
                elif gap >= idle_seconds and event.trace_id != previous.trace_id:
                    reason = "idle_candidate"
                if reason:
                    groups.append(_EpisodeGroup(events=current, boundary_reason=reason))
                    current = []
            current.append(event)

        if current:
            last = current[-1]
            if last.event_id in existing_boundaries:
                groups.append(
                    _EpisodeGroup(
                        events=current,
                        boundary_reason=existing_boundaries[last.event_id],
                    )
                )
            elif force_open:
                groups.append(
                    _EpisodeGroup(events=current, boundary_reason=force_reason or "manual_save")
                )
            else:
                age = max(
                    0.0,
                    (datetime.now(timezone.utc) - _parse_time(last.occurred_at)).total_seconds(),
                )
                if age >= force_seconds:
                    groups.append(
                        _EpisodeGroup(events=current, boundary_reason="idle_force_partial")
                    )
                elif age >= idle_seconds:
                    groups.append(
                        _EpisodeGroup(events=current, boundary_reason="idle_candidate")
                    )
        return groups

    def _episode_key(
        self,
        research_id: str,
        group: _EpisodeGroup,
        active_episodes: list[ResearchEpisode],
    ) -> str:
        group_ids = {event.event_id for event in group.events}
        best: tuple[float, str] | None = None
        for episode in active_episodes:
            old_ids = set(episode.event_ids)
            overlap = len(group_ids & old_ids) / max(1, len(group_ids | old_ids))
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, episode.episode_key)
        if best is not None:
            return best[1]
        digest = sha256_text(
            canonical_json(
                {
                    **self.owner,
                    "research_id": research_id,
                    "first_event_id": group.events[0].event_id,
                }
            )
        )
        return f"episode-key-{digest.split(':', 1)[-1][:24]}"

    def _derive_episode(self, project: Any, group: _EpisodeGroup) -> dict[str, Any]:
        events = group.events
        goal_event = next(
            (
                event
                for event in events
                if event.actor_type == "user"
                and _GOAL_PATTERN.search(_readable_event_text(event))
            ),
            None,
        )
        if goal_event is not None:
            goal_text = _readable_event_text(goal_event, limit=400)
            goal = _provenance(goal_text, "user_stated", goal_event.event_id)
        else:
            goal = _provenance(project.title, "inferred")

        artifacts: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        open_questions: list[dict[str, Any]] = []
        next_actions: list[dict[str, Any]] = []
        completion_signal = False
        failure_signal = False
        abandoned_signal = False
        success_signal = False

        last_agent_message_index = -1
        last_user_question: tuple[int, ResearchEvent, str] | None = None
        for index, event in enumerate(events):
            text = _readable_event_text(event)
            provenance = "user_stated" if event.actor_type == "user" else "observed"
            if text and event.actor_type == "agent":
                last_agent_message_index = index
            if text and event.actor_type == "user" and ("?" in text or "？" in text):
                last_user_question = (index, event, text)
            if event.event_type.startswith(_TOOL_PREFIXES) or event.source_kind in {
                "tool_call",
                "mcp_tool",
                "workflow",
                "subagent",
            }:
                status = event.event_type.rsplit(".", 1)[-1]
                tools.append(
                    {
                        "name": str(
                            event.payload.get("tool_name")
                            or event.payload.get("mcp_tool_name")
                            or event.actor_id
                            or event.source_kind
                        )[:160],
                        "status": status,
                        "event_id": event.event_id,
                        "source_ref": event.source_ref,
                        "provenance": "observed",
                    }
                )
                if event.event_type in _FAILURE_TYPES or status in {"failed", "error"}:
                    failure_signal = True
                    failures.append(
                        {
                            "value": f"{tools[-1]['name']} 执行失败，需要检查输入或运行条件。",
                            "provenance": "observed",
                            "source_event_ids": [event.event_id],
                        }
                    )
                elif status in {"completed", "succeeded"}:
                    success_signal = True
            if event.event_type in _ARTIFACT_TYPES or event.source_kind in {
                "artifact",
                "file_upload",
                "file_change",
                "document",
                "tool_artifact",
            }:
                ref = str(
                    event.source_ref
                    or event.payload.get("path")
                    or event.payload.get("filename")
                    or event.payload.get("output_path")
                    or ""
                ).strip()
                if ref:
                    artifacts.append(
                        {
                            "ref": ref[:1000],
                            "event_id": event.event_id,
                            "kind": event.event_type,
                            "provenance": "observed",
                        }
                    )
                    success_signal = True
            if text and _DECISION_PATTERN.search(text):
                decisions.append(_provenance(text, provenance, event.event_id))
            if text and _OPEN_PATTERN.search(text):
                open_questions.append(_provenance(text, provenance, event.event_id))
            if text and _NEXT_PATTERN.search(text):
                next_actions.append(_provenance(text, provenance, event.event_id))
            if event.event_type in _COMPLETION_TYPES or (
                event.actor_type == "user" and _COMPLETED_PATTERN.search(text)
            ):
                completion_signal = True
            if event.event_type in _FAILURE_TYPES:
                failure_signal = True
            if event.event_type in _ABANDONED_TYPES:
                abandoned_signal = True

        if last_user_question is not None and last_user_question[0] > last_agent_message_index:
            _, question_event, question_text = last_user_question
            open_questions.append(
                _provenance(question_text, "user_stated", question_event.event_id)
            )

        if abandoned_signal:
            outcome = "abandoned"
        elif completion_signal and not failure_signal:
            outcome = "completed"
        elif failure_signal and not success_signal:
            outcome = "failed"
        elif failure_signal or success_signal or group.boundary_reason in {
            "manual_save",
            "project_confirmed",
            "idle_force_partial",
        }:
            outcome = "partial"
        else:
            outcome = "unknown"

        session_ids = list(dict.fromkeys(event.session_id for event in events if event.session_id))
        trace_ids = list(dict.fromkeys(event.trace_id for event in events if event.trace_id))
        summary_value, summary_provenance, summary_event_id = _episode_summary(
            project,
            events,
            tools=tools,
            artifacts=artifacts,
            outcome=outcome,
        )
        source_digest = sha256_text(
            canonical_json(
                {
                    "events": [
                        [event.event_id, event.content_hash, event.occurred_at]
                        for event in events
                    ],
                    "boundary_reason": group.boundary_reason,
                    "version": EPISODE_DERIVATION_VERSION,
                }
            )
        )
        return {
            "title": _episode_title(project, events, goal),
            "goal": goal,
            "summary": _provenance(
                summary_value,
                summary_provenance,
                summary_event_id,
            ),
            "outcome_status": outcome,
            "event_ids": [event.event_id for event in events],
            "session_ids": session_ids,
            "trace_ids": trace_ids,
            "artifact_refs": _dedupe_dicts(artifacts, "ref"),
            "tool_calls": tools,
            "decisions": _dedupe_dicts(decisions),
            "failed_paths": _dedupe_dicts(failures),
            "open_questions": _dedupe_dicts(open_questions),
            "next_actions": _dedupe_dicts(next_actions),
            "started_at": events[0].occurred_at,
            "ended_at": events[-1].occurred_at,
            "boundary_reason": group.boundary_reason,
            "source_digest": source_digest,
        }

    def run_project(
        self,
        research_id: str,
        *,
        trigger_kind: str = "manual",
        force_open: bool = False,
        force_reason: str = "manual_save",
        checkpoint_on_change_only: bool = False,
    ) -> DerivationRunReport:
        if not self.settings.episodes_active:
            raise RuntimeError("Episode Preview is not active")
        project = self._project(research_id, require_active=True)
        run_id = ResearchDerivationStore.start_run(
            **self.owner,
            research_id=research_id,
            trigger_kind=trigger_kind,
            workdir=self.workdir,
            settings=self.settings,
        )
        report = DerivationRunReport(
            run_id=run_id,
            project_count=1,
            affected_research_ids=[research_id],
        )
        try:
            events = ResearchDerivationStore.list_project_events(
                research_id,
                **self.owner,
                limit=self.settings.episode_max_events,
                workdir=self.workdir,
                settings=self.settings,
            )
            report.event_count = len(events)
            active = ResearchDerivationStore.list_episodes(
                research_id,
                **self.owner,
                limit=1000,
                workdir=self.workdir,
                settings=self.settings,
            )
            groups = self._groups(
                events,
                active,
                force_open=force_open,
                force_reason=force_reason,
            )
            matched_keys: set[str] = set()
            for group in groups:
                episode_key = self._episode_key(research_id, group, active)
                payload = self._derive_episode(project, group)
                episode, created = ResearchDerivationStore.append_episode(
                    **self.owner,
                    research_id=research_id,
                    episode_key=episode_key,
                    workdir=self.workdir,
                    settings=self.settings,
                    **payload,
                )
                matched_keys.add(episode_key)
                report.episode_created_count += 1 if created else 0
                if created:
                    report.affected_episode_ids.append(episode.episode_id)
            stale_episode_ids = [
                episode.episode_id
                for episode in active
                if episode.episode_key not in matched_keys
            ]
            report.episode_stale_count = (
                ResearchDerivationStore.mark_unmatched_episodes_stale(
                    research_id,
                    **self.owner,
                    matched_episode_keys=matched_keys,
                    workdir=self.workdir,
                    settings=self.settings,
                )
            )
            if report.episode_stale_count:
                report.affected_episode_ids.extend(stale_episode_ids)
            report.affected_episode_ids = list(
                dict.fromkeys(report.affected_episode_ids)
            )
            if self.settings.checkpoints_active and (
                not checkpoint_on_change_only
                or report.episode_created_count > 0
                or report.episode_stale_count > 0
            ):
                _, checkpoint_created = self.refresh_checkpoint(research_id)
                report.checkpoint_created_count += 1 if checkpoint_created else 0
        except Exception as exc:
            report.status = "failed"
            report.errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
        ResearchDerivationStore.finish_run(
            run_id,
            report.to_dict(),
            workdir=self.workdir,
            settings=self.settings,
        )
        return report

    def run_projects(
        self,
        research_ids: list[str],
        *,
        trigger_kind: str = "background",
        force_open: bool = False,
        force_reason: str = "manual_save",
        checkpoint_on_change_only: bool = True,
    ) -> DerivationRunReport:
        """Refresh only explicitly affected local projects."""

        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in research_ids
                if str(item or "").strip()
            )
        )[:3]
        aggregate = DerivationRunReport(run_id=f"derivation-affected-{uuid.uuid4()}")
        for research_id in normalized:
            report = self.run_project(
                research_id,
                trigger_kind=trigger_kind,
                force_open=force_open,
                force_reason=force_reason,
                checkpoint_on_change_only=checkpoint_on_change_only,
            )
            aggregate.project_count += 1
            aggregate.event_count += report.event_count
            aggregate.episode_created_count += report.episode_created_count
            aggregate.episode_stale_count += report.episode_stale_count
            aggregate.checkpoint_created_count += report.checkpoint_created_count
            aggregate.affected_research_ids.extend(report.affected_research_ids)
            aggregate.affected_episode_ids.extend(report.affected_episode_ids)
            aggregate.errors.extend(report.errors)
        aggregate.affected_research_ids = list(
            dict.fromkeys(aggregate.affected_research_ids)
        )
        aggregate.affected_episode_ids = list(
            dict.fromkeys(aggregate.affected_episode_ids)
        )
        if aggregate.errors:
            aggregate.status = "partial"
        return aggregate

    def run_all(
        self,
        *,
        trigger_kind: str = "background",
        force_open: bool = False,
        force_reason: str = "manual_save",
    ) -> DerivationRunReport:
        aggregate = DerivationRunReport(run_id=f"derivation-batch-{uuid.uuid4()}")
        projects = ResearchTopicStore.list_projects(
            **self.owner,
            include_archived=False,
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        )
        for project in projects:
            report = self.run_project(
                project.research_id,
                trigger_kind=trigger_kind,
                force_open=force_open,
                force_reason=force_reason,
            )
            aggregate.project_count += 1
            aggregate.event_count += report.event_count
            aggregate.episode_created_count += report.episode_created_count
            aggregate.episode_stale_count += report.episode_stale_count
            aggregate.checkpoint_created_count += report.checkpoint_created_count
            aggregate.affected_research_ids.extend(report.affected_research_ids)
            aggregate.affected_episode_ids.extend(report.affected_episode_ids)
            aggregate.errors.extend(report.errors)
        aggregate.affected_research_ids = list(
            dict.fromkeys(aggregate.affected_research_ids)
        )
        aggregate.affected_episode_ids = list(
            dict.fromkeys(aggregate.affected_episode_ids)
        )
        if aggregate.errors:
            aggregate.status = "partial"
        return aggregate

    def refresh_checkpoint(self, research_id: str):
        if not self.settings.checkpoints_active:
            raise RuntimeError("Checkpoint Preview is not active")
        project = self._project(research_id, require_active=True)
        episodes = ResearchDerivationStore.list_episodes(
            research_id,
            **self.owner,
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        )
        findings = ResearchDerivationStore.list_findings(
            research_id,
            **self.owner,
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        )
        conflicts = ResearchDerivationStore.list_conflicts(
            research_id,
            **self.owner,
            statuses=["open", "under_review"],
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        ) if self.settings.conflicts_active else []
        if not episodes and not findings and not conflicts:
            ResearchDerivationStore.mark_active_checkpoint_stale(
                research_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            return None, False

        chronological = list(reversed(episodes))
        latest = episodes[0] if episodes else None
        confirmed_facts = [
            {
                "finding_id": finding.finding_id,
                "value": finding.statement,
                "status": finding.status,
                "provenance": finding.provenance,
                "evidence_ids": finding.evidence_ids,
                "source_event_ids": finding.source_event_ids,
            }
            for finding in findings
            if finding.status in {"active", "context_limited"}
            and finding.provenance in {"verified", "user_stated", "observed"}
        ]
        hypotheses = [
            {
                "finding_id": finding.finding_id,
                "value": finding.statement,
                "status": finding.status,
                "provenance": finding.provenance,
                "evidence_ids": finding.evidence_ids,
                "source_event_ids": finding.source_event_ids,
            }
            for finding in findings
            if finding.provenance == "inferred" or finding.status == "disputed"
        ]
        known_conflicts = [
            {
                "conflict_id": conflict.conflict_id,
                "type": conflict.conflict_type,
                "status": conflict.status,
                "summary": conflict.summary,
                "original_refs": conflict.original_refs,
                "challenging_refs": conflict.challenging_refs,
                "original_evidence_ids": conflict.original_evidence_ids,
                "challenging_evidence_ids": conflict.challenging_evidence_ids,
            }
            for conflict in conflicts
        ]
        recent_progress = [
            {
                "episode_id": episode.episode_id,
                "value": episode.summary.get("value", ""),
                "outcome_status": episode.outcome_status,
                # The Episode remains the canonical provenance container.  A
                # Checkpoint capsule only keeps bounded edge references so a
                # long Episode cannot consume the whole resume-token budget.
                "source_event_ids": (
                    episode.event_ids
                    if len(episode.event_ids) <= 8
                    else episode.event_ids[:4] + episode.event_ids[-4:]
                ),
                "source_event_count": len(episode.event_ids),
            }
            for episode in episodes[:5]
        ]
        decisions = _dedupe_dicts(
            [item for episode in chronological for item in episode.decisions]
        )[-20:]
        failed_paths = _dedupe_dicts(
            [item for episode in chronological for item in episode.failed_paths]
        )[-20:]
        artifacts = _dedupe_dicts(
            [item for episode in chronological for item in episode.artifact_refs], "ref"
        )[-30:]
        open_questions = _dedupe_dicts(
            [item for episode in chronological for item in episode.open_questions]
        )[-20:]
        next_actions = _dedupe_dicts(
            [item for episode in chronological for item in episode.next_actions]
        )[-20:]
        content: dict[str, Any] = {
            "research_goal": (
                str(latest.goal.get("value") or "") if latest else ""
            ) or project.description or project.title,
            "current_state": (
                str(latest.summary.get("value") or "") if latest else ""
            ),
            "confirmed_facts": confirmed_facts[:50],
            "working_hypotheses": hypotheses[:50],
            "recent_progress": recent_progress,
            "decisions": decisions,
            "failed_paths": failed_paths,
            "known_conflicts": known_conflicts,
            "important_artifacts": artifacts,
            "open_questions": open_questions,
            "next_actions": next_actions,
        }

        def estimate() -> int:
            return max(1, len(canonical_json(content)) // 4)

        budget = self.settings.resume_token_budget
        trim_order = [
            "recent_progress",
            "decisions",
            "failed_paths",
            "important_artifacts",
            "working_hypotheses",
            "confirmed_facts",
            "open_questions",
            "next_actions",
        ]
        for key in trim_order:
            while estimate() > budget and len(content[key]) > 1:
                content[key].pop(0)
        token_estimate = estimate()
        budget_status = (
            "within_budget"
            if token_estimate <= budget
            else "conflict_overflow"
            if known_conflicts
            else "minimum_overflow"
        )
        source_episode_ids = [episode.episode_id for episode in episodes]
        source_event_ids = list(
            dict.fromkeys(event_id for episode in episodes for event_id in episode.event_ids)
        )
        source_digest = sha256_text(
            canonical_json(
                {
                    "episodes": [
                        [episode.episode_id, episode.source_digest] for episode in episodes
                    ],
                    "findings": [
                        [finding.finding_id, finding.current_revision, finding.status]
                        for finding in findings
                    ],
                    "conflicts": [
                        [
                            conflict.conflict_id,
                            conflict.status,
                            conflict.current_resolution_id,
                        ]
                        for conflict in conflicts
                    ],
                    "project": [project.title, project.description, project.status],
                    "version": CHECKPOINT_GENERATOR_VERSION,
                }
            )
        )
        return ResearchDerivationStore.append_checkpoint(
            **self.owner,
            research_id=research_id,
            source_episode_ids=source_episode_ids,
            source_event_ids=source_event_ids,
            source_digest=source_digest,
            token_estimate=token_estimate,
            budget_status=budget_status,
            workdir=self.workdir,
            settings=self.settings,
            **content,
        )

    def context_capsule(self, research_id: str) -> dict[str, Any]:
        self._project(research_id)
        checkpoint = ResearchDerivationStore.get_checkpoint(
            research_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if checkpoint is None:
            return {
                "research_id": research_id,
                "available": False,
                "prompt_injection_enabled": False,
                "known_conflicts": [],
            }
        return {
            "research_id": research_id,
            "available": True,
            "prompt_injection_enabled": self.settings.resume_context_active,
            "checkpoint_id": checkpoint.checkpoint_id,
            "revision": checkpoint.revision,
            "research_goal": checkpoint.research_goal,
            "current_state": checkpoint.current_state,
            "confirmed_facts": checkpoint.confirmed_facts,
            "working_hypotheses": checkpoint.working_hypotheses,
            "recent_progress": checkpoint.recent_progress,
            "known_conflicts": checkpoint.known_conflicts,
            "important_artifacts": checkpoint.important_artifacts,
            "open_questions": checkpoint.open_questions,
            "next_actions": checkpoint.next_actions,
            "token_estimate": checkpoint.token_estimate,
            "budget_status": checkpoint.budget_status,
        }


class ResearchEvolutionManager:
    """User-owned Finding/Conflict operations; history is always append-only."""

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
            "tenant_id": tenant_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        }
        self.workdir = workdir
        self.settings = settings or load_research_settings()

    def _engine(self) -> ResearchDerivationEngine:
        return ResearchDerivationEngine(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )

    def _refresh(self, research_id: str) -> None:
        if self.settings.checkpoints_active:
            self._engine().refresh_checkpoint(research_id)

    def _project_findings(self, finding_ids: list[str]) -> None:
        if not self.settings.experience_projection_active:
            return
        try:
            from .experience import ResearchExperienceProjector

            ResearchExperienceProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).apply_findings(finding_ids)
        except Exception:
            # Experience is a repairable projection. A projection failure must
            # not roll back or mask an already committed Finding mutation.
            return

    def _project_conflict_overlay(
        self,
        conflict: Any,
        resolution: Any | None = None,
    ) -> None:
        try:
            from .pack_overlay import ResearchPackOverlayProjector

            projector = ResearchPackOverlayProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            projector.project_conflict(conflict)
            if resolution is not None:
                projector.project_resolution(resolution, conflict)
        except Exception:
            # Conflict/Resolution remain authoritative local objects. Overlay
            # is an owner-scoped, repairable continuation projection.
            return

    def _require_active_project(self, research_id: str) -> None:
        self._engine()._project(research_id, require_active=True)

    def create_finding(
        self,
        research_id: str,
        *,
        statement: str,
        provenance: str,
        source_event_ids: list[str],
        actor_id: str,
        episode_id: str = "",
        scope: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
    ):
        self._require_active_project(research_id)
        finding, created = ResearchDerivationStore.create_finding(
            **self.owner,
            research_id=research_id,
            statement=statement,
            provenance=provenance,
            source_event_ids=source_event_ids,
            actor_id=actor_id,
            episode_id=episode_id,
            scope=scope,
            evidence_ids=evidence_ids,
            workdir=self.workdir,
            settings=self.settings,
        )
        change = ResearchDerivationStore.append_change(
            **self.owner,
            research_id=research_id,
            change_type="finding_created",
            subject_type="episode_finding",
            subject_id=finding.finding_id,
            actor_id=actor_id,
            reason="finding created",
            evidence_ids=finding.evidence_ids,
            idempotency_key=f"finding-created:{finding.finding_id}",
            workdir=self.workdir,
            settings=self.settings,
        )
        if created:
            self._refresh(research_id)
        self._project_findings([finding.finding_id])
        return finding, change, created

    def create_conflict(
        self,
        research_id: str,
        *,
        conflict_type: str,
        original_refs: list[str],
        challenging_refs: list[str],
        summary: str,
        actor_id: str,
        original_evidence_ids: list[str] | None = None,
        challenging_evidence_ids: list[str] | None = None,
    ):
        self._require_active_project(research_id)
        conflict, created = ResearchDerivationStore.create_conflict(
            **self.owner,
            research_id=research_id,
            conflict_type=conflict_type,
            original_refs=original_refs,
            challenging_refs=challenging_refs,
            summary=summary,
            actor_id=actor_id,
            original_evidence_ids=original_evidence_ids,
            challenging_evidence_ids=challenging_evidence_ids,
            workdir=self.workdir,
            settings=self.settings,
        )
        evidence = list(
            dict.fromkeys(
                (original_evidence_ids or []) + (challenging_evidence_ids or [])
            )
        )
        if created:
            for finding_id in list(dict.fromkeys(original_refs + challenging_refs)):
                ResearchDerivationStore.set_finding_status(
                    finding_id,
                    **self.owner,
                    status="disputed",
                    actor_id=actor_id,
                    reason=summary,
                    evidence_ids=evidence,
                    workdir=self.workdir,
                    settings=self.settings,
                )
        change = ResearchDerivationStore.append_change(
            **self.owner,
            research_id=research_id,
            change_type="conflict_opened",
            subject_type="research_conflict",
            subject_id=conflict.conflict_id,
            created_ids=[conflict.conflict_id],
            actor_id=actor_id,
            reason=summary,
            evidence_ids=evidence,
            idempotency_key=f"conflict-opened:{conflict.conflict_id}",
            workdir=self.workdir,
            settings=self.settings,
        )
        if created:
            self._refresh(research_id)
            self._project_findings(
                list(dict.fromkeys(original_refs + challenging_refs))
            )
        self._project_conflict_overlay(conflict)
        return conflict, change, created

    def challenge_finding(
        self,
        finding_id: str,
        *,
        statement: str,
        provenance: str,
        source_event_ids: list[str],
        actor_id: str,
        conflict_type: str = "direct_contradiction",
        summary: str = "",
        episode_id: str = "",
        scope: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
    ):
        original = ResearchDerivationStore.get_finding(
            finding_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if original is None:
            raise ValueError("finding does not belong to owner")
        challenger, _, _ = self.create_finding(
            original.research_id,
            statement=statement,
            provenance=provenance,
            source_event_ids=source_event_ids,
            actor_id=actor_id,
            episode_id=episode_id,
            scope=scope,
            evidence_ids=evidence_ids,
        )
        conflict_summary = summary.strip() or (
            f"新 Finding 挑战旧 Finding：{original.statement[:160]}"
        )
        conflict, _, _ = self.create_conflict(
            original.research_id,
            conflict_type=conflict_type,
            original_refs=[original.finding_id],
            challenging_refs=[challenger.finding_id],
            summary=conflict_summary,
            actor_id=actor_id,
            original_evidence_ids=original.evidence_ids,
            challenging_evidence_ids=challenger.evidence_ids,
        )
        change = ResearchDerivationStore.append_change(
            **self.owner,
            research_id=original.research_id,
            change_type="finding_challenged",
            subject_type="episode_finding",
            subject_id=original.finding_id,
            created_ids=[challenger.finding_id, conflict.conflict_id],
            actor_id=actor_id,
            reason=conflict_summary,
            evidence_ids=challenger.evidence_ids,
            idempotency_key=(
                f"finding-challenged:{original.finding_id}:{challenger.finding_id}"
            ),
            workdir=self.workdir,
            settings=self.settings,
        )
        return original, challenger, conflict, change

    def review_conflict(self, conflict_id: str, *, actor_id: str, reason: str = ""):
        conflict = ResearchDerivationStore.get_conflict(
            conflict_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if conflict is None:
            raise ValueError("conflict does not belong to owner")
        self._require_active_project(conflict.research_id)
        if conflict.status == "resolved":
            raise ValueError("resolved conflict must be reopened before review")
        updated = ResearchDerivationStore.update_conflict(
            conflict_id,
            **self.owner,
            status="under_review",
            workdir=self.workdir,
            settings=self.settings,
        )
        change = ResearchDerivationStore.append_change(
            **self.owner,
            research_id=conflict.research_id,
            change_type="conflict_updated",
            subject_type="research_conflict",
            subject_id=conflict_id,
            actor_id=actor_id,
            reason=reason or "conflict under review",
            idempotency_key=f"conflict-review:{conflict_id}:{updated.updated_at}",
            workdir=self.workdir,
            settings=self.settings,
        )
        self._refresh(conflict.research_id)
        self._project_conflict_overlay(updated)
        return updated, change

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        result: str,
        actor_id: str,
        reason: str,
        evidence_ids: list[str],
        scope: dict[str, Any] | None = None,
    ):
        conflict = ResearchDerivationStore.get_conflict(
            conflict_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if conflict is None:
            raise ValueError("conflict does not belong to owner")
        self._require_active_project(conflict.research_id)
        existing = ResearchDerivationStore.list_resolutions(
            conflict_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if conflict.status == "resolved" and existing:
            latest = existing[-1]
            if (
                latest.result == result
                and latest.reason == " ".join(str(reason or "").split())
                and latest.evidence_ids == list(dict.fromkeys(evidence_ids))
            ):
                return conflict, latest, None
            raise ValueError("resolved conflict must be reopened before a new resolution")
        resolution, _ = ResearchDerivationStore.append_resolution(
            conflict_id,
            **self.owner,
            result=result,
            actor_id=actor_id,
            reason=reason,
            evidence_ids=evidence_ids,
            scope=scope,
            workdir=self.workdir,
            settings=self.settings,
        )
        updated = ResearchDerivationStore.update_conflict(
            conflict_id,
            **self.owner,
            status="resolved",
            current_resolution_id=resolution.resolution_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        status_map = {
            "original_upheld": ("active", "retracted"),
            "challenger_upheld": ("superseded", "active"),
            "context_dependent": ("context_limited", "context_limited"),
            "inconclusive": ("disputed", "disputed"),
            "invalidated": ("retracted", "retracted"),
        }
        original_status, challenger_status = status_map[resolution.result]
        for finding_id in conflict.original_refs:
            ResearchDerivationStore.set_finding_status(
                finding_id,
                **self.owner,
                status=original_status,
                actor_id=actor_id,
                reason=reason,
                evidence_ids=evidence_ids,
                workdir=self.workdir,
                settings=self.settings,
            )
        for finding_id in conflict.challenging_refs:
            ResearchDerivationStore.set_finding_status(
                finding_id,
                **self.owner,
                status=challenger_status,
                actor_id=actor_id,
                reason=reason,
                evidence_ids=evidence_ids,
                workdir=self.workdir,
                settings=self.settings,
            )
        change = ResearchDerivationStore.append_change(
            **self.owner,
            research_id=conflict.research_id,
            change_type="conflict_resolved",
            subject_type="research_conflict",
            subject_id=conflict_id,
            created_ids=[resolution.resolution_id],
            actor_id=actor_id,
            reason=reason,
            evidence_ids=evidence_ids,
            idempotency_key=f"conflict-resolved:{resolution.resolution_id}",
            workdir=self.workdir,
            settings=self.settings,
        )
        self._refresh(conflict.research_id)
        self._project_findings(
            list(dict.fromkeys(conflict.original_refs + conflict.challenging_refs))
        )
        self._project_conflict_overlay(updated, resolution)
        return updated, resolution, change

    def reopen_conflict(
        self,
        conflict_id: str,
        *,
        actor_id: str,
        reason: str,
        evidence_ids: list[str],
    ):
        conflict = ResearchDerivationStore.get_conflict(
            conflict_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if conflict is None:
            raise ValueError("conflict does not belong to owner")
        self._require_active_project(conflict.research_id)
        if conflict.status != "resolved":
            raise ValueError("only resolved conflicts can be reopened")
        if not reason.strip() or not evidence_ids:
            raise ValueError("reopening requires reason and evidence references")
        updated = ResearchDerivationStore.update_conflict(
            conflict_id,
            **self.owner,
            status="open",
            workdir=self.workdir,
            settings=self.settings,
        )
        for finding_id in conflict.original_refs + conflict.challenging_refs:
            ResearchDerivationStore.set_finding_status(
                finding_id,
                **self.owner,
                status="disputed",
                actor_id=actor_id,
                reason=reason,
                evidence_ids=evidence_ids,
                workdir=self.workdir,
                settings=self.settings,
            )
        change = ResearchDerivationStore.append_change(
            **self.owner,
            research_id=conflict.research_id,
            change_type="conflict_reopened",
            subject_type="research_conflict",
            subject_id=conflict_id,
            actor_id=actor_id,
            reason=reason,
            evidence_ids=evidence_ids,
            idempotency_key=f"conflict-reopened:{conflict_id}:{uuid.uuid4()}",
            workdir=self.workdir,
            settings=self.settings,
        )
        self._refresh(conflict.research_id)
        self._project_findings(
            list(dict.fromkeys(conflict.original_refs + conflict.challenging_refs))
        )
        self._project_conflict_overlay(updated)
        return updated, change
