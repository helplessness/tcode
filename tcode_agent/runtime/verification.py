"""Shared automatic-review budgets and per-request verification accounting."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..session.events import SessionEventStore
from ..sqlite_runtime import SQLITE_CONNECTION_LOCK, connect

VERIFICATION_POLICIES = {
    "light": {"candidate_chars": 12_000, "evidence_entries": 6, "max_tokens": 1024, "timeout": 30, "repairs": 0},
    "standard": {"candidate_chars": 48_000, "evidence_entries": 20, "max_tokens": 2048, "timeout": 60, "repairs": 1},
    "strict": {"candidate_chars": 64_000, "evidence_entries": 40, "max_tokens": 4096, "timeout": 90, "repairs": 2},
}
_recording_errors: dict[tuple[str, str], int] = {}
_log = logging.getLogger(__name__)


def _call_rows(session: Any, payload: dict | None = None) -> list[dict]:
    """Keep billing records independent of transcript compaction and retention."""
    path = Path(session.workdir) / ".tcode" / "verification_usage.sqlite"
    if payload is None and not path.exists():
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = tuple(str(getattr(session, key, "") or "") for key in
                  ("tenant_id", "user_id", "workspace_id"))
    with SQLITE_CONNECTION_LOCK:
        conn = connect(path)
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS verification_calls (
                tenant_id TEXT, user_id TEXT, workspace_id TEXT, session_id TEXT,
                call_id TEXT, payload TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, workspace_id, session_id, call_id))""")
            if payload is not None:
                conn.execute("INSERT OR REPLACE INTO verification_calls VALUES (?, ?, ?, ?, ?, ?)",
                    (*owner, str(session.id), payload["call_id"], json.dumps(payload, ensure_ascii=False, allow_nan=False)))
                conn.commit()
                return []
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM verification_calls WHERE tenant_id=? AND user_id=? AND workspace_id=? AND session_id=?",
                (*owner, str(session.id)))]
        finally:
            conn.close()


def verification_policy(mode: str, *, model: str = "") -> dict:
    policy = dict(VERIFICATION_POLICIES[mode])
    # DeepSeek V4 counts thinking and the final JSON in the same output cap.
    # Use the existing independent-review allowance unless explicitly capped.
    thinking_output = str(model).lower().rsplit("/", 1)[-1].startswith("deepseek-v4-")
    if thinking_output:
        policy["max_tokens"] = 65536
    for key, env, default, low, high in (
        ("max_tokens", "TCODE_OUTCOME_VALIDATOR_MAX_TOKENS",
         65536 if thinking_output else 2048, 512, 65536 if thinking_output else 4096),
        ("timeout", "TCODE_OUTCOME_VALIDATOR_TIMEOUT_SECONDS", 90, 5, 300),
    ):
        try:
            cap = int(os.environ.get(env, str(default))) if key == "max_tokens" else float(os.environ.get(env, str(default)))
            if not math.isfinite(cap):
                cap = default
        except ValueError:
            cap = default
        policy[key] = min(policy[key], max(low, min(high, cap)))
    policy["max_tokens"] = int(policy["max_tokens"])
    return policy


def effective_verification_settings(settings: dict) -> dict:
    """Report availability, not a promise that every task needs a model call."""
    def flag(name: str) -> bool:
        return os.environ.get(name, "0").strip().lower() not in {"0", "false", "no", "off", ""}

    enabled, mode = settings["enabled"], settings["mode"]
    available = {
        "planner": flag("TCODE_OUTCOME_INTENT_PLANNER_ENABLED") and mode != "light",
        "guard": flag("TCODE_OUTCOME_GUARD"),
        "worker_review": True,
        "retry_review": True,
        "workflow_final": flag("TCODE_EXPOSE_OUTCOME_VALIDATION") or flag("TCODE_PROOF_PACK_ENABLED"),
    }
    stages = {key: {
        "enabled": bool(enabled and value),
        "reason": "account_disabled" if not enabled else
                  "light_mode" if key == "planner" and mode == "light" else
                  "available_on_demand" if value else "deployment_disabled",
    } for key, value in available.items()}
    eligible = [value for key, value in stages.items() if not (key == "planner" and mode == "light")]
    state = "disabled" if not enabled else "enabled" if all(item["enabled"] for item in eligible) else "partial"
    policy = verification_policy(mode)
    return {"state": state, "stages": stages, "policy": policy,
            "workflow_condition": "requires_explicit_confirmation",
            "advisory": True, "worker_execution_retries": 0}


def _usage(raw: Any) -> tuple[dict, str]:
    if not isinstance(raw, dict):
        return {}, "unknown"
    # Keep provider-native cache/reasoning fields without adding them twice.
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    valid = all(isinstance(raw.get(key), int) and not isinstance(raw[key], bool) and raw[key] >= 0 for key in keys)
    usage = {key: raw[key] if isinstance(raw.get(key), int) and not isinstance(raw[key], bool) and raw[key] >= 0 else 0 for key in keys}
    valid = valid and usage["total_tokens"] >= usage["prompt_tokens"] + usage["completion_tokens"]
    return usage, "complete" if valid else "partial"


class VerificationCall:
    """One start/end event pair; exceptions and missing usage never mean free."""

    def __init__(self, provider: Any, *, source: str, session: Any = None,
                 session_id: str = "", workdir: str = "", mode: str = "",
                 role: str = "",
                 included_in_session_usage: bool = False):
        self.provider, self.source, self.session = provider, source, session
        self.session_id, self.workdir, self.mode = session_id, workdir, mode
        self.role = role
        self.included = included_in_session_usage
        self.call_id = uuid.uuid4().hex
        self.response: Any = None

    async def _append(self, event_type: str, payload: dict) -> None:
        if self.session is None:
            return
        try:
            await asyncio.to_thread(_call_rows, self.session, payload)
            await asyncio.to_thread(SessionEventStore.append, session_id=self.session_id,
                event_type=event_type, payload=payload, workdir=self.workdir,
                owner={key: str(getattr(self.session, key, "") or "")
                       for key in ("tenant_id", "user_id", "workspace_id")},
                idempotency_key=f"{event_type}:{self.call_id}")
        except Exception:
            key = (self.workdir, self.session_id)
            _recording_errors[key] = _recording_errors.get(key, 0) + 1
            _log.exception("Verification accounting unavailable for call %s", self.call_id)

    async def __aenter__(self):
        if self.session is None and self.session_id:
            from ..session.manager import SessionManager
            self.session = await SessionManager.get(self.session_id)
        if self.session is not None:
            self.session_id = str(self.session.id)
            self.workdir = str(getattr(self.session, "workdir", "") or self.workdir)
        self.started = time.monotonic()
        self.base = {"call_id": self.call_id, "source": self.source, "mode": self.mode,
            "role": self.role, "started_at": time.time(),
            "provider": str(getattr(self.provider, "name", "")),
            "model": str(getattr(self.provider, "model", "") or getattr(self.provider, "_model", "")),
            "included_in_session_usage": self.included}
        await self._append("verification.model.started", self.base)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        raw = getattr(self.response, "usage", None) or getattr(exc, "usage", None)
        usage, status = _usage(raw)
        await self._append("verification.model.finished", {**self.base,
            "usage": usage, "raw_usage": raw if isinstance(raw, dict) else None,
            "usage_status": status,
            "elapsed_seconds": round(time.monotonic() - self.started, 4),
            "finished_at": time.time(),
            "provider_request_id": str(getattr(self.response, "request_id", "") or ""),
            "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "error" if exc else "completed",
            "error_type": type(exc).__name__ if exc else "",
            "finish_reason": str(getattr(self.response, "finish_reason", "") or "")})
        return False


async def verification_chat(provider: Any, *, source: str, session: Any = None,
                            session_id: str = "", workdir: str = "", mode: str = "", role: str = "", **kwargs):
    async with VerificationCall(provider, source=source, session=session,
            session_id=session_id, workdir=workdir, mode=mode, role=role) as call:
        call.response = await provider.chat(**kwargs)
        return call.response


def verification_usage_summary(session: Any) -> dict:
    calls = {item["call_id"]: item for item in _call_rows(session)}
    known = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    additional = dict(known)
    unknown = 0
    for call in calls.values():
        if call.get("usage_status") != "complete":
            unknown += 1
        for key in known:
            value = int((call.get("usage") or {}).get(key, 0))
            known[key] += value
            if not call.get("included_in_session_usage"):
                additional[key] += value
    errors = _recording_errors.get((str(session.workdir), str(session.id)), 0)
    return {"session_id": session.id, "calls": list(calls.values()), "known_usage": known,
            "additional_known_usage": additional, "unknown_calls": unknown,
            "recording_errors": errors,
            "status": "partial" if errors else "not_recorded" if not calls else "partial" if unknown else "complete",
            "scope": "verification_calls_in_this_session_since_instrumentation"}
