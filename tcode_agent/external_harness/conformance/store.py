"""与 Runtime Definition digest 绑定的 conformance 证据存储。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .report import HarnessConformanceReport


def _safe_id(runtime_id: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(runtime_id or ""))
    return value[:100] or "unknown"


class HarnessConformanceStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=False) / "harness-conformance"

    def path_for(self, runtime_id: str) -> Path:
        return self.root / f"{_safe_id(runtime_id)}.json"

    def read(
        self, runtime_id: str, *, definition_digest: str
    ) -> tuple[str, list[str]]:
        try:
            payload = json.loads(
                self.path_for(runtime_id).read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                return "unverified", []
            report = HarnessConformanceReport.model_validate(
                payload.get("report")
            )
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return "unverified", []
        if str(payload.get("definitionDigest") or "") != definition_digest:
            return "stale", []
        return (
            "passed" if report.passed else "failed",
            list(report.capabilities_verified) if report.passed else [],
        )

    def write(
        self,
        report: HarnessConformanceReport,
        *,
        definition_digest: str,
    ) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema": "tcode.harness-conformance-evidence.v1",
            "definitionDigest": definition_digest,
            "report": report.model_dump(mode="json", by_alias=True),
        }
        path = self.path_for(report.runtime_id)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return path


__all__ = ["HarnessConformanceStore"]
