"""Human review queue helpers for MCP tool calls."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Optional

import aiohttp

from ..tools.base import ToolContext
from ._utils import (
    _build_review_session_data,
    _build_review_workspace_hint,
    _context_control_workdir,
    _extract_preview_ports,
)


def _record_human_review_runtime(
    *,
    issue_id: str,
    identifier: str,
    status: str,
    server_name: str,
    tool_name: str,
    args: dict,
    context: Optional[ToolContext],
    review_check: dict,
    tracker: str,
) -> None:
    try:
        from ..runtime.task_runtime import (
            TaskRuntimeStore,
            task_runtime_id_for_human_review,
        )

        TaskRuntimeStore.record_event(
            runtime_id=task_runtime_id_for_human_review(identifier),
            runtime_kind="human_review",
            status="waiting_review",
            phase=status or "pending_review",
            source="human_review",
            session_id=str(getattr(context, "session_id", "") or "").strip(),
            title=f"{server_name}.{tool_name}",
            external_id=identifier,
            tool_name=tool_name,
            metadata={
                "id": issue_id,
                "identifier": identifier,
                "tracker": tracker,
                "server_name": server_name,
                "tool_name": tool_name,
                "args": args,
                "reason": review_check.get("reason", ""),
                "trigger": review_check.get("trigger", ""),
                "check_type": review_check.get("check_type", ""),
                "supports_resume": True,
                "interrupt_behavior": "human_review",
            },
            workdir=_context_control_workdir(context) or None,
        )
    except Exception:
        pass


def _store_review_mirror(
    *,
    issue_id: str,
    identifier: str,
    status: str,
    server_name: str,
    tool_name: str,
    args: dict,
    context: Optional[ToolContext],
    review_check: dict,
    payload: dict[str, Any],
) -> tuple[str, str, str]:
    try:
        from ..runtime.shared_tracker import SharedTracker

        stored = SharedTracker.upsert_review(
            {
                "id": issue_id,
                "identifier": identifier,
                "status": status or "pending_review",
                "tool_server": server_name,
                "tool_name": tool_name,
                "tool_args": args,
                "reason": review_check.get("reason", "需要人工确认"),
                "review_data": payload.get("review_data", {}),
            },
            _context_control_workdir(context),
        )
        return (
            str(stored.get("id", "") or issue_id),
            str(stored.get("identifier", "") or identifier),
            str(stored.get("status", "") or status or "pending_review"),
        )
    except Exception:
        return issue_id, identifier, status or "pending_review"


async def enqueue_human_review_issue(
    *,
    server_name: str,
    tool_name: str,
    args: dict,
    context: Optional[ToolContext],
    review_check: dict,
    description: str = "",
    input_schema: Optional[dict] = None,
    review_data_extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """将需要人工 review 的任务加入队列。"""
    from ..workspace.input_resources import input_file_contract
    contract = input_file_contract(context, server_name, tool_name)
    if contract is not None:
        review_data_extra = {**(review_data_extra or {}), "input_file_contract": contract}
    issue_id = f"review-{uuid.uuid4().hex[:10]}"
    identifier = f"REVIEW-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    review_session_data: dict[str, Any] = {}
    review_workspace: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    try:
        api_base = (
            os.environ.get("TCODE_HUMAN_REVIEW_API_BASE")
            or os.environ.get("SYMPHONY_API_BASE", "http://127.0.0.1:7890")
        ).rstrip("/")
        url = f"{api_base}/api/human-review/submit"
        review_session_data = await _build_review_session_data(context)
        if context is not None:
            from ..runtime.async_resume import capture_task_context
            review_session_data['task_context'] = await capture_task_context(context)
        review_workspace = _build_review_workspace_hint(
            context=context,
            target_server=server_name,
            target_tool=tool_name,
            preview_ports=_extract_preview_ports(args),
            input_snapshot=args,
        )
        review_workspace["issue_identifier"] = identifier

        payload = {
            "issue_id": issue_id,
            "issue_identifier": identifier,
            "tool_server": server_name,
            "tool_name": tool_name,
            "tool_args": args,
            "reason": review_check.get("reason", "需要人工确认"),
            "review_data": {
                "trigger": review_check.get("trigger", "unknown"),
                **review_session_data,
                **(review_data_extra or {}),
                "workspace": review_workspace,
            },
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status < 300:
                    result = await resp.json()
                    stored_id, stored_identifier, stored_status = _store_review_mirror(
                        issue_id=str(result.get("id", "") or issue_id),
                        identifier=str(result.get("identifier", "") or identifier),
                        status=str(result.get("status", "") or "pending_review"),
                        server_name=server_name,
                        tool_name=tool_name,
                        args=args,
                        context=context,
                        review_check=review_check,
                        payload=payload,
                    )
                    _record_human_review_runtime(
                        issue_id=stored_id,
                        identifier=stored_identifier,
                        status=stored_status,
                        server_name=server_name,
                        tool_name=tool_name,
                        args=args,
                        context=context,
                        review_check=review_check,
                        tracker="human_review",
                    )
                    return {
                        "id": stored_id,
                        "identifier": stored_identifier,
                        "status": stored_status,
                        "tracker": "human_review",
                        "mirror_tracker": "sqlite",
                        "review_reason": review_check.get("reason", ""),
                        "review_trigger": review_check.get("trigger", ""),
                    }
    except Exception:
        pass

    if not payload:
        if not review_session_data:
            try:
                review_session_data = await _build_review_session_data(context)
            except Exception:
                review_session_data = {}
        try:
            review_workspace = _build_review_workspace_hint(
                context=context,
                target_server=server_name,
                target_tool=tool_name,
                preview_ports=_extract_preview_ports(args),
                input_snapshot=args,
            )
        except Exception:
            review_workspace = {}
        review_workspace["issue_identifier"] = identifier
        payload = {
            "issue_id": issue_id,
            "issue_identifier": identifier,
            "tool_server": server_name,
            "tool_name": tool_name,
            "tool_args": args,
            "reason": review_check.get("reason", "需要人工确认"),
            "review_data": {
                "trigger": review_check.get("trigger", "unknown"),
                **review_session_data,
                **(review_data_extra or {}),
                "workspace": review_workspace,
            },
        }

    issue_id, identifier, status = _store_review_mirror(
        issue_id=issue_id,
        identifier=identifier,
        status="pending_review",
        server_name=server_name,
        tool_name=tool_name,
        args=args,
        context=context,
        review_check=review_check,
        payload=payload,
    )
    _record_human_review_runtime(
        issue_id=issue_id,
        identifier=identifier,
        status=status,
        server_name=server_name,
        tool_name=tool_name,
        args=args,
        context=context,
        review_check=review_check,
        tracker="sqlite",
    )

    return {
        "id": issue_id,
        "identifier": identifier,
        "status": status,
        "tracker": "sqlite",
        "review_reason": review_check.get("reason", ""),
        "review_trigger": review_check.get("trigger", ""),
    }


async def _enqueue_human_review_issue(
    self, args: dict, context: Optional[ToolContext], review_check: dict
) -> dict[str, Any]:
    return await enqueue_human_review_issue(
        server_name=self.server_name,
        tool_name=self._tool_name,
        args=args,
        context=context,
        review_check=review_check,
    )
