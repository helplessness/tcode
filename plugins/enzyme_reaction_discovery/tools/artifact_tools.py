"""受工作区边界约束的运行记录与阶段产物持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ALLOWED_ARTIFACTS = frozenset(
    {
        "01_reaction_analysis.json",
        "02_mechanisms.json",
        "03_capabilities.json",
        "04_candidates.json",
        "05_routes.json",
        "06_critique.json",
        "07_final_report.json",
        "07_final_report.md",
        "mechanism_routes.mmd",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArtifactStore:
    """
    只在指定工作区中持久化白名单产物。

    字段：
    - ``_workspace``：实际文件读写由其进行路径边界校验。
    - ``_workspace_dir``：产物在工作区内的相对根目录。

    写入采用临时文件加 ``os.replace`` 原子替换，并在 manifest 中记录摘要与修订。
    """

    def __init__(self, workspace: Any, workspace_dir: str) -> None:
        normalized = str(workspace_dir or "").strip().rstrip("/")
        path = Path(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError("workspace_dir must be a non-empty relative path without '..'")
        self._workspace = workspace  # 受限文件系统能力。
        self._workspace_dir = path.as_posix()  # 工作区内的产物相对根路径。

    @staticmethod
    def validate_run_id(run_id: Any) -> str:
        """规范化并校验运行 ID，防止路径注入。"""
        normalized = str(run_id or "").strip()
        if not _RUN_ID.fullmatch(normalized) or ".." in normalized:
            raise ValueError("run_id must contain only letters, digits, dot, underscore, or hyphen and must not contain '..'")
        return normalized

    def _run_path(self, run_id: str, name: str) -> str:
        return f"{self._workspace_dir}/runs/{self.validate_run_id(run_id)}/{name}"

    def _exists(self, path: str) -> bool:
        resolved = Path(self._workspace.resolve_read_path(path))
        return resolved.is_file()

    def _read_text_if_exists(self, path: str) -> str | None:
        if not self._exists(path):
            return None
        return self._workspace.read_text(path, encoding="utf-8")

    def _read_json_if_exists(self, path: str) -> dict[str, Any] | None:
        text = self._read_text_if_exists(path)
        if text is None:
            return None
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError(f"stored JSON root is not an object: {path}")
        return value

    def _atomic_write(self, path: str, content: str) -> None:
        temporary = f"{path}.tmp-{uuid.uuid4().hex}"
        self._workspace.write_text(temporary, content, encoding="utf-8")
        temporary_resolved = Path(self._workspace.resolve_write_path(temporary))
        target_resolved = Path(self._workspace.resolve_write_path(path))
        try:
            os.replace(temporary_resolved, target_resolved)
        finally:
            if temporary_resolved.exists():
                temporary_resolved.unlink()

    def review_reentry(self, run_id: str) -> dict[str, Any]:
        """重审绑定已校验的上游文件；正在重审时禁止改写前三阶段。"""
        state = self._read_json_if_exists(self._run_path(run_id, "review_reentry.json")) or {}
        if state.get("status") == "active":
            for name, expected in state["upstream_hashes"].items():
                text = self._read_text_if_exists(self._run_path(run_id, name))
                if text is None or _digest(text) != expected:
                    raise ValueError("review-only upstream changed: " + name)
        return state

    def prepare_review(self, run_id: str, knowledge_fingerprint: str) -> dict[str, Any]:
        """显式重开第四阶段，完整备份旧交付，不重算或改写上游科学产物。"""
        run_id = self.validate_run_id(run_id)
        run = self._read_json_if_exists(self._run_path(run_id, "run.json")) or {}
        if run.get("run_id") != run_id or run.get("knowledge_fingerprint") != knowledge_fingerprint:
            raise ValueError("review-only run or knowledge fingerprint mismatch")
        checkpoint = self._read_json_if_exists(self._run_path(run_id, "resume_checkpoint.json"))
        if checkpoint and checkpoint.get("stage") is not None:
            raise ValueError("run is unfinished; resume its existing checkpoint instead")
        if self.review_reentry(run_id).get("status") == "active":
            raise ValueError("review-only already active; resume it, do not prepare again")
        manifest = self._read_json_if_exists(self._run_path(run_id, "artifact_manifest.json")) or {}
        entries = {row["name"]: row for row in manifest.get("artifacts", [])}
        upstream = {}
        for name in ("01_reaction_analysis.json", "02_mechanisms.json", "03_capabilities.json", "04_candidates.json", "05_routes.json"):
            text = self._read_text_if_exists(self._run_path(run_id, name))
            if text is None or _digest(text) != entries.get(name, {}).get("sha256"):
                raise ValueError("review-only requires intact committed artifact: " + name)
            if json.loads(text).get("run_id") != run_id:
                raise ValueError("review-only artifact belongs to another run: " + name)
            upstream[name] = _digest(text)
        for name in ("06_critique.json", "07_final_report.json", "07_final_report.md"):
            if self._read_text_if_exists(self._run_path(run_id, name)) is None:
                raise ValueError("review-only requires a previous completed delivery: " + name)
        review_id = uuid.uuid4().hex
        archive = ".history/reviews/" + review_id
        archived = {}
        for name in ("06_critique.json", "07_final_report.json", "07_final_report.md", "mechanism_routes.mmd",
                     "artifact_manifest.json", "report_evidence_audit.json", "resume_checkpoint.json", "review_reentry.json"):
            text = self._read_text_if_exists(self._run_path(run_id, name))
            if text is not None:
                self._atomic_write(self._run_path(run_id, archive + "/" + name), text)
                archived[name] = _digest(text)
        state = {"review_id": review_id, "status": "active", "review_only": True,
                 "archive": archive, "archived_hashes": archived, "upstream_hashes": upstream,
                 "revision_count": (checkpoint or {}).get("state", {}).get("revision_count", 0)}
        self._atomic_write(self._run_path(run_id, archive + "/record.json"), _pretty(state))
        self._atomic_write(self._run_path(run_id, "review_reentry.json"), _pretty(state))
        # 旧证据交付缓存已备份；新审核必须重新读取，不能沿用旧 PASS 或旧页面。
        self._atomic_write(self._run_path(run_id, "report_evidence_audit.json"), "{}\n")
        return state

    def init_run(
        self,
        *,
        run_id: str,
        reaction_input: Any,
        idempotency_key: str,
        plugin_version: str,
        knowledge_version: str,
        knowledge_fingerprint: str = "",
    ) -> dict[str, Any]:
        """幂等初始化运行元数据和空产物清单。"""
        normalized_run_id = self.validate_run_id(run_id)
        if not isinstance(reaction_input, dict):
            raise ValueError("reaction_input must be an object")
        input_digest = _digest(_canonical(reaction_input))
        effective_key = str(idempotency_key or input_digest).strip()
        run_path = self._run_path(normalized_run_id, "run.json")
        manifest_path = self._run_path(normalized_run_id, "artifact_manifest.json")
        existing = self._read_json_if_exists(run_path)
        if existing is not None:
            if knowledge_fingerprint and existing.get("knowledge_fingerprint") != knowledge_fingerprint:
                raise ValueError("Wiki changed or this is a legacy run; initialize a fresh run")
            if existing.get("input_digest") == input_digest:
                if self._read_json_if_exists(manifest_path) is None:
                    recovered_at = _now()
                    self._atomic_write(
                        manifest_path,
                        _pretty(
                            {
                                "schema_version": "1.0",
                                "run_id": normalized_run_id,
                                "revision": 0,
                                "updated_at": recovered_at,
                                "artifacts": [],
                                "recovered_after_partial_init": True,
                            }
                        ),
                    )
                return {
                    "created": False,
                    "idempotent": True,
                    "retry_key_changed": existing.get("idempotency_key") != effective_key,
                    "run": existing,
                }
            raise ValueError("run_id already exists with different input")
        created_at = _now()
        run_document = {
            "schema_version": "1.0",
            "run_id": normalized_run_id,
            "status": "initialized",
            "plugin_version": plugin_version,
            "knowledge_version": knowledge_version,
            "knowledge_fingerprint": knowledge_fingerprint,
            "created_at": created_at,
            "updated_at": created_at,
            "input_digest": input_digest,
            "idempotency_key": effective_key,
            "reaction_input": reaction_input,
        }
        manifest = {
            "schema_version": "1.0",
            "run_id": normalized_run_id,
            "revision": 0,
            "updated_at": created_at,
            "artifacts": [],
        }
        self._atomic_write(run_path, _pretty(run_document))
        self._atomic_write(manifest_path, _pretty(manifest))
        return {"created": True, "idempotent": False, "run": run_document}

    def commit(
        self,
        *,
        run_id: str,
        artifact_name: str,
        content: str,
        media_type: str,
        schema_name: str | None,
        idempotency_key: str,
        allow_supersede: bool = False,
    ) -> dict[str, Any]:
        """原子提交白名单产物，同步更新 manifest 和修订历史。"""
        normalized_run_id = self.validate_run_id(run_id)
        name = str(artifact_name or "").strip()
        if name not in ALLOWED_ARTIFACTS:
            raise ValueError(f"artifact_name is not in the P2 whitelist: {name}")
        if not isinstance(content, str) or not content:
            raise ValueError("artifact content must be a non-empty string")
        run_path = self._run_path(normalized_run_id, "run.json")
        if self._read_json_if_exists(run_path) is None:
            raise FileNotFoundError(f"run is not initialized: {normalized_run_id}")
        artifact_path = self._run_path(normalized_run_id, name)
        review = self.review_reentry(normalized_run_id)
        if review.get("status") == "active" and name.startswith(("01_", "02_", "03_", "04_", "05_")):
            raise ValueError("review-only cannot modify upstream artifacts")
        manifest_path = self._run_path(normalized_run_id, "artifact_manifest.json")
        manifest = self._read_json_if_exists(manifest_path)
        if manifest is None:
            raise FileNotFoundError(f"artifact manifest is missing: {normalized_run_id}")
        content_digest = _digest(content)
        effective_key = str(idempotency_key or content_digest).strip()
        existing_entry = next(
            (item for item in manifest.get("artifacts") or [] if item.get("name") == name),
            None,
        )
        if existing_entry is not None:
            if existing_entry.get("sha256") == content_digest:
                return {"committed": False, "idempotent": True, "artifact": existing_entry}
            if not allow_supersede:
                raise ValueError(
                    "artifact already exists with different content; "
                    "set allow_supersede=true only for a workflow retry or fallback revision"
                )
        existing_text = self._read_text_if_exists(artifact_path)
        if (
            existing_entry is None
            and existing_text is not None
            and _digest(existing_text) != content_digest
        ):
            raise ValueError("artifact file exists outside manifest with different content")
        if existing_entry is not None and existing_text is not None:
            history_revision = int(manifest.get("revision") or 0) + 1
            history_path = self._run_path(
                normalized_run_id,
                f".history/{name}.r{history_revision}-{str(existing_entry.get('sha256') or '')[:12]}",
            )
            if self._read_text_if_exists(history_path) is None:
                self._atomic_write(history_path, existing_text)
            manifest.setdefault("history", []).append(
                {
                    **dict(existing_entry),
                    "history_path": history_path,
                    "superseded_at": _now(),
                }
            )
        if existing_text is None or _digest(existing_text) != content_digest:
            self._atomic_write(artifact_path, content)
        entry = {
            "name": name,
            "path": artifact_path,
            "media_type": media_type,
            "schema_name": schema_name,
            "sha256": content_digest,
            "bytes": len(content.encode("utf-8")),
            "idempotency_key": effective_key,
            "committed_at": _now(),
        }
        if existing_entry is None:
            manifest.setdefault("artifacts", []).append(entry)
        else:
            manifest["artifacts"] = [
                entry if item.get("name") == name else item
                for item in manifest.get("artifacts") or []
            ]
        manifest["artifacts"] = sorted(manifest["artifacts"], key=lambda item: item["name"])
        manifest["revision"] = int(manifest.get("revision") or 0) + 1
        manifest["updated_at"] = _now()
        self._atomic_write(manifest_path, _pretty(manifest))
        return {
            "committed": True,
            "idempotent": False,
            "superseded": existing_entry is not None,
            "artifact": entry,
        }

    def internal_snapshot(self, run_id, *, artifact_names):
        """服务端审核/汇总逐文件读取；不把公开工具回包限额当作内部汇总限额。"""
        result = self.snapshot(run_id)
        for name in dict.fromkeys(artifact_names):
            part = self.snapshot(run_id, artifact_names=[name], _internal=True)
            if part["run"] != result["run"] or part["artifact_manifest"] != result["artifact_manifest"]:
                raise ValueError("artifacts changed during internal aggregation; no mixed-version report")
            result["documents"].update(part["documents"])
        return result

    def snapshot(
        self,
        run_id: str,
        *,
        artifact_names: list[str] | tuple[str, ...] | None = None,
        _internal: bool = False,
    ) -> dict[str, Any]:
        """读取运行元数据、manifest 和已提交产物的一致性快照。"""
        normalized_run_id = self.validate_run_id(run_id)
        run = self._read_json_if_exists(self._run_path(normalized_run_id, "run.json"))
        if run is None:
            raise FileNotFoundError(f"run not found: {normalized_run_id}")
        manifest = self._read_json_if_exists(
            self._run_path(normalized_run_id, "artifact_manifest.json")
        ) or {"artifacts": []}
        selected_names = list(dict.fromkeys(str(item or "").strip() for item in artifact_names or []))
        if len(selected_names) > 8:
            raise ValueError("artifact_names supports at most eight artifacts per snapshot")
        documents: dict[str, Any] = {}
        selected_chars = 0
        for name in selected_names:
            if name not in ALLOWED_ARTIFACTS:
                raise ValueError(f"artifact_name is not in the P3 whitelist: {name}")
            content = self._read_text_if_exists(self._run_path(normalized_run_id, name))
            if content is None:
                raise FileNotFoundError(f"artifact is not committed: {name}")
            if len(content) > 200_000:
                raise ValueError(f"artifact exceeds snapshot read budget: {name}")
            selected_chars += len(content)
            if selected_chars > 100_000 and not _internal:
                raise ValueError(
                    "selected artifacts exceed the 100000-character snapshot budget; "
                    "read them in smaller groups"
                )
            documents[name] = json.loads(content) if name.endswith(".json") else content
        return {
            "run": run,
            "artifact_manifest": manifest,
            "artifact_count": len(manifest.get("artifacts") or []),
            "documents": documents,
        }


__all__ = ["ALLOWED_ARTIFACTS", "ArtifactStore"]
