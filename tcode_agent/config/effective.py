"""Effective Config 来源解释、脱敏快照与显式 Reload 合同。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .manager import ConfigManager


_SECRET_MARKERS = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "AUTHORIZATION",
    "COOKIE",
)
_RUNTIME_ENV_RE = re.compile(r"^(?:TCODE|TCODE|TMCP|SYMPHONY)_")


def is_sensitive_config_key(name: str) -> bool:
    """仅依据键名判断值是否必须完全脱敏。"""

    normalized = str(name or "").upper()
    return any(marker in normalized for marker in _SECRET_MARKERS)


def redact_config_value(
    name: str,
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 5,
) -> Any:
    """递归脱敏配置值，并限制可诊断输出的深度和长度。"""

    if is_sensitive_config_key(name):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:476] + " ...[truncated]"
    if depth >= max_depth:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {
            str(key)[:160]: redact_config_value(
                f"{name}.{key}" if name else str(key),
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [
            redact_config_value(
                name,
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in value[:100]
        ]
    return str(value)[:500]


@dataclass(frozen=True, slots=True)
class EffectiveConfigEntry:
    """单个 Effective Config 键的值、来源和重载属性。"""

    key: str
    value: Any
    source: str
    reloadability: str
    sensitive: bool

    def to_dict(self) -> dict[str, Any]:
        """返回已脱敏的配置条目。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class EffectiveConfigSnapshot:
    """一次可比较但不会泄露 Secret 的有效配置快照。"""

    workdir: str
    config_root: str
    sources: tuple[str, ...]
    entries: tuple[EffectiveConfigEntry, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        """返回 API 和合同测试使用的字典结构。"""

        return {
            "workdir": self.workdir,
            "config_root": self.config_root,
            "sources": list(self.sources),
            "entries": [entry.to_dict() for entry in self.entries],
            "fingerprint": self.fingerprint,
        }


class EffectiveConfigResolver:
    """解析 JSON 配置和进程环境的最终来源并提供显式重载。"""

    _snapshots: dict[str, EffectiveConfigSnapshot] = {}

    @staticmethod
    def _flatten(value: Any, *, prefix: str = "") -> dict[str, Any]:
        """把嵌套字典展开为稳定点路径；数组作为一个原子值处理。"""

        if not isinstance(value, dict):
            return {prefix: value} if prefix else {}
        flattened: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                flattened.update(EffectiveConfigResolver._flatten(item, prefix=path))
            else:
                flattened[path] = item
        return flattened

    @staticmethod
    def _source_maps(paths: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
        """按合并顺序计算每个 JSON 点路径的最终值和最后覆盖来源。"""

        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for path_text in paths:
            path = Path(path_text)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            for key, value in EffectiveConfigResolver._flatten(payload).items():
                values[key] = value
                sources[key] = path.resolve().as_posix()
        return values, sources

    @staticmethod
    def _fingerprint(entries: list[EffectiveConfigEntry]) -> str:
        """使用已脱敏条目计算稳定摘要，避免摘要成为 Secret 旁路。"""

        payload = [entry.to_dict() for entry in entries]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def resolve(cls, workdir: Optional[str] = None) -> EffectiveConfigSnapshot:
        """解析当前工作目录的有效配置、来源和 Reloadability。"""

        normalized_workdir = ConfigManager._normalize_dir(workdir, fallback=os.getcwd())
        ConfigManager.load(normalized_workdir)
        paths = ConfigManager.get_config_paths(normalized_workdir)
        values, sources = cls._source_maps(paths)
        entries: list[EffectiveConfigEntry] = []
        for key in sorted(values):
            sensitive = is_sensitive_config_key(key)
            entries.append(
                EffectiveConfigEntry(
                    key=key,
                    value=redact_config_value(key, values[key]),
                    source=sources.get(key, "tcode.json"),
                    reloadability="reloadable",
                    sensitive=sensitive,
                )
            )
        for key in sorted(name for name in os.environ if _RUNTIME_ENV_RE.match(name)):
            sensitive = is_sensitive_config_key(key)
            entries.append(
                EffectiveConfigEntry(
                    key=f"env.{key}",
                    value=redact_config_value(key, os.environ.get(key, "")),
                    source="process-environment",
                    reloadability="startup-only",
                    sensitive=sensitive,
                )
            )
        entries.sort(key=lambda item: item.key)
        snapshot = EffectiveConfigSnapshot(
            workdir=normalized_workdir,
            config_root=ConfigManager.resolve_config_root(normalized_workdir),
            sources=tuple(path for path in paths),
            entries=tuple(entries),
            fingerprint=cls._fingerprint(entries),
        )
        cls._snapshots[normalized_workdir] = snapshot
        return snapshot

    @classmethod
    def reload(cls, workdir: Optional[str] = None) -> dict[str, Any]:
        """显式重载 JSON 配置并返回不含 Secret 值的差异摘要。

        修改前状态优先使用上一次已经解析并对运行时可见的快照，避免文件先被
        外部写入后，再次读取磁盘把新值误当作 Reload 前状态。
        """

        normalized_workdir = ConfigManager._normalize_dir(workdir, fallback=os.getcwd())
        before = cls._snapshots.get(normalized_workdir)
        if before is None:
            before = cls.resolve(normalized_workdir)
        ConfigManager.reload(before.workdir, emit=True)
        after = cls.resolve(before.workdir)
        before_map = {entry.key: entry.to_dict() for entry in before.entries}
        after_map = {entry.key: entry.to_dict() for entry in after.entries}
        changed_keys = sorted(
            key
            for key in set(before_map) | set(after_map)
            if before_map.get(key) != after_map.get(key)
        )
        return {
            "before_fingerprint": before.fingerprint,
            "after_fingerprint": after.fingerprint,
            "changed": before.fingerprint != after.fingerprint,
            "changed_keys": changed_keys,
            "snapshot": after.to_dict(),
        }


def effective_config_contract_document() -> dict[str, Any]:
    """返回第三批 Effective Config 合同说明。"""

    return {
        "schema_version": 1,
        "contract_id": "tcode.effective-config.v1",
        "source_precedence": "config-chain-then-process-environment",
        "secret_values": "always-redacted",
        "fingerprint_uses_redacted_values": True,
        "reloadable": ["tcode.json"],
        "startup_only": ["process-environment"],
        "reload_is_explicit": True,
        "task_snapshot": {
            "schema_version": 1,
            "immutable_after_task_start": True,
            "persisted_secret_values": False,
            "secret_recovery": "overlay-current-trusted-secrets-only",
            "lookup_endpoint": "/api/config/snapshot",
            "replay_endpoint": "/api/config/snapshot/replay",
        },
    }


__all__ = [
    "EffectiveConfigEntry",
    "EffectiveConfigResolver",
    "EffectiveConfigSnapshot",
    "effective_config_contract_document",
    "is_sensitive_config_key",
    "redact_config_value",
]
