"""Durable, user-private temporary workflow plans."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .registry import WorkflowTemplateRegistry


class WorkflowDraftStore:
    _ID = re.compile(r"^wfd-[a-f0-9]{12}$")
    _EDITABLE = {"editing"}

    @classmethod
    def _root(cls, workdir: str | Path) -> Path:
        root = Path(workdir).expanduser().resolve() / ".tcode" / "workflow" / "drafts"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def _path(cls, draft_id: str, workdir: str | Path) -> Path:
        normalized = str(draft_id or "").strip()
        if not cls._ID.fullmatch(normalized):
            raise ValueError("invalid workflow draft_id")
        return cls._root(workdir) / f"{normalized}.json"

    @staticmethod
    def _owner_matches(draft: dict[str, Any], user_id: str) -> bool:
        return str(draft.get("owner_user_id") or "") == str(user_id or "")

    @classmethod
    def create(
        cls, *, goal: str, template: dict[str, Any], session_id: str,
        user_id: str, model: str, workdir: str | Path,
    ) -> dict[str, Any]:
        validated = WorkflowTemplateRegistry.validate_payload(template).to_dict()
        now = time.time()
        draft = {
            "draft_id": f"wfd-{uuid.uuid4().hex[:12]}",
            "owner_user_id": str(user_id or ""),
            "session_id": str(session_id or ""),
            "name": str(validated.get("display_name") or "临时 Workflow"),
            "goal": str(goal or "").strip(),
            "status": "editing",
            "revision": 1,
            "model": str(model or ""),
            "template": validated,
            "source_run_id": "",
            "created_at": now,
            "updated_at": now,
        }
        cls._write(draft, workdir)
        return draft

    @classmethod
    def _write(cls, draft: dict[str, Any], workdir: str | Path) -> None:
        path = cls._path(str(draft.get("draft_id") or ""), workdir)
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(path)

    @classmethod
    def get(cls, draft_id: str, *, user_id: str, workdir: str | Path) -> Optional[dict[str, Any]]:
        path = cls._path(draft_id, workdir)
        if not path.exists():
            return None
        draft = json.loads(path.read_text(encoding="utf-8"))
        return draft if isinstance(draft, dict) and cls._owner_matches(draft, user_id) else None

    @classmethod
    def list(cls, *, user_id: str, workdir: str | Path) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        for path in cls._root(workdir).glob("wfd-*.json"):
            try:
                draft = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(draft, dict) and cls._owner_matches(draft, user_id):
                drafts.append(draft)
        return sorted(drafts, key=lambda item: float(item.get("updated_at") or 0), reverse=True)

    @classmethod
    def update(
        cls, draft_id: str, *, template: dict[str, Any], name: str,
        user_id: str, workdir: str | Path,
    ) -> dict[str, Any]:
        draft = cls.get(draft_id, user_id=user_id, workdir=workdir)
        if draft is None:
            raise KeyError(f"workflow draft not found: {draft_id}")
        if str(draft.get("status") or "") not in cls._EDITABLE:
            raise ValueError("confirmed workflow draft is immutable")
        validated = WorkflowTemplateRegistry.validate_payload(template).to_dict()
        draft.update(
            template=validated,
            name=str(name or validated.get("display_name") or "临时 Workflow").strip(),
            revision=int(draft.get("revision") or 0) + 1,
            updated_at=time.time(),
        )
        cls._write(draft, workdir)
        return draft

    @classmethod
    def transition(
        cls, draft_id: str, *, status: str, user_id: str, workdir: str | Path,
        source_run_id: str = "",
    ) -> dict[str, Any]:
        draft = cls.get(draft_id, user_id=user_id, workdir=workdir)
        if draft is None:
            raise KeyError(f"workflow draft not found: {draft_id}")
        draft["status"] = str(status or draft.get("status") or "editing")
        if source_run_id:
            draft["source_run_id"] = str(source_run_id)
        draft["updated_at"] = time.time()
        cls._write(draft, workdir)
        return draft

    @classmethod
    def delete(cls, draft_id: str, *, user_id: str, workdir: str | Path) -> bool:
        draft = cls.get(draft_id, user_id=user_id, workdir=workdir)
        if draft is None:
            return False
        cls._path(draft_id, workdir).unlink(missing_ok=True)
        return True

