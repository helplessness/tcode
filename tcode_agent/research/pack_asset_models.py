"""Imported ResearchPack asset, source-scope, and local overlay models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


RESEARCH_PACK_ASSET_SCHEMA_VERSION = 1
RESEARCH_PACK_ASSET_STATES = {"active", "archived", "invalid"}
RESEARCH_PACK_SCOPE_STATES = {"active", "hidden"}
RESEARCH_PACK_OVERLAY_STATES = {"active", "sealed", "superseded"}
RESEARCH_PACK_OVERLAY_OBJECT_STATES = {"active", "removed"}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except Exception:
            value = {}
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except Exception:
            value = []
    return list(value) if isinstance(value, list) else []


def _strings(value: Any, *, limit: int = 1000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in _json_list(value)
            if str(item or "").strip()
        )
    )[: max(0, limit)]


@dataclass(frozen=True)
class ResearchPackAsset:
    asset_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    pack_id: str
    pack_schema_version: str
    title: str
    status: str
    scope_mode: str
    started_at: str
    ended_at: str
    source_research_ids: list[str]
    manifest: dict[str, Any]
    summary: dict[str, Any]
    lineage: dict[str, Any]
    source_digest: str
    archive_sha256: str
    imported_at: str
    created_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackAsset":
        value = dict(row)
        value["source_research_ids"] = _strings(
            value.pop("source_research_ids_json", "[]")
        )
        for key in ("manifest", "summary", "lineage"):
            value[key] = _json_object(value.pop(f"{key}_json", "{}"))
        value["schema_version"] = int(value.get("schema_version") or 0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchPackSourceScope:
    scope_id: str
    schema_version: int
    asset_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    scope_type: str
    source_research_id: str
    title: str
    summary: str
    keywords: list[str]
    state: str
    created_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackSourceScope":
        value = dict(row)
        value["keywords"] = _strings(value.pop("keywords_json", "[]"), limit=100)
        value["schema_version"] = int(value.get("schema_version") or 0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchPackOverlay:
    overlay_id: str
    schema_version: int
    asset_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    parent_pack_ids: list[str]
    status: str
    source_digest: str
    created_at: str
    updated_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackOverlay":
        value = dict(row)
        value["parent_pack_ids"] = _strings(
            value.pop("parent_pack_ids_json", "[]"),
            limit=100,
        )
        value["schema_version"] = int(value.get("schema_version") or 0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchPackOverlayObject:
    overlay_object_id: str
    schema_version: int
    overlay_id: str
    asset_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    object_type: str
    object_key: str
    local_object_id: str
    imported_scope_refs: list[dict[str, Any]]
    origin_pack_ids: list[str]
    payload: dict[str, Any]
    content_hash: str
    revision: int
    state: str
    source_digest: str
    supersedes_overlay_object_id: str
    superseded_by: str
    created_at: str
    updated_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackOverlayObject":
        value = dict(row)
        value["imported_scope_refs"] = [
            dict(item)
            for item in _json_list(value.pop("imported_scope_refs_json", "[]"))
            if isinstance(item, dict)
        ][:100]
        value["origin_pack_ids"] = _strings(
            value.pop("origin_pack_ids_json", "[]"),
            limit=100,
        )
        value["payload"] = _json_object(value.pop("payload_json", "{}"))
        value["schema_version"] = int(value.get("schema_version") or 0)
        value["revision"] = int(value.get("revision") or 0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchPackObjectMapping:
    mapping_id: str
    schema_version: int
    asset_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    object_type: str
    source_object_id: str
    local_object_id: str
    content_hash: str
    metadata: dict[str, Any]
    created_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackObjectMapping":
        value = dict(row)
        value["metadata"] = _json_object(value.pop("metadata_json", "{}"))
        value["schema_version"] = int(value.get("schema_version") or 0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchPackObjectSnapshot:
    object_id: str
    schema_version: int
    asset_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    object_type: str
    source_object_id: str
    source_research_ids: list[str]
    content_hash: str
    immutable: bool
    distribution: str
    available_layers: list[str]
    missing_layers: list[str]
    payload: dict[str, Any]
    created_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPackObjectSnapshot":
        value = dict(row)
        value["source_research_ids"] = _strings(
            value.pop("source_research_ids_json", "[]")
        )
        value["available_layers"] = _strings(
            value.pop("available_layers_json", "[]")
        )
        value["missing_layers"] = _strings(
            value.pop("missing_layers_json", "[]")
        )
        value["payload"] = _json_object(value.pop("payload_json", "{}"))
        value["schema_version"] = int(value.get("schema_version") or 0)
        value["immutable"] = bool(value.get("immutable"))
        return cls(**value)


__all__ = [
    "RESEARCH_PACK_ASSET_SCHEMA_VERSION",
    "RESEARCH_PACK_ASSET_STATES",
    "RESEARCH_PACK_OVERLAY_STATES",
    "RESEARCH_PACK_OVERLAY_OBJECT_STATES",
    "RESEARCH_PACK_SCOPE_STATES",
    "ResearchPackAsset",
    "ResearchPackObjectMapping",
    "ResearchPackObjectSnapshot",
    "ResearchPackOverlay",
    "ResearchPackOverlayObject",
    "ResearchPackSourceScope",
]
