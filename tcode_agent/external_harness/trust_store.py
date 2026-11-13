"""外部 Harness 的启用状态与 Python entry point 信任记录。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()


def default_settings_path() -> Path:
    explicit = str(os.environ.get("TCODE_HARNESS_SETTINGS_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    root = Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()))
    return (root / ".tcode" / "harness-runtimes.json").resolve(strict=False)


class HarnessTrustStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_settings_path()).resolve(strict=False)

    def read(self) -> dict[str, Any]:
        with _LOCK:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return {}
        return value if isinstance(value, dict) else {}

    def enabled_ids(self) -> set[str]:
        return {
            str(item).strip()
            for item in self.read().get("enabled") or []
            if str(item).strip()
        }

    def trusted_entrypoints(self) -> set[str]:
        return {
            str(item).strip()
            for item in self.read().get("trustedEntrypoints") or []
            if str(item).strip()
        }

    def enabled_definition_digest(self, runtime_id: str) -> str:
        values = self.read().get("enabledDefinitions") or {}
        if not isinstance(values, dict):
            return ""
        return str(values.get(str(runtime_id or "").strip()) or "")

    def is_enabled(self, runtime_id: str) -> bool:
        return str(runtime_id or "").strip() in self.enabled_ids()

    def is_entrypoint_trusted(self, source_key: str) -> bool:
        return str(source_key or "").strip() in self.trusted_entrypoints()

    def update(
        self,
        *,
        runtime_id: str,
        enabled: bool,
        trusted_entrypoint: str = "",
        definition_digest: str = "",
    ) -> None:
        runtime_id = str(runtime_id or "").strip()
        if not runtime_id:
            raise ValueError("runtime_id is required")
        with _LOCK:
            current = self.read()
            enabled_ids = {
                str(item).strip()
                for item in current.get("enabled") or []
                if str(item).strip()
            }
            trusted = {
                str(item).strip()
                for item in current.get("trustedEntrypoints") or []
                if str(item).strip()
            }
            enabled_definitions = {
                str(key): str(value)
                for key, value in (current.get("enabledDefinitions") or {}).items()
                if str(key).strip() and str(value).strip()
            } if isinstance(current.get("enabledDefinitions") or {}, dict) else {}
            if enabled:
                enabled_ids.add(runtime_id)
                if definition_digest:
                    enabled_definitions[runtime_id] = definition_digest
            else:
                enabled_ids.discard(runtime_id)
                enabled_definitions.pop(runtime_id, None)
            if trusted_entrypoint:
                trusted.add(str(trusted_entrypoint))
            payload = {
                "schema": "tcode.harness-settings.v2",
                "enabled": sorted(enabled_ids),
                "enabledDefinitions": enabled_definitions,
                "trustedEntrypoints": sorted(trusted),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)


__all__ = ["HarnessTrustStore", "default_settings_path"]
