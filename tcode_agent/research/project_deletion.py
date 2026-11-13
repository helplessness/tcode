"""Permanent, owner-scoped deletion of an archived Research Project.

Project deletion is intentionally different from Session deletion.  Source
messages stay in the private Session, while their ResearchEvent projections are
permanently excluded so deterministic reconciliation cannot recreate the topic.
Only files under explicit managed roots are eligible for physical deletion.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .attribution_models import AttributionCorrectionInput, ResearchEventLinkInput
from .config import ResearchSettings, load_research_settings
from .models import canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore
from .topic_store import ResearchTopicStore


_PATH_KEYS = {
    "path",
    "paths",
    "relative_path",
    "container_path",
    "output_path",
    "artifact_path",
    "artifact_paths",
    "host_artifact_path",
    "source_path",
    "source_ref",
}
_FILE_EVENT_TYPES = {
    "file.uploaded",
    "file.written",
    "file.changed",
    "artifact.created",
    "document.created",
}


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _strings(value: Any) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (value if isinstance(value, list) else [])
            if str(item or "").strip()
        )
    )


def _chunks(values: Iterable[str], size: int = 400) -> Iterable[list[str]]:
    normalized = list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))
    for offset in range(0, len(normalized), size):
        yield normalized[offset : offset + size]


def _path_strings(value: Any, *, key: str = "") -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            normalized = str(child_key or "").strip().lower()
            if normalized in _PATH_KEYS or normalized.endswith(("_path", "_paths")):
                result.extend(_path_strings(child, key=normalized))
            elif isinstance(child, (dict, list, tuple)):
                result.extend(_path_strings(child, key=normalized))
    elif isinstance(value, (list, tuple)):
        for child in value:
            result.extend(_path_strings(child, key=key))
    elif isinstance(value, str) and key:
        text = value.strip()
        if text:
            result.append(text)
    return result


@dataclass
class ResearchProjectDeletionReport:
    deletion_id: str
    research_id: str
    records_deleted: dict[str, int] = field(default_factory=dict)
    private_session_event_count: int = 0
    reassigned_event_count: int = 0
    local_files_removed: list[str] = field(default_factory=list)
    local_files_preserved: list[str] = field(default_factory=list)
    imported_papers_removed: int = 0
    imported_wiki_pages_removed: int = 0
    evidence_records_removed: int = 0
    cleanup_errors: list[str] = field(default_factory=list)

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        value = asdict(self)
        if public:
            value["local_files_removed"] = len(self.local_files_removed)
            value["local_files_preserved"] = len(self.local_files_preserved)
            value["cleanup_error_count"] = len(self.cleanup_errors)
            value.pop("cleanup_errors", None)
        return value


class ResearchProjectDeletionManager:
    """Hard-delete one archived project without deleting its private Session."""

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
        if not all(self.owner.values()):
            raise ValueError("complete owner context is required")
        self.workdir = Path(workdir).expanduser().resolve(strict=False)
        self.settings = settings or load_research_settings()

    @property
    def _owner(self) -> tuple[str, str, str]:
        return (
            self.owner["tenant_id"],
            self.owner["user_id"],
            self.owner["workspace_id"],
        )

    def _candidate_ids(self, connection, project) -> list[str]:
        result = set(project.origin_candidate_ids)
        rows = connection.execute(
            "SELECT candidate_id FROM research_topic_candidates WHERE tenant_id=? AND user_id=? "
            "AND workspace_id=? AND confirmed_research_id=?",
            (*self._owner, project.research_id),
        ).fetchall()
        result.update(str(row["candidate_id"] or "") for row in rows)
        return sorted(item for item in result if item)

    def _project_event_ids(
        self,
        connection,
        research_id: str,
        candidate_ids: list[str],
    ) -> tuple[set[str], dict[str, list[str]], set[str]]:
        event_ids: set[str] = set()
        activity_ids: set[str] = set()
        activity_events: dict[str, list[str]] = {}
        candidate_clause = ""
        params: list[Any] = [*self._owner, research_id]
        if candidate_ids:
            candidate_clause = " OR candidate_id IN (" + ",".join("?" for _ in candidate_ids) + ")"
            params.extend(candidate_ids)
        rows = connection.execute(
            "SELECT event_id FROM research_event_links WHERE tenant_id=? AND user_id=? AND workspace_id=? "
            f"AND (research_id=?{candidate_clause})",
            params,
        ).fetchall()
        event_ids.update(str(row["event_id"] or "") for row in rows)
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = connection.execute(
                "SELECT event_id FROM research_candidate_memberships WHERE tenant_id=? AND user_id=? "
                f"AND workspace_id=? AND candidate_id IN ({placeholders})",
                (*self._owner, *candidate_ids),
            ).fetchall()
            event_ids.update(str(row["event_id"] or "") for row in rows)
        rows = connection.execute(
            "SELECT object_id FROM research_object_scopes WHERE tenant_id=? AND user_id=? "
            "AND workspace_id=? AND research_id=? AND object_type='event'",
            (*self._owner, research_id),
        ).fetchall()
        event_ids.update(str(row["object_id"] or "") for row in rows)

        scoped_activities = {
            str(row["object_id"] or "")
            for row in connection.execute(
                "SELECT object_id FROM research_object_scopes WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND research_id=? AND object_type='activity'",
                (*self._owner, research_id),
            ).fetchall()
        }
        rows = connection.execute(
            "SELECT activity_id,research_id,candidate_id,event_ids_json FROM research_activities "
            "WHERE tenant_id=? AND user_id=? AND workspace_id=?",
            self._owner,
        ).fetchall()
        for row in rows:
            activity_id = str(row["activity_id"] or "")
            ids = _strings(_json(row["event_ids_json"], []))
            belongs = (
                str(row["research_id"] or "") == research_id
                or str(row["candidate_id"] or "") in candidate_ids
                or activity_id in scoped_activities
                or bool(event_ids.intersection(ids))
            )
            if belongs:
                activity_ids.add(activity_id)
                activity_events[activity_id] = ids
                event_ids.update(ids)

        source_columns = (
            ("research_episodes", "event_ids_json"),
            ("research_checkpoints", "source_event_ids_json"),
            ("research_findings", "source_event_ids_json"),
            ("research_finding_versions", "source_event_ids_json"),
        )
        for table, column in source_columns:
            rows = connection.execute(
                f"SELECT {column} FROM {table} WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND research_id=?",
                (*self._owner, research_id),
            ).fetchall()
            for row in rows:
                event_ids.update(_strings(_json(row[column], [])))
        event_ids.discard("")
        return event_ids, activity_events, activity_ids

    def _remaining_event_targets(
        self,
        connection,
        event_ids: set[str],
        research_id: str,
        candidate_ids: list[str],
    ) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        if not event_ids:
            return result
        candidate_projects = {
            str(row["candidate_id"]): str(row["confirmed_research_id"] or "")
            for row in connection.execute(
                "SELECT candidate_id,confirmed_research_id FROM research_topic_candidates "
                "WHERE tenant_id=? AND user_id=? AND workspace_id=?",
                self._owner,
            ).fetchall()
        }
        for chunk in _chunks(event_ids):
            placeholders = ",".join("?" for _ in chunk)
            scopes = connection.execute(
                "SELECT object_id,research_id FROM research_object_scopes WHERE tenant_id=? "
                "AND user_id=? AND workspace_id=? AND object_type='event' AND state='active' "
                f"AND superseded_by='' AND object_id IN ({placeholders}) AND research_id!=?",
                (*self._owner, *chunk, research_id),
            ).fetchall()
            for row in scopes:
                result.setdefault(str(row["object_id"]), ("", str(row["research_id"])))
            memberships = connection.execute(
                "SELECT event_id,candidate_id FROM research_candidate_memberships WHERE tenant_id=? "
                "AND user_id=? AND workspace_id=? AND state='active' AND superseded_by='' "
                f"AND event_id IN ({placeholders})",
                (*self._owner, *chunk),
            ).fetchall()
            for row in memberships:
                candidate_id = str(row["candidate_id"] or "")
                if not candidate_id or candidate_id in candidate_ids:
                    continue
                other_project = candidate_projects.get(candidate_id, "")
                if other_project != research_id:
                    result.setdefault(str(row["event_id"]), (candidate_id, other_project))
            links = connection.execute(
                "SELECT event_id,candidate_id,research_id FROM research_event_links WHERE tenant_id=? "
                "AND user_id=? AND workspace_id=? AND superseded_by='' "
                f"AND event_id IN ({placeholders})",
                (*self._owner, *chunk),
            ).fetchall()
            for row in links:
                other_project = str(row["research_id"] or "")
                other_candidate = str(row["candidate_id"] or "")
                if other_project and other_project != research_id:
                    result.setdefault(str(row["event_id"]), ("", other_project))
                elif other_candidate and other_candidate not in candidate_ids:
                    result.setdefault(
                        str(row["event_id"]),
                        (other_candidate, candidate_projects.get(other_candidate, "")),
                    )
        return result

    def _make_private_tombstones(
        self,
        *,
        deletion_id: str,
        event_ids: set[str],
        remaining_targets: dict[str, tuple[str, str]],
        actor_id: str,
    ) -> tuple[int, int]:
        private_count = 0
        reassigned_count = 0
        for event_id in sorted(event_ids):
            candidate_id, other_research_id = remaining_targets.get(event_id, ("", ""))
            action = "include" if candidate_id or other_research_id else "exclude"
            correction = AttributionCorrectionInput(
                **self.owner,
                event_id=event_id,
                action=action,
                actor_id=actor_id,
                candidate_id=candidate_id,
                research_id=other_research_id,
                reason=(
                    "原课题已永久删除；保留该事件在其他研究中的归属"
                    if action == "include"
                    else "原课题已永久删除；原始内容仅保留在私人 Session"
                ),
                idempotency_key=f"{deletion_id}:{event_id}:{action}",
            )
            stored = ResearchEventStore.append_attribution_correction(
                correction,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            link = ResearchEventLinkInput(
                **self.owner,
                event_id=event_id,
                state="included" if action == "include" else "excluded",
                relevance="research" if action == "include" else "private_only",
                decision_source="user",
                confidence=1.0,
                reason_code="project_deleted_reassigned" if action == "include" else "project_deleted_private",
                run_id=deletion_id,
                candidate_id=candidate_id,
                research_id=other_research_id,
                correction_id=str(stored.get("correction_id") or ""),
                reason_detail={"project_deleted": True},
            ).build()
            ResearchEventStore.append_attribution_link(
                link,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            if action == "include":
                reassigned_count += 1
            else:
                private_count += 1
        return private_count, reassigned_count

    def _managed_path(self, raw: str) -> Path | None:
        text = str(raw or "").strip().replace("\\", "/")
        if not text or "://" in text or text.startswith(("session:", "trace:", "event:")):
            return None
        if text == "/workspace":
            return None
        if text.startswith("/workspace/"):
            candidate = self.workdir / text[len("/workspace/") :]
        else:
            path = Path(text).expanduser()
            candidate = path if path.is_absolute() else self.workdir / path
        candidate = candidate.resolve(strict=False)
        roots = (
            (self.workdir / "upload").resolve(strict=False),
            (self.workdir / ".tcode" / "artifacts").resolve(strict=False),
            self.settings.research_pack_root(self.workdir.as_posix()).resolve(strict=False),
            self.settings.research_pack_import_path(self.workdir.as_posix()).resolve(strict=False),
            (self.workdir / ".tcode" / "research" / "pack-indexes").resolve(strict=False),
        )
        for root in roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate != root:
                return candidate
        return None

    def _event_paths(self, row: Any) -> set[Path]:
        event_type = str(row["event_type"] or "").strip().lower()
        source_kind = str(row["source_kind"] or "").strip().lower()
        if event_type not in _FILE_EVENT_TYPES and source_kind not in {"tool_artifact", "file_upload"}:
            return set()
        payload = _json(row["payload_json"], {})
        raw_values = [str(row["source_ref"] or ""), *_path_strings(payload)]
        return {path for value in raw_values if (path := self._managed_path(value)) is not None}

    def _file_plan(
        self,
        connection,
        *,
        research_id: str,
        exclusive_event_ids: set[str],
        target_import_ids: list[str],
        exclusive_asset_ids: list[str],
    ) -> tuple[list[Path], list[str], dict[str, list[str]], dict[str, str]]:
        target_paths: set[Path] = set()
        preserved: list[str] = []
        if exclusive_event_ids:
            for chunk in _chunks(exclusive_event_ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT event_type,source_kind,source_ref,payload_json FROM research_events "
                    f"WHERE event_id IN ({placeholders}) AND tenant_id=? AND user_id=? AND workspace_id=?",
                    (*chunk, *self._owner),
                ).fetchall()
                for row in rows:
                    target_paths.update(self._event_paths(row))

        shared_paths: set[Path] = set()
        rows = connection.execute(
            "SELECT event_id,event_type,source_kind,source_ref,payload_json FROM research_events "
            "WHERE tenant_id=? AND user_id=? AND workspace_id=?",
            self._owner,
        ).fetchall()
        for row in rows:
            if str(row["event_id"] or "") in exclusive_event_ids:
                continue
            shared_paths.update(self._event_paths(row))
        for path in sorted(target_paths.intersection(shared_paths)):
            preserved.append(self._display_path(path))
        target_paths.difference_update(shared_paths)

        exports = connection.execute(
            "SELECT pack_id,archive_path FROM research_pack_exports WHERE tenant_id=? AND user_id=? "
            "AND workspace_id=? AND research_id=?",
            (*self._owner, research_id),
        ).fetchall()
        pack_root = self.settings.research_pack_root(self.workdir.as_posix())
        for row in exports:
            archive = self._managed_path(str(row["archive_path"] or ""))
            if archive is not None:
                target_paths.add(archive)
            folder = self._managed_path((pack_root / str(row["pack_id"] or "")).as_posix())
            if folder is not None:
                target_paths.add(folder)

        imported_objects: dict[str, list[str]] = {"evidence": [], "wiki_page": [], "paper": []}
        pack_by_import: dict[str, str] = {}
        paper_pack_ids: dict[str, str] = {}
        if target_import_ids:
            placeholders = ",".join("?" for _ in target_import_ids)
            rows = connection.execute(
                "SELECT import_id,pack_id,staged_path,asset_id FROM research_pack_imports "
                f"WHERE import_id IN ({placeholders})",
                tuple(target_import_ids),
            ).fetchall()
            materialized_import_ids: list[str] = []
            for row in rows:
                pack_by_import[str(row["import_id"] or "")] = str(row["pack_id"] or "")
                staged = self._managed_path(str(row["staged_path"] or ""))
                if staged is not None:
                    target_paths.add(staged)
                asset_id = str(row["asset_id"] or "")
                if not asset_id or asset_id in exclusive_asset_ids:
                    materialized_import_ids.append(str(row["import_id"] or ""))
            if materialized_import_ids:
                mapping_placeholders = ",".join("?" for _ in materialized_import_ids)
                mappings = connection.execute(
                    "SELECT import_id,object_type,local_object_id FROM research_pack_id_mappings "
                    f"WHERE import_id IN ({mapping_placeholders})",
                    tuple(materialized_import_ids),
                ).fetchall()
                for row in mappings:
                    kind = str(row["object_type"] or "")
                    if kind in imported_objects:
                        local_id = str(row["local_object_id"] or "")
                        imported_objects[kind].append(local_id)
                        if kind == "paper" and local_id:
                            paper_pack_ids[local_id] = pack_by_import.get(
                                str(row["import_id"] or ""), ""
                            )

        for asset_id in exclusive_asset_ids:
            key = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:32]
            index_path = self._managed_path(
                (self.workdir / ".tcode" / "research" / "pack-indexes" / key).as_posix()
            )
            if index_path is not None:
                target_paths.add(index_path)
        return sorted(target_paths), sorted(set(preserved)), imported_objects, paper_pack_ids

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve(strict=False).relative_to(self.workdir).as_posix()
        except ValueError:
            return f"受管目录/{path.name}"

    def _stage_files(self, paths: list[Path], deletion_id: str) -> tuple[Path, list[tuple[Path, Path]]]:
        stage_root = self.settings.database_path(self.workdir.as_posix()).parent / "delete-staging" / deletion_id
        moved: list[tuple[Path, Path]] = []
        stage_root.mkdir(parents=True, exist_ok=True)
        try:
            existing = [path for path in paths if path.exists() or path.is_symlink()]
            collapsed: list[Path] = []
            for path in sorted(existing, key=lambda item: (len(item.parts), item.as_posix())):
                if any(path == parent or parent in path.parents for parent in collapsed):
                    continue
                collapsed.append(path)
            for index, source in enumerate(collapsed):
                if source.is_symlink():
                    raise RuntimeError(f"拒绝删除符号链接：{self._display_path(source)}")
                destination = stage_root / f"{index:04d}-{sha256_text(source.as_posix())[:12]}-{source.name}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
                moved.append((source, destination))
            return stage_root, moved
        except Exception:
            self._restore_files(moved)
            shutil.rmtree(stage_root, ignore_errors=True)
            raise

    @staticmethod
    def _restore_files(moved: list[tuple[Path, Path]]) -> None:
        for source, staged in reversed(moved):
            if not staged.exists() and not staged.is_symlink():
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(source)

    @staticmethod
    def _delete_ids(connection, table: str, column: str, ids: Iterable[str]) -> int:
        count = 0
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                tuple(chunk),
            )
            count += max(0, int(cursor.rowcount or 0))
        return count

    def _delete_database_rows(
        self,
        connection,
        *,
        deletion_id: str,
        research_id: str,
        candidate_ids: list[str],
        event_ids: set[str],
        activity_ids: set[str],
        target_import_ids: list[str],
        exclusive_asset_ids: list[str],
        actor_id: str,
        private_event_count: int,
        local_file_count: int,
    ) -> dict[str, int]:
        deleted: dict[str, int] = {}

        def direct(table: str) -> None:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                (*self._owner, research_id),
            )
            deleted[table] = max(0, int(cursor.rowcount or 0))

        for table in (
            "research_resolutions",
            "research_change_events",
            "research_finding_versions",
            "research_knowledge_lint_runs",
            "research_knowledge_links",
            "research_derivation_runs",
            "research_context_audits",
        ):
            direct(table)
        for table in (
            "research_continuations",
            "research_snapshots",
            "research_experiments",
            "research_hypotheses",
            "research_conflicts",
            "research_findings",
            "research_checkpoints",
            "research_episodes",
            "research_pack_exports",
        ):
            direct(table)

        if activity_ids:
            shared_activity_ids: set[str] = set()
            for chunk in _chunks(activity_ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT object_id,research_id FROM research_object_scopes WHERE tenant_id=? "
                    "AND user_id=? AND workspace_id=? AND object_type='activity' AND state='active' "
                    f"AND superseded_by='' AND object_id IN ({placeholders}) AND research_id!=?",
                    (*self._owner, *chunk, research_id),
                ).fetchall()
                for row in rows:
                    activity_id = str(row["object_id"] or "")
                    other = str(row["research_id"] or "")
                    shared_activity_ids.add(activity_id)
                    connection.execute(
                        "UPDATE research_activities SET research_id=?,candidate_id='' WHERE activity_id=? "
                        "AND tenant_id=? AND user_id=? AND workspace_id=?",
                        (other, activity_id, *self._owner),
                    )
            delete_activity_ids = activity_ids.difference(shared_activity_ids)
            deleted["research_activities"] = self._delete_ids(
                connection, "research_activities", "activity_id", delete_activity_ids
            )

        experience_rows = connection.execute(
            "SELECT experience_id,source_research_ids_json FROM research_experience_items "
            "WHERE tenant_id=? AND user_id=? AND workspace_id=?",
            self._owner,
        ).fetchall()
        delete_experiences: list[str] = []
        for row in experience_rows:
            research_ids = _strings(_json(row["source_research_ids_json"], []))
            if research_id not in research_ids:
                continue
            remaining = [item for item in research_ids if item != research_id]
            if remaining:
                connection.execute(
                    "UPDATE research_experience_items SET source_research_ids_json=? WHERE experience_id=?",
                    (canonical_json(remaining), str(row["experience_id"])),
                )
            else:
                delete_experiences.append(str(row["experience_id"]))
        deleted["research_experience_items"] = self._delete_ids(
            connection, "research_experience_items", "experience_id", delete_experiences
        )

        route_rows = connection.execute(
            "SELECT route_id,research_ids_json FROM research_turn_routes WHERE tenant_id=? AND user_id=? "
            "AND workspace_id=?",
            self._owner,
        ).fetchall()
        route_ids = [
            str(row["route_id"])
            for row in route_rows
            if research_id in _strings(_json(row["research_ids_json"], []))
        ]
        deleted["research_turn_routes"] = self._delete_ids(
            connection, "research_turn_routes", "route_id", route_ids
        )
        direct("research_object_scopes")

        if exclusive_asset_ids:
            for table in (
                "research_pack_overlay_objects",
                "research_pack_objects",
                "research_pack_object_mappings",
                "research_pack_overlays",
                "research_pack_scopes",
            ):
                deleted[table] = self._delete_ids(connection, table, "asset_id", exclusive_asset_ids)
            deleted["research_pack_assets"] = self._delete_ids(
                connection, "research_pack_assets", "asset_id", exclusive_asset_ids
            )
        if target_import_ids:
            deleted["research_pack_id_mappings"] = self._delete_ids(
                connection, "research_pack_id_mappings", "import_id", target_import_ids
            )
            deleted["research_pack_imports"] = self._delete_ids(
                connection, "research_pack_imports", "import_id", target_import_ids
            )

        operation_ids: list[str] = []
        rows = connection.execute(
            "SELECT operation_id,source_candidate_ids_json,target_candidate_ids_json,before_json,after_json "
            "FROM research_topic_operations WHERE tenant_id=? AND user_id=? AND workspace_id=?",
            self._owner,
        ).fetchall()
        target_candidates = set(candidate_ids)
        for row in rows:
            sources = set(_strings(_json(row["source_candidate_ids_json"], [])))
            targets = set(_strings(_json(row["target_candidate_ids_json"], [])))
            before = _json(row["before_json"], {})
            after = _json(row["after_json"], {})
            if (
                target_candidates.intersection(sources | targets)
                or str(before.get("research_id") or "") == research_id
                or str(after.get("research_id") or "") == research_id
            ):
                operation_ids.append(str(row["operation_id"]))
        deleted["research_topic_operations"] = self._delete_ids(
            connection, "research_topic_operations", "operation_id", operation_ids
        )

        action_target_ids = [research_id, *candidate_ids]
        deleted["research_pending_actions"] = self._delete_ids(
            connection,
            "research_pending_actions",
            "target_id",
            action_target_ids,
        )

        suggestion_ids: list[str] = []
        rows = connection.execute(
            "SELECT suggestion_id,candidate_ids_json FROM research_topic_merge_suggestions "
            "WHERE tenant_id=? AND user_id=? AND workspace_id=?",
            self._owner,
        ).fetchall()
        for row in rows:
            if target_candidates.intersection(_strings(_json(row["candidate_ids_json"], []))):
                suggestion_ids.append(str(row["suggestion_id"]))
        deleted["research_topic_merge_suggestions"] = self._delete_ids(
            connection, "research_topic_merge_suggestions", "suggestion_id", suggestion_ids
        )

        if candidate_ids:
            deleted["research_candidate_memberships"] = self._delete_ids(
                connection, "research_candidate_memberships", "candidate_id", candidate_ids
            )
            deleted["research_topic_candidates"] = self._delete_ids(
                connection, "research_topic_candidates", "candidate_id", candidate_ids
            )
            placeholders = ",".join("?" for _ in candidate_ids)
            cursor = connection.execute(
                "DELETE FROM research_attribution_corrections WHERE tenant_id=? AND user_id=? "
                f"AND workspace_id=? AND (research_id=? OR candidate_id IN ({placeholders}))",
                (*self._owner, research_id, *candidate_ids),
            )
            deleted["research_attribution_corrections"] = max(0, int(cursor.rowcount or 0))
            cursor = connection.execute(
                "DELETE FROM research_event_links WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                f"AND (research_id=? OR candidate_id IN ({placeholders}))",
                (*self._owner, research_id, *candidate_ids),
            )
            deleted["research_event_links"] = max(0, int(cursor.rowcount or 0))
        else:
            for table in ("research_attribution_corrections", "research_event_links"):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                    (*self._owner, research_id),
                )
                deleted[table] = max(0, int(cursor.rowcount or 0))

        cursor = connection.execute(
            "DELETE FROM research_projects WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
            (research_id, *self._owner),
        )
        deleted["research_projects"] = max(0, int(cursor.rowcount or 0))
        if deleted["research_projects"] != 1:
            raise RuntimeError("research project deletion lost owner scope")

        now = utc_now_iso()
        connection.execute(
            "INSERT INTO research_project_deletions(deletion_id,tenant_id,user_id,workspace_id,"
            "research_id_hash,actor_id,private_event_count,local_file_count,records_json,"
            "cleanup_errors_json,deleted_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                deletion_id,
                *self._owner,
                sha256_text(research_id),
                actor_id,
                private_event_count,
                local_file_count,
                canonical_json(deleted),
                "[]",
                now,
                sha256_text(canonical_json({"deletion_id": deletion_id, "owner": self._owner})),
            ),
        )
        return deleted

    def _evidence_ids(self, connection, research_id: str, imported: list[str]) -> list[str]:
        result = set(imported)
        rows = connection.execute(
            "SELECT object_id FROM research_object_scopes WHERE tenant_id=? AND user_id=? "
            "AND workspace_id=? AND research_id=? AND object_type='evidence' AND state='active' "
            "AND superseded_by=''",
            (*self._owner, research_id),
        ).fetchall()
        result.update(str(row["object_id"] or "") for row in rows)
        result.discard("")

        rows = connection.execute(
            "SELECT object_id FROM research_object_scopes WHERE tenant_id=? AND user_id=? "
            "AND workspace_id=? AND research_id!=? AND object_type='evidence' AND state='active' "
            "AND superseded_by=''",
            (*self._owner, research_id),
        ).fetchall()
        shared = {str(row["object_id"] or "") for row in rows}
        return sorted(result.difference(shared))

    def _cleanup_materialized(
        self,
        report: ResearchProjectDeletionReport,
        imported_objects: dict[str, list[str]],
        paper_pack_ids: dict[str, str],
        evidence_ids: list[str],
    ) -> None:
        try:
            from .pack_importer import ResearchPackRecoveryManager

            recovery = ResearchPackRecoveryManager(
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            report.imported_wiki_pages_removed = recovery._cleanup_wiki(
                imported_objects.get("wiki_page", [])
            )
        except Exception as exc:
            report.cleanup_errors.append(f"Wiki 清理失败：{exc}")
        try:
            from ..server.paper_library_loader import paper_library_module

            module = paper_library_module()
            library_root = (
                self.workdir
                / ".tcode"
                / "plugins"
                / "paper_research_workflow"
                / "library"
            )
            store = module.PaperLibraryStore(
                library_root=library_root,
                workdir=self.workdir.as_posix(),
            )
            for paper_id in imported_objects.get("paper", []):
                if store.remove_imported_record(
                    paper_id,
                    pack_id=paper_pack_ids.get(paper_id, ""),
                ):
                    report.imported_papers_removed += 1
            if report.imported_papers_removed:
                try:
                    store._rebuild_chunk_index()
                except Exception:
                    pass
        except Exception as exc:
            report.cleanup_errors.append(f"Paper Library 清理失败：{exc}")
        try:
            from ..evidence.store import EvidenceStore

            if evidence_ids:
                connection = EvidenceStore._connect(self.workdir.as_posix())
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for chunk in _chunks(evidence_ids):
                        placeholders = ",".join("?" for _ in chunk)
                        cursor = connection.execute(
                            f"DELETE FROM evidence_records WHERE evidence_id IN ({placeholders})",
                            tuple(chunk),
                        )
                        report.evidence_records_removed += max(0, int(cursor.rowcount or 0))
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                finally:
                    connection.close()
        except Exception as exc:
            report.cleanup_errors.append(f"证据记录清理失败：{exc}")

    def delete(
        self,
        research_id: str,
        *,
        actor_id: str,
        confirmation: str,
    ) -> ResearchProjectDeletionReport:
        normalized_id = str(research_id or "").strip()
        project = ResearchTopicStore.get_project(
            normalized_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if project is None:
            raise LookupError("project not found")
        if project.status != "archived":
            raise RuntimeError("project must be archived before permanent deletion")
        if str(confirmation or "").strip().lower() != "yes":
            raise ValueError("permanent deletion requires confirmation: yes")

        deletion_id = f"project-delete-{uuid.uuid4().hex}"
        report = ResearchProjectDeletionReport(
            deletion_id=deletion_id,
            research_id=normalized_id,
        )
        connection = ResearchEventStore._connect(
            self.workdir.as_posix(), settings=self.settings
        )
        stage_root: Path | None = None
        moved: list[tuple[Path, Path]] = []
        try:
            candidate_ids = self._candidate_ids(connection, project)
            event_ids, _activity_events, activity_ids = self._project_event_ids(
                connection, normalized_id, candidate_ids
            )
            remaining_targets = self._remaining_event_targets(
                connection, event_ids, normalized_id, candidate_ids
            )
            target_import_rows = connection.execute(
                "SELECT import_id,asset_id FROM research_pack_imports WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND local_research_id=?",
                (*self._owner, normalized_id),
            ).fetchall()
            target_import_ids = [str(row["import_id"] or "") for row in target_import_rows]
            target_asset_ids = {str(row["asset_id"] or "") for row in target_import_rows if str(row["asset_id"] or "")}
            exclusive_asset_ids: list[str] = []
            for asset_id in target_asset_ids:
                shared = connection.execute(
                    "SELECT 1 FROM research_pack_imports WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                    "AND asset_id=? AND local_research_id!=? LIMIT 1",
                    (*self._owner, asset_id, normalized_id),
                ).fetchone()
                if shared is None:
                    exclusive_asset_ids.append(asset_id)

            paths, preserved, imported_objects, paper_pack_ids = self._file_plan(
                connection,
                research_id=normalized_id,
                exclusive_event_ids=event_ids.difference(remaining_targets),
                target_import_ids=target_import_ids,
                exclusive_asset_ids=exclusive_asset_ids,
            )
            evidence_ids = self._evidence_ids(
                connection, normalized_id, imported_objects.get("evidence", [])
            )
            execution_manifests = [
                _json(row["execution_manifest_json"], {})
                for row in connection.execute(
                    "SELECT execution_manifest_json FROM research_experiments WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                    (*self._owner, normalized_id),
                ).fetchall()
            ]
            report.local_files_preserved = preserved
            connection.close()

            private_count, reassigned_count = self._make_private_tombstones(
                deletion_id=deletion_id,
                event_ids=event_ids,
                remaining_targets=remaining_targets,
                actor_id=str(actor_id or self.owner["user_id"]),
            )
            report.private_session_event_count = private_count
            report.reassigned_event_count = reassigned_count
            stage_root, moved = self._stage_files(paths, deletion_id)
            report.local_files_removed = [self._display_path(source) for source, _ in moved]
            from .experiment_execution import ExperimentExecutionManager
            for manifest in execution_manifests:
                try:
                    ExperimentExecutionManager.cleanup(manifest, workspace=self.workdir.as_posix())
                except Exception as exc:
                    report.cleanup_errors.append(f"实验执行目录清理失败：{exc}")

            connection = ResearchEventStore._connect(
                self.workdir.as_posix(), settings=self.settings
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                report.records_deleted = self._delete_database_rows(
                    connection,
                    deletion_id=deletion_id,
                    research_id=normalized_id,
                    candidate_ids=candidate_ids,
                    event_ids=event_ids,
                    activity_ids=activity_ids,
                    target_import_ids=target_import_ids,
                    exclusive_asset_ids=exclusive_asset_ids,
                    actor_id=str(actor_id or self.owner["user_id"]),
                    private_event_count=private_count,
                    local_file_count=len(moved),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                self._restore_files(moved)
                moved = []
                if stage_root is not None:
                    shutil.rmtree(stage_root, ignore_errors=True)
                raise
            finally:
                connection.close()

            try:
                shutil.rmtree(stage_root)
                moved = []
            except Exception as exc:
                report.cleanup_errors.append(f"本地文件最终清理失败：{exc}")
            self._cleanup_materialized(
                report,
                imported_objects,
                paper_pack_ids,
                evidence_ids,
            )

            try:
                audit = ResearchEventStore._connect(
                    self.workdir.as_posix(), settings=self.settings
                )
                try:
                    audit.execute(
                        "UPDATE research_project_deletions SET local_file_count=?,cleanup_errors_json=?,records_json=? "
                        "WHERE deletion_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
                        (
                            len(report.local_files_removed)
                            + report.imported_papers_removed
                            + report.imported_wiki_pages_removed,
                            canonical_json(report.cleanup_errors),
                            canonical_json(report.records_deleted),
                            deletion_id,
                            *self._owner,
                        ),
                    )
                finally:
                    audit.close()
            except Exception as exc:
                report.cleanup_errors.append(f"删除审计更新失败：{exc}")
            return report
        finally:
            try:
                connection.close()
            except Exception:
                pass
            # A surviving staging directory can only happen after the database
            # commit; the user-visible originals are already gone and the audit
            # records the cleanup warning for maintenance retry.


__all__ = ["ResearchProjectDeletionManager", "ResearchProjectDeletionReport"]
