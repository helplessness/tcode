"""Persistent per-agent activation overrides for skills."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class SkillActivationStore:
    """Stores opt-out overrides without modifying skill packages or agent files."""

    _lock = threading.RLock()
    VERSION = 1

    @classmethod
    def _path(cls, workdir: Optional[str] = None) -> Path:
        # The web server may run sessions in child workspaces. Keep activation
        # overrides at the configured project root so every agent sees the same
        # operator choice immediately.
        configured_root = str(os.environ.get("TCODE_WORKDIR") or "").strip()
        configured_path = Path(configured_root).expanduser() if configured_root else None
        root = (
            configured_path
            if configured_path is not None and configured_path.exists()
            else Path(str(workdir or os.getcwd())).expanduser()
        )
        base = root
        if base.is_file():
            base = base.parent
        return base.resolve(strict=False) / ".tcode" / "skill-activation.json"

    @classmethod
    def _empty(cls) -> dict[str, Any]:
        return {"version": cls.VERSION, "agents": {}}

    @classmethod
    def load(cls, workdir: Optional[str] = None) -> dict[str, Any]:
        path = cls._path(workdir)
        with cls._lock:
            if not path.exists():
                return cls._empty()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return cls._empty()
            if not isinstance(payload, dict):
                return cls._empty()
            agents = payload.get("agents")
            if not isinstance(agents, dict):
                agents = {}
            return {
                "version": cls.VERSION,
                "agents": agents,
                "updated_at": str(payload.get("updated_at") or ""),
            }

    @classmethod
    def is_enabled(
        cls,
        skill_name: str,
        agent_name: str,
        workdir: Optional[str] = None,
    ) -> bool:
        skill = str(skill_name or "").strip().lower()
        agent = str(agent_name or "").strip().lower()
        if not skill or not agent:
            return True
        payload = cls.load(workdir)
        agents = payload.get("agents", {})
        for scope in ("*", agent):
            config = agents.get(scope, {})
            if not isinstance(config, dict):
                continue
            disabled = {
                str(item or "").strip().lower()
                for item in config.get("disabled", [])
                if str(item or "").strip()
            }
            if skill in disabled:
                return False
        return True

    @classmethod
    def set_enabled(
        cls,
        skill_name: str,
        agent_name: str,
        enabled: bool,
        workdir: Optional[str] = None,
    ) -> bool:
        skill = str(skill_name or "").strip().lower()
        agent = str(agent_name or "").strip().lower()
        if not skill:
            raise ValueError("skill_name is required")
        if not agent:
            raise ValueError("agent_name is required")

        path = cls._path(workdir)
        with cls._lock:
            payload = cls.load(workdir)
            agents = payload.setdefault("agents", {})
            config = agents.get(agent)
            if not isinstance(config, dict):
                config = {}
            disabled = {
                str(item or "").strip().lower()
                for item in config.get("disabled", [])
                if str(item or "").strip()
            }
            if enabled:
                disabled.discard(skill)
            else:
                disabled.add(skill)

            if disabled:
                config["disabled"] = sorted(disabled)
                agents[agent] = config
            else:
                config.pop("disabled", None)
                if config:
                    agents[agent] = config
                else:
                    agents.pop(agent, None)

            payload["version"] = cls.VERSION
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".json.tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, path)
        return cls.is_enabled(skill, agent, workdir)

    @classmethod
    def snapshot(cls, workdir: Optional[str] = None) -> dict[str, Any]:
        return cls.load(workdir)
