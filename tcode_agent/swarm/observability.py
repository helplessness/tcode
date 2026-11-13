"""Persistent, session-scoped observability records for swarm runs."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional


class SwarmTraceStore:
    _LOCK = threading.RLock()

    @classmethod
    def _root(cls, workdir: Optional[str]) -> Path:
        base = Path(workdir or os.environ.get("TCODE_WORKDIR", os.getcwd()))
        root = base / ".tcode" / "runtime" / "swarm_runs"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(run_id or ""))

    @classmethod
    def _path(cls, run_id: str, workdir: Optional[str]) -> Path:
        return cls._root(workdir) / f"{cls._safe_run_id(run_id)}.json"

    @classmethod
    def _load_path(cls, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _save(cls, payload: dict[str, Any], workdir: Optional[str]) -> dict[str, Any]:
        path = cls._path(str(payload.get("run_id", "")), workdir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return payload

    @classmethod
    def start(
        cls,
        *,
        run_id: str,
        session_id: str,
        workdir: Optional[str],
        topology: str,
        bus_backend: str,
        agent_names: list[str],
        max_rounds: int,
    ) -> dict[str, Any]:
        now = time.time()
        payload = {
            "run_id": run_id,
            "session_id": session_id,
            "status": "running",
            "topology": topology,
            "bus_backend": bus_backend,
            "agent_names": list(agent_names),
            "max_rounds": max_rounds,
            "started_at": now,
            "updated_at": now,
            "events": [],
            "rounds": [],
            "messages": [],
            "metrics": {},
        }
        with cls._LOCK:
            return cls._save(payload, workdir)

    @classmethod
    def _update(
        cls,
        run_id: str,
        workdir: Optional[str],
        updater,
    ) -> dict[str, Any]:
        with cls._LOCK:
            path = cls._path(run_id, workdir)
            payload = cls._load_path(path)
            if not payload:
                payload = {"run_id": run_id, "events": [], "rounds": [], "messages": []}
            updater(payload)
            payload["updated_at"] = time.time()
            return cls._save(payload, workdir)

    @classmethod
    def append_event(
        cls,
        run_id: str,
        *,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        workdir: Optional[str],
    ) -> dict[str, Any]:
        def _append(record: dict[str, Any]) -> None:
            events = record.setdefault("events", [])
            events.append({"kind": kind, "created_at": time.time(), **dict(payload or {})})
            record["events"] = events[-300:]

        return cls._update(run_id, workdir, _append)

    @classmethod
    def record_round(
        cls,
        run_id: str,
        *,
        round_data: dict[str, Any],
        agent_updates: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        workdir: Optional[str],
    ) -> dict[str, Any]:
        def _append(record: dict[str, Any]) -> None:
            rounds = record.setdefault("rounds", [])
            rounds.append({**round_data, "agents": agent_updates})
            record["rounds"] = rounds[-50:]
            message_log = record.setdefault("messages", [])
            message_log.extend(messages)
            record["messages"] = message_log[-500:]

        return cls._update(run_id, workdir, _append)

    @classmethod
    def finish(
        cls,
        run_id: str,
        *,
        status: str,
        metrics: Optional[dict[str, Any]],
        error: str = "",
        workdir: Optional[str],
    ) -> dict[str, Any]:
        def _finish(record: dict[str, Any]) -> None:
            record["status"] = status
            record["finished_at"] = time.time()
            record["metrics"] = dict(metrics or {})
            if error:
                record["error"] = str(error)[:2000]

        return cls._update(run_id, workdir, _finish)

    @classmethod
    def get(cls, run_id: str, *, workdir: Optional[str]) -> Optional[dict[str, Any]]:
        payload = cls._load_path(cls._path(run_id, workdir))
        return payload or None

    @classmethod
    def list_runs(
        cls,
        *,
        session_id: str = "",
        workdir: Optional[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in cls._root(workdir).glob("*.json"):
            payload = cls._load_path(path)
            if not payload:
                continue
            if session_id and str(payload.get("session_id", "")) != session_id:
                continue
            records.append(payload)
        records.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
        return records[: max(1, min(int(limit or 20), 100))]
