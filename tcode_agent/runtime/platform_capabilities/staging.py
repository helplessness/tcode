"""Read-only, bounded Skill staging for one external Harness Run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .models import StagedSkill
from .policy import PlatformCapabilityPolicy


_SAFE_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".pdf",
    ".svg",
}


def _safe_segment(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return normalized[:120] or fallback


def _limits() -> tuple[int, int, int]:
    try:
        total = max(64 * 1024, min(int(os.environ.get("TCODE_PLATFORM_BRIDGE_SKILL_MAX_BYTES", "2097152")), 20 * 1024 * 1024))
    except Exception:
        total = 2 * 1024 * 1024
    try:
        per_file = max(16 * 1024, min(int(os.environ.get("TCODE_PLATFORM_BRIDGE_SKILL_FILE_MAX_BYTES", "524288")), 5 * 1024 * 1024))
    except Exception:
        per_file = 512 * 1024
    try:
        files = max(1, min(int(os.environ.get("TCODE_PLATFORM_BRIDGE_SKILL_MAX_FILES", "64")), 512))
    except Exception:
        files = 64
    return total, per_file, files


def _skill_source_root(location: str) -> Path | None:
    source = Path(str(location or "")).expanduser().resolve(strict=False)
    if source.is_file():
        return source.parent
    if source.is_dir():
        return source
    return None


def _iter_assets(root: Path):
    for directory_name in ("assets", "references"):
        directory = root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _SAFE_SUFFIXES:
                continue
            yield path


async def stage_allowed_skills(
    *,
    workdir: str,
    run_id: str,
    policy: PlatformCapabilityPolicy,
) -> tuple[list[StagedSkill], str]:
    """Materialize admitted instructions/assets without exposing source paths."""

    if not policy.allows("skill.read"):
        return [], ""
    from ...skill import SkillManager

    await SkillManager.load(workdir=workdir)
    admitted = [
        item
        for item in SkillManager.all()
        if policy.skill_allowed(item.name, item.skill_id)
    ][:64]
    if not admitted:
        return [], ""

    workspace = Path(workdir).expanduser().resolve(strict=True)
    runtime_root = workspace / ".tcode" / "runtime" / "platform-bridge"
    cursor = workspace
    for part in Path(".tcode/runtime/platform-bridge").parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("Platform Skill staging cannot traverse a symbolic link")
    root = runtime_root / _safe_segment(run_id, "run") / "skills"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    total_limit, per_file_limit, file_limit = _limits()
    total_bytes = 0
    copied_files = 0
    result: list[StagedSkill] = []

    for skill in admitted:
        if copied_files >= file_limit or total_bytes >= total_limit:
            break
        name = _safe_segment(skill.name, "skill")
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        content = str(skill.content or "")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > per_file_limit or total_bytes + len(encoded) > total_limit:
            shutil.rmtree(target, ignore_errors=True)
            continue
        (target / "SKILL.md").write_bytes(encoded)
        total_bytes += len(encoded)
        copied_files += 1

        source_root = _skill_source_root(skill.location)
        if source_root is not None:
            for source in _iter_assets(source_root):
                if copied_files >= file_limit or total_bytes >= total_limit:
                    break
                try:
                    size = int(source.stat().st_size)
                    relative = source.relative_to(source_root)
                except Exception:
                    continue
                if size <= 0 or size > per_file_limit or total_bytes + size > total_limit:
                    continue
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(0o444)
                total_bytes += size
                copied_files += 1

        metadata = {
            "schema": "tcode.staged-skill.v1",
            "skill_id": skill.skill_id or skill.name,
            "name": skill.name,
            "version": skill.version,
            "content_hash": skill.content_hash,
            "source_kind": skill.source_kind,
        }
        metadata_bytes = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
        (target / "metadata.json").write_bytes(metadata_bytes)
        (target / "SKILL.md").chmod(0o444)
        (target / "metadata.json").chmod(0o444)
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        result.append(
            StagedSkill(
                skillId=str(skill.skill_id or skill.name),
                name=skill.name,
                version=str(skill.version or "1"),
                digest=digest,
                relativePath=target.relative_to(workspace).as_posix(),
                sourceKind=str(skill.source_kind or "workspace"),
            )
        )
        for directory in sorted(
            (path for path in target.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        target.chmod(0o555)
    root.chmod(0o555)
    return result, root.relative_to(workspace).as_posix()


def cleanup_staging(workdir: str, run_id: str) -> None:
    workspace = Path(workdir).expanduser().resolve(strict=False)
    target = workspace / ".tcode" / "runtime" / "platform-bridge" / _safe_segment(run_id, "run")
    try:
        target.relative_to(workspace / ".tcode" / "runtime" / "platform-bridge")
    except ValueError:
        return
    if target.exists():
        for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                try:
                    path.chmod(0o755)
                except OSError:
                    pass
        try:
            target.chmod(0o755)
        except OSError:
            pass
    shutil.rmtree(target, ignore_errors=True)


__all__ = ["cleanup_staging", "stage_allowed_skills"]
