"""不执行第三方代码的 Harness Profile/entry point 枚举。"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any

from .profile_loader import discover_profile_files


ENTRY_POINT_GROUP = "tcode.harness_adapters"


@dataclass(frozen=True)
class HarnessEntryPointCandidate:
    runtime_id: str
    source_key: str
    distribution_name: str
    distribution_version: str
    point: Any


def discover_entry_points() -> list[HarnessEntryPointCandidate]:
    try:
        points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        points = importlib.metadata.entry_points().get(ENTRY_POINT_GROUP, [])
    except Exception:
        return []
    result: list[HarnessEntryPointCandidate] = []
    for point in points:
        distribution = getattr(point, "dist", None)
        metadata = getattr(distribution, "metadata", {}) or {}
        distribution_name = str(
            metadata.get("Name")
            or getattr(distribution, "name", "")
            or "<unknown-distribution>"
        )
        distribution_version = str(
            getattr(distribution, "version", "") or metadata.get("Version") or ""
        )
        version_key = distribution_version or "<unknown-version>"
        source_key = (
            f"{distribution_name}=={version_key}:{point.name}"
        )
        result.append(
            HarnessEntryPointCandidate(
                runtime_id=str(point.name),
                source_key=source_key,
                distribution_name=distribution_name,
                distribution_version=distribution_version,
                point=point,
            )
        )
    return sorted(
        result,
        key=lambda item: (
            item.runtime_id,
            item.distribution_name,
            item.distribution_version,
        ),
    )


__all__ = [
    "ENTRY_POINT_GROUP",
    "HarnessEntryPointCandidate",
    "discover_entry_points",
    "discover_profile_files",
]
