"""Harness Adapter 与当前 TCode 版本的兼容性判断。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from .. import __version__
from .runtime_def import HarnessRuntimeDef


@dataclass(frozen=True)
class CompatibilityResult:
    state: str
    diagnostics: tuple[str, ...] = ()


def definition_digest(definition: HarnessRuntimeDef) -> str:
    payload = definition.model_dump(mode="json", by_alias=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def evaluate_compatibility(definition: HarnessRuntimeDef) -> CompatibilityResult:
    if definition.compatibility.adapter_api_version != "1":
        return CompatibilityResult(
            "incompatible",
            (
                "unsupported Harness Adapter API: "
                + definition.compatibility.adapter_api_version,
            ),
        )
    try:
        supported = SpecifierSet(definition.compatibility.tcode_version)
    except InvalidSpecifier:
        return CompatibilityResult(
            "error",
            (
                "invalid compatibility.tcodeVersion: "
                + definition.compatibility.tcode_version,
            ),
        )
    if Version(__version__) not in supported:
        return CompatibilityResult(
            "incompatible",
            (
                f"TCode {__version__} is outside "
                f"{definition.compatibility.tcode_version}",
            ),
        )
    return CompatibilityResult("compatible")


__all__ = [
    "CompatibilityResult",
    "definition_digest",
    "evaluate_compatibility",
]
