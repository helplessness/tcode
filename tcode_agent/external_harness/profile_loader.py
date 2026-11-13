"""声明式 Harness Runtime Profile 的发现与严格加载。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .compatibility import definition_digest
from .runtime_def import HarnessRuntimeDef
from .security import render_argv


MAX_PROFILE_BYTES = 262_144
PROFILE_SUFFIXES = {".json", ".yaml", ".yml"}
SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "harness-runtime-v2.schema.json"


@dataclass(frozen=True)
class LoadedHarnessProfile:
    path: Path
    definition: HarnessRuntimeDef
    digest: str


def profile_directories() -> list[Path]:
    configured = [
        Path(item).expanduser().resolve(strict=False)
        for item in str(os.environ.get("TCODE_HARNESS_PROFILE_PATHS") or "").split(
            os.pathsep
        )
        if item.strip()
    ]
    workdir = Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()))
    defaults = [
        (workdir / ".tcode" / "harnesses").resolve(strict=False),
        (Path.home() / ".config" / "tcode" / "harnesses").resolve(strict=False),
    ]
    result: list[Path] = []
    for path in [*configured, *defaults]:
        if path not in result:
            result.append(path)
    return result


def discover_profile_files() -> list[Path]:
    files: list[Path] = []
    for directory in profile_directories():
        try:
            # ``Path.iterdir()`` is lazy: missing/unmounted directories raise
            # while the iterator is consumed, not necessarily when it is
            # created.  Materialize it inside the guarded block so optional
            # discovery roots can never prevent TCode from starting.
            candidates = list(directory.iterdir())
        except OSError:
            continue
        for path in candidates:
            try:
                if path.is_file() and path.suffix.lower() in PROFILE_SUFFIXES:
                    files.append(path.resolve(strict=False))
            except OSError:
                # A file may disappear or become unreadable between listing
                # and inspection.  Isolate that entry like a bad Provider.
                continue
    return sorted(set(files), key=lambda path: path.as_posix())


def _document(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_PROFILE_BYTES:
        raise ValueError(f"Harness profile exceeds {MAX_PROFILE_BYTES} bytes")
    text = path.read_text(encoding="utf-8")
    value = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("Harness profile root must be an object")
    return value


def _validate_executable(value: str) -> None:
    value = str(value or "")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("Harness executable contains control characters")
    path = Path(value)
    if value == path.name:
        return
    if not path.is_absolute():
        raise ValueError("Harness executable must be a PATH name or absolute path")
    allowed_roots = [
        Path(item).expanduser().resolve(strict=False)
        for item in str(
            os.environ.get("TCODE_HARNESS_ALLOWED_EXECUTABLE_PATHS") or ""
        ).split(os.pathsep)
        if item.strip()
    ]
    resolved = path.resolve(strict=False)
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError(
            "absolute Harness executable is outside "
            "TCODE_HARNESS_ALLOWED_EXECUTABLE_PATHS"
        )


def load_profile(path: Path) -> LoadedHarnessProfile:
    path = Path(path).expanduser().resolve(strict=True)
    document = _document(path)
    if document.get("schema") != "tcode.harness-runtime.v2":
        raise ValueError(
            "Harness profile schema must be tcode.harness-runtime.v2"
        )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise ValueError(f"invalid Harness profile at {location}: {first.message}")
    definition = HarnessRuntimeDef.model_validate(document)
    for executable in [
        definition.executables.primary,
        *definition.executables.fallbacks,
    ]:
        _validate_executable(executable)
    render_argv(
        definition.invocation.args,
        workspace="/tcode/workspace",
        model_id="provider/model",
    )
    return LoadedHarnessProfile(
        path=path,
        definition=definition,
        digest=definition_digest(definition),
    )


__all__ = [
    "LoadedHarnessProfile",
    "MAX_PROFILE_BYTES",
    "SCHEMA_PATH",
    "discover_profile_files",
    "load_profile",
    "profile_directories",
]
