"""Environment-backed configuration for research recording.

All public defaults are documented in ``config/templates/symphony_route.env.example``.  Keeping the
parser here gives tests and non-server entry points the same bounded behaviour.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _truthy(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(str(os.environ.get(name, default) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ResearchSettings:
    enabled: bool
    journal_enabled: bool
    attribution_enabled: bool
    attribution_shadow_mode: bool
    topics_enabled: bool
    activities_enabled: bool
    experience_projection_enabled: bool
    episodes_enabled: bool
    checkpoints_enabled: bool
    conflicts_enabled: bool
    knowledge_links_enabled: bool
    resume_context_enabled: bool
    research_pack_enabled: bool
    research_pack_import_enabled: bool
    research_pack_import_recovery_enabled: bool
    store_content_preview: bool
    content_preview_chars: int
    max_payload_bytes: int
    pending_ttl_days: int
    research_action_ttl_minutes: int
    auto_include_threshold: float
    attribution_batch_size: int
    attribution_queue_size: int
    attribution_debounce_ms: int
    attribution_session_context_minutes: int
    attribution_auto_dispatch: bool
    attribution_model_enabled: bool
    auto_reconcile_on_startup: bool
    reconcile_recent_days: int
    topic_auto_discovery: bool
    topic_minimum_meaningful_events: int
    topic_cross_session_signal: int
    topic_keyword_overlap_threshold: float
    topic_stable_score_threshold: float
    topic_max_candidates: int
    topic_max_projects_per_workspace: int
    routing_enabled: bool
    routing_model_fallback_enabled: bool
    routing_confidence_threshold: float
    routing_context_token_budget: int
    routing_imported_context_token_budget: int
    routing_imported_max_assets: int
    routing_fallback_timeout_seconds: int
    activity_incomplete_minutes: int
    activity_incremental_max_events: int
    episode_auto_build: bool
    episode_max_events: int
    episode_idle_candidate_minutes: int
    episode_idle_force_partial_minutes: int
    resume_token_budget: int
    resume_context_target_tokens: int
    resume_context_min_relevance: float
    resume_context_max_findings: int
    resume_context_max_episodes: int
    resume_context_audit_enabled: bool
    knowledge_max_wiki_pages: int
    research_pack_max_events: int
    research_pack_max_bytes: int
    research_pack_max_paper_bytes: int
    research_pack_max_paper_layer_bytes: int
    research_pack_max_text_artifact_bytes: int
    research_pack_export_root: str
    research_pack_import_max_archive_bytes: int
    research_pack_import_max_uncompressed_bytes: int
    research_pack_import_max_files: int
    research_pack_import_max_compression_ratio: int
    research_pack_import_inspection_ttl_minutes: int
    research_pack_import_root: str
    background_token_ratio: float
    database_busy_timeout_ms: int
    write_fail_open: bool
    store_root: str

    @property
    def journal_active(self) -> bool:
        return self.enabled and self.journal_enabled

    @property
    def attribution_shadow_active(self) -> bool:
        return (
            self.journal_active
            and self.attribution_enabled
            and self.attribution_shadow_mode
        )

    @property
    def topics_active(self) -> bool:
        return self.attribution_shadow_active and self.topics_enabled

    @property
    def activities_active(self) -> bool:
        return self.attribution_shadow_active and self.activities_enabled

    @property
    def experience_projection_active(self) -> bool:
        return self.activities_active and self.experience_projection_enabled

    @property
    def routing_active(self) -> bool:
        return self.topics_active and self.routing_enabled

    @property
    def episodes_active(self) -> bool:
        return self.topics_active and self.episodes_enabled

    @property
    def checkpoints_active(self) -> bool:
        return self.episodes_active and self.checkpoints_enabled

    @property
    def conflicts_active(self) -> bool:
        return self.episodes_active and self.conflicts_enabled

    @property
    def knowledge_links_active(self) -> bool:
        return self.episodes_active and self.knowledge_links_enabled

    @property
    def resume_context_active(self) -> bool:
        return self.checkpoints_active and self.resume_context_enabled

    @property
    def research_pack_active(self) -> bool:
        return self.knowledge_links_active and self.research_pack_enabled

    @property
    def research_pack_import_active(self) -> bool:
        return self.research_pack_active and self.research_pack_import_enabled

    def database_path(self, workdir: str | None = None) -> Path:
        base = Path(
            str(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
        ).expanduser()
        configured = str(self.store_root or "").strip()
        if configured:
            root = Path(configured).expanduser()
            if not root.is_absolute():
                root = base / root
        else:
            root = base / ".tcode" / "research"
        return root.resolve(strict=False) / "research.sqlite3"

    def dead_letter_path(self, workdir: str | None = None) -> Path:
        return self.database_path(workdir).parent / "dead_letter" / "events.jsonl"

    def research_pack_root(self, workdir: str | None = None) -> Path:
        base = Path(
            str(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
        ).expanduser()
        configured = str(self.research_pack_export_root or "").strip()
        if configured:
            root = Path(configured).expanduser()
            if not root.is_absolute():
                root = base / root
        else:
            root = self.database_path(workdir).parent / "packs"
        return root.resolve(strict=False)

    def research_pack_import_path(self, workdir: str | None = None) -> Path:
        base = Path(
            str(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
        ).expanduser()
        configured = str(self.research_pack_import_root or "").strip()
        if configured:
            root = Path(configured).expanduser()
            if not root.is_absolute():
                root = base / root
        else:
            root = self.database_path(workdir).parent / "pack-imports"
        return root.resolve(strict=False)


def load_research_settings() -> ResearchSettings:
    candidate_minutes = _bounded_int(
        "TCODE_RESEARCH_EPISODE_IDLE_CANDIDATE_MINUTES",
        30,
        1,
        24 * 60,
    )
    force_partial_minutes = _bounded_int(
        "TCODE_RESEARCH_EPISODE_IDLE_FORCE_PARTIAL_MINUTES",
        120,
        1,
        7 * 24 * 60,
    )
    force_partial_minutes = max(force_partial_minutes, candidate_minutes)

    return ResearchSettings(
        enabled=_truthy(os.environ.get("TCODE_RESEARCH_ENABLED"), default=False),
        journal_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_JOURNAL_ENABLED"),
            default=True,
        ),
        attribution_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_ATTRIBUTION_ENABLED"),
            default=False,
        ),
        attribution_shadow_mode=_truthy(
            os.environ.get("TCODE_RESEARCH_ATTRIBUTION_SHADOW_MODE"),
            default=True,
        ),
        topics_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_TOPICS_ENABLED"),
            default=False,
        ),
        activities_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_ACTIVITIES_ENABLED"),
            default=True,
        ),
        experience_projection_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_EXPERIENCE_PROJECTION_ENABLED"),
            default=True,
        ),
        episodes_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_EPISODES_ENABLED"),
            default=False,
        ),
        checkpoints_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_CHECKPOINTS_ENABLED"),
            default=False,
        ),
        conflicts_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_CONFLICTS_ENABLED"),
            default=False,
        ),
        knowledge_links_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_KNOWLEDGE_LINKS_ENABLED"),
            default=False,
        ),
        resume_context_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_RESUME_CONTEXT_ENABLED"),
            default=False,
        ),
        research_pack_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_PACK_ENABLED"),
            default=False,
        ),
        research_pack_import_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_PACK_IMPORT_ENABLED"),
            default=False,
        ),
        research_pack_import_recovery_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_PACK_IMPORT_RECOVERY_ENABLED"),
            default=True,
        ),
        store_content_preview=_truthy(
            os.environ.get("TCODE_RESEARCH_STORE_CONTENT_PREVIEW"),
            default=True,
        ),
        content_preview_chars=_bounded_int(
            "TCODE_RESEARCH_CONTENT_PREVIEW_CHARS",
            500,
            0,
            8000,
        ),
        max_payload_bytes=_bounded_int(
            "TCODE_RESEARCH_MAX_PAYLOAD_BYTES",
            65536,
            4096,
            1024 * 1024,
        ),
        pending_ttl_days=_bounded_int(
            "TCODE_RESEARCH_PENDING_TTL_DAYS",
            30,
            1,
            3650,
        ),
        research_action_ttl_minutes=_bounded_int(
            "TCODE_RESEARCH_ACTION_TTL_MINUTES",
            30,
            5,
            24 * 60,
        ),
        auto_include_threshold=_bounded_float(
            "TCODE_RESEARCH_AUTO_INCLUDE_THRESHOLD",
            0.90,
            0.0,
            1.0,
        ),
        attribution_batch_size=_bounded_int(
            "TCODE_RESEARCH_ATTRIBUTION_BATCH_SIZE",
            100,
            1,
            1000,
        ),
        attribution_queue_size=_bounded_int(
            "TCODE_RESEARCH_ATTRIBUTION_QUEUE_SIZE",
            4096,
            16,
            100000,
        ),
        attribution_debounce_ms=_bounded_int(
            "TCODE_RESEARCH_ATTRIBUTION_DEBOUNCE_MS",
            1000,
            0,
            10000,
        ),
        attribution_session_context_minutes=_bounded_int(
            "TCODE_RESEARCH_ATTRIBUTION_SESSION_CONTEXT_MINUTES",
            20,
            1,
            24 * 60,
        ),
        attribution_auto_dispatch=_truthy(
            os.environ.get("TCODE_RESEARCH_ATTRIBUTION_AUTO_DISPATCH"),
            default=True,
        ),
        attribution_model_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_ATTRIBUTION_MODEL_ENABLED"),
            default=False,
        ),
        auto_reconcile_on_startup=_truthy(
            os.environ.get("TCODE_RESEARCH_AUTO_RECONCILE_ON_STARTUP"),
            default=True,
        ),
        reconcile_recent_days=_bounded_int(
            "TCODE_RESEARCH_RECONCILE_RECENT_DAYS",
            30,
            1,
            3650,
        ),
        topic_auto_discovery=_truthy(
            os.environ.get("TCODE_RESEARCH_TOPIC_AUTO_DISCOVERY"),
            default=True,
        ),
        topic_minimum_meaningful_events=_bounded_int(
            "TCODE_RESEARCH_TOPIC_MINIMUM_MEANINGFUL_EVENTS",
            3,
            2,
            100,
        ),
        topic_cross_session_signal=_bounded_int(
            "TCODE_RESEARCH_TOPIC_CROSS_SESSION_SIGNAL",
            2,
            2,
            20,
        ),
        topic_keyword_overlap_threshold=_bounded_float(
            "TCODE_RESEARCH_TOPIC_KEYWORD_OVERLAP_THRESHOLD",
            0.34,
            0.05,
            1.0,
        ),
        topic_stable_score_threshold=_bounded_float(
            "TCODE_RESEARCH_TOPIC_STABLE_SCORE_THRESHOLD",
            0.65,
            0.1,
            1.0,
        ),
        topic_max_candidates=_bounded_int(
            "TCODE_RESEARCH_TOPIC_MAX_CANDIDATES",
            3,
            1,
            10,
        ),
        topic_max_projects_per_workspace=_bounded_int(
            "TCODE_RESEARCH_MAX_PROJECTS_PER_WORKSPACE",
            1,
            1,
            1,
        ),
        routing_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_ROUTING_ENABLED"),
            default=True,
        ),
        routing_model_fallback_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_ROUTING_MODEL_FALLBACK_ENABLED"),
            default=True,
        ),
        routing_confidence_threshold=_bounded_float(
            "TCODE_RESEARCH_ROUTING_CONFIDENCE_THRESHOLD",
            0.72,
            0.5,
            1.0,
        ),
        routing_context_token_budget=_bounded_int(
            "TCODE_RESEARCH_ROUTING_CONTEXT_TOKEN_BUDGET",
            400,
            128,
            2000,
        ),
        routing_imported_context_token_budget=_bounded_int(
            "TCODE_RESEARCH_ROUTING_IMPORTED_CONTEXT_TOKEN_BUDGET",
            320,
            128,
            2000,
        ),
        routing_imported_max_assets=_bounded_int(
            "TCODE_RESEARCH_ROUTING_IMPORTED_MAX_ASSETS",
            12,
            1,
            100,
        ),
        routing_fallback_timeout_seconds=_bounded_int(
            "TCODE_RESEARCH_ROUTING_FALLBACK_TIMEOUT_SECONDS",
            20,
            2,
            120,
        ),
        activity_incomplete_minutes=_bounded_int(
            "TCODE_RESEARCH_ACTIVITY_INCOMPLETE_MINUTES",
            30,
            1,
            7 * 24 * 60,
        ),
        activity_incremental_max_events=_bounded_int(
            "TCODE_RESEARCH_ACTIVITY_INCREMENTAL_MAX_EVENTS",
            1000,
            50,
            5000,
        ),
        episode_auto_build=_truthy(
            os.environ.get("TCODE_RESEARCH_EPISODE_AUTO_BUILD"),
            default=True,
        ),
        episode_max_events=_bounded_int(
            "TCODE_RESEARCH_EPISODE_MAX_EVENTS",
            5000,
            100,
            10000,
        ),
        episode_idle_candidate_minutes=candidate_minutes,
        episode_idle_force_partial_minutes=force_partial_minutes,
        resume_token_budget=_bounded_int(
            "TCODE_RESEARCH_RESUME_TOKEN_BUDGET",
            1500,
            128,
            16000,
        ),
        resume_context_target_tokens=_bounded_int(
            "TCODE_RESEARCH_RESUME_TARGET_TOKENS",
            800,
            128,
            8000,
        ),
        resume_context_min_relevance=_bounded_float(
            "TCODE_RESEARCH_RESUME_MIN_RELEVANCE",
            0.18,
            0.0,
            1.0,
        ),
        resume_context_max_findings=_bounded_int(
            "TCODE_RESEARCH_RESUME_MAX_FINDINGS",
            8,
            1,
            100,
        ),
        resume_context_max_episodes=_bounded_int(
            "TCODE_RESEARCH_RESUME_MAX_EPISODES",
            2,
            0,
            20,
        ),
        resume_context_audit_enabled=_truthy(
            os.environ.get("TCODE_RESEARCH_RESUME_AUDIT_ENABLED"),
            default=True,
        ),
        knowledge_max_wiki_pages=_bounded_int(
            "TCODE_RESEARCH_KNOWLEDGE_MAX_WIKI_PAGES",
            500,
            10,
            5000,
        ),
        research_pack_max_events=_bounded_int(
            "TCODE_RESEARCH_PACK_MAX_EVENTS",
            5000,
            100,
            10000,
        ),
        research_pack_max_bytes=_bounded_int(
            "TCODE_RESEARCH_PACK_MAX_BYTES",
            268435456,
            1024 * 1024,
            2 * 1024 * 1024 * 1024,
        ),
        research_pack_max_paper_bytes=_bounded_int(
            "TCODE_RESEARCH_PACK_MAX_PAPER_BYTES",
            134217728,
            1024 * 1024,
            2 * 1024 * 1024 * 1024,
        ),
        research_pack_max_paper_layer_bytes=_bounded_int(
            "TCODE_RESEARCH_PACK_MAX_PAPER_LAYER_BYTES",
            67108864,
            1024 * 1024,
            2 * 1024 * 1024 * 1024,
        ),
        research_pack_max_text_artifact_bytes=_bounded_int(
            "TCODE_RESEARCH_PACK_MAX_TEXT_ARTIFACT_BYTES",
            2097152,
            4096,
            64 * 1024 * 1024,
        ),
        research_pack_export_root=str(
            os.environ.get("TCODE_RESEARCH_PACK_EXPORT_ROOT", "") or ""
        ).strip(),
        research_pack_import_max_archive_bytes=_bounded_int(
            "TCODE_RESEARCH_PACK_IMPORT_MAX_ARCHIVE_BYTES",
            268435456,
            1024 * 1024,
            2 * 1024 * 1024 * 1024,
        ),
        research_pack_import_max_uncompressed_bytes=_bounded_int(
            "TCODE_RESEARCH_PACK_IMPORT_MAX_UNCOMPRESSED_BYTES",
            268435456,
            1024 * 1024,
            4 * 1024 * 1024 * 1024,
        ),
        research_pack_import_max_files=_bounded_int(
            "TCODE_RESEARCH_PACK_IMPORT_MAX_FILES",
            5000,
            32,
            50000,
        ),
        research_pack_import_max_compression_ratio=_bounded_int(
            "TCODE_RESEARCH_PACK_IMPORT_MAX_COMPRESSION_RATIO",
            100,
            10,
            1000,
        ),
        research_pack_import_inspection_ttl_minutes=_bounded_int(
            "TCODE_RESEARCH_PACK_IMPORT_INSPECTION_TTL_MINUTES",
            60,
            5,
            24 * 60,
        ),
        research_pack_import_root=str(
            os.environ.get("TCODE_RESEARCH_PACK_IMPORT_ROOT", "") or ""
        ).strip(),
        background_token_ratio=_bounded_float(
            "TCODE_RESEARCH_BACKGROUND_TOKEN_RATIO",
            0.05,
            0.0,
            1.0,
        ),
        database_busy_timeout_ms=_bounded_int(
            "TCODE_RESEARCH_DB_BUSY_TIMEOUT_MS",
            30000,
            100,
            300000,
        ),
        write_fail_open=_truthy(
            os.environ.get("TCODE_RESEARCH_WRITE_FAIL_OPEN"),
            default=True,
        ),
        store_root=str(os.environ.get("TCODE_RESEARCH_STORE_ROOT", "") or "").strip(),
    )
