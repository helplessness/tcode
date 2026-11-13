"""Bounded path and payload validation for platform knowledge services."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .contracts import PlatformKnowledgeAccessError, PlatformKnowledgeValidationError


class KnowledgeAccessPolicy:
    @staticmethod
    def bounded_text(value: object, *, maximum: int, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise PlatformKnowledgeValidationError(f"{field} is required")
        if len(text) > maximum:
            raise PlatformKnowledgeValidationError(f"{field} exceeds {maximum} characters")
        return text

    @staticmethod
    def source_file(workdir: str, value: object) -> tuple[Path, str, str]:
        root = Path(workdir).expanduser().resolve(strict=True)
        raw = Path(str(value or "").strip()).expanduser()
        candidate = raw if raw.is_absolute() else root / raw
        unresolved = candidate.absolute()
        try:
            relative_unresolved = unresolved.relative_to(root)
        except ValueError as exc:
            raise PlatformKnowledgeAccessError("Evidence source is outside the workspace") from exc
        cursor = root
        for part in relative_unresolved.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise PlatformKnowledgeAccessError("Evidence source cannot traverse a symbolic link")
        target = candidate.resolve(strict=True)
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise PlatformKnowledgeAccessError("Evidence source escaped the workspace") from exc
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PlatformKnowledgeValidationError("Evidence source must be a regular file")
        max_bytes = int(os.environ.get("TCODE_PLATFORM_BRIDGE_EVIDENCE_SOURCE_MAX_BYTES", "52428800"))
        if info.st_size <= 0 or info.st_size > max(1024, min(max_bytes, 500 * 1024 * 1024)):
            raise PlatformKnowledgeValidationError("Evidence source size is outside the admitted range")
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return target, relative, digest.hexdigest()


__all__ = ["KnowledgeAccessPolicy"]
