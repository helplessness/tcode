"""Kanban card projection and dismissal endpoints.

Routes retain their original paths and are registered by app.py.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..session.manager import SessionManager
from ..runtime.guidance_store import GuidanceSQLiteStore
from ._shared import _guidance_store_workdir
from ..team.registry import WorkerRegistry
from . import workdirs as workdir_utils
from .workspace_scope import (
    _list_user_workdirs,
    _session_owner_kwargs,
    _workspace_context_from_request,
)
from .kanban import (
    KanbanHiddenStore,
    build_kanban_cards,
    filter_hidden_cards,
    filter_kanban_cards,
    kanban_counts,
    kanban_poll_interval_seconds,
)
from .state import ServerState

router = APIRouter()


def _kanban_cards_for_user(
    workspace_context: workdir_utils.WorkspaceContext,
    *,
    workspace_id: str = "",
    session_id: str = "",
) -> list[dict[str, Any]]:
    sessions = SessionManager.get_all_sync(
        **_session_owner_kwargs(workspace_context),
    )
    pending = GuidanceSQLiteStore.active_counts(workdir=_guidance_store_workdir())
    running_session_ids = {
        session_id for session_id in sessions
        if ServerState.is_running(session_id) or session_id in pending
    }
    cards = build_kanban_cards(
        sessions=sessions.values(),
        workdirs=_list_user_workdirs(workspace_context),
        review_workdirs=[ServerState.get_workdir()],
        worker_lookup=WorkerRegistry.get,
        running_session_ids=running_session_ids,
    )
    hidden_ids = KanbanHiddenStore.hidden_refs(
        ServerState.get_workdir(),
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
    )
    return filter_kanban_cards(
        filter_hidden_cards(
            cards,
            hidden_ids=hidden_ids,
        ),
        workspace_id=workspace_id,
        session_id=session_id,
    )


@router.get("/api/kanban")
async def get_kanban(
    request: Request,
    workspace_id: str = Query(default=""),
    session_id: str = Query(default=""),
):
    workspace_context = _workspace_context_from_request(request)
    filter_workspace = str(workspace_id or "").strip()
    filter_session = str(session_id or "").strip()
    cards = _kanban_cards_for_user(
        workspace_context,
        workspace_id=filter_workspace,
        session_id=filter_session,
    )
    return {
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "filters": {
            "workspace_id": filter_workspace,
            "session_id": filter_session,
        },
        "poll_interval_seconds": kanban_poll_interval_seconds(),
        "counts": kanban_counts(cards),
        "cards": cards,
    }


@router.delete("/api/kanban/cards/{card_id}")
async def delete_kanban_card(card_id: str, request: Request):
    workspace_context = _workspace_context_from_request(request)
    card = next(
        (
            item
            for item in _kanban_cards_for_user(workspace_context)
            if str(item.get("id") or "") == str(card_id or "")
        ),
        None,
    )
    if card is None:
        raise HTTPException(404, "Kanban card not found")
    try:
        KanbanHiddenStore.hide(
            ServerState.get_workdir(),
            tenant_id=workspace_context.tenant_id,
            user_id=workspace_context.user_id,
            card=card,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "deleted", "card_id": card_id}
