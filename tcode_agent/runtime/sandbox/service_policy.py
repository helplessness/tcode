from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import SandboxSpec


@dataclass(frozen=True)
class WorkspaceServiceBinding:
    containers: tuple[tuple[str, str], ...] = ()
    env: dict[str, str] = field(default_factory=dict)


class WorkspaceServicePolicy:
    def __init__(self, policy_file: str):
        self.path = (
            Path(policy_file).expanduser().resolve(strict=False)
            if str(policy_file or "").strip()
            else None
        )
        self._mtime_ns = -1
        self._data: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return {}
        if info.st_mode & 0o007:
            raise PermissionError(
                "workspace service policy must not be accessible by other users"
            )
        if info.st_mtime_ns == self._mtime_ns:
            return self._data
        parsed = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("workspace service policy must be a JSON object")
        self._data = parsed
        self._mtime_ns = info.st_mtime_ns
        return parsed

    @staticmethod
    def _key(spec: SandboxSpec) -> str:
        metadata = dict(spec.metadata or {})
        return "/".join(
            str(metadata.get(key) or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
        )

    def binding_for(self, spec: SandboxSpec) -> WorkspaceServiceBinding:
        key = self._key(spec)
        raw = self._load().get(key)
        if not isinstance(raw, dict):
            return WorkspaceServiceBinding()

        containers: list[tuple[str, str]] = []
        for item in list(raw.get("containers") or []):
            if not isinstance(item, dict):
                raise ValueError(f"invalid service container entry for {key}")
            name = str(item.get("name") or "").strip()
            alias = str(item.get("alias") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                raise ValueError(f"invalid service container name for {key}")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", alias):
                raise ValueError(f"invalid service container alias for {key}")
            containers.append((name, alias))

        env: dict[str, str] = {}
        raw_env = raw.get("env")
        if isinstance(raw_env, dict):
            for raw_key, raw_value in raw_env.items():
                env_key = str(raw_key or "").strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
                    raise ValueError(f"invalid service environment key for {key}")
                env[env_key] = str(raw_value or "")
        return WorkspaceServiceBinding(containers=tuple(containers), env=env)
