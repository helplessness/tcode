"""PlanManager: session-scoped structured plan persistence and validation.

Bridges the structured Plan model with session metadata so plans survive
across turns and can be inspected by verification subagents.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from .plan_schema import Plan, PlanStep, PlanStatus


class PlanManager:
    """Manages structured plans per session.

    Plans are persisted in session metadata under the `structured_plan` key
    and cached in memory for fast access during a session's lifetime.
    """

    _cache: dict[str, Plan] = {}

    # ── Public API ───────────────────────────────────────────────

    @classmethod
    def get(cls, session_id: str, *, workdir: Optional[str] = None) -> Optional[Plan]:
        """Return the cached or persisted plan for the session, or None."""
        cached = cls._cache.get(session_id)
        if cached is not None:
            return cached

        plan = cls._load_from_session(session_id)
        if plan is not None:
            cls._cache[session_id] = plan
        return plan

    @classmethod
    async def get_async(cls, session_id: str, *, workdir: Optional[str] = None) -> Optional[Plan]:
        """Async variant that reads from session storage."""
        cached = cls._cache.get(session_id)
        if cached is not None:
            return cached

        plan = await cls._load_from_session_async(session_id)
        if plan is not None:
            cls._cache[session_id] = plan
        return plan

    @classmethod
    async def save(cls, session_id: str, plan: Plan) -> Plan:
        """Persist a plan to session metadata and update the cache."""
        plan.touch()
        cls._cache[session_id] = plan
        await cls._persist_to_session(session_id, plan)
        return plan

    @classmethod
    def save_sync(cls, session_id: str, plan: Plan) -> Plan:
        """Synchronous save for contexts where async is unavailable."""
        plan.touch()
        cls._cache[session_id] = plan
        try:
            cls._persist_to_session_sync(session_id, plan)
        except Exception:
            pass
        return plan

    @classmethod
    def clear(cls, session_id: str) -> None:
        cls._cache.pop(session_id, None)

    @classmethod
    def validate_plan(cls, plan: Plan) -> list[str]:
        """Validate a plan and return errors. Also stores errors on the plan."""
        return plan.validate()

    @classmethod
    def is_plan_ready(cls, plan: Plan) -> bool:
        """Check if a plan is ready for execution (valid + reviewed/approved)."""
        if not plan.is_valid():
            return False
        return plan.status in {PlanStatus.REVIEWED, PlanStatus.APPROVED, PlanStatus.EXECUTING}

    @classmethod
    def plan_json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema for structured plans."""
        return Plan.JSON_SCHEMA

    # ── Persistence helpers ──────────────────────────────────────

    @classmethod
    def _load_from_session(cls, session_id: str) -> Optional[Plan]:
        try:
            from ..session.manager import SessionManager

            sessions = SessionManager.get_all_sync()
            session = sessions.get(session_id)
            if session is None:
                return None
            metadata = dict(getattr(session, "metadata", {}) or {})
            raw = metadata.get("structured_plan")
            if isinstance(raw, dict):
                return Plan.from_dict(raw)
            if isinstance(raw, str) and raw.strip():
                return Plan.from_dict(json.loads(raw))
            return None
        except Exception:
            return None

    @classmethod
    async def _load_from_session_async(cls, session_id: str) -> Optional[Plan]:
        try:
            from ..session.manager import SessionManager

            session = await SessionManager.get(session_id)
            if session is None:
                return None
            metadata = dict(getattr(session, "metadata", {}) or {})
            raw = metadata.get("structured_plan")
            if isinstance(raw, dict):
                return Plan.from_dict(raw)
            if isinstance(raw, str) and raw.strip():
                return Plan.from_dict(json.loads(raw))
            return None
        except Exception:
            return None

    @classmethod
    async def _persist_to_session(cls, session_id: str, plan: Plan) -> None:
        from ..session.manager import SessionManager

        session = await SessionManager.get(session_id)
        if session is None:
            return
        metadata = dict(getattr(session, "metadata", {}) or {})
        metadata["structured_plan"] = plan.to_dict()
        await SessionManager.update(
            session_id,
            metadata=metadata,
            last_active_at=datetime.now().timestamp(),
        )

    @classmethod
    def _persist_to_session_sync(cls, session_id: str, plan: Plan) -> None:
        from ..session.manager import SessionManager

        sessions = SessionManager.get_all_sync()
        session = sessions.get(session_id)
        if session is None:
            return
        metadata = dict(getattr(session, "metadata", {}) or {})
        metadata["structured_plan"] = plan.to_dict()
        # Best-effort sync update
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                future = asyncio.run_coroutine_threadsafe(
                    SessionManager.update(
                        session_id,
                        metadata=metadata,
                        last_active_at=datetime.now().timestamp(),
                    ),
                    loop,
                )
                future.result(timeout=5)
        except Exception:
            pass