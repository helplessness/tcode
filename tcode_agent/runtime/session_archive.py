"""Archive bundle helpers for persisted chat sessions."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class SessionArchiveManager:
    _BUNDLE_VERSION = 1

    @classmethod
    def _base_dir(cls, workdir: Optional[str] = None) -> Path:
        base = str(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
        return Path(base) / ".tcode" / "archived_sessions"

    @classmethod
    def bundle_dir(cls, session_id: str, workdir: Optional[str] = None) -> Path:
        path = cls._base_dir(workdir) / str(session_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def bundle_paths(cls, session_id: str, workdir: Optional[str] = None) -> dict[str, str]:
        bundle_dir = cls.bundle_dir(session_id, workdir)
        return {
            "bundle_dir": str(bundle_dir),
            "session_json": str(bundle_dir / "session.json"),
            "messages_json": str(bundle_dir / "messages.json"),
            "artifacts_manifest_json": str(bundle_dir / "artifacts_manifest.json"),
            "readme_md": str(bundle_dir / "README.md"),
        }

    @classmethod
    def bundle_exists(cls, session_id: str, workdir: Optional[str] = None) -> bool:
        paths = cls.bundle_paths(session_id, workdir)
        return all(
            Path(paths[key]).is_file()
            for key in ("session_json", "messages_json", "artifacts_manifest_json")
        )

    @classmethod
    def write_bundle(
        cls,
        *,
        session_data: dict[str, Any],
        messages: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        workdir: Optional[str] = None,
    ) -> dict[str, str]:
        session_id = str(session_data.get("id") or "").strip()
        if not session_id:
            raise ValueError("session id is required for archive bundle")

        archive_paths = cls.bundle_paths(session_id, workdir)
        generated_at = datetime.now().timestamp()
        artifact_root = str(
            Path(str(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip())
            / ".tcode"
            / "artifacts"
            / session_id
        )

        session_document = {
            "bundle_version": cls._BUNDLE_VERSION,
            "generated_at": generated_at,
            "session": dict(session_data or {}),
            "message_count": len(messages or []),
            "artifact_count": len(artifacts or []),
        }
        messages_document = {
            "bundle_version": cls._BUNDLE_VERSION,
            "generated_at": generated_at,
            "session_id": session_id,
            "messages": list(messages or []),
        }
        artifacts_document = {
            "bundle_version": cls._BUNDLE_VERSION,
            "generated_at": generated_at,
            "session_id": session_id,
            "artifact_root": artifact_root,
            "artifact_count": len(artifacts or []),
            "artifacts": list(artifacts or []),
        }
        readme_text = "\n".join(
            [
                "# Archived Session Bundle",
                "",
                "This directory is generated automatically when a session is archived.",
                "",
                "- `session.json`: session metadata snapshot",
                "- `messages.json`: serialized message snapshot",
                "- `artifacts_manifest.json`: artifact manifest for the session",
            ]
        )

        Path(archive_paths["session_json"]).write_text(
            json.dumps(session_document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(archive_paths["messages_json"]).write_text(
            json.dumps(messages_document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(archive_paths["artifacts_manifest_json"]).write_text(
            json.dumps(artifacts_document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(archive_paths["readme_md"]).write_text(readme_text, encoding="utf-8")
        return archive_paths

    @classmethod
    def delete_bundle(cls, session_id: str, workdir: Optional[str] = None) -> None:
        bundle_dir = cls._base_dir(workdir) / str(session_id)
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir, ignore_errors=True)
