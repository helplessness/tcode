"""Interpret Job Protocol cancellation receipts without assuming HTTP success stops work."""
from __future__ import annotations

import asyncio
import json
from typing import Any


def cancellation_receipt(raw: Any, job_ref: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("isError"):
        raise ValueError("远程服务未确认取消，请稍后重试")
    payload = raw.get("structuredContent", raw)
    if isinstance(payload, dict) and "job_id" not in payload:
        for item in raw.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    candidate = json.loads(item.get("text", ""))
                except (TypeError, ValueError):
                    continue
                if isinstance(candidate, dict) and "job_id" in candidate:
                    payload = candidate
                    break
    if not isinstance(payload, dict) or str(payload.get("job_id") or "") != str(job_ref.get("job_id") or ""):
        raise ValueError("远程取消回执与任务不匹配，尚未确认停止")
    if job_ref.get("process_id") and str(payload.get("process_id") or "") != str(job_ref["process_id"]):
        raise ValueError("远程取消回执与执行不匹配，尚未确认停止")
    status = str(payload.get("status") or "").lower()
    terminal = {"completed", "done", "failed", "error", "timed_out", "terminated", "cancelled", "canceled"}
    if status not in terminal:
        raise ValueError("远程任务仍未确认停止，请稍后重试取消")
    if status in {"completed", "done"} and (payload.get("terminated") is True or payload.get("reason") == "already_finished"):
        state = "Done"
        confirmed = False
    elif payload.get("terminated") is True:
        state = "Cancelled"
        confirmed = True
    elif payload.get("reason") == "already_finished":
        state = "Done" if status in {"completed", "done"} else "Cancelled" if status in {"terminated", "cancelled", "canceled"} else "Failed"
        confirmed = state == "Cancelled"
    else:
        raise ValueError("远程服务未提供可验证的终止回执")
    return {"state": state, "cancelled": confirmed, "remote_status": status,
            "job_id": str(payload["job_id"]), "reason": str(payload.get("reason") or "")[:200]}


async def cancel_remote_job(job_ref: dict[str, Any]) -> dict[str, Any]:
    raw = await call_job_tool(job_ref, "terminate_job")
    return cancellation_receipt(raw, job_ref)


async def call_job_tool(job_ref: dict[str, Any], tool: str) -> dict[str, Any]:
    from ..mcp.manager_auto import MCPManager, MCPSessionExpiredError

    scope = job_ref.get("request_scope") if isinstance(job_ref.get("request_scope"), dict) else {}
    # Scope may carry credentials, but cannot override the persisted job identity.
    allowed = {key: scope[key] for key in ("issuer", "agent_id", "subject", "tenant_id", "user_id", "workspace_id", "capabilities", "workspace_grant") if key in scope}
    if tool not in {"terminate_job", "get_job_status", "collect_job_result"}:
        raise ValueError("unsupported recovery operation")
    arguments = {
        "input": {**allowed, "job_id": job_ref["job_id"], "process_id": str(job_ref.get("process_id") or ""),
                  "job_token": str(job_ref.get("job_token") or "")},
    }
    async with asyncio.timeout(30):
        try:
            raw = await MCPManager.call_tool("job-protocol-mcp", tool, arguments)
        except MCPSessionExpiredError:
            # Only original-job operations can replay here, and only after a
            # protocol-confirmed rejection. Ambiguous failures remain pending.
            raw = await MCPManager.call_tool("job-protocol-mcp", tool, arguments)
    if not isinstance(raw, dict) or raw.get("isError"):
        raise RuntimeError("远程服务未返回有效结果")
    return raw


def job_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("structuredContent", raw)
    if isinstance(payload, dict) and payload.get("job_id"):
        return payload
    for item in raw.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text", ""))
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError):
                pass
    return payload if isinstance(payload, dict) else {}
