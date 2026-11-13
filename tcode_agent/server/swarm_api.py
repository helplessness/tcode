"""Swarm observability API kept separate from the main server module."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..session.manager import SessionManager
from ..swarm.observability import SwarmTraceStore
from .auth import current_user_from_request


router = APIRouter(prefix="/api/swarm", tags=["swarm"])


async def _owned_session(session_id: str, request: Request):
    user = current_user_from_request(request)
    owner = {}
    if user is not None:
        owner = {"tenant_id": user.tenant_id, "user_id": user.user_id}
    session = await SessionManager.get(session_id, **owner)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@router.get("/sessions/{session_id}/runs")
async def list_session_swarm_runs(
    session_id: str,
    request: Request,
    limit: int = Query(10, ge=1, le=100),
):
    session = await _owned_session(session_id, request)
    workdir = str(getattr(session, "workdir", "") or "").strip()
    return {
        "session_id": session_id,
        "runs": SwarmTraceStore.list_runs(
            session_id=session_id,
            workdir=workdir or None,
            limit=limit,
        ),
    }


@router.get("/sessions/{session_id}/runs/{run_id}")
async def get_session_swarm_run(session_id: str, run_id: str, request: Request):
    session = await _owned_session(session_id, request)
    workdir = str(getattr(session, "workdir", "") or "").strip()
    run = SwarmTraceStore.get(run_id, workdir=workdir or None)
    if not run or str(run.get("session_id", "")) != session_id:
        raise HTTPException(404, "Swarm run not found")
    return run
