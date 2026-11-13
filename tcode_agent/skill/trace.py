"""Skill 目录快照与路由 Trace 持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .routing_config import resolve_skill_routing_output_path

from ..file_lock import append_locked_text, state_thread_lock


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query_fingerprint(query: str) -> str:
    return hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:20]


class SkillRoutingTraceStore:
    _thread_lock = threading.Lock()

    @classmethod
    def write_catalog_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        workdir: Optional[str],
        config: dict[str, Any],
    ) -> Optional[str]:
        catalog_cfg = config.get("catalog", {}) if isinstance(config.get("catalog"), dict) else {}
        if not bool(catalog_cfg.get("write_snapshot", False)):
            return None
        path = resolve_skill_routing_output_path(
            str(catalog_cfg.get("snapshot_path") or ".tcode/skill-routing/catalog.json"),
            workdir=workdir,
        )
        payload = dict(snapshot)
        payload.setdefault("written_at", utc_now_iso())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
            return str(path)
        except Exception:
            return None

    @classmethod
    def append_route_trace(
        cls,
        trace: dict[str, Any],
        *,
        workdir: Optional[str],
        config: dict[str, Any],
    ) -> Optional[str]:
        trace_cfg = config.get("trace", {}) if isinstance(config.get("trace"), dict) else {}
        if not bool(trace_cfg.get("enabled", False)):
            return None
        path = resolve_skill_routing_output_path(
            str(trace_cfg.get("path") or ".tcode/skill-routing/traces.jsonl"),
            workdir=workdir,
        )
        payload = dict(trace)
        payload.setdefault("created_at", utc_now_iso())
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with state_thread_lock(cls._thread_lock):
                append_locked_text(path, line)
            return str(path)
        except Exception:
            return None

