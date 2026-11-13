"""Shared ResearchPack export audit models and legacy v1 constants."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


RESEARCH_PACK_FORMAT = "tcode-researchpack"
RESEARCH_PACK_SCHEMA_VERSION = "1.0.0"
RESEARCH_PACK_GENERATOR_VERSION = "researchpack-1"
RESEARCH_PACK_PROFILES = {"strict", "standard", "full-research"}
RESEARCH_PACK_ATTRIBUTION_MODES = {"anonymous", "display_name"}
RESEARCH_PACK_STATUSES = {"building", "completed", "failed"}
RESEARCH_PACK_IMPORT_STATUSES = {
    "inspected",
    "importing",
    "completed",
    "failed",
    "cancelled",
    "expired",
}
RESEARCH_PACK_DUPLICATE_ACTIONS = {"open_existing", "copy", "cancel"}


@dataclass(frozen=True)
class ResearchPackPreview:
    research_id: str
    privacy_profile: str
    attribution_mode: str
    source_digest: str
    export_ready: bool
    estimated_bytes: int
    counts: dict[str, int] = field(default_factory=dict)
    exclusions: dict[str, int] = field(default_factory=dict)
    privacy_report: dict[str, Any] = field(default_factory=dict)
    compatibility_report: dict[str, Any] = field(default_factory=dict)
    lint: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    durability_counts: dict[str, int] = field(default_factory=dict)
    routing_counts: dict[str, int] = field(default_factory=dict)
    dependency_counts: dict[str, int] = field(default_factory=dict)
    model_call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchPackExport:
    pack_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    scope_mode: str
    scope: dict[str, Any]
    started_at: str
    ended_at: str
    source_research_ids: list[str]
    schema_version: str
    generator_version: str
    status: str
    privacy_profile: str
    attribution_mode: str
    source_digest: str
    manifest: dict[str, Any]
    privacy_report: dict[str, Any]
    compatibility_report: dict[str, Any]
    archive_name: str
    archive_path: str
    archive_sha256: str
    archive_size: int
    created_at: str
    completed_at: str
    error: str
    idempotency_key: str

    def to_dict(self, *, include_internal_path: bool = False) -> dict[str, Any]:
        value = asdict(self)
        if not include_internal_path:
            value.pop("tenant_id", None)
            value.pop("user_id", None)
            value.pop("workspace_id", None)
            value.pop("archive_path", None)
            value.pop("idempotency_key", None)
        value["download_ready"] = self.status == "completed" and bool(self.archive_name)
        return value

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackExport":
        value = dict(row)
        for key in ("scope", "manifest", "privacy_report", "compatibility_report"):
            raw = value.pop(f"{key}_json", "{}")
            try:
                parsed = json.loads(str(raw or "{}"))
            except Exception:
                parsed = {}
            value[key] = parsed if isinstance(parsed, dict) else {}
        raw_research_ids = value.pop("source_research_ids_json", "[]")
        try:
            parsed_research_ids = json.loads(str(raw_research_ids or "[]"))
        except Exception:
            parsed_research_ids = []
        value["source_research_ids"] = (
            [str(item) for item in parsed_research_ids if str(item or "").strip()]
            if isinstance(parsed_research_ids, list)
            else []
        )
        value["archive_size"] = int(value.get("archive_size") or 0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchPackImport:
    import_id: str
    inspection_id: str
    pack_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    asset_id: str
    local_research_id: str
    schema_version: str
    status: str
    duplicate_action: str
    archive_name: str
    staged_path: str
    archive_sha256: str
    archive_size: int
    source_manifest: dict[str, Any]
    inspection_report: dict[str, Any]
    id_mapping: dict[str, Any]
    compatibility_report: dict[str, Any]
    created_at: str
    inspected_at: str
    imported_at: str
    expires_at: str
    error: str
    idempotency_key: str
    hidden_at: str = ""

    def to_dict(self, *, include_internal_path: bool = False) -> dict[str, Any]:
        value = asdict(self)
        if not include_internal_path:
            for key in (
                "tenant_id",
                "user_id",
                "workspace_id",
                "staged_path",
                "idempotency_key",
            ):
                value.pop(key, None)
        value["import_ready"] = self.status == "inspected" and not self.hidden_at
        return value

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackImport":
        value = dict(row)
        for key in (
            "source_manifest",
            "inspection_report",
            "id_mapping",
            "compatibility_report",
        ):
            raw = value.pop(f"{key}_json", "{}")
            try:
                parsed = json.loads(str(raw or "{}"))
            except Exception:
                parsed = {}
            value[key] = parsed if isinstance(parsed, dict) else {}
        value["archive_size"] = int(value.get("archive_size") or 0)
        return cls(**value)
