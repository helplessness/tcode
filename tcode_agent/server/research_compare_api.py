"""Personal-session API for the first research comparison adapter."""
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from starlette.concurrency import run_in_threadpool

from ..agui.auth import APIKeyPrincipal
from ..presentation.research_compare import checked_result, read_evidence, selection_for, save_selection, decision_state, save_decisions, decision_history
from ..presentation.result_selection import enabled
from ..session.manager import SessionManager
from .auth import require_current_user
from .intelligent_ui_api import rate_limiter, _effective_style
from ..agui.rate_limit import RateLimitExceeded

router = APIRouter(prefix="/api/intelligent-ui/v1/comparisons", tags=["IntelligentUI"])


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(default="", max_length=2000)


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str = Field(min_length=1, max_length=256)
    state: Literal["adopt", "defer", "exclude"]
    reason: str = Field(min_length=1, max_length=500)


class Decisions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decisions: list[CandidateDecision] = Field(max_length=20)
    research_id: str = Field(default="", max_length=160)
    expected_revision: int = Field(ge=0)


async def _session(request, session_id, write=False):
    user = require_current_user(request)
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "comparison_requires_user_session")
    if write and not enabled():
        raise HTTPException(403, "result_selection_disabled")
    try:
        rate_limiter.consume(f"compare:{user.tenant_id}:{user.user_id}", "write" if write else "read", limit=60 if write else 300)
    except RateLimitExceeded as exc:
        raise HTTPException(429, "comparison_rate_limited") from exc
    session = await SessionManager.get(session_id, tenant_id=user.tenant_id, user_id=user.user_id)
    if session is None:
        raise HTTPException(404, "session_not_found")
    return session


async def _call(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except ValueError as exc:
        raise HTTPException(409 if str(exc) in {"result_changed_reload_required", "research_decision_changed_reload_required"} else 422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(404, "comparison_artifact_unavailable") from exc


@router.get("/{run_id}")
async def read_comparison(run_id: str, request: Request, session_id: str = Query(min_length=1, max_length=200),
                          result_digest: str = Query(pattern=r"^[a-f0-9]{64}$")):
    session = await _session(request, session_id)
    value = await _call(checked_result, session.workdir, run_id, result_digest)
    value["selection"] = selection_for(session, run_id)
    value["display_style"] = await _effective_style(request)
    value["decision_state"] = await _call(decision_state, session, result_digest, run_id)
    if value["selection"] and value["selection"].get("result_digest") != result_digest:
        value["selection_status"] = "stale"
    else:
        value["selection_status"] = "current"
    return value


@router.post("/{run_id}/decisions")
async def decide_candidates(run_id: str, body: Decisions, request: Request, session_id: str = Query(min_length=1, max_length=200)):
    session = await _session(request, session_id, True)
    return await _call(save_decisions, session, run_id, body.result_digest,
        [d.model_dump() for d in body.decisions], body.research_id, body.expected_revision)


@router.get("/{run_id}/decisions/history")
async def read_decision_history(run_id: str, request: Request, session_id: str = Query(min_length=1, max_length=200),
                                before_revision: int = Query(default=0, ge=0)):
    session = await _session(request, session_id)
    return await _call(decision_history, session, run_id, before_revision)


@router.post("/{run_id}/selection")
async def select_candidates(run_id: str, body: Selection, request: Request, session_id: str = Query(min_length=1, max_length=200)):
    session = await _session(request, session_id, True)
    try:
        result = await SessionManager.mutate(session.id,
            lambda current: save_selection(current, run_id, body.result_digest, body.candidate_ids, body.reason),
            tenant_id=session.tenant_id, user_id=session.user_id, workspace_id=session.workspace_id)
        if result is None:
            raise HTTPException(404, "session_not_found")
        return result
    except ValueError as exc:
        raise HTTPException(409 if str(exc) == "result_changed_reload_required" else 422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(404, "comparison_artifact_unavailable") from exc


@router.get("/{run_id}/evidence")
async def evidence(run_id: str, request: Request, session_id: str = Query(min_length=1, max_length=200),
                   result_digest: str = Query(pattern=r"^[a-f0-9]{64}$"),
                   candidate_id: str = Query(min_length=1, max_length=256), reference: str = Query(min_length=1, max_length=500)):
    session = await _session(request, session_id)
    return await _call(read_evidence, session.workdir, run_id, result_digest, candidate_id, reference)
