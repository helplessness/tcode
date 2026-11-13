"""Deterministic Topic Candidate discovery and user correction workflows."""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .attribution import ResearchAttributionEngine, _event_text, _parse_time
from .activities import _event_file_keys
from .attribution_models import AttributionCorrectionInput, ResearchEventLinkInput
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent, canonical_json, sha256_text
from .store import ResearchEventStore
from .topic_models import (
    TOPIC_DECISION_VERSION,
    ResearchTopicCandidate,
    TopicRunReport,
    research_candidate_digest,
)
from .topic_store import ResearchTopicStore


_ENGLISH_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_.+-]{2,}")
_CHINESE_CHUNK = re.compile(r"[\u4e00-\u9fff]{2,16}")
_GOAL_PATTERN = re.compile(
    r"(?i)(?:研究|实验|实现|分析|复现|开发|优化)(?:目标|目的)|"
    r"(?:目标|目的|课题)(?:是|为|：|:)|我(?:想|要|希望)(?:研究|实现|验证|复现)|"
    r"\b(?:research goal|goal is|objective|we aim|i want to (?:study|build|test))\b"
)
_GOAL_BOILERPLATE_PATTERN = re.compile(
    r"(?i)(?:我的?|我们)(?:研究|实验|实现|分析|复现)?(?:目标|目的)(?:是|为|：|:)?|"
    r"(?:研究|实验|实现|分析|复现)(?:目标|目的)(?:是|为|：|:)?|"
    r"\b(?:research goal|goal is|objective|we aim|i want to)\b"
)
_TITLE_LEADING_PATTERN = re.compile(
    r"(?i)^(?:请|麻烦)?(?:帮我|协助我)?(?:现在|接下来|继续)?(?:想要?|希望|需要|准备)?\s*"
)
_TITLE_ACTION_PATTERN = re.compile(
    r"^(复现|验证|分析|研究|实现|开发|优化|评估|比较|探索|构建|测试|解决)\s*(.+)$",
    re.IGNORECASE,
)
_DOCUMENT_OPERATION_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:这|该|上述|当前|刚才|刚上传|上传的)?\s*(?:一|这)?\s*(?:篇|份|个)?\s*"
    r"(?:论文|文献|文章|paper|pdf|document|文件)"
    r"|(?:本地|上传)?\s*(?:pdf|文件)"
    r").*?(?:解析|阅读|总结|概括|入库|保存|上传|导入|查重|收录|建档|提取)|"
    r"(?:解析|阅读|总结|概括|入库|保存|上传|导入|查重|收录|建档|提取).*?(?:"
    r"这|该|上述|当前|刚才|刚上传|上传的|论文|文献|文章|paper|pdf|document|文件)"
)
_PAPER_RESULT_TOOLS = {
    "paper_library_ingest_pdf",
    "paper_library_lookup_pdf",
    "paper_library_get_record",
}
_PAPER_TITLE_LINE_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(?:paper[ _-]?title|document[ _-]?title|title|标题)"
    r"(?:\*\*)?\s*[:：]\s*(.+?)\s*$"
)
_PAPER_SUCCESS_PATTERN = re.compile(
    r"(?i)(?:入库完成|解析完成|已保存到本地文献库|本地文献库(?:入库完成|已复用)|"
    r"doc_id\s*[:：]|paper library (?:ingest|record))"
)
_RAW_TITLE_PATTERN = re.compile(
    r"(?i)(?:我的?|我们)(?:研究|实验)?(?:目标|目的)|"
    r"^(?:请|帮我|我想|我希望|我需要|继续|session|turn[-_ ])"
)
_OPEN_ACTION_PATTERN = re.compile(
    r"(?i)(?:下一步|待解决|开放问题|后续需要|TODO|next step|open question)"
)
_CONTINUATION_PATTERN = re.compile(
    r"(?i)^\s*(?:好的[，,。\s]*)?(?:请)?(?:继续|接着|然后|下一步|按(?:刚才|上述|上一轮|上一步))"
    r"|\b(?:continue|proceed|next step|carry on)\b"
)
_ARTIFACT_EVENT_TYPES = {
    "file.uploaded",
    "file.changed",
    "artifact.created",
    "artifact.updated",
    "document.created",
}
_TOOL_EVENT_PREFIXES = ("tool.", "mcp.", "workflow.", "agent.")
_IGNORED_EVENT_TYPES = {
    "session.created",
    "session.archived",
    "session.resumed",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "message.compacted",
}
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "into",
    "using",
    "please",
    "research",
    "paper",
    "analysis",
    "result",
    "results",
    "研究",
    "论文",
    "分析",
    "结果",
    "内容",
    "这个",
    "一个",
    "我们",
    "可以",
    "需要",
    "进行",
    "验证",
    "复现",
    "实现",
    "开发",
    "优化",
}


@dataclass
class _TopicEvent:
    event: ResearchEvent
    tokens: set[str]
    explicit_goal: bool
    open_action: bool
    artifact: bool
    tool: bool
    candidate_id: str = ""
    has_membership: bool = False


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _tokens(event: ResearchEvent) -> set[str]:
    # Session/message/call identifiers are routing metadata, not topic semantics.
    # File references are still useful for connecting events, but are handled by
    # `_event_file_keys` below rather than becoming candidate-title keywords.
    if _is_tool(event):
        parts = [
            event.payload.get("output_preview"),
            event.payload.get("error_preview"),
        ]
    elif _is_artifact(event):
        parts = [
            event.payload.get("content_preview"),
            event.payload.get("paper_title"),
            event.payload.get("document_title"),
            event.payload.get("title"),
            event.payload.get("filename"),
        ]
    else:
        parts = [
            event.payload.get("content_preview"),
            event.payload.get("output_preview"),
        ]
    text = "\n".join(
        str(part).strip() for part in parts if isinstance(part, str) and part.strip()
    ).lower()[:6000]
    text = _GOAL_BOILERPLATE_PATTERN.sub(" ", text)
    values = {
        match.group(0).strip("._+-")
        for match in _ENGLISH_TOKEN.finditer(text)
    }
    for match in _CHINESE_CHUNK.finditer(text):
        chunk = match.group(0)
        values.add(chunk)
        if len(chunk) >= 4:
            values.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return {
        value
        for value in values
        if len(value) >= 2 and value not in _STOPWORDS and not value.isdigit()
    }


def _is_artifact(event: ResearchEvent) -> bool:
    return event.event_type in _ARTIFACT_EVENT_TYPES or event.source_kind in {
        "artifact",
        "file_upload",
        "file_change",
        "document",
    }


def _is_tool(event: ResearchEvent) -> bool:
    return event.event_type.startswith(_TOOL_EVENT_PREFIXES) or event.source_kind in {
        "tool_call",
        "mcp_tool",
        "workflow",
        "subagent",
    }


def is_topic_artifact_event(event: ResearchEvent) -> bool:
    """Public topic-count predicate shared by candidate and project views."""

    return _is_artifact(event)


def is_topic_tool_event(event: ResearchEvent) -> bool:
    """Public topic-count predicate shared by candidate and project views."""

    return _is_tool(event)


def _meaningful(event: ResearchEvent) -> bool:
    if event.event_type in _IGNORED_EVENT_TYPES:
        return False
    return bool(_event_text(event).strip() or _is_artifact(event) or _is_tool(event))


def _is_continuation_message(event: ResearchEvent) -> bool:
    """判断用户消息是否明确承接同一会话中的上一项研究工作。"""

    return bool(
        event.actor_type == "user"
        and event.source_kind == "session_message"
        and _CONTINUATION_PATTERN.search(_event_text(event))
    )


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _candidate_id(owner: dict[str, str], event_id: str) -> str:
    digest = sha256_text(canonical_json({**owner, "first_event_id": event_id}))
    return f"topic-{digest.split(':', 1)[-1][:20]}"


def _clean_title(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" \t\r\n-—:：")
    return value[:80]


def _clean_entity_title(text: Any) -> str:
    lines = str(text or "").splitlines()
    value = lines[0].strip() if lines else ""
    value = re.sub(r"^[`*_'\"“”]+|[`*_'\"“”]+$", "", value).strip()
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n-—:：")
    if not value or value.lower() in {"na", "n/a", "none", "null", "unknown"}:
        return ""
    if value.lower() in {"paper library ingest", "paper library lookup", "paper library record"}:
        return ""
    if re.fullmatch(r"(?:paper-)?[a-f0-9]{12,}", value, re.IGNORECASE):
        return ""
    if re.search(r"[/\\]", value) or value.lower().endswith((".pdf", ".json", ".md")):
        return ""
    return value[:160].rstrip(" ·-—:：")


def _title_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("paper_title", "document_title", "title"):
        title = _clean_entity_title(value.get(key))
        if title:
            return title
    for key in ("record", "metadata", "document", "paper"):
        title = _title_from_mapping(value.get(key))
        if title:
            return title
    return ""


def _title_from_result_text(text: Any) -> str:
    match = _PAPER_TITLE_LINE_PATTERN.search(str(text or ""))
    return _clean_entity_title(match.group(1)) if match else ""


def _paper_title(items: list[_TopicEvent]) -> str:
    """Return one canonical paper title, preferring complete trusted evidence."""

    titles: list[str] = []
    trusted_traces: set[str] = set()
    for item in items:
        event = item.event
        tool_name = str(event.payload.get("tool_name") or "").strip().lower()
        direct_title = _clean_entity_title(
            event.payload.get("paper_title") or event.payload.get("document_title")
        )
        if direct_title:
            titles.append(direct_title)
        if item.artifact:
            artifact_title = _title_from_mapping(event.payload)
            if artifact_title:
                titles.append(artifact_title)
        if tool_name not in _PAPER_RESULT_TOOLS or event.event_type not in {
            "tool.completed",
            "mcp.completed",
        }:
            continue
        trusted_traces.add(event.trace_id)
        tool_title = (
            direct_title
            or _title_from_mapping(event.payload.get("metadata"))
            or _title_from_result_text(event.payload.get("output_preview"))
        )
        if tool_title:
            titles.append(tool_title)

    # A successful paper tool may be followed by a user-facing Agent response
    # containing a less-truncated title. It is accepted only inside that same
    # trusted trace, so arbitrary chat text cannot rename a research project.
    for item in items:
        event = item.event
        if (
            event.actor_type != "agent"
            or event.source_kind != "session_message"
            or event.trace_id not in trusted_traces
        ):
            continue
        text = str(event.payload.get("content_preview") or "")
        if not _PAPER_SUCCESS_PATTERN.search(text):
            continue
        title = _title_from_result_text(text)
        if title:
            titles.append(title)

    distinct: list[str] = []
    for title in sorted(set(titles), key=len, reverse=True):
        lowered = title.casefold()
        if any(lowered in existing.casefold() or existing.casefold() in lowered for existing in distinct):
            continue
        distinct.append(title)
    return distinct[0] if len(distinct) == 1 else ""


def _looks_like_document_operation(text: str) -> bool:
    return bool(_DOCUMENT_OPERATION_PATTERN.search(str(text or "")))


def _summarize_title(text: str, keywords: list[str]) -> str:
    lines = str(text or "").splitlines()
    value = lines[0] if lines else ""
    value = re.split(r"[。！？!?；;]|(?:，|,)\s*(?:并且|然后|下一步|后续)", value, maxsplit=1)[0]
    value = _GOAL_BOILERPLATE_PATTERN.sub(" ", value)
    value = _TITLE_LEADING_PATTERN.sub("", value)
    value = re.sub(r"^(?:课题|主题)(?:是|为|：|:)?\s*", "", value)
    value = _clean_title(value)
    match = _TITLE_ACTION_PATTERN.match(value)
    if match:
        action, subject = match.groups()
        subject = _clean_title(subject)
        if subject:
            value = f"{subject} · {action}"
    value = re.sub(r"\s*[-—:：]\s*$", "", value).strip()
    if not value and keywords:
        value = " · ".join(keywords[:3])
    if not value:
        return "未命名研究课题"
    return value[:48].rstrip(" ·-—:：")


def _looks_unsummarized_title(title: str) -> bool:
    value = _clean_title(title)
    return len(value) > 48 or bool(_RAW_TITLE_PATTERN.search(value))


def _suggest_title(items: list[_TopicEvent], keywords: list[str]) -> str:
    canonical_paper_title = _paper_title(items)
    # Agent summaries can repeat goal markers; the user's own goal wins even
    # when candidate events are returned newest first.
    for item in sorted(items, key=lambda item: item.event.actor_type != "user"):
        text = _event_text(item.event).splitlines()[0] if _event_text(item.event) else ""
        if (
            item.explicit_goal
            and _clean_title(text)
            and not _looks_like_document_operation(text)
        ):
            return _summarize_title(text, keywords)

    # A user's own request is the safest fallback for a slowly emerging topic.
    # Tool display names (for example ``paper library lookup``) describe an
    # implementation step and must never become the research-topic name.
    operational_request = ""
    for item in items:
        event = item.event
        text = _event_text(event).splitlines()[0] if _event_text(event) else ""
        if (
            event.actor_type == "user"
            and event.source_kind == "session_message"
            and _clean_title(text)
            and not text.lstrip().startswith("# Task Contract")
        ):
            if _looks_like_document_operation(text):
                operational_request = operational_request or text
                continue
            return _summarize_title(text, keywords)

    # “解析这篇论文并入库”是动作，不是课题。解析前可暂时使用它，
    # 一旦得到唯一的规范论文标题，就自动升级为研究实体名称。
    if canonical_paper_title:
        return canonical_paper_title
    if operational_request:
        return _summarize_title(operational_request, keywords)

    for item in items:
        event = item.event
        if not item.artifact:
            continue
        title = (
            event.payload.get("paper_title")
            or event.payload.get("document_title")
            or event.payload.get("filename")
            or event.payload.get("title")
        )
        if isinstance(title, str) and _clean_title(title):
            return _summarize_title(title, keywords)
    if keywords:
        return _summarize_title("", keywords)
    return "未命名研究课题"


def _suggest_title_source(items: list[_TopicEvent], title: str) -> str:
    paper_title = _paper_title(items)
    return "paper_metadata" if paper_title and title == paper_title else "auto_summary"


def _rank_keywords(items: list[_TopicEvent]) -> list[str]:
    counts: Counter[str] = Counter(token for item in items for token in item.tokens)

    def noisy_single_cjk_bigram(token: str) -> bool:
        return bool(re.fullmatch(r"[\u4e00-\u9fff]{2}", token)) and counts[token] < 2

    return [
        token
        for token, _ in sorted(
            (
                (token, count)
                for token, count in counts.items()
                if not noisy_single_cjk_bigram(token)
            ),
            key=lambda pair: (-pair[1], -len(pair[0]), pair[0]),
        )
    ][:16]


def _stability(items: list[_TopicEvent], minimum_events: int) -> float:
    event_count = len(items)
    sessions = {item.event.session_id for item in items if item.event.session_id}
    artifact_count = sum(1 for item in items if item.artifact)
    tool_count = sum(1 for item in items if item.tool)
    explicit_goal = any(item.explicit_goal for item in items)
    times = [_parse_time(item.event.occurred_at) for item in items]
    duration_days = (
        max(0.0, (max(times) - min(times)).total_seconds() / 86400) if times else 0.0
    )
    score = 0.0
    score += 0.25 * min(1.0, event_count / max(1, minimum_events * 2))
    score += 0.20 * min(1.0, len(sessions) / 3)
    score += 0.15 * min(1.0, artifact_count / 2)
    score += 0.10 * min(1.0, tool_count / 3)
    score += 0.20 if explicit_goal else 0.0
    score += 0.10 * min(1.0, duration_days / 7)
    return round(min(1.0, score), 4)


class ResearchTopicEngine:
    """Discover explainable candidates without model calls or prompt injection."""

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
        self.workdir = workdir
        self.settings = settings or load_research_settings()

    def _write_link(
        self,
        item: _TopicEvent,
        candidate: ResearchTopicCandidate,
        *,
        run_id: str,
        confidence: float,
        reason_code: str,
    ) -> bool:
        membership, changed = ResearchTopicStore.append_membership(
            **self.owner,
            event_id=item.event.event_id,
            candidate_id=candidate.candidate_id,
            state="active",
            decision_source="topic_rule",
            confidence=confidence,
            reason_code=reason_code,
            workdir=self.workdir,
            settings=self.settings,
        )
        ResearchEventStore.append_attribution_link(
            ResearchEventLinkInput(
                **self.owner,
                event_id=item.event.event_id,
                candidate_id=candidate.candidate_id,
                research_id=candidate.confirmed_research_id,
                state="included",
                relevance="research",
                decision_source="topic",
                confidence=membership.confidence,
                reason_code="topic_candidate_membership",
                reason_detail={
                    "membership_id": membership.membership_id,
                    "topic_reason": reason_code,
                },
                run_id=run_id,
                decision_version=TOPIC_DECISION_VERSION,
            ).build(),
            workdir=self.workdir,
            settings=self.settings,
        )
        return changed

    def _defer_for_candidate_limit(self, item: _TopicEvent, *, run_id: str) -> None:
        ResearchEventStore.append_attribution_link(
            ResearchEventLinkInput(
                **self.owner,
                event_id=item.event.event_id,
                state="pending",
                relevance="research",
                decision_source="topic",
                confidence=0.9,
                reason_code="topic_candidate_limit",
                reason_detail={
                    "max_candidates": self.settings.topic_max_candidates,
                },
                run_id=run_id,
                decision_version=TOPIC_DECISION_VERSION,
            ).build(),
            workdir=self.workdir,
            settings=self.settings,
        )

    def _refresh_candidate(self, candidate_id: str) -> ResearchTopicCandidate:
        candidate = ResearchTopicStore.get_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if candidate is None:
            raise ValueError("candidate does not belong to owner")
        events = ResearchTopicStore.list_candidate_events(
            candidate_id=candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            limit=5000,
        )
        items = [
            _TopicEvent(
                event=event,
                tokens=_tokens(event),
                explicit_goal=bool(_GOAL_PATTERN.search(_event_text(event))),
                open_action=bool(_OPEN_ACTION_PATTERN.search(_event_text(event))),
                artifact=_is_artifact(event),
                tool=_is_tool(event),
                candidate_id=candidate_id,
                has_membership=True,
            )
            for event in events
        ]
        keywords = _rank_keywords(items)
        summarized_title = _suggest_title(items, keywords)
        summarized_title_source = _suggest_title_source(items, summarized_title)
        explicit_goal_item = next((item for item in items if item.explicit_goal), None)
        goal_summary = (
            _clean_title(_event_text(explicit_goal_item.event))
            if explicit_goal_item is not None
            else candidate.goal_summary
        )
        should_refresh_title = candidate.title_source != "user" or _looks_unsummarized_title(
            candidate.suggested_title
        )
        score = _stability(items, self.settings.topic_minimum_meaningful_events)
        next_state = candidate.state
        if candidate.state in {"emerging", "stable"}:
            next_state = (
                "stable"
                if score >= self.settings.topic_stable_score_threshold
                else "emerging"
            )
        first_seen = min((event.occurred_at for event in events), default=candidate.first_seen_at)
        last_seen = max((event.occurred_at for event in events), default=candidate.last_seen_at)
        updates: dict[str, Any] = {
            "keywords_json": keywords,
            "goal_summary": goal_summary,
            "supporting_event_count": len(events),
            "session_count": len({event.session_id for event in events if event.session_id}),
            "artifact_count": sum(1 for item in items if item.artifact),
            "tool_count": sum(1 for item in items if item.tool),
            "explicit_goal": any(item.explicit_goal for item in items),
            "stability_score": score,
            "state": next_state,
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "decision_version": TOPIC_DECISION_VERSION,
        }
        if should_refresh_title and (
            summarized_title != candidate.suggested_title
            or summarized_title_source != candidate.title_source
        ):
            updates.update(
                suggested_title=summarized_title,
                title_source=summarized_title_source,
            )
            if candidate.confirmed_research_id:
                project = ResearchTopicStore.get_project(
                    candidate.confirmed_research_id,
                    **self.owner,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                if project is not None and (
                    project.title == candidate.suggested_title
                    or _looks_unsummarized_title(project.title)
                ):
                    ResearchTopicStore.update_project_title(
                        project.research_id,
                        **self.owner,
                        title=summarized_title,
                        workdir=self.workdir,
                        settings=self.settings,
                    )
            ResearchTopicStore.record_operation(
                **self.owner,
                action="auto_summarize_title",
                actor_id="topic_title_synthesizer",
                source_candidate_ids=[candidate_id],
                target_candidate_ids=[candidate_id],
                before={"title": candidate.suggested_title},
                after={"title": summarized_title, "title_source": summarized_title_source},
                reason="candidate evidence refreshed",
                idempotency_key=f"auto-title:{candidate_id}:{sha256_text(summarized_title)}",
                workdir=self.workdir,
                settings=self.settings,
            )
        return ResearchTopicStore.update_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            **updates,
        )

    def run(
        self,
        *,
        trigger_kind: str = "manual",
        limit: int = 1000,
    ) -> TopicRunReport:
        if not self.settings.topics_active:
            raise RuntimeError("Topic Candidate Preview is not active")
        run_id = ResearchTopicStore.start_run(
            **self.owner,
            trigger_kind=trigger_kind,
            workdir=self.workdir,
            settings=self.settings,
        )
        report = TopicRunReport(run_id=run_id)
        touched_candidates: set[str] = set()
        try:
            open_candidates = ResearchTopicStore.list_candidates(
                **self.owner,
                states=["emerging", "stable"],
                limit=1000,
                workdir=self.workdir,
                settings=self.settings,
            )
            available_candidate_slots = max(
                0,
                self.settings.topic_max_candidates - len(open_candidates),
            )
            bounded_limit = max(1, min(int(limit or 1000), 5000))
            events = ResearchEventStore.list_events_for_attribution(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                limit=bounded_limit,
            )
            links = ResearchEventStore.list_attribution_links(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                active_only=True,
                limit=5000,
            )
            link_by_event = {link.event_id: link for link in links}
            memberships = ResearchTopicStore.list_memberships(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                active_only=True,
                limit=5000,
            )
            membership_by_event = {item.event_id: item for item in memberships}
            items: list[_TopicEvent] = []
            for event in events:
                link = link_by_event.get(event.event_id)
                if link is None or link.relevance != "research":
                    continue
                if link.state not in {"pending", "included"}:
                    continue
                if link.decision_source == "user" and not (
                    link.candidate_id or link.research_id
                ):
                    continue
                if link.research_id and not link.candidate_id:
                    ResearchTopicStore.ensure_project(
                        **self.owner,
                        research_id=link.research_id,
                        title=_suggest_title(
                            [
                                _TopicEvent(
                                    event=event,
                                    tokens=_tokens(event),
                                    explicit_goal=True,
                                    open_action=False,
                                    artifact=_is_artifact(event),
                                    tool=_is_tool(event),
                                )
                            ],
                            [],
                        ),
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    continue
                if not _meaningful(event):
                    continue
                membership = membership_by_event.get(event.event_id)
                candidate_id = str(
                    getattr(membership, "candidate_id", "") or link.candidate_id or ""
                )
                item = _TopicEvent(
                    event=event,
                    tokens=_tokens(event),
                    explicit_goal=bool(_GOAL_PATTERN.search(_event_text(event))),
                    open_action=bool(_OPEN_ACTION_PATTERN.search(_event_text(event))),
                    artifact=_is_artifact(event),
                    tool=_is_tool(event),
                    candidate_id=candidate_id,
                    has_membership=membership is not None,
                )
                if candidate_id and ResearchTopicStore.get_candidate(
                    candidate_id,
                    **self.owner,
                    workdir=self.workdir,
                    settings=self.settings,
                ) is None:
                    if available_candidate_slots <= 0:
                        self._defer_for_candidate_limit(item, run_id=run_id)
                        continue
                    item_group = [item]
                    item_title = _suggest_title(item_group, sorted(item.tokens))
                    _, created = ResearchTopicStore.create_candidate(
                        **self.owner,
                        candidate_id=candidate_id,
                        suggested_title=item_title,
                        goal_summary=_clean_title(_event_text(event)),
                        keywords=sorted(item.tokens)[:16],
                        first_seen_at=event.occurred_at,
                        last_seen_at=event.occurred_at,
                        title_source=_suggest_title_source(item_group, item_title),
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    if created:
                        available_candidate_slots -= 1
                items.append(item)

            report.event_count = len(items)
            union = _UnionFind(len(items))
            trace_first: dict[str, int] = {}
            file_first: dict[str, int] = {}
            token_indices: dict[str, list[int]] = defaultdict(list)
            for index, item in enumerate(items):
                if item.event.trace_id:
                    previous = trace_first.setdefault(item.event.trace_id, index)
                    union.union(previous, index)
                # Upload, user message and tool calls often spell the same file as
                # host path, /workspace/upload path, or SHA-256. Normalize those
                # references so the File event follows the actual research action.
                for file_key in _event_file_keys(item.event):
                    previous = file_first.setdefault(file_key, index)
                    union.union(previous, index)
                for token in item.tokens:
                    token_indices[token].append(index)
            user_messages_by_session: dict[str, list[int]] = defaultdict(list)
            for index, item in enumerate(items):
                if item.event.actor_type != "user" or item.event.source_kind != "session_message":
                    continue
                user_messages_by_session[item.event.session_id].append(index)
            for indices in user_messages_by_session.values():
                ordered = sorted(
                    indices,
                    key=lambda index: (
                        _parse_time(items[index].event.occurred_at),
                        items[index].event.ingested_at,
                    ),
                )
                for previous, current in zip(ordered, ordered[1:]):
                    if _is_continuation_message(items[current].event):
                        union.union(previous, current)
            candidate_pairs: set[tuple[int, int]] = set()
            for indices in token_indices.values():
                bounded = indices[:200]
                for offset, left in enumerate(bounded):
                    for right in bounded[offset + 1 :]:
                        candidate_pairs.add((left, right))
            for left, right in candidate_pairs:
                if _overlap(items[left].tokens, items[right].tokens) >= (
                    self.settings.topic_keyword_overlap_threshold
                ):
                    union.union(left, right)
            grouped: dict[int, list[_TopicEvent]] = defaultdict(list)
            for index, item in enumerate(items):
                grouped[union.find(index)].append(item)
            report.group_count = len(grouped)

            for group in grouped.values():
                candidate_ids = sorted(
                    {item.candidate_id for item in group if item.candidate_id}
                )
                keywords = _rank_keywords(group)
                if len(candidate_ids) > 1:
                    if ResearchTopicStore.record_merge_suggestion(
                        **self.owner,
                        candidate_ids=candidate_ids,
                        score=max(
                            (_overlap(group[0].tokens, item.tokens) for item in group[1:]),
                            default=0.0,
                        ),
                        reason_code="overlapping_candidate_cluster",
                        workdir=self.workdir,
                        settings=self.settings,
                    ):
                        report.merge_suggestion_count += 1
                    continue

                sessions = {item.event.session_id for item in group if item.event.session_id}
                artifact_count = sum(1 for item in group if item.artifact)
                tool_count = sum(1 for item in group if item.tool)
                explicit_goal = any(item.explicit_goal for item in group)
                has_user_request = any(
                    item.event.actor_type == "user"
                    and item.event.source_kind == "session_message"
                    for item in group
                )
                repeated_artifact = any(
                    count >= 2
                    for ref, count in Counter(
                        item.event.source_ref
                        for item in group
                        if item.artifact and item.event.source_ref
                    ).items()
                )
                qualifies = bool(
                    explicit_goal
                    or (
                        len(group) >= self.settings.topic_minimum_meaningful_events
                        and (artifact_count > 0 or tool_count > 0)
                    )
                    or (
                        len(sessions) >= self.settings.topic_cross_session_signal
                        and len(group) >= 2
                    )
                    or repeated_artifact
                    or (len(group) >= 2 and any(item.open_action for item in group))
                )
                if not candidate_ids and not qualifies:
                    continue
                if not candidate_ids and not has_user_request and artifact_count == 0:
                    # 纯工具错误、重试和运行时诊断属于执行噪声，不能脱离用户请求
                    # 自行升级为研究课题；真实产物事件仍允许形成候选。
                    continue
                if candidate_ids:
                    candidate = ResearchTopicStore.get_candidate(
                        candidate_ids[0],
                        **self.owner,
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    if candidate is None or candidate.state in {
                        "merged",
                        "split",
                        "dismissed",
                    }:
                        continue
                    reason_code = "existing_candidate_cluster"
                else:
                    if available_candidate_slots <= 0:
                        for item in group:
                            self._defer_for_candidate_limit(item, run_id=run_id)
                        continue
                    earliest = min(
                        group,
                        key=lambda item: (item.event.occurred_at, item.event.ingested_at),
                    )
                    goal_item = next((item for item in group if item.explicit_goal), None)
                    group_title = _suggest_title(group, keywords)
                    candidate, created = ResearchTopicStore.create_candidate(
                        **self.owner,
                        candidate_id=_candidate_id(self.owner, earliest.event.event_id),
                        suggested_title=group_title,
                        goal_summary=(
                            _clean_title(_event_text(goal_item.event))
                            if goal_item is not None
                            else ""
                        ),
                        keywords=keywords,
                        first_seen_at=min(item.event.occurred_at for item in group),
                        last_seen_at=max(item.event.occurred_at for item in group),
                        title_source=_suggest_title_source(group, group_title),
                        workdir=self.workdir,
                        settings=self.settings,
                    )
                    if created:
                        report.candidate_created_count += 1
                        available_candidate_slots -= 1
                    reason_code = (
                        "explicit_goal"
                        if explicit_goal
                        else "cross_session_signal"
                        if len(sessions) >= self.settings.topic_cross_session_signal
                        else "meaningful_event_cluster"
                    )
                confidence = 0.98 if explicit_goal else 0.92 if qualifies else 0.90
                for item in group:
                    if item.candidate_id and item.candidate_id != candidate.candidate_id:
                        continue
                    if item.has_membership and item.candidate_id == candidate.candidate_id:
                        continue
                    if self._write_link(
                        item,
                        candidate,
                        run_id=run_id,
                        confidence=confidence,
                        reason_code=reason_code,
                    ):
                        report.membership_changed_count += 1
                touched_candidates.add(candidate.candidate_id)

            for candidate_id in sorted(touched_candidates):
                self._refresh_candidate(candidate_id)
        except Exception as exc:
            report.status = "failed"
            report.errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
        ResearchTopicStore.finish_run(
            run_id,
            report.to_dict(),
            workdir=self.workdir,
            settings=self.settings,
        )
        return report


class ResearchTopicManager:
    """User-owned Candidate operations; every mutation is append-audited."""

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

    def _candidate(self, candidate_id: str) -> ResearchTopicCandidate:
        candidate = ResearchTopicStore.get_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if candidate is None:
            raise ValueError("candidate does not belong to owner")
        return candidate

    def _refresh_derivations(
        self,
        research_ids: list[str],
        *,
        trigger_kind: str,
        force_reason: str,
        force_open: bool = False,
    ) -> None:
        if not (self.settings.episodes_active and self.settings.episode_auto_build):
            return
        try:
            from .episodes import ResearchDerivationEngine

            engine = ResearchDerivationEngine(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            for research_id in sorted(set(item for item in research_ids if item)):
                engine.run_project(
                    research_id,
                    trigger_kind=trigger_kind,
                    force_open=force_open,
                    force_reason=force_reason,
                )
        except Exception:
            # Research derivations are fail-open and must not break corrections.
            pass

    def sync_attribution_correction(
        self,
        event_id: str,
        *,
        action: str,
        actor_id: str,
        candidate_id: str = "",
        reason: str = "",
        correction_id: str = "",
    ) -> None:
        """Keep Candidate membership consistent with the generic correction API."""

        normalized_action = str(action or "").strip().lower()
        existing = ResearchTopicStore.list_memberships(
            **self.owner,
            event_id=event_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        touched = {item.candidate_id for item in existing}
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action=f"attribution_{normalized_action}",
            actor_id=actor_id,
            source_candidate_ids=sorted(touched),
            target_candidate_ids=[candidate_id] if candidate_id else [],
            event_ids=[event_id],
            reason=reason,
            idempotency_key=(
                f"topic-sync:{correction_id}" if correction_id else ""
            ),
            workdir=self.workdir,
            settings=self.settings,
        )
        if normalized_action == "include" and candidate_id:
            self._candidate(candidate_id)
            ResearchTopicStore.append_membership(
                **self.owner,
                event_id=event_id,
                candidate_id=candidate_id,
                state="active",
                decision_source="user",
                confidence=1.0,
                reason_code="attribution_correction",
                operation_id=operation["operation_id"],
                workdir=self.workdir,
                settings=self.settings,
            )
            touched.add(candidate_id)
        elif normalized_action in {"exclude", "pending", "restore_frozen"}:
            for membership in existing:
                ResearchTopicStore.append_membership(
                    **self.owner,
                    event_id=event_id,
                    candidate_id=membership.candidate_id,
                    state="removed",
                    decision_source="user",
                    confidence=1.0,
                    reason_code=f"attribution_{normalized_action}",
                    operation_id=operation["operation_id"],
                    workdir=self.workdir,
                    settings=self.settings,
                )
        refresher = ResearchTopicEngine(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        for touched_id in sorted(touched):
            refresher._refresh_candidate(touched_id)
        research_ids = [
            candidate.confirmed_research_id
            for candidate in (
                ResearchTopicStore.get_candidate(
                    candidate_id,
                    **self.owner,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                for candidate_id in touched
            )
            if candidate is not None and candidate.confirmed_research_id
        ]
        self._refresh_derivations(
            research_ids,
            trigger_kind="attribution_correction",
            force_reason="user_correction",
            force_open=False,
        )

    def rename(self, candidate_id: str, *, title: str, actor_id: str, reason: str = ""):
        candidate = self._candidate(candidate_id)
        normalized = _clean_title(title)
        if not normalized:
            raise ValueError("candidate title is required")
        updated = ResearchTopicStore.update_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            suggested_title=normalized,
            title_source="user",
        )
        project = None
        if candidate.confirmed_research_id:
            project = ResearchTopicStore.update_project_title(
                candidate.confirmed_research_id,
                **self.owner,
                title=normalized,
                workdir=self.workdir,
                settings=self.settings,
            )
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action="rename_candidate",
            actor_id=actor_id,
            source_candidate_ids=[candidate_id],
            target_candidate_ids=[candidate_id],
            before={"title": candidate.suggested_title},
            after={
                "title": updated.suggested_title,
                "research_id": project.research_id if project else "",
            },
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        self._refresh_derivations(
            [project.research_id] if project else [],
            trigger_kind="project_renamed",
            force_reason="project_renamed",
            force_open=False,
        )
        return updated, operation

    def assign_event(
        self,
        event_id: str,
        *,
        candidate_id: str,
        actor_id: str,
        reason: str = "",
        action: str = "include_event",
    ):
        candidate = self._candidate(candidate_id)
        if candidate.state in {"merged", "split", "dismissed"}:
            raise ValueError("cannot assign events to an inactive candidate")
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action=action,
            actor_id=actor_id,
            target_candidate_ids=[candidate_id],
            event_ids=[event_id],
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        membership, _ = ResearchTopicStore.append_membership(
            **self.owner,
            event_id=event_id,
            candidate_id=candidate_id,
            state="active",
            decision_source="user",
            confidence=1.0,
            reason_code=action,
            operation_id=operation["operation_id"],
            workdir=self.workdir,
            settings=self.settings,
        )
        _, report, link = ResearchAttributionEngine(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        ).correct(
            AttributionCorrectionInput(
                **self.owner,
                event_id=event_id,
                action="include",
                actor_id=actor_id,
                candidate_id=candidate_id,
                research_id=candidate.confirmed_research_id,
                reason=reason,
                idempotency_key=f"{operation['operation_id']}:{event_id}",
            )
        )
        ResearchTopicEngine(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )._refresh_candidate(candidate_id)
        self._refresh_derivations(
            [candidate.confirmed_research_id],
            trigger_kind="event_assignment",
            force_reason="user_correction",
            force_open=False,
        )
        return membership, link, report, operation

    def exclude_event(self, event_id: str, *, actor_id: str, reason: str = ""):
        memberships = ResearchTopicStore.list_memberships(
            **self.owner,
            event_id=event_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        source_ids = [item.candidate_id for item in memberships]
        research_ids = [
            candidate.confirmed_research_id
            for candidate in (
                ResearchTopicStore.get_candidate(
                    candidate_id,
                    **self.owner,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                for candidate_id in source_ids
            )
            if candidate is not None and candidate.confirmed_research_id
        ]
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action="exclude_event",
            actor_id=actor_id,
            source_candidate_ids=source_ids,
            event_ids=[event_id],
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        for membership in memberships:
            ResearchTopicStore.append_membership(
                **self.owner,
                event_id=event_id,
                candidate_id=membership.candidate_id,
                state="removed",
                decision_source="user",
                confidence=1.0,
                reason_code="exclude_event",
                operation_id=operation["operation_id"],
                workdir=self.workdir,
                settings=self.settings,
            )
        stored, report, link = ResearchAttributionEngine(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        ).correct(
            AttributionCorrectionInput(
                **self.owner,
                event_id=event_id,
                action="exclude",
                actor_id=actor_id,
                reason=reason,
                idempotency_key=f"{operation['operation_id']}:{event_id}",
            )
        )
        for candidate_id in source_ids:
            ResearchTopicEngine(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )._refresh_candidate(candidate_id)
        self._refresh_derivations(
            research_ids,
            trigger_kind="event_exclusion",
            force_reason="user_correction",
            force_open=False,
        )
        return stored, link, report, operation

    def confirm(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        title: str = "",
        description: str = "",
        reason: str = "",
        expected_candidate_digest: str = "",
    ):
        candidate = self._candidate(candidate_id)
        expected_digest = str(expected_candidate_digest or "").strip()
        if expected_digest and research_candidate_digest(candidate) != expected_digest:
            raise ValueError(
                "candidate changed since preview; refresh the candidate and confirm again"
            )
        if candidate.state in {"merged", "split", "dismissed"}:
            raise ValueError("cannot confirm an inactive candidate")
        confirmation_key = f"confirm-project:{candidate.candidate_id}"
        if candidate.state == "confirmed":
            project = ResearchTopicStore.get_project(
                candidate.confirmed_research_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if project is None:
                raise ValueError("confirmed candidate project is missing")
            operation = ResearchTopicStore.record_operation(
                **self.owner,
                action="confirm_project",
                actor_id=actor_id,
                source_candidate_ids=[candidate_id],
                target_candidate_ids=[candidate_id],
                before={"state": "confirmed"},
                after={"state": "confirmed", "research_id": project.research_id},
                reason=reason,
                idempotency_key=confirmation_key,
                workdir=self.workdir,
                settings=self.settings,
            )
            return candidate, project, operation
        normalized_title = _clean_title(title)
        project, _ = ResearchTopicStore.ensure_project(
            **self.owner,
            title=normalized_title or candidate.suggested_title,
            description=description,
            origin_candidate_ids=[candidate_id],
            workdir=self.workdir,
            settings=self.settings,
        )
        updated = ResearchTopicStore.update_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            state="confirmed",
            confirmed_research_id=project.research_id,
            suggested_title=project.title,
            title_source="user" if normalized_title else candidate.title_source,
        )
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action="confirm_project",
            actor_id=actor_id,
            source_candidate_ids=[candidate_id],
            target_candidate_ids=[candidate_id],
            before={"state": candidate.state},
            after={"state": "confirmed", "research_id": project.research_id},
            reason=reason,
            idempotency_key=confirmation_key,
            workdir=self.workdir,
            settings=self.settings,
        )
        memberships = ResearchTopicStore.list_memberships(
            **self.owner,
            candidate_id=candidate_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        for membership in memberships:
            ResearchAttributionEngine(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).correct(
                AttributionCorrectionInput(
                    **self.owner,
                    event_id=membership.event_id,
                    action="include",
                    actor_id=actor_id,
                    candidate_id=candidate_id,
                    research_id=project.research_id,
                    reason="candidate confirmed",
                    idempotency_key=f"{operation['operation_id']}:{membership.event_id}",
                )
            )
        self._refresh_derivations(
            [project.research_id],
            trigger_kind="project_confirmed",
            force_reason="project_confirmed",
            force_open=True,
        )
        return updated, project, operation

    def dismiss(self, candidate_id: str, *, actor_id: str, reason: str = ""):
        candidate = self._candidate(candidate_id)
        if candidate.state == "confirmed":
            raise ValueError("confirmed candidate must be archived through its project")
        if candidate.state in {"merged", "split", "dismissed"}:
            raise ValueError("cannot dismiss an inactive candidate")
        memberships = ResearchTopicStore.list_memberships(
            **self.owner,
            candidate_id=candidate_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action="dismiss_candidate",
            actor_id=actor_id,
            source_candidate_ids=[candidate_id],
            event_ids=[item.event_id for item in memberships],
            before={"state": candidate.state},
            after={"state": "dismissed"},
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        updated = ResearchTopicStore.update_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            state="dismissed",
        )
        for membership in memberships:
            ResearchTopicStore.append_membership(
                **self.owner,
                event_id=membership.event_id,
                candidate_id=candidate_id,
                state="removed",
                decision_source="user",
                confidence=1.0,
                reason_code="dismiss_candidate",
                operation_id=operation["operation_id"],
                workdir=self.workdir,
                settings=self.settings,
            )
            ResearchAttributionEngine(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).correct(
                AttributionCorrectionInput(
                    **self.owner,
                    event_id=membership.event_id,
                    action="pending",
                    actor_id=actor_id,
                    reason="candidate dismissed",
                    idempotency_key=f"{operation['operation_id']}:{membership.event_id}",
                )
            )
        return updated, operation

    def merge(
        self,
        candidate_ids: list[str],
        *,
        actor_id: str,
        title: str = "",
        reason: str = "",
    ):
        normalized_ids = list(dict.fromkeys(str(item or "").strip() for item in candidate_ids))
        normalized_ids = [item for item in normalized_ids if item]
        if len(normalized_ids) < 2:
            raise ValueError("merge requires at least two candidates")
        candidates = [self._candidate(item) for item in normalized_ids]
        if any(
            item.state in {"confirmed", "merged", "split", "dismissed"}
            for item in candidates
        ):
            raise ValueError("only emerging or stable candidates can be merged")
        event_ids = sorted(
            {
                membership.event_id
                for candidate_id in normalized_ids
                for membership in ResearchTopicStore.list_memberships(
                    **self.owner,
                    candidate_id=candidate_id,
                    workdir=self.workdir,
                    settings=self.settings,
                )
            }
        )
        first_seen = min(item.first_seen_at for item in candidates)
        last_seen = max(item.last_seen_at for item in candidates)
        target, _ = ResearchTopicStore.create_candidate(
            **self.owner,
            candidate_id=f"topic-{uuid.uuid4()}",
            suggested_title=_clean_title(title) or candidates[0].suggested_title,
            goal_summary=next((item.goal_summary for item in candidates if item.goal_summary), ""),
            keywords=sorted({token for item in candidates for token in item.keywords})[:32],
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            title_source="user",
            generation=max(item.generation for item in candidates) + 1,
            workdir=self.workdir,
            settings=self.settings,
        )
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action="merge_candidates",
            actor_id=actor_id,
            source_candidate_ids=normalized_ids,
            target_candidate_ids=[target.candidate_id],
            event_ids=event_ids,
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        for source in candidates:
            ResearchTopicStore.update_candidate(
                source.candidate_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                state="merged",
                merged_into=target.candidate_id,
            )
        for event_id in event_ids:
            self.assign_event(
                event_id,
                candidate_id=target.candidate_id,
                actor_id=actor_id,
                reason=reason or "candidates merged",
                action="merge_event",
            )
        return ResearchTopicEngine(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )._refresh_candidate(target.candidate_id), operation

    def split(
        self,
        candidate_id: str,
        groups: list[dict[str, Any]],
        *,
        actor_id: str,
        reason: str = "",
    ):
        source = self._candidate(candidate_id)
        if source.state not in {"emerging", "stable"}:
            raise ValueError("only emerging or stable candidates can be split")
        memberships = ResearchTopicStore.list_memberships(
            **self.owner,
            candidate_id=candidate_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        source_events = {item.event_id for item in memberships}
        normalized_groups = [
            [str(item or "").strip() for item in group.get("event_ids", [])]
            for group in groups
        ]
        assigned: list[str] = []
        for event_ids in normalized_groups:
            assigned.extend(event_ids)
        if (
            not groups
            or any(not event_ids or any(not event_id for event_id in event_ids) for event_ids in normalized_groups)
            or len(assigned) != len(set(assigned))
            or set(assigned) != source_events
        ):
            raise ValueError("split groups must assign every active event exactly once")
        open_candidates = ResearchTopicStore.list_candidates(
            **self.owner,
            states=["emerging", "stable"],
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        )
        other_count = sum(item.candidate_id != candidate_id for item in open_candidates)
        if other_count + len(normalized_groups) > self.settings.topic_max_candidates:
            raise ValueError("拆分后会超过当前 Workspace 的候选课题上限")
        targets: list[ResearchTopicCandidate] = []
        for group, event_ids in zip(groups, normalized_groups):
            events = ResearchEventStore.list_events_for_attribution(
                **self.owner,
                event_ids=event_ids,
                limit=len(event_ids),
                workdir=self.workdir,
                settings=self.settings,
            )
            target, _ = ResearchTopicStore.create_candidate(
                **self.owner,
                candidate_id=f"topic-{uuid.uuid4()}",
                suggested_title=_clean_title(str(group.get("title") or "")) or source.suggested_title,
                first_seen_at=min(event.occurred_at for event in events),
                last_seen_at=max(event.occurred_at for event in events),
                title_source="user",
                split_from=[candidate_id],
                generation=source.generation + 1,
                workdir=self.workdir,
                settings=self.settings,
            )
            targets.append(target)
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action="split_candidate",
            actor_id=actor_id,
            source_candidate_ids=[candidate_id],
            target_candidate_ids=[item.candidate_id for item in targets],
            event_ids=assigned,
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        ResearchTopicStore.update_candidate(
            candidate_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            state="split",
        )
        for event_ids, target in zip(normalized_groups, targets):
            for event_id in event_ids:
                self.assign_event(
                    event_id,
                    candidate_id=target.candidate_id,
                    actor_id=actor_id,
                    reason=reason or "candidate split",
                    action="split_event",
                )
        refreshed = [
            ResearchTopicEngine(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )._refresh_candidate(item.candidate_id)
            for item in targets
        ]
        return refreshed, operation

    def set_project_status(
        self,
        research_id: str,
        *,
        status: str,
        actor_id: str,
        reason: str = "",
    ):
        before = ResearchTopicStore.get_project(
            research_id,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if before is None:
            raise ValueError("project does not belong to owner")
        if status == "active":
            other_active = [
                item
                for item in ResearchTopicStore.list_projects(
                    **self.owner,
                    include_archived=True,
                    limit=1000,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                if item.research_id != research_id and item.status == "active"
            ]
            if other_active:
                raise ValueError("当前 Workspace 已有活跃的正式课题")
        project = ResearchTopicStore.set_project_status(
            research_id,
            **self.owner,
            status=status,
            workdir=self.workdir,
            settings=self.settings,
        )
        operation = ResearchTopicStore.record_operation(
            **self.owner,
            action={
                "active": "activate_project",
                "paused": "pause_project",
                "archived": "archive_project",
            }[project.status],
            actor_id=actor_id,
            source_candidate_ids=before.origin_candidate_ids,
            before={"research_id": research_id, "status": before.status},
            after={"research_id": research_id, "status": project.status},
            reason=reason,
            workdir=self.workdir,
            settings=self.settings,
        )
        return project, operation
