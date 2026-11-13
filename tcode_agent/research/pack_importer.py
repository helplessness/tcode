"""Safe two-step ResearchPack inspection and transactional local-copy import."""

from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..evidence import EvidenceRecord, EvidenceStore
from .config import ResearchSettings, load_research_settings
from .episode_models import (
    CONFLICT_STATUSES,
    CONFLICT_TYPES,
    EPISODE_OUTCOMES,
    EPISODE_STATES,
    FINDING_STATUSES,
    PROVENANCE_TYPES,
    RESOLUTION_RESULTS,
)
from .knowledge_models import (
    KNOWLEDGE_OBJECT_TYPES,
    KNOWLEDGE_RELATIONS,
    KNOWLEDGE_SIDES,
    KNOWLEDGE_STATES,
    KNOWLEDGE_SUBJECT_TYPES,
)
from .models import canonical_json, sha256_text, utc_now_iso
from .pack import ResearchPackValidator, _sha256_file
from .pack_import_store import ResearchPackImportStore
from .pack_asset_store import ResearchPackAssetStore
from .pack_models import (
    RESEARCH_PACK_DUPLICATE_ACTIONS,
    RESEARCH_PACK_FORMAT,
    RESEARCH_PACK_SCHEMA_VERSION,
    ResearchPackImport,
)
from .pack_v2_models import RESEARCH_PACK_V2_SCHEMA_VERSION, ResearchPackV2Scope
from .store import ResearchEventStore


_SAFE_PACK_ID = re.compile(r"^pack-[A-Za-z0-9_-]{8,160}$")
_TEXT_SUFFIXES = {".json", ".jsonl", ".md"}
_JSON_FILES = {
    "manifest.json": "manifest",
    "snapshot/research.json": "project",
    "snapshot/checkpoint.json": "checkpoint",
    "history/lineage.json": "lineage",
    "evidence/lint-report.json": "source_lint",
    "papers/manifest.json": "papers_manifest",
    "artifacts/manifest.json": "artifacts_manifest",
    "provenance.json": "provenance",
    "privacy-report.json": "privacy_report",
    "compatibility-report.json": "compatibility_report",
}
_JSONL_FILES = {
    "timeline/episodes.jsonl": "episodes",
    "timeline/selected-events.jsonl": "events",
    "history/changes.jsonl": "changes",
    "history/findings.jsonl": "finding_versions",
    "history/claim-revisions.jsonl": "wiki_revisions",
    "history/conflicts.jsonl": "conflicts",
    "history/resolutions.jsonl": "resolutions",
    "history/knowledge-links.jsonl": "knowledge_links",
    "knowledge/wiki/pages.jsonl": "wiki_pages",
    "evidence/records.jsonl": "evidence",
}
_OPTIONAL_JSON_FILES = {
    "handoff/snapshot.json": "handoff_snapshot",
}
_OPTIONAL_JSONL_FILES = {
    "experience/items.jsonl": "experience_items",
}
_V2_JSON_FILES = {
    "manifest.json": "manifest",
    "handoff/overview.json": "handoff_overview",
    "history/lineage.json": "lineage",
    "papers/manifest.json": "papers_manifest",
    "artifacts/manifest.json": "artifacts_manifest",
    "provenance.json": "provenance",
    "privacy-report.json": "privacy_report",
    "compatibility-report.json": "compatibility_report",
}
_V2_JSONL_FILES = {
    "snapshot/checkpoints.jsonl": "checkpoints",
    "snapshot/findings.jsonl": "findings",
    "research/hypotheses.jsonl": "hypotheses",
    "research/experiments.jsonl": "experiments",
    "timeline/activities.jsonl": "activities",
    "timeline/episodes.jsonl": "episodes",
    "experience/items.jsonl": "experience_items",
    "handoff/project-scopes.jsonl": "project_scopes",
    "history/conflicts.jsonl": "conflicts",
    "history/resolutions.jsonl": "resolutions",
    "history/knowledge-links.jsonl": "knowledge_links",
    "evidence/records.jsonl": "evidence",
    "evidence/dependencies.jsonl": "evidence_dependencies",
    "provenance/events.jsonl": "events",
    "knowledge/wiki/pages.jsonl": "wiki_pages",
}


def _utc_after(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=max(1, int(minutes or 1)))
    ).isoformat(timespec="seconds")


def _clean_time(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return fallback
    return raw[:48]


def _strings(value: Any, limit: int = 5000) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in value
            if str(item or "").strip()
        )
    )[:limit]


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class InspectedArchive:
    manifest: dict[str, Any]
    report: dict[str, Any]
    data: dict[str, Any]


class ResearchPackArchiveInspector:
    """Validate an untrusted ZIP without using extractall or executing content."""

    def __init__(self, *, settings: ResearchSettings):
        self.settings = settings

    def inspect(self, archive_path: str | Path, *, include_data: bool = True) -> InspectedArchive:
        archive_file = Path(archive_path).expanduser().resolve(strict=False)
        issues: list[dict[str, Any]] = []

        def block(code: str, message: str, **context: Any) -> None:
            issues.append({"severity": "error", "code": code, "message": message, **context})

        if not archive_file.is_file():
            raise ValueError("ResearchPack archive was not found")
        if archive_file.suffix.lower() != ".zip" or not archive_file.name.endswith(".researchpack.zip"):
            raise ValueError("only .researchpack.zip archives can be imported")
        archive_size = archive_file.stat().st_size
        if archive_size <= 0 or archive_size > self.settings.research_pack_import_max_archive_bytes:
            raise ValueError("ResearchPack archive exceeds the compressed size limit")
        if not zipfile.is_zipfile(archive_file):
            raise ValueError("ResearchPack archive is not a valid ZIP file")

        # 上传文件已经被复制到 Owner 私有的 Import Root；检查临时目录与它同级，
        # 避免根据包内路径或任意调用方 workdir 重新推导落盘位置。
        import_root = archive_file.parent
        import_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".inspect-", dir=import_root))
        try:
            with zipfile.ZipFile(archive_file, "r") as archive:
                members = archive.infolist()
                files = [item for item in members if not item.is_dir()]
                if not files:
                    block("empty_archive", "ResearchPack ZIP 不能为空。")
                if len(files) > self.settings.research_pack_import_max_files:
                    block("zip_file_count_exceeded", "ZIP 文件数量超过配置上限。", file_count=len(files))
                roots: set[str] = set()
                names: set[str] = set()
                folded_names: set[str] = set()
                total_uncompressed = 0
                total_compressed = 0
                normalized: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
                for member in members:
                    name = str(member.filename or "")
                    if not name or "\x00" in name or "\\" in name:
                        block("unsafe_zip_path", "ZIP 包含非法路径。", path=name[:300])
                        continue
                    pure = PurePosixPath(name)
                    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                        block("zip_path_traversal", "ZIP 包含路径穿越。", path=name[:300])
                        continue
                    if len(pure.parts) < 2:
                        block("zip_root_invalid", "ZIP 必须只有一个顶层 Pack 目录。", path=name[:300])
                        continue
                    roots.add(pure.parts[0])
                    key = pure.as_posix()
                    folded = key.casefold()
                    if key in names or folded in folded_names:
                        block("duplicate_zip_path", "ZIP 包含重复或大小写冲突路径。", path=key)
                        continue
                    names.add(key)
                    folded_names.add(folded)
                    unix_mode = (member.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(unix_mode):
                        block("zip_symlink_forbidden", "ZIP 不允许包含符号链接。", path=key)
                        continue
                    if member.flag_bits & 0x1:
                        block("encrypted_zip_forbidden", "不支持加密 ZIP。", path=key)
                        continue
                    if not member.is_dir():
                        suffix = Path(pure.name).suffix.lower()
                        if suffix not in _TEXT_SUFFIXES:
                            block("unsupported_pack_file", "ResearchPack 只允许 JSON、JSONL 和 Markdown。", path=key)
                        total_uncompressed += max(0, int(member.file_size or 0))
                        total_compressed += max(0, int(member.compress_size or 0))
                        if member.file_size > self.settings.research_pack_import_max_uncompressed_bytes:
                            block("zip_entry_too_large", "ZIP 单文件解压大小超过上限。", path=key)
                        ratio = member.file_size / max(1, member.compress_size)
                        if ratio > self.settings.research_pack_import_max_compression_ratio:
                            block("zip_entry_ratio_exceeded", "ZIP 单文件压缩比异常。", path=key, ratio=round(ratio, 2))
                    normalized.append((member, pure))
                if len(roots) != 1:
                    block("zip_root_count_invalid", "ZIP 必须只有一个顶层目录。", roots=sorted(roots)[:20])
                if total_uncompressed > self.settings.research_pack_import_max_uncompressed_bytes:
                    block("zip_uncompressed_size_exceeded", "ZIP 解压总大小超过配置上限。", total_bytes=total_uncompressed)
                total_ratio = total_uncompressed / max(1, total_compressed)
                if total_ratio > self.settings.research_pack_import_max_compression_ratio:
                    block("zip_total_ratio_exceeded", "ZIP 总压缩比异常。", ratio=round(total_ratio, 2))
                if issues:
                    raise ValueError("ResearchPack ZIP inspection failed: " + ", ".join(item["code"] for item in issues[:10]))

                root_name = next(iter(roots))
                pack_folder = temporary / root_name
                for member, pure in normalized:
                    relative = PurePosixPath(*pure.parts[1:])
                    target = (pack_folder / relative.as_posix()).resolve(strict=False)
                    try:
                        target.relative_to(pack_folder.resolve(strict=False))
                    except ValueError as exc:
                        raise ValueError("ResearchPack ZIP path escaped extraction root") from exc
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with archive.open(member, "r") as source, target.open("wb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > self.settings.research_pack_import_max_uncompressed_bytes:
                                raise ValueError("ResearchPack ZIP entry exceeded the streaming limit")
                            output.write(chunk)
                    if written != member.file_size:
                        raise ValueError("ResearchPack ZIP entry size changed while reading")

            validation = ResearchPackValidator(
                max_bytes=self.settings.research_pack_import_max_uncompressed_bytes
            ).validate_folder(pack_folder)
            if not validation.get("valid"):
                codes = [str(item.get("code") or "invalid_pack") for item in validation.get("issues") or []]
                raise ValueError("ResearchPack validation failed: " + ", ".join(codes[:10]))
            manifest = self._read_json(pack_folder / "manifest.json")
            pack_id = str(manifest.get("pack_id") or "")
            if not _SAFE_PACK_ID.fullmatch(pack_id) or pack_id != root_name:
                raise ValueError("ResearchPack root directory does not match manifest pack_id")
            if manifest.get("format") != RESEARCH_PACK_FORMAT:
                raise ValueError("unsupported ResearchPack format")
            if manifest.get("schema_version") != RESEARCH_PACK_V2_SCHEMA_VERSION:
                raise ValueError("unsupported ResearchPack schema version")
            compatibility = self._read_json(pack_folder / "compatibility-report.json")
            if compatibility.get("automatic_execution_allowed") not in {False, None}:
                raise ValueError("ResearchPack requests automatic execution permission")
            mcp = self._read_json(pack_folder / "reproducibility/mcp.json")
            if mcp.get("servers_auto_registered") not in {False, None}:
                raise ValueError("ResearchPack requests automatic MCP registration")
            data = self._load_data_v2(pack_folder) if include_data else {}
            if include_data:
                self._semantic_validate_v2(data)
            conflicts = list(data.get("conflicts") or []) if include_data else []
            open_conflicts = [
                str(item.get("conflict_id") or "")
                for item in conflicts
                if str(item.get("status") or "") in {"open", "under_review"}
            ]
            report = {
                "valid": True,
                "pack_id": pack_id,
                "schema_version": manifest.get("schema_version"),
                "archive_bytes": archive_size,
                "uncompressed_bytes": total_uncompressed,
                "file_count": len(files),
                "compression_ratio": round(total_ratio, 2),
                "checksums_verified": True,
                "content_safety_scanned": True,
                "automatic_execution_allowed": False,
                "required_permissions": [],
                "issues": [],
                "contents": dict(manifest.get("contents") or {}),
                "conflict_summary": {
                    "total": len(conflicts),
                    "open": len(open_conflicts),
                    "open_conflict_ids": open_conflicts[:100],
                    "continuation_blocked": bool(open_conflicts),
                },
                "lineage": dict(manifest.get("lineage") or {}),
                "researches": list(manifest.get("researches") or []),
                "scope": dict(manifest.get("scope") or {}),
            }
            return InspectedArchive(manifest=manifest, report=report, data=data)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"ResearchPack JSON object expected: {path.name}")
        return value

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"ResearchPack JSONL object expected: {path.name}")
            result.append(value)
        return result

    @staticmethod
    def _read_json_list(path: Path) -> list[dict[str, Any]]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError(f"ResearchPack JSON list expected: {path.name}")
        if any(not isinstance(item, dict) for item in value):
            raise ValueError(f"ResearchPack JSON list contains a non-object: {path.name}")
        return [dict(item) for item in value]

    def _load_data_v2(self, root: Path) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for relative, key in _V2_JSON_FILES.items():
            data[key] = self._read_json(root / relative)
        for relative, key in _V2_JSONL_FILES.items():
            data[key] = self._read_jsonl(root / relative)
        data["projects"] = self._read_json_list(
            root / "snapshot/researches.json"
        )
        data["paper_records"] = self._load_paper_records(
            root,
            _json_object(data.get("papers_manifest")),
        )
        data["artifact_files"] = self._load_artifact_files(
            root,
            _json_object(data.get("artifacts_manifest")),
        )
        return data

    @staticmethod
    def _load_artifact_files(
        root: Path,
        artifact_manifest: dict[str, Any],
    ) -> dict[str, str]:
        """Load already-validated portable text artifacts for immutable storage."""

        result: dict[str, str] = {}
        for artifact in _json_list(artifact_manifest.get("artifacts")):
            artifact_id = str(artifact.get("artifact_id") or "").strip()
            relative = str(artifact.get("embedded_path") or "").strip()
            if not artifact_id or not relative:
                continue
            posix = PurePosixPath(relative)
            if (
                not relative.startswith("artifacts/files/")
                or posix.is_absolute()
                or ".." in posix.parts
                or artifact_id in result
            ):
                raise ValueError("ResearchPack embedded Artifact path is invalid")
            path = (root / relative).resolve(strict=False)
            try:
                path.relative_to(root.resolve(strict=False))
            except ValueError as exc:
                raise ValueError("ResearchPack embedded Artifact escapes the package") from exc
            if not path.is_file():
                raise ValueError("ResearchPack embedded Artifact is missing")
            result[artifact_id] = path.read_text(encoding="utf-8")
        return result

    @staticmethod
    def _semantic_validate_v2(data: dict[str, Any]) -> None:
        manifest = _json_object(data.get("manifest"))
        scope_value = _json_object(manifest.get("scope"))
        scope = ResearchPackV2Scope(
            mode=str(scope_value.get("mode") or ""),
            research_ids=_strings(scope_value.get("source_research_ids"), limit=100),
            started_at=str(scope_value.get("started_at") or ""),
            ended_at=str(scope_value.get("ended_at") or ""),
        )
        declared_research_ids = {
            str(item.get("source_research_id") or "")
            for item in list(manifest.get("researches") or [])
            if isinstance(item, dict) and str(item.get("source_research_id") or "")
        }
        if scope.mode == "project" and declared_research_ids != set(scope.research_ids):
            raise ValueError("ResearchPack project Scope does not match Manifest researches")
        project_ids = {
            str(item.get("research_id") or item.get("source_research_id") or "")
            for item in data.get("projects") or []
            if str(item.get("research_id") or item.get("source_research_id") or "")
        }
        if project_ids != declared_research_ids:
            raise ValueError("ResearchPack source project snapshot does not match Scope")

        def ids(key: str, field: str) -> set[str]:
            values = [str(item.get(field) or "") for item in data.get(key) or []]
            if "" in values or len(values) != len(set(values)):
                raise ValueError(f"ResearchPack {key} contains a missing or duplicate ID")
            return set(values)

        activity_ids = ids("activities", "activity_id")
        checkpoint_ids = ids("checkpoints", "checkpoint_id")
        episode_ids = ids("episodes", "episode_id")
        finding_ids = ids("findings", "finding_id")
        conflict_ids = ids("conflicts", "conflict_id")
        resolution_ids = ids("resolutions", "resolution_id")
        experience_ids = ids("experience_items", "experience_id")
        evidence_ids = ids("evidence", "evidence_id")
        event_ids = ids("events", "event_id")
        knowledge_link_ids = ids("knowledge_links", "link_id")
        wiki_page_ids = ids("wiki_pages", "page_id")
        del checkpoint_ids, knowledge_link_ids, wiki_page_ids

        for activity in data.get("activities") or []:
            runs = _json_list(activity.get("agent_runs"))
            run_ids = [str(item.get("agent_run_id") or "") for item in runs]
            if "" in run_ids or len(run_ids) != len(set(run_ids)):
                raise ValueError(
                    "ResearchPack Activity contains a missing or duplicate AgentRun ID"
                )
            known_runs = set(run_ids)
            for run in runs:
                parent_id = str(run.get("parent_agent_run_id") or "")
                if parent_id and parent_id not in known_runs:
                    raise ValueError(
                        "ResearchPack AgentRun references a missing parent AgentRun"
                    )
                child_ids = set(_strings(run.get("child_agent_run_ids")))
                if not child_ids.issubset(known_runs):
                    raise ValueError(
                        "ResearchPack AgentRun references a missing child AgentRun"
                    )
            for key in ("tool_calls", "workflow_calls", "agent_calls"):
                for record in _json_list(activity.get(key)):
                    producer_id = str(
                        record.get("producer_agent_run_id")
                        or (
                            record.get("agent_run_id")
                            if key == "agent_calls"
                            else ""
                        )
                        or ""
                    )
                    if producer_id and known_runs and producer_id not in known_runs:
                        raise ValueError(
                            "ResearchPack execution record references a missing AgentRun"
                        )
            for record in _json_list(activity.get("file_refs")):
                producer_ids = set(
                    _strings(record.get("producer_agent_run_ids"))
                )
                if known_runs and not producer_ids.issubset(known_runs):
                    raise ValueError(
                        "ResearchPack File reference points to a missing AgentRun"
                    )

        for key in ("activities", "experience_items"):
            for item in data.get(key) or []:
                sources = set(_strings(item.get("source_research_ids"), limit=100))
                if not sources.issubset(declared_research_ids):
                    raise ValueError("ResearchPack object references an undeclared source project")
        for finding in data.get("findings") or []:
            episode_id = str(finding.get("episode_id") or "")
            if episode_id and episode_id not in episode_ids:
                raise ValueError("ResearchPack Finding references a missing Episode")
        for conflict in data.get("conflicts") or []:
            refs = set(
                _strings(conflict.get("original_refs"))
                + _strings(conflict.get("challenging_refs"))
            )
            if not refs or not refs.issubset(finding_ids):
                raise ValueError("ResearchPack Conflict contains an incomplete Finding reference")
            current = str(conflict.get("current_resolution_id") or "")
            if current and current not in resolution_ids:
                raise ValueError("ResearchPack Conflict current Resolution is missing")
        for resolution in data.get("resolutions") or []:
            if str(resolution.get("conflict_id") or "") not in conflict_ids:
                raise ValueError("ResearchPack Resolution references a missing Conflict")
        for experience in data.get("experience_items") or []:
            if not set(_strings(experience.get("source_episode_ids"))).issubset(
                episode_ids
            ):
                raise ValueError("ResearchPack Experience references a missing Episode")
        for key, field in (
            ("activities", "source_event_ids"),
            ("experience_items", "source_event_ids"),
            ("episodes", "event_ids"),
            ("findings", "source_event_ids"),
        ):
            for item in data.get(key) or []:
                if not set(_strings(item.get(field))).issubset(event_ids):
                    raise ValueError("ResearchPack object references a missing Event")
        for experience in data.get("experience_items") or []:
            if not set(_strings(experience.get("source_activity_ids"))).issubset(
                activity_ids
            ):
                raise ValueError("ResearchPack Experience references a missing Activity")

        def validate_evidence_refs(values: Any) -> None:
            for reference in _strings(values):
                if reference.startswith("event:"):
                    if reference.split(":", 1)[1] not in event_ids:
                        raise ValueError("ResearchPack object references a missing Event Evidence")
                elif reference not in evidence_ids:
                    raise ValueError("ResearchPack object references missing Evidence")

        for key in ("experience_items", "findings"):
            for item in data.get(key) or []:
                validate_evidence_refs(item.get("evidence_ids"))
        for conflict in data.get("conflicts") or []:
            validate_evidence_refs(conflict.get("original_evidence_ids"))
            validate_evidence_refs(conflict.get("challenging_evidence_ids"))
        for dependency in data.get("evidence_dependencies") or []:
            if str(dependency.get("evidence_id") or "") not in evidence_ids:
                raise ValueError("ResearchPack Evidence dependency is missing its record")

        seen_scope_keys: set[tuple[str, str]] = set()
        project_scope_ids: set[str] = set()
        for item in data.get("project_scopes") or []:
            scope_type = str(item.get("scope_type") or "project")
            source_id = str(item.get("source_research_id") or "")
            if scope_type == "project":
                if source_id not in declared_research_ids:
                    raise ValueError("ResearchPack project Scope is undeclared")
                project_scope_ids.add(source_id)
            elif scope_type == "unassigned":
                if source_id:
                    raise ValueError("ResearchPack unassigned Scope cannot name a project")
            else:
                raise ValueError("ResearchPack source Scope type is unsupported")
            key = (scope_type, source_id)
            if key in seen_scope_keys:
                raise ValueError("ResearchPack contains duplicate source Scopes")
            seen_scope_keys.add(key)
            if not set(_strings(item.get("activity_ids"))).issubset(activity_ids):
                raise ValueError("ResearchPack source Scope references a missing Activity")
            if not set(_strings(item.get("experience_ids"))).issubset(experience_ids):
                raise ValueError("ResearchPack source Scope references missing Experience")
        if project_scope_ids != declared_research_ids:
            raise ValueError("ResearchPack source project Scope set is incomplete")

    def _load_data(self, root: Path) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for relative, key in _JSON_FILES.items():
            data[key] = self._read_json(root / relative)
        for relative, key in _JSONL_FILES.items():
            data[key] = self._read_jsonl(root / relative)
        for relative, key in _OPTIONAL_JSON_FILES.items():
            path = root / relative
            data[key] = self._read_json(path) if path.is_file() else {}
        for relative, key in _OPTIONAL_JSONL_FILES.items():
            path = root / relative
            data[key] = self._read_jsonl(path) if path.is_file() else []
        findings = json.loads((root / "snapshot/findings.json").read_text(encoding="utf-8"))
        data["findings"] = [dict(item) for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
        current_links = json.loads((root / "snapshot/knowledge-links.json").read_text(encoding="utf-8"))
        data["current_knowledge_links"] = (
            [dict(item) for item in current_links if isinstance(item, dict)]
            if isinstance(current_links, list)
            else []
        )
        data["paper_records"] = self._load_paper_records(
            root,
            _json_object(data.get("papers_manifest")),
        )
        return data

    def _load_paper_records(
        self,
        root: Path,
        paper_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        layer_keys = {
            "metadata": "metadata",
            "l0_summary": "l0_summary",
            "l1_passages": "l1_passage_summaries",
            "l2_facts": "l2_facts",
            "l2_chunks": "l2_chunks",
            "tables": "structured_tables",
            "anchors": "evidence_anchors",
            "views": "materialized_views",
            "figures": "figure_descriptions",
            "doc_chunk_index": "doc_chunk_index",
        }
        seen_paths: set[str] = set()
        for raw in _json_list(paper_manifest.get("papers")):
            if not isinstance(raw, dict) or not isinstance(raw.get("layers"), dict):
                continue
            source_doc_id = str(raw.get("doc_id") or "").strip()
            if not source_doc_id:
                raise ValueError("ResearchPack Paper Manifest doc_id is required")
            record: dict[str, Any] = {
                "doc_id": source_doc_id,
                "sha256": str(raw.get("source_pdf_sha256") or "").removeprefix("sha256:"),
                "pdf_path": "",
                "available_layers": list(raw.get("available_layers") or []),
                "missing_layers": list(raw.get("missing_layers") or []),
                "distribution": str(raw.get("distribution") or "derived_partial"),
                "source_pdf_missing": True,
                "parser": _json_object(raw.get("parser")),
            }
            loaded_layers: set[str] = set()
            for layer_name, descriptor in dict(raw.get("layers") or {}).items():
                if layer_name not in layer_keys or not isinstance(descriptor, dict):
                    continue
                if descriptor.get("status") != "available":
                    continue
                relative = str(descriptor.get("path") or "")
                posix = PurePosixPath(relative)
                if (
                    not relative.startswith("papers/records/")
                    or posix.is_absolute()
                    or ".." in posix.parts
                    or relative in seen_paths
                ):
                    raise ValueError("ResearchPack Paper Layer path is unsafe or duplicated")
                seen_paths.add(relative)
                path = (root / relative).resolve(strict=False)
                try:
                    path.relative_to(root.resolve(strict=False))
                except ValueError as exc:
                    raise ValueError("ResearchPack Paper Layer escapes the package") from exc
                if not path.is_file():
                    raise ValueError("ResearchPack Paper Layer is missing")
                if int(descriptor.get("bytes", -1)) != path.stat().st_size:
                    raise ValueError("ResearchPack Paper Layer size mismatch")
                if str(descriptor.get("sha256") or "") != _sha256_file(path):
                    raise ValueError("ResearchPack Paper Layer checksum mismatch")
                format_name = str(descriptor.get("format") or "")
                if format_name == "json":
                    value: Any = json.loads(path.read_text(encoding="utf-8"))
                elif format_name == "jsonl":
                    value = self._read_jsonl(path)
                else:
                    raise ValueError("ResearchPack Paper Layer format is unsupported")
                expected_list = layer_name in {
                    "l1_passages", "l2_facts", "l2_chunks", "tables", "anchors", "figures"
                }
                if expected_list and not isinstance(value, list):
                    raise ValueError("ResearchPack Paper Layer expected a list")
                if not expected_list and not isinstance(value, dict):
                    raise ValueError("ResearchPack Paper Layer expected an object")
                record[layer_keys[layer_name]] = value
                loaded_layers.add(layer_name)
            if loaded_layers != set(raw.get("available_layers") or []):
                raise ValueError("ResearchPack Paper Layer availability declaration mismatch")
            result.append({"manifest": dict(raw), "record": record})
        return result

    @staticmethod
    def _semantic_validate(data: dict[str, Any]) -> None:
        finding_ids = {str(item.get("finding_id") or "") for item in data.get("findings") or []}
        conflict_ids = {str(item.get("conflict_id") or "") for item in data.get("conflicts") or []}
        resolution_ids = {str(item.get("resolution_id") or "") for item in data.get("resolutions") or []}
        episode_ids = {str(item.get("episode_id") or "") for item in data.get("episodes") or []}
        for conflict in data.get("conflicts") or []:
            refs = _strings(conflict.get("original_refs")) + _strings(conflict.get("challenging_refs"))
            if not refs or any(ref not in finding_ids for ref in refs):
                raise ValueError("ResearchPack Conflict contains an incomplete Finding reference")
            current = str(conflict.get("current_resolution_id") or "")
            if current and current not in resolution_ids:
                raise ValueError("ResearchPack Conflict current Resolution is missing")
        for resolution in data.get("resolutions") or []:
            if str(resolution.get("conflict_id") or "") not in conflict_ids:
                raise ValueError("ResearchPack Resolution references a missing Conflict")
        for finding in data.get("findings") or []:
            episode_id = str(finding.get("episode_id") or "")
            if episode_id and episode_id not in episode_ids:
                raise ValueError("ResearchPack Finding references a missing Episode")
        for item in data.get("experience_items") or []:
            episode_id = str(item.get("source_episode_id") or "")
            if episode_id and episode_id not in episode_ids:
                raise ValueError("ResearchPack Experience references a missing Episode")
        handoff = _json_object(data.get("handoff_snapshot"))
        gate = _json_object(handoff.get("conflict_gate"))
        for item in _json_list(gate.get("items")):
            conflict_id = str(_json_object(item).get("conflict_id") or "")
            if conflict_id and conflict_id not in conflict_ids:
                raise ValueError("ResearchPack Handoff references a missing Conflict")


class ResearchPackImporter:
    """Import a verified v2 Pack as an immutable Asset with an empty Overlay."""

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
        self.inspector = ResearchPackArchiveInspector(settings=self.settings)

    @property
    def owner(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
        }

    def inspect_upload(self, source: str | Path, *, archive_name: str) -> ResearchPackImport:
        if not self.settings.research_pack_import_active:
            raise RuntimeError("ResearchPack import is not active")
        source_path = Path(source).expanduser().resolve(strict=False)
        safe_name = Path(str(archive_name or source_path.name)).name
        if not safe_name.endswith(".researchpack.zip"):
            raise ValueError("only .researchpack.zip archives can be imported")
        root = self.settings.research_pack_import_path(self.workdir.as_posix())
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        staged = root / f"staged-{uuid.uuid4().hex}.researchpack.zip"
        if source_path.stat().st_size > self.settings.research_pack_import_max_archive_bytes:
            raise ValueError("ResearchPack archive exceeds the compressed size limit")
        shutil.copyfile(source_path, staged)
        try:
            staged.chmod(0o600)
        except OSError:
            pass
        try:
            inspected = self.inspector.inspect(staged, include_data=True)
            existing = ResearchPackImportStore.completed_for_pack(
                str(inspected.manifest["pack_id"]),
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            report = dict(inspected.report)
            parent_pack_ids = _strings(
                _json_object(inspected.manifest.get("lineage")).get(
                    "parent_pack_ids"
                ),
                limit=100,
            )
            available_pack_ids = {
                asset.pack_id
                for asset in ResearchPackAssetStore.list_assets(
                    **self.owner,
                    status="",
                    limit=1000,
                    workdir=self.workdir.as_posix(),
                    settings=self.settings,
                )
            }
            missing_parent_ids = [
                pack_id
                for pack_id in parent_pack_ids
                if pack_id not in available_pack_ids
            ]
            report["duplicate"] = {
                "already_imported": existing is not None,
                "existing_asset_id": existing.asset_id if existing else "",
                "allowed_actions": ["open_existing", "copy", "cancel"] if existing else ["copy", "cancel"],
            }
            report["lineage_availability"] = {
                "parent_pack_ids": parent_pack_ids,
                "missing_parent_pack_ids": missing_parent_ids,
                "static_read_allowed": True,
                "continuation_warning": bool(missing_parent_ids),
            }
            report["import_preview"] = self._preview_counts(inspected.data)
            return ResearchPackImportStore.create_inspection(
                pack_id=str(inspected.manifest["pack_id"]),
                **self.owner,
                schema_version=str(inspected.manifest["schema_version"]),
                archive_name=safe_name,
                staged_path=staged.as_posix(),
                archive_sha256=_sha256_file(staged),
                archive_size=staged.stat().st_size,
                source_manifest=inspected.manifest,
                inspection_report=report,
                compatibility_report=_json_object(inspected.data.get("compatibility_report")),
                expires_at=_utc_after(self.settings.research_pack_import_inspection_ttl_minutes),
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
        except Exception:
            staged.unlink(missing_ok=True)
            raise

    @staticmethod
    def _preview_counts(data: dict[str, Any]) -> dict[str, int]:
        return {
            "events": len(data.get("events") or []),
            "activities": len(data.get("activities") or []),
            "agent_runs": sum(
                len(_json_list(item.get("agent_runs")))
                for item in data.get("activities") or []
            ),
            "workflow_runs": sum(
                len(_json_list(item.get("workflow_calls")))
                for item in data.get("activities") or []
            ),
            "episodes": len(data.get("episodes") or []),
            "checkpoints": len(data.get("checkpoints") or []),
            "findings": len(data.get("findings") or []),
            "changes": len(data.get("changes") or []),
            "conflicts": len(data.get("conflicts") or []),
            "resolutions": len(data.get("resolutions") or []),
            "knowledge_links": len(data.get("knowledge_links") or []),
            "evidence_records": len(data.get("evidence") or []),
            "wiki_pages": len(data.get("wiki_pages") or []),
            "papers": len(_json_list(_json_object(data.get("papers_manifest")).get("papers"))),
            "embedded_text_artifacts": len(
                _json_object(data.get("artifact_files"))
            ),
            "experience_items": len(data.get("experience_items") or []),
        }

    def import_copy(
        self,
        inspection_id: str,
        *,
        duplicate_action: str = "open_existing",
        title: str = "",
    ) -> ResearchPackImport:
        if not self.settings.research_pack_import_active:
            raise RuntimeError("ResearchPack import is not active")
        action = str(duplicate_action or "copy").strip().lower()
        if action not in RESEARCH_PACK_DUPLICATE_ACTIONS:
            raise ValueError("unsupported duplicate import action")
        item = ResearchPackImportStore.get(
            inspection_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if item is None:
            raise ValueError("ResearchPack inspection was not found")
        if item.hidden_at:
            raise ValueError('导入记录已删除，请先在已删除资料与记录中恢复')
        if item.status == "completed":
            return item
        if item.status != "inspected":
            raise ValueError(f"ResearchPack inspection is {item.status}")
        if ResearchPackImportStore.is_expired(item):
            self._set_status(item.import_id, "expired", error="inspection expired")
            staged = ResearchPackImportStore.staged_file(item, workdir=self.workdir.as_posix(), settings=self.settings)
            if staged:
                staged.unlink(missing_ok=True)
            raise ValueError("ResearchPack inspection has expired; upload it again")
        existing = ResearchPackImportStore.completed_for_pack(
            item.pack_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if action == "cancel":
            self._set_status(item.import_id, "cancelled", duplicate_action=action)
            self._remove_staged(item)
            return self._get_required(item.import_id)
        if action == "open_existing":
            if existing is not None:
                if existing.asset_id and ResearchPackAssetStore.get_asset(existing.asset_id, **self.owner, workdir=self.workdir.as_posix(), settings=self.settings) is None:
                    raise ValueError('该研究资料已删除，请先在已删除资料与记录中恢复')
                self._set_status(
                    item.import_id,
                    "cancelled",
                    duplicate_action=action,
                    asset_id=existing.asset_id,
                )
                self._remove_staged(item)
                return existing
            action = "copy"

        staged = ResearchPackImportStore.staged_file(
            item,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if staged is None or _sha256_file(staged) != item.archive_sha256:
            raise ValueError("staged ResearchPack changed after inspection")
        try:
            inspected = self.inspector.inspect(staged, include_data=True)
            if inspected.manifest != item.source_manifest:
                raise ValueError("ResearchPack manifest changed after inspection")
            result = self._transactional_import(item, inspected.data, title=title, action=action)
            self._remove_staged(item)
            return result
        except Exception as exc:
            self._remove_staged(item)
            ResearchPackImportStore.fail(
                item.import_id,
                str(exc),
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            raise

    def _get_required(self, item_id: str) -> ResearchPackImport:
        item = ResearchPackImportStore.get(
            item_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if item is None:
            raise RuntimeError("ResearchPack import audit disappeared")
        return item

    def _set_status(
        self,
        import_id: str,
        status: str,
        *,
        duplicate_action: str | None = None,
        asset_id: str | None = None,
        local_research_id: str | None = None,
        error: str = "",
    ) -> None:
        connection = ResearchEventStore._connect(self.workdir.as_posix(), settings=self.settings)
        try:
            assignments = ["status=?", "error=?"]
            params: list[Any] = [status, error[:4000]]
            if duplicate_action is not None:
                assignments.append("duplicate_action=?")
                params.append(duplicate_action)
            if asset_id is not None:
                assignments.append("asset_id=?")
                params.append(asset_id)
            if local_research_id is not None:
                assignments.append("local_research_id=?")
                params.append(local_research_id)
            params.append(import_id)
            connection.execute(
                f"UPDATE research_pack_imports SET {', '.join(assignments)} WHERE import_id=?",
                params,
            )
        finally:
            connection.close()

    def _remove_staged(self, item: ResearchPackImport) -> None:
        staged = ResearchPackImportStore.staged_file(
            item,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if staged:
            staged.unlink(missing_ok=True)

    def _prepare_recovery_plan(
        self,
        audit: ResearchPackImport,
        maps: dict[str, dict[str, str]],
        *,
        asset_id: str = "",
        local_research_id: str = "",
        action: str,
        started_at: str,
    ) -> None:
        """Commit cleanup identities before any external Store can commit."""
        compatibility = dict(audit.compatibility_report)
        compatibility["recovery_plan"] = {
            "version": "researchpack-recovery-1",
            "started_at": started_at,
            "asset_id": asset_id,
            "local_research_id": local_research_id,
            "evidence_ids": list(maps.get("evidence", {}).values()),
            "wiki_page_ids": list(maps.get("wiki_page", {}).values()),
            "paper_ids": list(maps.get("paper", {}).values()),
        }
        connection = ResearchEventStore._connect(self.workdir.as_posix(), settings=self.settings)
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE research_pack_imports SET status='importing',duplicate_action=?,"
                "asset_id=?,local_research_id=?,id_mapping_json=?,compatibility_report_json=?,error='' "
                "WHERE import_id=? AND status='inspected' AND hidden_at=''",
                (
                    action,
                    asset_id,
                    local_research_id,
                    canonical_json(maps),
                    canonical_json(compatibility),
                    audit.import_id,
                ),
            )
            if not changed.rowcount:
                raise ValueError('导入记录已删除或状态已改变，请重新检查')
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _v2_scopes(data: dict[str, Any]) -> list[dict[str, Any]]:
        scopes = [dict(item) for item in data.get("project_scopes") or []]
        if scopes:
            return scopes
        return [
            {
                "scope_type": "project",
                "source_research_id": str(
                    project.get("research_id")
                    or project.get("source_research_id")
                    or ""
                ),
                "title": str(project.get("title") or "未命名来源项目"),
                "summary": str(project.get("description") or ""),
                "keywords": [],
            }
            for project in data.get("projects") or []
        ]

    @staticmethod
    def _v2_object_rows(
        data: dict[str, Any],
        materialized_maps: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        def append(
            object_type: str,
            values: list[dict[str, Any]],
            id_field: str,
            *,
            map_type: str = "",
            default_distribution: str = "snapshot",
        ) -> None:
            for raw in values:
                source_id = str(raw.get(id_field) or "").strip()
                if not source_id:
                    continue
                source_research_ids = _strings(
                    raw.get("source_research_ids"), limit=100
                )
                direct_research_id = str(raw.get("research_id") or "").strip()
                if direct_research_id and direct_research_id not in source_research_ids:
                    source_research_ids.append(direct_research_id)
                content_hash = str(
                    raw.get("content_hash")
                    or raw.get("source_digest")
                    or raw.get("record_hash")
                    or raw.get("portable_key")
                    or ""
                )
                effective_map_type = map_type or object_type
                rows.append(
                    {
                        "object_type": object_type,
                        "source_object_id": source_id,
                        "source_research_ids": source_research_ids,
                        "content_hash": content_hash,
                        "distribution": str(
                            raw.get("distribution") or default_distribution
                        ),
                        "available_layers": list(
                            raw.get("available_layers") or []
                        ),
                        "missing_layers": list(raw.get("missing_layers") or []),
                        "materialized_local_id": materialized_maps.get(
                            effective_map_type, {}
                        ).get(source_id, ""),
                        "payload": dict(raw),
                    }
                )

        append("research", list(data.get("projects") or []), "research_id")
        append("checkpoint", list(data.get("checkpoints") or []), "checkpoint_id")
        append("finding", list(data.get("findings") or []), "finding_id")
        append("hypothesis", list(data.get("hypotheses") or []), "hypothesis_id")
        append("experiment", list(data.get("experiments") or []), "experiment_id")
        append("activity", list(data.get("activities") or []), "activity_id")
        append("episode", list(data.get("episodes") or []), "episode_id")
        append("experience", list(data.get("experience_items") or []), "experience_id")
        append("conflict", list(data.get("conflicts") or []), "conflict_id")
        append("resolution", list(data.get("resolutions") or []), "resolution_id")
        append(
            "knowledge_link",
            list(data.get("knowledge_links") or []),
            "link_id",
        )
        append(
            "evidence",
            list(data.get("evidence") or []),
            "evidence_id",
            map_type="evidence",
            default_distribution="embedded",
        )
        append(
            "event",
            list(data.get("events") or []),
            "event_id",
            default_distribution="sanitized_trace",
        )
        append(
            "wiki_page",
            list(data.get("wiki_pages") or []),
            "page_id",
            map_type="wiki_page",
            default_distribution="embedded",
        )
        artifacts = [
            {
                **dict(item),
                **(
                    {"embedded_content": str(
                        _json_object(data.get("artifact_files")).get(
                            str(item.get("artifact_id") or ""),
                            "",
                        )
                    )}
                    if str(item.get("artifact_id") or "")
                    in _json_object(data.get("artifact_files"))
                    else {}
                ),
            }
            for item in _json_list(
                _json_object(data.get("artifacts_manifest")).get("artifacts")
            )
            if isinstance(item, dict)
        ]
        append(
            "artifact",
            artifacts,
            "artifact_id",
            default_distribution="reference_only",
        )
        papers = [
            dict(item)
            for item in _json_list(
                _json_object(data.get("papers_manifest")).get("papers")
            )
            if isinstance(item, dict)
        ]
        append(
            "paper",
            papers,
            "doc_id",
            map_type="paper",
            default_distribution="derived_partial",
        )
        return rows

    def _transactional_import_v2(
        self,
        audit: ResearchPackImport,
        data: dict[str, Any],
        *,
        title: str,
        action: str,
    ) -> ResearchPackImport:
        now = utc_now_iso()
        asset_id = f"pack-asset-{uuid.uuid4()}"
        materialized_maps = self._build_mappings_v2(data)
        self._prepare_recovery_plan(
            audit,
            materialized_maps,
            asset_id=asset_id,
            action=action,
            started_at=now,
        )
        audit = self._get_required(audit.import_id)
        manifest = _json_object(data.get("manifest"))
        scope = _json_object(manifest.get("scope"))
        lineage = _json_object(manifest.get("lineage"))
        overview = _json_object(data.get("handoff_overview"))
        parent_pack_ids = _strings(lineage.get("parent_pack_ids"), limit=100)
        available_pack_ids = {
            asset.pack_id
            for asset in ResearchPackAssetStore.list_assets(
                **self.owner,
                status="",
                limit=1000,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
        }
        missing_parent_ids = [
            pack_id for pack_id in parent_pack_ids if pack_id not in available_pack_ids
        ]
        open_conflict_ids = [
            str(item.get("conflict_id") or "")
            for item in data.get("conflicts") or []
            if str(item.get("status") or "") in {"open", "under_review"}
        ]
        asset_summary = {
            **overview,
            "object_counts": self._preview_counts(data),
            "conflict_gate": {
                "continuation_blocked": bool(open_conflict_ids),
                "open_conflict_ids": open_conflict_ids[:100],
                "reason": "unresolved_conflicts" if open_conflict_ids else "",
            },
            "lineage_availability": {
                "missing_parent_pack_ids": missing_parent_ids,
                "static_read_allowed": True,
                "continuation_warning": bool(missing_parent_ids),
            },
            "overlay": {"state": "empty", "local_object_count": 0},
        }
        objects = self._v2_object_rows(data, materialized_maps)
        scopes = self._v2_scopes(data)
        source_research_ids = _strings(
            scope.get("source_research_ids"), limit=100
        )
        display_title = _bounded_text(
            title
            or overview.get("title")
            or next(
                (
                    item.get("title")
                    for item in list(manifest.get("researches") or [])
                    if isinstance(item, dict) and item.get("title")
                ),
                "导入的研究资料包",
            ),
            500,
        )
        connection = ResearchEventStore._connect(
            self.workdir.as_posix(), settings=self.settings
        )
        evidence_connection = None
        wiki_connection = None
        paper_store = None
        created_paper_ids: list[str] = []
        wiki_store = None
        created_wiki_ids: list[str] = []
        evidence_committed = False
        wiki_committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            evidence_connection, _ = self._stage_evidence(
                data, materialized_maps, audit.pack_id, now
            )
            wiki_store, wiki_connection, created_wiki_ids = self._stage_wiki(
                data, materialized_maps, audit.pack_id
            )
            paper_store, created_paper_ids = self._stage_papers(
                data, materialized_maps, audit.pack_id
            )
            _, _, object_mappings = ResearchPackAssetStore.insert_import_snapshot(
                connection,
                asset_id=asset_id,
                **self.owner,
                pack_id=audit.pack_id,
                pack_schema_version=RESEARCH_PACK_V2_SCHEMA_VERSION,
                title=display_title,
                scope_mode=str(scope.get("mode") or ""),
                started_at=str(scope.get("started_at") or ""),
                ended_at=str(scope.get("ended_at") or ""),
                source_research_ids=source_research_ids,
                manifest=manifest,
                summary=asset_summary,
                lineage=lineage,
                source_digest=str(manifest.get("source_digest") or ""),
                archive_sha256=audit.archive_sha256,
                scopes=scopes,
                objects=objects,
                parent_pack_ids=parent_pack_ids,
                imported_at=now,
            )
            id_mapping = {
                "objects": object_mappings,
                "materialized": materialized_maps,
            }
            compatibility = dict(audit.compatibility_report)
            compatibility.update(
                {
                    "imported_by": "researchpack-asset-import-2",
                    "automatic_execution_allowed": False,
                    "immutable_source_objects": True,
                    "overlay_initialized": True,
                    "relationship_lint": "pass",
                    "local_projects_created": 0,
                }
            )
            connection.execute(
                "UPDATE research_pack_imports SET status='completed',asset_id=?,"
                "local_research_id='',id_mapping_json=?,compatibility_report_json=?,"
                "imported_at=?,staged_path='',error='' WHERE import_id=?",
                (
                    asset_id,
                    canonical_json(id_mapping),
                    canonical_json(compatibility),
                    now,
                    audit.import_id,
                ),
            )
            if evidence_connection is not None:
                evidence_connection.execute("COMMIT")
                evidence_committed = True
            if wiki_connection is not None:
                wiki_connection.execute("COMMIT")
                wiki_committed = True
            connection.execute("COMMIT")
        except Exception:
            for candidate in (evidence_connection, wiki_connection, connection):
                if candidate is not None:
                    try:
                        candidate.execute("ROLLBACK")
                    except Exception:
                        pass
            if paper_store is not None:
                for paper_id in created_paper_ids:
                    try:
                        paper_store.remove_imported_record(
                            paper_id, pack_id=audit.pack_id
                        )
                    except Exception:
                        pass
                try:
                    paper_store._rebuild_chunk_index()
                except Exception:
                    pass
            if evidence_committed:
                cleanup = EvidenceStore._connect(self.workdir.as_posix())
                try:
                    cleanup.execute("BEGIN IMMEDIATE")
                    cleanup.executemany(
                        "DELETE FROM evidence_records WHERE evidence_id=?",
                        [
                            (value,)
                            for value in materialized_maps.get("evidence", {}).values()
                        ],
                    )
                    cleanup.execute("COMMIT")
                except Exception:
                    try:
                        cleanup.execute("ROLLBACK")
                    except Exception:
                        pass
                finally:
                    cleanup.close()
            if wiki_committed and wiki_store is not None:
                cleanup = wiki_store._connect()
                try:
                    cleanup.execute("BEGIN IMMEDIATE")
                    for page_id in created_wiki_ids:
                        cleanup.execute("DELETE FROM revisions WHERE page_id=?", (page_id,))
                        cleanup.execute("DELETE FROM events WHERE page_id=?", (page_id,))
                        cleanup.execute("DELETE FROM dependencies WHERE page_id=?", (page_id,))
                        cleanup.execute("DELETE FROM pages WHERE page_id=?", (page_id,))
                        try:
                            cleanup.execute("DELETE FROM page_fts WHERE page_id=?", (page_id,))
                        except Exception:
                            pass
                    cleanup.execute("COMMIT")
                except Exception:
                    try:
                        cleanup.execute("ROLLBACK")
                    except Exception:
                        pass
                finally:
                    cleanup.close()
            raise
        finally:
            if evidence_connection is not None:
                evidence_connection.close()
            if wiki_connection is not None:
                wiki_connection.close()
            connection.close()
        if wiki_store is not None:
            for page_id in created_wiki_ids:
                try:
                    wiki_store._materialize_page(wiki_store.get_page(page_id))
                except Exception:
                    pass
            try:
                wiki_store._materialize_indexes()
            except Exception:
                pass
        return self._get_required(audit.import_id)

    def _transactional_import(
        self,
        audit: ResearchPackImport,
        data: dict[str, Any],
        *,
        title: str,
        action: str,
    ) -> ResearchPackImport:
        if str(_json_object(data.get("manifest")).get("schema_version") or "") == RESEARCH_PACK_V2_SCHEMA_VERSION:
            return self._transactional_import_v2(
                audit,
                data,
                title=title,
                action=action,
            )
        now = utc_now_iso()
        maps = self._build_mappings(data)
        local_research_id = maps["project"][str(data["project"].get("research_id") or audit.source_manifest.get("research", {}).get("source_research_id") or "source")]
        self._prepare_recovery_plan(
            audit,
            maps,
            local_research_id=local_research_id,
            action=action,
            started_at=now,
        )
        audit = self._get_required(audit.import_id)
        connection = ResearchEventStore._connect(self.workdir.as_posix(), settings=self.settings)
        evidence_connection = None
        wiki_connection = None
        paper_store = None
        created_paper_ids: list[str] = []
        wiki_store = None
        created_wiki_ids: list[str] = []
        evidence_committed = False
        wiki_committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM research_projects WHERE tenant_id=? AND user_id=? AND workspace_id=?",
                    (self.tenant_id, self.user_id, self.workspace_id),
                ).fetchone()[0]
            )
            if count >= self.settings.topic_max_projects_per_workspace:
                raise ValueError("当前 Workspace 已存在正式课题；请切换到空 Workspace 后再导入")
            evidence_connection, _evidence_hashes = self._stage_evidence(data, maps, audit.pack_id, now)
            wiki_store, wiki_connection, created_wiki_ids = self._stage_wiki(data, maps, audit.pack_id)
            paper_store, created_paper_ids = self._stage_papers(data, maps, audit.pack_id)
            self._insert_research(connection, audit, data, maps, local_research_id, title, now)
            self._insert_mappings(connection, audit.import_id, audit.pack_id, maps, data, now)
            compatibility = dict(audit.compatibility_report)
            compatibility.update(
                {
                    "imported_by": "researchpack-import-1",
                    "automatic_execution_allowed": False,
                    "id_mapping_complete": True,
                    "relationship_lint": "pass",
                }
            )
            connection.execute(
                "UPDATE research_pack_imports SET status='completed',local_research_id=?,"
                "id_mapping_json=?,compatibility_report_json=?,imported_at=?,staged_path='',"
                "error='' WHERE import_id=?",
                (
                    local_research_id,
                    canonical_json(maps),
                    canonical_json(compatibility),
                    now,
                    audit.import_id,
                ),
            )
            if evidence_connection is not None:
                evidence_connection.execute("COMMIT")
                evidence_committed = True
            if wiki_connection is not None:
                wiki_connection.execute("COMMIT")
                wiki_committed = True
            connection.execute("COMMIT")
        except Exception:
            for candidate in (evidence_connection, wiki_connection, connection):
                if candidate is not None:
                    try:
                        candidate.execute("ROLLBACK")
                    except Exception:
                        pass
            if paper_store is not None:
                for paper_id in created_paper_ids:
                    try:
                        paper_store.remove_imported_record(
                            paper_id,
                            pack_id=audit.pack_id,
                        )
                    except Exception:
                        pass
                try:
                    paper_store._rebuild_chunk_index()
                except Exception:
                    pass
            if evidence_committed:
                cleanup = EvidenceStore._connect(self.workdir.as_posix())
                try:
                    cleanup.execute("BEGIN IMMEDIATE")
                    cleanup.executemany(
                        "DELETE FROM evidence_records WHERE evidence_id=?",
                        [(value,) for value in maps.get("evidence", {}).values()],
                    )
                    cleanup.execute("COMMIT")
                except Exception:
                    try:
                        cleanup.execute("ROLLBACK")
                    except Exception:
                        pass
                finally:
                    cleanup.close()
            if wiki_committed and wiki_store is not None:
                cleanup = wiki_store._connect()
                try:
                    cleanup.execute("BEGIN IMMEDIATE")
                    for page_id in created_wiki_ids:
                        cleanup.execute("DELETE FROM revisions WHERE page_id=?", (page_id,))
                        cleanup.execute("DELETE FROM events WHERE page_id=?", (page_id,))
                        cleanup.execute("DELETE FROM dependencies WHERE page_id=?", (page_id,))
                        cleanup.execute("DELETE FROM pages WHERE page_id=?", (page_id,))
                        try:
                            cleanup.execute("DELETE FROM page_fts WHERE page_id=?", (page_id,))
                        except Exception:
                            pass
                    cleanup.execute("COMMIT")
                except Exception:
                    try:
                        cleanup.execute("ROLLBACK")
                    except Exception:
                        pass
                finally:
                    cleanup.close()
            raise
        finally:
            if evidence_connection is not None:
                evidence_connection.close()
            if wiki_connection is not None:
                wiki_connection.close()
            connection.close()
        if wiki_store is not None:
            for page_id in created_wiki_ids:
                try:
                    wiki_store._materialize_page(wiki_store.get_page(page_id))
                except Exception:
                    pass
            try:
                wiki_store._materialize_indexes()
            except Exception:
                pass
        return self._get_required(audit.import_id)

    def _build_mappings(self, data: dict[str, Any]) -> dict[str, dict[str, str]]:
        if str(_json_object(data.get("manifest")).get("schema_version") or "") == RESEARCH_PACK_V2_SCHEMA_VERSION:
            return self._build_mappings_v2(data)
        source_project = str(
            data.get("project", {}).get("research_id")
            or data.get("manifest", {}).get("research", {}).get("source_research_id")
            or "source"
        )
        maps: dict[str, dict[str, str]] = {"project": {source_project: _new_id("research")}}

        def add(kind: str, values: list[str], prefix: str) -> None:
            bucket = maps.setdefault(kind, {})
            for value in values:
                if value and value not in bucket:
                    bucket[value] = _new_id(prefix)

        event_ids = [str(item.get("event_id") or "") for item in data.get("events") or []]
        for episode in data.get("episodes") or []:
            event_ids.extend(_strings(episode.get("event_ids")))
        checkpoint = _json_object(data.get("checkpoint"))
        event_ids.extend(_strings(checkpoint.get("source_event_ids")))
        for finding in data.get("findings") or []:
            event_ids.extend(_strings(finding.get("source_event_ids")))
        add("event", event_ids, "event")
        add("episode", [str(item.get("episode_id") or "") for item in data.get("episodes") or []], "episode")
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        add("checkpoint", [checkpoint_id] if checkpoint_id else [], "checkpoint")
        add("finding", [str(item.get("finding_id") or "") for item in data.get("findings") or []], "finding")
        add("finding_version", [str(item.get("version_id") or "") for item in data.get("finding_versions") or []], "finding-version")
        add("change", [str(item.get("change_id") or "") for item in data.get("changes") or []], "change")
        add("conflict", [str(item.get("conflict_id") or "") for item in data.get("conflicts") or []], "conflict")
        add("resolution", [str(item.get("resolution_id") or "") for item in data.get("resolutions") or []], "resolution")
        add("knowledge_link", [str(item.get("link_id") or "") for item in data.get("knowledge_links") or []], "knowledge-link")

        evidence_ids = [str(item.get("evidence_id") or "") for item in data.get("evidence") or []]
        for finding in data.get("findings") or []:
            evidence_ids.extend(item for item in _strings(finding.get("evidence_ids")) if not item.startswith("event:"))
        for conflict in data.get("conflicts") or []:
            evidence_ids.extend(item for item in _strings(conflict.get("original_evidence_ids")) if not item.startswith("event:"))
            evidence_ids.extend(item for item in _strings(conflict.get("challenging_evidence_ids")) if not item.startswith("event:"))
        evidence_ids.extend(
            str(item.get("object_id") or "")
            for item in data.get("knowledge_links") or []
            if item.get("object_type") == "evidence"
            and not str(item.get("object_id") or "").startswith("event:")
        )
        add("evidence", evidence_ids, "ev")

        wiki_pages = data.get("wiki_pages") or []
        page_ids = [str(item.get("page_id") or "") for item in wiki_pages]
        page_ids.extend(
            str(item.get("object_id") or "")
            for item in data.get("knowledge_links") or []
            if item.get("object_type") == "wiki_page"
        )
        add("wiki_page", page_ids, "wiki")
        claim_ids = [
            str(claim.get("claim_id") or "")
            for page in wiki_pages
            for claim in _json_list(page.get("claims"))
            if isinstance(claim, dict)
        ]
        claim_ids.extend(
            str(item.get("object_id") or "")
            for item in data.get("knowledge_links") or []
            if item.get("object_type") == "wiki_claim"
        )
        add("wiki_claim", claim_ids, "claim")

        papers = _json_list(_json_object(data.get("papers_manifest")).get("papers"))
        portable_records = [
            _json_object(item.get("record"))
            for item in data.get("paper_records") or []
            if isinstance(item, dict)
        ]
        paper_ids = [str(item.get("doc_id") or "") for item in papers if isinstance(item, dict)]
        paper_ids.extend(
            str(item.get("object_id") or "")
            for item in data.get("knowledge_links") or []
            if item.get("object_type") == "paper"
        )
        add("paper", paper_ids, "paper")
        passages = [
            str(passage.get("passage_id") or "")
            for paper in [*papers, *portable_records]
            if isinstance(paper, dict)
            for passage in _json_list(paper.get("l1_passage_summaries"))
            if isinstance(passage, dict)
        ]
        passages.extend(
            str(item.get("object_id") or "")
            for item in data.get("knowledge_links") or []
            if item.get("object_type") == "passage"
        )
        add("passage", passages, "passage")
        add(
            "paper_chunk",
            [
                str(item.get("chunk_id") or "")
                for record in portable_records
                for item in _json_list(record.get("l2_chunks"))
                if isinstance(item, dict)
            ],
            "paper-chunk",
        )
        add(
            "paper_fact",
            [
                str(item.get("fact_id") or "")
                for record in portable_records
                for item in _json_list(record.get("l2_facts"))
                if isinstance(item, dict)
            ],
            "paper-fact",
        )
        add(
            "paper_anchor",
            [
                str(item.get("anchor_id") or "")
                for record in portable_records
                for item in _json_list(record.get("evidence_anchors"))
                if isinstance(item, dict)
            ],
            "paper-anchor",
        )
        add(
            "paper_table",
            [
                str(item.get("table_id") or "")
                for record in portable_records
                for item in _json_list(record.get("structured_tables"))
                if isinstance(item, dict)
            ],
            "paper-table",
        )
        add(
            "paper_figure",
            [
                str(item.get("figure_id") or "")
                for record in portable_records
                for item in _json_list(record.get("figure_descriptions"))
                if isinstance(item, dict)
            ],
            "paper-figure",
        )
        seen_paper_object_ids: set[str] = set()
        for kind in ("paper_chunk", "paper_fact", "paper_anchor", "paper_table", "paper_figure"):
            bucket = maps.get(kind, {})
            maps[kind] = {
                source_id: local_id
                for source_id, local_id in bucket.items()
                if source_id not in seen_paper_object_ids
            }
            seen_paper_object_ids.update(maps[kind])
        return maps

    def _build_mappings_v2(
        self,
        data: dict[str, Any],
    ) -> dict[str, dict[str, str]]:
        maps: dict[str, dict[str, str]] = {}

        def add(kind: str, values: list[str], prefix: str) -> None:
            bucket = maps.setdefault(kind, {})
            for value in values:
                normalized = str(value or "").strip()
                if normalized and normalized not in bucket:
                    bucket[normalized] = _new_id(prefix)

        add(
            "evidence",
            [str(item.get("evidence_id") or "") for item in data.get("evidence") or []],
            "ev",
        )
        wiki_pages = list(data.get("wiki_pages") or [])
        add(
            "wiki_page",
            [str(item.get("page_id") or "") for item in wiki_pages],
            "wiki",
        )
        add(
            "wiki_claim",
            [
                str(claim.get("claim_id") or "")
                for page in wiki_pages
                for claim in _json_list(page.get("claims"))
                if isinstance(claim, dict)
            ],
            "claim",
        )
        paper_records = [
            _json_object(item.get("record"))
            for item in data.get("paper_records") or []
            if isinstance(item, dict)
        ]
        papers = [
            _json_object(item)
            for item in _json_list(
                _json_object(data.get("papers_manifest")).get("papers")
            )
            if isinstance(item, dict)
        ]
        add(
            "paper",
            [str(item.get("doc_id") or "") for item in papers],
            "paper",
        )
        layer_specs = (
            ("paper_chunk", "l2_chunks", "chunk_id", "paper-chunk"),
            ("paper_fact", "l2_facts", "fact_id", "paper-fact"),
            ("paper_anchor", "evidence_anchors", "anchor_id", "paper-anchor"),
            ("paper_table", "structured_tables", "table_id", "paper-table"),
            ("paper_figure", "figure_descriptions", "figure_id", "paper-figure"),
            ("passage", "l1_passage_summaries", "passage_id", "passage"),
        )
        seen_layer_ids: set[str] = set()
        for kind, collection, field, prefix in layer_specs:
            values = [
                str(item.get(field) or "")
                for record in paper_records
                for item in _json_list(record.get(collection))
                if isinstance(item, dict)
                and str(item.get(field) or "") not in seen_layer_ids
            ]
            add(kind, values, prefix)
            seen_layer_ids.update(maps.get(kind, {}))
        return maps

    @staticmethod
    def _map_string(value: str, maps: dict[str, dict[str, str]]) -> str:
        if value.startswith("event:"):
            target = maps.get("event", {}).get(value.split(":", 1)[1])
            return f"event:{target}" if target else value
        for kind in (
            "project", "event", "episode", "checkpoint", "finding", "finding_version",
            "change", "conflict", "resolution", "knowledge_link", "evidence",
            "wiki_page", "wiki_claim", "paper", "passage", "paper_chunk",
            "paper_fact", "paper_anchor", "paper_table", "paper_figure",
        ):
            if value in maps.get(kind, {}):
                return maps[kind][value]
        return value

    @classmethod
    def _map_value(cls, value: Any, maps: dict[str, dict[str, str]], depth: int = 0) -> Any:
        if depth > 20:
            return None
        if isinstance(value, str):
            return cls._map_string(value, maps)
        if isinstance(value, dict):
            return {str(key)[:160]: cls._map_value(item, maps, depth + 1) for key, item in list(value.items())[:1000]}
        if isinstance(value, list):
            return [cls._map_value(item, maps, depth + 1) for item in value[:10000]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:20000]

    def _stage_evidence(
        self,
        data: dict[str, Any],
        maps: dict[str, dict[str, str]],
        pack_id: str,
        now: str,
    ):
        records = {str(item.get("evidence_id") or ""): item for item in data.get("evidence") or []}
        connection = EvidenceStore._connect(self.workdir.as_posix())
        connection.execute("BEGIN IMMEDIATE")
        hashes: dict[str, str] = {}
        for source_id, local_id in maps.get("evidence", {}).items():
            raw = dict(records.get(source_id) or {})
            content = str(raw.get("content") or f"Reference-only Evidence imported from {pack_id}: {source_id}")[:200000]
            metadata = self._map_value(_json_object(raw.get("metadata")), maps)
            metadata = {
                **_json_object(metadata),
                "imported_from_pack_id": pack_id,
                "source_evidence_id": source_id,
                "source_type_before_import": str(raw.get("source_type") or ""),
            }
            record = EvidenceRecord.from_dict(
                {
                    **self._map_value(raw, maps),
                    "evidence_id": local_id,
                    "content": content,
                    "source_type": "researchpack",
                    "source_id": pack_id,
                    "source_path": "",
                    "metadata": metadata,
                    "created_at": now,
                    "updated_at": now,
                    "embedding": [],
                    "embedding_version": "",
                    "status": raw.get("status") if raw else "stale",
                }
            )
            EvidenceStore._normalize_record(record)
            EvidenceStore._write_record(connection, record)
            hashes[local_id] = record.content_hash
        return connection, hashes

    def _wiki_store(self):
        from ..server.paper_wiki_loader import paper_wiki_module

        module = paper_wiki_module()
        library_root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
        return module.PaperWikiStore(
            library_root=library_root,
            workdir=self.workdir.as_posix(),
            vector_search_enabled=False,
        )

    def _stage_wiki(self, data: dict[str, Any], maps: dict[str, dict[str, str]], pack_id: str):
        pages = data.get("wiki_pages") or []
        if not pages:
            return None, None, []
        store = self._wiki_store()
        connection = store._connect()
        connection.execute("BEGIN IMMEDIATE")
        created: list[str] = []
        for source in pages:
            source_id = str(source.get("page_id") or "")
            local_id = maps.get("wiki_page", {}).get(source_id)
            if not local_id:
                continue
            payload = self._map_value(dict(source), maps)
            payload.update(
                {
                    "page_id": local_id,
                    "canonical_key": f"researchpack:{pack_id}:{local_id}",
                    "origin": "researchpack_import",
                    "status": str(source.get("status") or "canonical"),
                }
            )
            store._write_page_in_connection(
                connection,
                payload,
                actor="researchpack-import",
                reason=f"imported from {pack_id}",
                origin="researchpack_import",
            )
            created.append(local_id)
        return store, connection, created

    def _paper_store(self):
        from ..server.paper_library_loader import paper_library_module

        module = paper_library_module()
        library_root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
        return module.PaperLibraryStore(
            library_root=library_root,
            workdir=self.workdir.as_posix(),
        )

    def _stage_papers(
        self,
        data: dict[str, Any],
        maps: dict[str, dict[str, str]],
        pack_id: str,
    ):
        created: list[str] = []
        papers = _json_list(_json_object(data.get("papers_manifest")).get("papers"))
        if not papers:
            return None, created
        store = self._paper_store()
        try:
            portable_by_id = {
                str(_json_object(item.get("manifest")).get("doc_id") or ""): item
                for item in data.get("paper_records") or []
                if isinstance(item, dict)
                and str(_json_object(item.get("manifest")).get("doc_id") or "")
            }
            for raw_item in papers:
                raw = _json_object(raw_item)
                source_id = str(raw.get("doc_id") or "")
                local_id = maps.get("paper", {}).get(source_id)
                if not local_id:
                    continue
                portable_item = portable_by_id.get(source_id)
                if portable_item is not None:
                    source_record = _json_object(portable_item.get("record"))
                    mapped_record = _json_object(self._map_value(source_record, maps))
                    mapped_record.update(
                        {
                            "doc_id": local_id,
                            "distribution": str(raw.get("distribution") or "derived_partial"),
                            "available_layers": list(raw.get("available_layers") or []),
                            "missing_layers": list(raw.get("missing_layers") or []),
                            "source_pdf_missing": True,
                        }
                    )
                    store.import_portable_record(
                        mapped_record,
                        pack_id=pack_id,
                        source_doc_id=source_id,
                        record_hash=str(raw.get("record_hash") or ""),
                    )
                else:
                    passages = self._map_value(_json_list(raw.get("l1_passage_summaries")), maps)
                    record = {
                        "doc_id": local_id,
                        "metadata": {
                            **_json_object(self._map_value(_json_object(raw.get("metadata")), maps)),
                            "title": str(raw.get("title") or source_id),
                            "imported_from_pack_id": pack_id,
                            "source_doc_id": source_id,
                            "distribution": "reference_only",
                        },
                        "pdf_path": "",
                        "l1_passage_summaries": passages,
                        "evidence_anchors": self._map_value(_json_list(raw.get("evidence_anchors")), maps),
                        "imported_reference_only": True,
                    }
                    store.import_reference_record(
                        record,
                        pack_id=pack_id,
                        source_doc_id=source_id,
                        record_hash=str(raw.get("record_hash") or ""),
                    )
                created.append(local_id)
            if portable_by_id:
                store._rebuild_chunk_index()
        except BaseException:
            for paper_id in created:
                try:
                    store.remove_imported_record(paper_id, pack_id=pack_id)
                except Exception:
                    pass
            try:
                store._rebuild_chunk_index()
            except Exception:
                pass
            raise
        return store, created

    def _insert_research(
        self,
        connection,
        audit: ResearchPackImport,
        data: dict[str, Any],
        maps: dict[str, dict[str, str]],
        research_id: str,
        title: str,
        now: str,
    ) -> None:
        source_project = _json_object(data.get("project"))
        manifest_research = _json_object(audit.source_manifest.get("research"))
        project_title = _bounded_text(title or source_project.get("title") or manifest_research.get("title") or "导入的研究课题", 160)
        description = _bounded_text(
            source_project.get("description") or manifest_research.get("summary") or f"Imported from {audit.pack_id}",
            4000,
        )
        connection.execute(
            "INSERT INTO research_projects(research_id,schema_version,tenant_id,user_id,workspace_id,"
            "title,description,status,origin_candidate_ids_json,created_at,updated_at,archived_at,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                research_id, 1, self.tenant_id, self.user_id, self.workspace_id, project_title,
                description, "active", "[]", now, now, "",
                sha256_text(canonical_json({"import": audit.import_id, "project": research_id})),
            ),
        )
        events_by_id = {str(item.get("event_id") or ""): dict(item) for item in data.get("events") or []}
        for source_id, local_id in maps.get("event", {}).items():
            raw = events_by_id.get(source_id, {})
            occurred_at = _clean_time(raw.get("occurred_at"), now)
            content_hash = str(raw.get("content_hash") or sha256_text(canonical_json(raw)))
            payload = self._map_value(_json_object(raw.get("payload")), maps)
            connection.execute(
                "INSERT INTO research_events(event_id,schema_version,tenant_id,user_id,workspace_id,"
                "session_id,turn_id,trace_id,parent_event_id,actor_type,actor_id,event_type,source_kind,"
                "source_id,source_ref,occurred_at,ingested_at,payload_json,content_hash,privacy_state,"
                "ingest_version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, 1, self.tenant_id, self.user_id, self.workspace_id, "", "", "", "",
                    str(raw.get("actor_type") or "system")[:40], "",
                    str(raw.get("event_type") or "researchpack.imported_event")[:120],
                    "researchpack", audit.pack_id, "", occurred_at, now,
                    canonical_json({"imported_from_pack_id": audit.pack_id, "source_event_id": source_id, **_json_object(payload)}),
                    content_hash, "private", "researchpack-import-1",
                    sha256_text(canonical_json({"import": audit.import_id, "event": source_id})),
                ),
            )
            link_id = _new_id("rel")
            fingerprint = sha256_text(canonical_json({"event": local_id, "research": research_id, "import": audit.import_id}))
            connection.execute(
                "INSERT INTO research_event_links(link_id,schema_version,tenant_id,user_id,workspace_id,"
                "event_id,candidate_id,research_id,state,relevance,decision_source,confidence,reason_code,"
                "reason_detail_json,shadow,decision_version,run_id,correction_id,semantic_fingerprint,"
                "idempotency_key,decided_at,expires_at,superseded_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    link_id, 1, self.tenant_id, self.user_id, self.workspace_id, local_id, "", research_id,
                    "included", "research", "import", 1.0, "researchpack_import",
                    canonical_json({"pack_id": audit.pack_id, "source_event_id": source_id}), 0,
                    "researchpack-import-1", audit.import_id, "", fingerprint,
                    sha256_text(f"{audit.import_id}:{source_id}:link"), now, "", "",
                ),
            )

        for index, raw in enumerate(data.get("episodes") or []):
            source_id = str(raw.get("episode_id") or "")
            local_id = maps.get("episode", {}).get(source_id)
            if not local_id:
                continue
            mapped = self._map_value(raw, maps)
            state = str(raw.get("state") or "active")
            if state not in EPISODE_STATES:
                state = "active"
            outcome = str(raw.get("outcome_status") or "unknown")
            if outcome not in EPISODE_OUTCOMES:
                outcome = "unknown"
            connection.execute(
                "INSERT INTO research_episodes(episode_id,episode_key,schema_version,tenant_id,user_id,"
                "workspace_id,research_id,revision,title,goal_json,summary_json,outcome_status,event_ids_json,"
                "session_ids_json,trace_ids_json,artifact_refs_json,tool_calls_json,decisions_json,"
                "failed_paths_json,open_questions_json,next_actions_json,started_at,ended_at,boundary_reason,"
                "derivation_version,source_digest,state,supersedes_episode_id,created_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, f"import:{research_id}:{index}:{local_id}", 1, self.tenant_id, self.user_id,
                    self.workspace_id, research_id, max(1, int(raw.get("revision") or 1)),
                    _bounded_text(raw.get("title") or "Imported episode", 500),
                    canonical_json(_json_object(mapped.get("goal"))), canonical_json(_json_object(mapped.get("summary"))),
                    outcome, canonical_json(_strings(mapped.get("event_ids"))), "[]", "[]",
                    canonical_json(_json_list(mapped.get("artifact_refs"))), canonical_json(_json_list(mapped.get("tool_calls"))),
                    canonical_json(_json_list(mapped.get("decisions"))), canonical_json(_json_list(mapped.get("failed_paths"))),
                    canonical_json(_json_list(mapped.get("open_questions"))), canonical_json(_json_list(mapped.get("next_actions"))),
                    _clean_time(raw.get("started_at"), now), _clean_time(raw.get("ended_at"), now),
                    _bounded_text(raw.get("boundary_reason") or "researchpack_import", 120), "researchpack-import-1",
                    str(raw.get("source_digest") or sha256_text(canonical_json(raw))), state,
                    str(mapped.get("supersedes_episode_id") or ""), now,
                    sha256_text(canonical_json({"import": audit.import_id, "episode": source_id})),
                ),
            )

        checkpoint = _json_object(data.get("checkpoint"))
        source_checkpoint = str(checkpoint.get("checkpoint_id") or "")
        if source_checkpoint and source_checkpoint in maps.get("checkpoint", {}):
            mapped = self._map_value(checkpoint, maps)
            local_checkpoint = maps["checkpoint"][source_checkpoint]
            connection.execute(
                "INSERT INTO research_checkpoints(checkpoint_id,schema_version,tenant_id,user_id,workspace_id,"
                "research_id,revision,research_goal,current_state,confirmed_facts_json,working_hypotheses_json,"
                "recent_progress_json,decisions_json,failed_paths_json,known_conflicts_json,important_artifacts_json,"
                "open_questions_json,next_actions_json,source_episode_ids_json,source_event_ids_json,generated_at,"
                "generator_version,source_digest,state,supersedes_checkpoint_id,token_estimate,budget_status,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_checkpoint, 1, self.tenant_id, self.user_id, self.workspace_id, research_id,
                    max(1, int(checkpoint.get("revision") or 1)), _bounded_text(checkpoint.get("research_goal"), 4000),
                    _bounded_text(checkpoint.get("current_state"), 12000),
                    *[canonical_json(_json_list(mapped.get(key))) for key in (
                        "confirmed_facts", "working_hypotheses", "recent_progress", "decisions", "failed_paths",
                        "known_conflicts", "important_artifacts", "open_questions", "next_actions",
                        "source_episode_ids", "source_event_ids",
                    )],
                    now, "researchpack-import-1", str(checkpoint.get("source_digest") or sha256_text(canonical_json(checkpoint))),
                    "active", "", max(0, int(checkpoint.get("token_estimate") or 0)),
                    str(checkpoint.get("budget_status") or "within_budget")[:40],
                    sha256_text(canonical_json({"import": audit.import_id, "checkpoint": source_checkpoint})),
                ),
            )

        for raw in data.get("findings") or []:
            source_id = str(raw.get("finding_id") or "")
            local_id = maps.get("finding", {}).get(source_id)
            if not local_id:
                continue
            mapped = self._map_value(raw, maps)
            status_value = str(raw.get("status") or "active")
            provenance = str(raw.get("provenance") or "inferred")
            connection.execute(
                "INSERT INTO research_findings(finding_id,schema_version,tenant_id,user_id,workspace_id,research_id,"
                "episode_id,statement,scope_json,status,provenance,evidence_ids_json,source_event_ids_json,"
                "current_revision,created_by,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, 1, self.tenant_id, self.user_id, self.workspace_id, research_id,
                    str(mapped.get("episode_id") or ""), _bounded_text(raw.get("statement"), 12000),
                    canonical_json(_json_object(mapped.get("scope"))), status_value if status_value in FINDING_STATUSES else "active",
                    provenance if provenance in PROVENANCE_TYPES else "inferred", canonical_json(_strings(mapped.get("evidence_ids"))),
                    canonical_json(_strings(mapped.get("source_event_ids"))), max(1, int(raw.get("current_revision") or 1)),
                    "researchpack-import", _clean_time(raw.get("created_at"), now), now,
                    sha256_text(canonical_json({"import": audit.import_id, "finding": source_id})),
                ),
            )

        for raw in data.get("finding_versions") or []:
            source_id = str(raw.get("version_id") or "")
            local_id = maps.get("finding_version", {}).get(source_id)
            mapped_finding = maps.get("finding", {}).get(str(raw.get("finding_id") or ""))
            if not local_id or not mapped_finding:
                continue
            mapped = self._map_value(raw, maps)
            connection.execute(
                "INSERT INTO research_finding_versions(version_id,finding_id,tenant_id,user_id,workspace_id,research_id,"
                "revision,statement,scope_json,status,provenance,evidence_ids_json,source_event_ids_json,reason,"
                "changed_by,occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, mapped_finding, self.tenant_id, self.user_id, self.workspace_id, research_id,
                    max(1, int(raw.get("revision") or 1)), _bounded_text(raw.get("statement"), 12000),
                    canonical_json(_json_object(mapped.get("scope"))), str(raw.get("status") or "active")[:40],
                    str(raw.get("provenance") or "inferred")[:40], canonical_json(_strings(mapped.get("evidence_ids"))),
                    canonical_json(_strings(mapped.get("source_event_ids"))), _bounded_text(raw.get("reason"), 4000),
                    "researchpack-import", _clean_time(raw.get("occurred_at"), now),
                    sha256_text(canonical_json({"import": audit.import_id, "finding_version": source_id})),
                ),
            )

        for raw in data.get("changes") or []:
            source_id = str(raw.get("change_id") or "")
            local_id = maps.get("change", {}).get(source_id)
            if not local_id:
                continue
            mapped = self._map_value(raw, maps)
            connection.execute(
                "INSERT INTO research_change_events(change_id,schema_version,tenant_id,user_id,workspace_id,research_id,"
                "change_type,subject_type,subject_id,created_ids_json,actor_type,actor_id,reason,evidence_ids_json,"
                "occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, 1, self.tenant_id, self.user_id, self.workspace_id, research_id,
                    str(raw.get("change_type") or "imported")[:80], str(raw.get("subject_type") or "project")[:40],
                    str(mapped.get("subject_id") or research_id), canonical_json(_strings(mapped.get("created_ids") or mapped.get("affected_ids"))),
                    "import", "", _bounded_text(raw.get("reason") or f"Imported from {audit.pack_id}", 4000),
                    canonical_json(_strings(mapped.get("evidence_ids"))), _clean_time(raw.get("occurred_at"), now),
                    sha256_text(canonical_json({"import": audit.import_id, "change": source_id})),
                ),
            )

        for raw in data.get("conflicts") or []:
            source_id = str(raw.get("conflict_id") or "")
            local_id = maps.get("conflict", {}).get(source_id)
            if not local_id:
                continue
            mapped = self._map_value(raw, maps)
            conflict_type = str(raw.get("conflict_type") or "interpretation_dispute")
            status_value = str(raw.get("status") or "open")
            connection.execute(
                "INSERT INTO research_conflicts(conflict_id,schema_version,tenant_id,user_id,workspace_id,research_id,"
                "conflict_type,status,original_refs_json,challenging_refs_json,original_evidence_ids_json,"
                "challenging_evidence_ids_json,summary,detected_by,detected_at,current_resolution_id,updated_at,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, 1, self.tenant_id, self.user_id, self.workspace_id, research_id,
                    conflict_type if conflict_type in CONFLICT_TYPES else "interpretation_dispute",
                    status_value if status_value in CONFLICT_STATUSES else "open",
                    canonical_json(_strings(mapped.get("original_refs"))), canonical_json(_strings(mapped.get("challenging_refs"))),
                    canonical_json(_strings(mapped.get("original_evidence_ids"))), canonical_json(_strings(mapped.get("challenging_evidence_ids"))),
                    _bounded_text(raw.get("summary"), 12000), "researchpack-import", _clean_time(raw.get("detected_at"), now),
                    str(mapped.get("current_resolution_id") or ""), now,
                    sha256_text(canonical_json({"import": audit.import_id, "conflict": source_id})),
                ),
            )

        for raw in data.get("resolutions") or []:
            source_id = str(raw.get("resolution_id") or "")
            local_id = maps.get("resolution", {}).get(source_id)
            conflict_id = maps.get("conflict", {}).get(str(raw.get("conflict_id") or ""))
            if not local_id or not conflict_id:
                continue
            mapped = self._map_value(raw, maps)
            result = str(raw.get("result") or "inconclusive")
            connection.execute(
                "INSERT INTO research_resolutions(resolution_id,schema_version,tenant_id,user_id,workspace_id,"
                "conflict_id,research_id,revision,result,actor_id,reason,evidence_ids_json,scope_json,resolved_at,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, 1, self.tenant_id, self.user_id, self.workspace_id, conflict_id, research_id,
                    max(1, int(raw.get("revision") or 1)), result if result in RESOLUTION_RESULTS else "inconclusive",
                    "researchpack-import", _bounded_text(raw.get("reason"), 12000), canonical_json(_strings(mapped.get("evidence_ids"))),
                    canonical_json(_json_object(mapped.get("scope"))), _clean_time(raw.get("resolved_at"), now),
                    sha256_text(canonical_json({"import": audit.import_id, "resolution": source_id})),
                ),
            )

        for raw in data.get("knowledge_links") or []:
            source_id = str(raw.get("link_id") or "")
            local_id = maps.get("knowledge_link", {}).get(source_id)
            if not local_id:
                continue
            mapped = self._map_value(raw, maps)
            subject_type = str(raw.get("subject_type") or "project")
            object_type = str(raw.get("object_type") or "evidence")
            if subject_type not in KNOWLEDGE_SUBJECT_TYPES or object_type not in KNOWLEDGE_OBJECT_TYPES:
                continue
            relation = str(raw.get("relation") or "references")
            side = str(raw.get("side") or "neutral")
            state_value = str(raw.get("state") or "active")
            subject_id = str(mapped.get("subject_id") or "")
            object_id = str(mapped.get("object_id") or "")
            if subject_type == "project":
                subject_id = research_id
            if not subject_id or not object_id:
                continue
            link_key = sha256_text(canonical_json({"research": research_id, "subject": [subject_type, subject_id], "object": [object_type, object_id], "relation": relation, "side": side, "import": audit.import_id}))
            connection.execute(
                "INSERT INTO research_knowledge_links(link_id,link_key,schema_version,tenant_id,user_id,workspace_id,"
                "research_id,subject_type,subject_id,object_type,object_id,relation,side,state,source_revision,"
                "source_hash,metadata_json,discovered_by,revision,created_at,updated_at,supersedes_link_id,"
                "superseded_by,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    local_id, link_key, 1, self.tenant_id, self.user_id, self.workspace_id, research_id,
                    subject_type, subject_id, object_type, object_id,
                    relation if relation in KNOWLEDGE_RELATIONS else "references",
                    side if side in KNOWLEDGE_SIDES else "neutral",
                    state_value if state_value in KNOWLEDGE_STATES else "active",
                    max(0, int(raw.get("source_revision") or 0)), "",
                    canonical_json({**_json_object(mapped.get("metadata")), "imported_from_pack_id": audit.pack_id, "source_link_id": source_id}),
                    "researchpack-import", max(1, int(raw.get("revision") or 1)),
                    _clean_time(raw.get("created_at"), now), now,
                    str(mapped.get("supersedes_link_id") or ""), str(mapped.get("superseded_by") or ""),
                    sha256_text(canonical_json({"import": audit.import_id, "knowledge_link": source_id})),
                ),
            )

    def _insert_mappings(
        self,
        connection,
        import_id: str,
        pack_id: str,
        maps: dict[str, dict[str, str]],
        data: dict[str, Any],
        now: str,
    ) -> None:
        changes = {str(item.get("change_id") or ""): item for item in data.get("changes") or []}
        for object_type, values in maps.items():
            for source_id, local_id in values.items():
                metadata: dict[str, Any] = {"origin_pack_id": pack_id}
                if object_type == "change":
                    raw = changes.get(source_id, {})
                    metadata.update(
                        {
                            "origin_pack_id": raw.get("origin_pack_id") or pack_id,
                            "parent_change_id": self._map_string(str(raw.get("parent_change_id") or ""), maps),
                        }
                    )
                connection.execute(
                    "INSERT INTO research_pack_id_mappings(mapping_id,import_id,object_type,source_object_id,"
                    "local_object_id,content_hash,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        _new_id("mapping"), import_id, object_type, source_id, local_id,
                        sha256_text(canonical_json({"type": object_type, "source": source_id, "local": local_id})),
                        canonical_json(metadata), now,
                    ),
                )


@dataclass
class ResearchPackRecoveryReport:
    inspected_seen: int = 0
    expired_inspections: int = 0
    interrupted_imports: int = 0
    staged_files_removed: int = 0
    evidence_removed: int = 0
    wiki_pages_removed: int = 0
    papers_removed: int = 0
    preserved_assets: int = 0
    preserved_projects: int = 0
    untracked_legacy_imports: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchPackRecoveryManager:
    """Recover expired inspections and interrupted cross-Store imports."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str,
        settings: ResearchSettings | None = None,
    ):
        self.owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        self.workdir = Path(workdir).expanduser().resolve(strict=False)
        self.settings = settings or load_research_settings()

    def _remove_staged(self, item: ResearchPackImport, report: ResearchPackRecoveryReport) -> None:
        staged = ResearchPackImportStore.staged_file(
            item,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if staged is not None:
            staged.unlink(missing_ok=True)
            report.staged_files_removed += 1

    def _project_exists(self, research_id: str) -> bool:
        if not research_id:
            return False
        connection = ResearchEventStore._connect(self.workdir.as_posix(), settings=self.settings)
        try:
            row = connection.execute(
                "SELECT 1 FROM research_projects WHERE research_id=? AND tenant_id=? "
                "AND user_id=? AND workspace_id=?",
                (research_id, self.owner["tenant_id"], self.owner["user_id"], self.owner["workspace_id"]),
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def _asset_exists(self, asset_id: str) -> bool:
        if not asset_id:
            return False
        return (
            ResearchPackAssetStore.get_asset(
                asset_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            is not None
        )

    def _cleanup_evidence(self, ids: list[str]) -> int:
        values = list(dict.fromkeys(str(item or "").strip() for item in ids if str(item or "").strip()))
        if not values:
            return 0
        connection = EvidenceStore._connect(self.workdir.as_posix())
        try:
            connection.execute("BEGIN IMMEDIATE")
            removed = 0
            for evidence_id in values:
                cursor = connection.execute(
                    "DELETE FROM evidence_records WHERE evidence_id=? AND source_type='researchpack'",
                    (evidence_id,),
                )
                removed += max(0, int(cursor.rowcount or 0))
            connection.execute("COMMIT")
            return removed
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def _wiki_store(self):
        from ..server.paper_wiki_loader import paper_wiki_module

        module = paper_wiki_module()
        library_root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
        return module.PaperWikiStore(
            library_root=library_root,
            workdir=self.workdir.as_posix(),
            vector_search_enabled=False,
        )

    def _cleanup_wiki(self, ids: list[str]) -> int:
        values = list(dict.fromkeys(str(item or "").strip() for item in ids if str(item or "").strip()))
        if not values:
            return 0
        store = self._wiki_store()
        connection = store._connect()
        materialized: list[Path] = []
        removed = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            for page_id in values:
                row = connection.execute(
                    "SELECT markdown_path,origin FROM pages WHERE page_id=?",
                    (page_id,),
                ).fetchone()
                if row is None or str(row["origin"] or "") != "researchpack_import":
                    continue
                raw_path = Path(str(row["markdown_path"] or ""))
                if str(raw_path):
                    materialized.append(raw_path if raw_path.is_absolute() else self.workdir / raw_path)
                connection.execute("DELETE FROM revisions WHERE page_id=?", (page_id,))
                connection.execute("DELETE FROM events WHERE page_id=?", (page_id,))
                connection.execute("DELETE FROM dependencies WHERE page_id=?", (page_id,))
                connection.execute("DELETE FROM pages WHERE page_id=?", (page_id,))
                try:
                    connection.execute("DELETE FROM page_fts WHERE page_id=?", (page_id,))
                except Exception:
                    pass
                removed += 1
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()
        for path in materialized:
            try:
                path.resolve(strict=False).relative_to(store.pages_root.resolve(strict=False))
            except ValueError:
                continue
            path.unlink(missing_ok=True)
        try:
            store._materialize_indexes()
        except Exception:
            pass
        return removed

    def _cleanup_papers(self, ids: list[str]) -> int:
        from ..server.paper_library_loader import paper_library_module

        module = paper_library_module()
        library_root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
        store = module.PaperLibraryStore(
            library_root=library_root,
            workdir=self.workdir.as_posix(),
        )
        removed = 0
        for paper_id in dict.fromkeys(str(item or "").strip() for item in ids if str(item or "").strip()):
            if store.remove_imported_record(paper_id):
                removed += 1
        if removed:
            try:
                store._rebuild_chunk_index()
            except Exception:
                pass
        return removed

    def run(self, *, recover_importing: bool = True) -> ResearchPackRecoveryReport:
        report = ResearchPackRecoveryReport()
        if not self.settings.research_pack_import_recovery_enabled:
            return report
        items = ResearchPackImportStore.list_recoverable(
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        for item in items:
            try:
                if item.status == "inspected":
                    report.inspected_seen += 1
                    if ResearchPackImportStore.is_expired(item):
                        self._remove_staged(item, report)
                        ResearchPackImportStore.mark_recovered(
                            item.import_id,
                            status="expired",
                            error="inspection expired and staged archive was cleaned",
                            workdir=self.workdir.as_posix(),
                            settings=self.settings,
                        )
                        report.expired_inspections += 1
                    continue
                if item.status != "importing" or not recover_importing:
                    continue
                report.interrupted_imports += 1
                if self._asset_exists(item.asset_id):
                    report.preserved_assets += 1
                    ResearchPackImportStore.mark_recovered(
                        item.import_id,
                        status="failed",
                        error="recovery preserved an unexpected visible Pack Asset for manual review",
                        workdir=self.workdir.as_posix(),
                        settings=self.settings,
                    )
                    continue
                if self._project_exists(item.local_research_id):
                    # Never delete a visible project automatically. This state should be
                    # impossible for v8 because audit completion shares its transaction.
                    report.preserved_projects += 1
                    ResearchPackImportStore.mark_recovered(
                        item.import_id,
                        status="failed",
                        error="recovery preserved an unexpected visible project for manual review",
                        workdir=self.workdir.as_posix(),
                        settings=self.settings,
                    )
                    continue
                maps = item.id_mapping if isinstance(item.id_mapping, dict) else {}
                if isinstance(maps.get("materialized"), dict):
                    maps = dict(maps["materialized"])
                if not maps:
                    report.untracked_legacy_imports += 1
                report.evidence_removed += self._cleanup_evidence(list((maps.get("evidence") or {}).values()))
                report.wiki_pages_removed += self._cleanup_wiki(list((maps.get("wiki_page") or {}).values()))
                report.papers_removed += self._cleanup_papers(list((maps.get("paper") or {}).values()))
                self._remove_staged(item, report)
                ResearchPackImportStore.mark_recovered(
                    item.import_id,
                    status="failed",
                    error="interrupted import recovered; staged and orphan objects cleaned",
                    workdir=self.workdir.as_posix(),
                    settings=self.settings,
                )
            except Exception as exc:
                report.errors.append(f"{item.import_id}: {str(exc)[:500]}")
        return report
