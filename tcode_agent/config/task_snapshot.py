"""Durable, task-scoped configuration snapshots.

Only redacted configuration is written to disk.  The process keeps the complete
configuration in memory while a task is active; after a restart, secret fields
are restored from the current trusted configuration without changing the
snapshot's non-secret behaviour.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..runtime.telemetry import TelemetryGateway
from .effective import (
    EffectiveConfigResolver,
    is_sensitive_config_key,
    redact_config_value,
)
from .manager import ConfigManager


_SNAPSHOT_ID_RE = re.compile(r"^cfg-[a-f0-9]{32}$")
_SNAPSHOT_ID_IN_TEXT_RE = re.compile(r"cfg-[a-f0-9]{32}")


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contains_depth_limit(value: Any) -> bool:
    if value == "<depth-limit>":
        return True
    if isinstance(value, dict):
        return any(_contains_depth_limit(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_depth_limit(item) for item in value)
    return False


def _command_policy(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("command_policy", {})
    policy = dict(raw) if isinstance(raw, dict) else {}
    mode = str(policy.get("mode") or "enforce").strip().lower()
    if mode not in {"enforce", "audit", "disabled"}:
        mode = "enforce"
    return {
        "schema_version": 1,
        "mode": mode,
        "allow_workspace_write": bool(policy.get("allow_workspace_write", True)),
        "allow_network": bool(policy.get("allow_network", True)),
        "allow_dependency_install": bool(
            policy.get("allow_dependency_install", True)
        ),
        "allow_system_operations": bool(
            policy.get("allow_system_operations", False)
        ),
        "allow_background_process": bool(
            policy.get("allow_background_process", True)
        ),
        "allow_external_read": bool(policy.get("allow_external_read", False)),
        "allow_external_write": bool(policy.get("allow_external_write", False)),
    }


def _plugin_snapshot(workdir: str) -> dict[str, Any]:
    """Capture plugin selection and source hashes without importing plugin code."""

    configured: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    auto_discover = True
    plugin_files = [
        Path(path).parent / "plugins.json"
        for path in ConfigManager.get_config_paths(workdir)
    ]
    effective = Path(ConfigManager.get_effective_config_root(workdir)) / "plugins.json"
    plugin_files.append(effective)
    for path in plugin_files:
        resolved = path.expanduser().resolve(strict=False)
        key = resolved.as_posix()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if not resolved.is_file():
            continue
        try:
            raw = resolved.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        auto_discover = bool(payload.get("auto_discover", auto_discover))
        sources.append(
            {
                "path": key,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        for item in payload.get("plugins", []):
            if not isinstance(item, dict):
                continue
            captured = copy.deepcopy(item)
            captured["_source_root"] = resolved.parent.as_posix()
            configured.append(captured)

    discovered: list[dict[str, Any]] = []
    if auto_discover:
        plugins_root = Path(ConfigManager.get_effective_config_root(workdir)) / "plugins"
        if plugins_root.is_dir():
            for child in sorted(plugins_root.iterdir()):
                if child.name.startswith((".", "_")):
                    continue
                manifest = child / "plugin.json"
                module = child / "plugin.py"
                if not child.is_dir() or not manifest.is_file() or not module.is_file():
                    continue
                discovered.append(
                    {
                        "name": child.name.replace("_", "-"),
                        "path": child.as_posix(),
                        "enabled": True,
                        "config": {},
                        "source": "auto_discover_snapshot",
                        "_source_root": plugins_root.parent.as_posix(),
                        "manifest_sha256": hashlib.sha256(
                            manifest.read_bytes()
                        ).hexdigest(),
                        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                    }
                )
    return {
        "schema_version": 1,
        "config_root": ConfigManager.get_effective_config_root(workdir),
        "auto_discover": auto_discover,
        "configured": configured,
        "discovered": discovered,
        "sources": sources,
    }


def _summary(config: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    providers = config.get("provider", {})
    agents = config.get("agent", {})
    permissions = config.get("permission", {})
    plugin_snapshot = config.get("__task_snapshot_plugins__", {})
    plugins = []
    if isinstance(plugin_snapshot, dict):
        plugins = [
            str(item.get("name") or item.get("path") or "")
            for item in [
                *list(plugin_snapshot.get("configured") or []),
                *list(plugin_snapshot.get("discovered") or []),
            ]
            if isinstance(item, dict) and bool(item.get("enabled", True))
        ]
    if not plugins:
        plugins = config.get("plugin", config.get("plugins", []))
    sandbox_profiles = config.get("sandbox_profiles", {})
    return {
        "model": str(config.get("model") or ""),
        "default_agent": str(config.get("default_agent") or "build"),
        "providers": sorted(providers) if isinstance(providers, dict) else [],
        "agents": sorted(agents) if isinstance(agents, dict) else [],
        "permissions": redact_config_value("permission", permissions),
        "plugins": redact_config_value("plugins", plugins),
        "sandbox_profiles": (
            sorted(sandbox_profiles) if isinstance(sandbox_profiles, dict) else []
        ),
        "command_policy": dict(policy),
    }


def _restore_sensitive_values(
    frozen: Any,
    current: Any,
    *,
    path: str = "",
) -> Any:
    """Overlay credentials only; all behavioural values remain historical."""

    if is_sensitive_config_key(path):
        return copy.deepcopy(current) if current is not None else None
    if isinstance(frozen, dict):
        current_dict = current if isinstance(current, dict) else {}
        result: dict[str, Any] = {}
        for key, value in frozen.items():
            child_path = f"{path}.{key}" if path else str(key)
            restored = _restore_sensitive_values(
                value,
                current_dict.get(key),
                path=child_path,
            )
            if restored is not None:
                result[str(key)] = restored
        return result
    if isinstance(frozen, list):
        current_list = current if isinstance(current, list) else []
        return [
            _restore_sensitive_values(
                value,
                current_list[index] if index < len(current_list) else None,
                path=path,
            )
            for index, value in enumerate(frozen)
        ]
    return copy.deepcopy(frozen)


@dataclass(frozen=True, slots=True)
class TaskConfigSnapshot:
    """A public durable snapshot plus a process-private runtime config."""

    snapshot_id: str
    created_at: str
    workdir: str
    task_id: str
    effective_config: dict[str, Any]
    runtime_config_redacted: dict[str, Any]
    summary: dict[str, Any]
    command_policy: dict[str, Any]
    config_fingerprint: str
    policy_fingerprint: str
    _runtime_config: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def runtime_config(self) -> dict[str, Any]:
        return copy.deepcopy(self._runtime_config)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "workdir": self.workdir,
            "task_id": self.task_id,
            "effective_config": copy.deepcopy(self.effective_config),
            "runtime_config": copy.deepcopy(self.runtime_config_redacted),
            "summary": copy.deepcopy(self.summary),
            "command_policy": copy.deepcopy(self.command_policy),
            "config_fingerprint": self.config_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "secrets_persisted": False,
            "secret_recovery": "overlay-current-trusted-secrets-only",
        }


class TaskConfigSnapshotStore:
    """Capture, persist, bind and replay configuration by snapshot id."""

    _runtime: dict[str, TaskConfigSnapshot] = {}
    _task_bindings: dict[tuple[str, str], str] = {}
    _lock = threading.RLock()
    _last_cleanup_at: dict[str, float] = {}

    @staticmethod
    def _root(workdir: str) -> Path:
        return (
            Path(workdir).expanduser().resolve(strict=False)
            / ".tcode"
            / "config_snapshots"
        )

    @classmethod
    def _path(cls, snapshot_id: str, workdir: str) -> Path:
        normalized = str(snapshot_id or "").strip()
        if not _SNAPSHOT_ID_RE.fullmatch(normalized):
            raise ValueError("invalid config snapshot id")
        return cls._root(workdir) / f"{normalized}.json"

    @classmethod
    def capture(
        cls,
        workdir: str,
        *,
        task_id: str = "",
        force: bool = False,
        runtime_overrides: Optional[dict[str, Any]] = None,
    ) -> TaskConfigSnapshot:
        with cls._lock:
            return cls._capture_locked(
                workdir,
                task_id=task_id,
                force=force,
                runtime_overrides=runtime_overrides,
            )

    @classmethod
    def _capture_locked(
        cls,
        workdir: str,
        *,
        task_id: str = "",
        force: bool = False,
        runtime_overrides: Optional[dict[str, Any]] = None,
    ) -> TaskConfigSnapshot:
        normalized_workdir = ConfigManager._normalize_dir(workdir)
        normalized_task_id = str(task_id or "").strip()
        binding_key = (normalized_workdir, normalized_task_id)
        if normalized_task_id and not force:
            existing_id = cls._task_bindings.get(binding_key)
            if existing_id:
                return cls.get(existing_id, normalized_workdir)

        ConfigManager.load(normalized_workdir)
        runtime_config = ConfigManager.get_config_copy()
        for key, value in dict(runtime_overrides or {}).items():
            normalized_key = str(key or "").strip()
            if normalized_key and value is not None:
                runtime_config[normalized_key] = copy.deepcopy(value)
        runtime_config["__task_snapshot_plugins__"] = _plugin_snapshot(
            normalized_workdir
        )
        effective = EffectiveConfigResolver.resolve(normalized_workdir).to_dict()
        redacted = redact_config_value("config", runtime_config, max_depth=8)
        if not isinstance(redacted, dict):
            redacted = {}
        # Plugin selection, permission allow-lists and nested tool/model config
        # are executable behaviour, not diagnostic display data.  Persist their
        # secret-redacted shape losslessly and fail closed if it exceeds the
        # explicit safety bound instead of replaying a sentinel as a permission.
        redacted_plugins = redact_config_value(
            "config.__task_snapshot_plugins__",
            runtime_config["__task_snapshot_plugins__"],
            max_depth=16,
        )
        if _contains_depth_limit(redacted_plugins):
            raise ValueError("plugin task snapshot exceeds safe nesting depth")
        redacted["__task_snapshot_plugins__"] = redacted_plugins
        policy = _command_policy(runtime_config)
        snapshot = TaskConfigSnapshot(
            snapshot_id=f"cfg-{uuid.uuid4().hex}",
            created_at=datetime.now(timezone.utc).isoformat(),
            workdir=normalized_workdir,
            task_id=normalized_task_id,
            effective_config=effective,
            runtime_config_redacted=redacted,
            summary=_summary(runtime_config, policy),
            command_policy=policy,
            config_fingerprint=_json_fingerprint(
                {
                    "effective": effective,
                    "runtime_config": redacted,
                }
            ),
            policy_fingerprint=_json_fingerprint(policy),
            _runtime_config=copy.deepcopy(runtime_config),
        )
        path = cls._path(snapshot.snapshot_id, normalized_workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".json.{os.getpid()}.{time.time_ns()}.tmp")
        temporary.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        cls._runtime[snapshot.snapshot_id] = snapshot
        if normalized_task_id:
            cls._task_bindings[binding_key] = snapshot.snapshot_id
        TelemetryGateway.emit(
            "config.snapshot.created",
            {
                "snapshot_id": snapshot.snapshot_id,
                "config_fingerprint": snapshot.config_fingerprint,
                "policy_fingerprint": snapshot.policy_fingerprint,
            },
            session_id=normalized_task_id,
        )
        last_cleanup = cls._last_cleanup_at.get(normalized_workdir, 0.0)
        if time.time() - last_cleanup >= 86400:
            cls.cleanup_expired(normalized_workdir, apply=True)
            cls._last_cleanup_at[normalized_workdir] = time.time()
        return snapshot

    @classmethod
    def cleanup_expired(
        cls,
        workdir: str,
        *,
        ttl_days: Optional[float] = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Remove expired snapshots unless a live binding or Session references them."""

        normalized_workdir = ConfigManager._normalize_dir(workdir)
        if ttl_days is None:
            try:
                ttl_days = float(os.environ.get("TCODE_CONFIG_SNAPSHOT_TTL_DAYS", "30"))
            except ValueError:
                ttl_days = 30.0
        report: dict[str, Any] = {
            "ttl_days": float(ttl_days),
            "apply": bool(apply),
            "candidate_count": 0,
            "candidate_bytes": 0,
            "deleted_count": 0,
            "protected_count": 0,
        }
        if float(ttl_days) <= 0:
            return report
        cutoff = time.time() - float(ttl_days) * 86400
        protected = set(cls._task_bindings.values())
        database = Path(normalized_workdir) / ".tcode" / "sessions.sqlite"
        if database.exists():
            try:
                connection = sqlite3.connect(database)
                try:
                    for (raw_metadata,) in connection.execute(
                        "SELECT metadata_json FROM sessions WHERE metadata_json LIKE '%cfg-%'"
                    ):
                        protected.update(_SNAPSHOT_ID_IN_TEXT_RE.findall(str(raw_metadata or "")))
                finally:
                    connection.close()
            except sqlite3.Error as exc:
                report["error"] = f"session_reference_scan_failed:{type(exc).__name__}"
                return report
        root = cls._root(normalized_workdir)
        for path in root.glob("cfg-*.json") if root.exists() else []:
            if path.stem in protected:
                report["protected_count"] += 1
                continue
            try:
                age_source = path.stat().st_mtime
                size = path.stat().st_size
            except OSError:
                continue
            if age_source > cutoff:
                continue
            report["candidate_count"] += 1
            report["candidate_bytes"] += size
            if apply:
                path.unlink(missing_ok=True)
                cls._runtime.pop(path.stem, None)
                report["deleted_count"] += 1
        return report

    @classmethod
    def get(cls, snapshot_id: str, workdir: str) -> TaskConfigSnapshot:
        with cls._lock:
            return cls._get_locked(snapshot_id, workdir)

    @classmethod
    def _get_locked(cls, snapshot_id: str, workdir: str) -> TaskConfigSnapshot:
        normalized = str(snapshot_id or "").strip()
        cached = cls._runtime.get(normalized)
        if cached is not None:
            return cached
        path = cls._path(normalized, workdir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("snapshot_id") != normalized:
            raise ValueError("invalid config snapshot payload")
        redacted = payload.get("runtime_config", {})
        if not isinstance(redacted, dict):
            raise ValueError("invalid redacted runtime config")
        policy = dict(payload.get("command_policy") or {})
        expected_policy_fingerprint = _json_fingerprint(policy)
        if str(payload.get("policy_fingerprint") or "") != expected_policy_fingerprint:
            raise ValueError("config snapshot policy fingerprint mismatch")
        expected_config_fingerprint = _json_fingerprint(
            {
                "effective": dict(payload.get("effective_config") or {}),
                "runtime_config": redacted,
            }
        )
        if str(payload.get("config_fingerprint") or "") != expected_config_fingerprint:
            raise ValueError("config snapshot fingerprint mismatch")

        # Rehydrate credentials from the current trusted source.  No other value
        # is allowed to drift, so model, permissions and policy stay historical.
        ConfigManager.load(str(payload.get("workdir") or workdir))
        current = ConfigManager.get_config_copy()
        runtime_config = _restore_sensitive_values(redacted, current)
        snapshot = TaskConfigSnapshot(
            snapshot_id=normalized,
            created_at=str(payload.get("created_at") or ""),
            workdir=str(payload.get("workdir") or workdir),
            task_id=str(payload.get("task_id") or ""),
            effective_config=dict(payload.get("effective_config") or {}),
            runtime_config_redacted=copy.deepcopy(redacted),
            summary=dict(payload.get("summary") or {}),
            command_policy=policy,
            config_fingerprint=str(payload.get("config_fingerprint") or ""),
            policy_fingerprint=str(payload.get("policy_fingerprint") or ""),
            _runtime_config=runtime_config,
        )
        cls._runtime[normalized] = snapshot
        if snapshot.task_id:
            cls._task_bindings[
                (ConfigManager._normalize_dir(snapshot.workdir), snapshot.task_id)
            ] = normalized
        return snapshot

    @classmethod
    def for_task(
        cls,
        task_id: str,
        workdir: str,
        *,
        snapshot_id: str = "",
        runtime_overrides: Optional[dict[str, Any]] = None,
    ) -> TaskConfigSnapshot:
        normalized_task_id = str(task_id or "").strip()
        requested = str(snapshot_id or "").strip()
        if requested:
            snapshot = cls.get(requested, workdir)
            if normalized_task_id:
                cls._task_bindings[
                    (ConfigManager._normalize_dir(workdir), normalized_task_id)
                ] = snapshot.snapshot_id
            return snapshot
        return cls.capture(
            workdir,
            task_id=normalized_task_id,
            runtime_overrides=runtime_overrides,
        )

    @classmethod
    def clear_memory(cls) -> None:
        """Test/restart helper; durable snapshots remain on disk."""

        with cls._lock:
            cls._runtime.clear()
            cls._task_bindings.clear()


__all__ = ["TaskConfigSnapshot", "TaskConfigSnapshotStore"]
