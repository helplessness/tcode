"""Validate the packaged, read-only SQLite ontology release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .sqlite_bundle import file_digest


class BundleValidationError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_packaged_bundle(workspace: Any, plugin_root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    root = plugin_root.resolve() / "knowledge"
    path = Path(workspace.resolve_read_path(str(root / "entity_manifest.json")))
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise BundleValidationError("entity manifest is missing or unsafe")
    try:
        manifest = json.loads(workspace.read_text(str(path), encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BundleValidationError("invalid entity manifest") from exc
    digest = manifest.get("bundle_digest")
    payload = dict(manifest)
    payload.pop("bundle_digest", None)
    if (manifest.get("schema_version"), manifest.get("storage_format")) not in {("1.0", "sqlite-v1"), ("2.0", "sqlite-v2")} or hashlib.sha256(canonical_bytes(payload)).hexdigest() != digest:
        raise BundleValidationError("unsupported or corrupt entity manifest")
    seen: set[str] = set()
    for entry in manifest.get("files") or []:
        relative = Path(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in seen:
            raise BundleValidationError("unsafe or duplicate entity bundle path")
        seen.add(relative.as_posix())
        item = Path(workspace.resolve_read_path(str(root / relative)))
        if item.is_symlink() or not item.is_file() or not item.resolve().is_relative_to(root):
            raise BundleValidationError(f"missing or unsafe entity bundle file: {relative}")
        if file_digest(item) != entry.get("sha256"):
            raise BundleValidationError(f"bundle checksum mismatch: {relative}")
    databases = [name for name in seen if name.startswith("public/releases/") and name.endswith("/entities.sqlite")]
    if len(databases) != 1:
        raise BundleValidationError("entity manifest omits required database")
    if manifest["storage_format"] == "sqlite-v1":
        if not {"ontology.json", "curation_source.json"} <= seen:
            raise BundleValidationError("legacy manifest omits required files")
    else:
        prefix = "public/releases/" + str(manifest.get("release_id", "")) + "/"
        if (any(not name.startswith(prefix) for name in seen)
                or manifest.get("database_path") != databases[0]
                or manifest.get("ontology_path") != prefix + "ontology.json"
                or manifest["ontology_path"] not in seen):
            raise BundleValidationError("release dependencies must be sealed in the same version")
    return manifest, {}
