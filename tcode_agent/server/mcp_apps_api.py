"""MCP Apps 资源宿主 API；远端 MCP 连接失败时保持显式错误。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..agui.auth import APIKeyPrincipal
from ..agui.rate_limit import GatewayRateLimiter, RateLimitExceeded
from ..mcp.apps import (
    MCP_APP_MIME_TYPE,
    MCPAppTokenStore,
    append_mcp_app_audit,
    build_app_id,
    grant_resource_ui_metadata,
    resource_uri_from_metadata,
    tool_is_visible_to_model,
    validate_resource_uri,
)
from ..mcp.manager_auto import MCPManager
from ..runtime.workspace_registry import registry_workspace
from ..session.manager import SessionManager
from ..presentation.result_selection import latest_selections
from .auth import require_current_user


router = APIRouter(prefix="/api/mcp-apps/v1", tags=["MCP Apps"])
token_store = MCPAppTokenStore()
rate_limiter = GatewayRateLimiter()


class CallToolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=256)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if any(character in normalized for character in "\r\n\x00"):
            raise ValueError("tool name contains control characters")
        return normalized


class ResolveAppBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=200)
    server_name: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=300)
    resource_uri: str = Field(min_length=1, max_length=2_000)
    app_id: str = Field(default="", max_length=200)


class ReadResourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1, max_length=2_000)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        return validate_resource_uri(value)


def _enabled() -> None:
    if str(os.environ.get("TCODE_MCP_APPS_ENABLED", "0")).lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(404, "MCP Apps host is disabled")


def _consume(token: str, action: str, limit: int) -> None:
    try:
        rate_limiter.consume(f"mcp-app:{token_store._digest(token)}", action, limit=limit)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            "MCP App rate limit exceeded",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


def _require_scope(request: Request) -> None:
    principal = getattr(request.state, "agui_api_key", None)
    if isinstance(principal, APIKeyPrincipal) and not principal.permits(
        "mcp_apps:invoke"
    ):
        raise HTTPException(403, "API key is missing scope: mcp_apps:invoke")


def _require_direct_server(server_name: str) -> None:
    route = MCPManager.get_server_route_info(server_name)
    if not route.get("user_callable"):
        raise HTTPException(
            403,
            str(route.get("route_reason") or "MCP server is not available for direct Apps access"),
        )


def _require_current_capability(record: dict[str, Any]) -> None:
    expected = str(record.get("capability_digest") or "")
    if not expected:
        return
    current = MCPManager.get_server_capability_digest(str(record["server_name"]))
    if not current or not hmac.compare_digest(expected, current):
        raise HTTPException(409, "MCP App capability changed; reload the App")


async def _owned_session(request: Request, session_id: str):
    user = require_current_user(request)
    _require_scope(request)
    session = await SessionManager.get(
        str(session_id),
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if session is None:
        raise HTTPException(404, "MCP App session not found")
    return session


@router.post("/resolve")
async def resolve_mcp_app(payload: ResolveAppBody, request: Request):
    """Resolve a persisted App descriptor into a fresh least-privilege token."""

    _enabled()
    session = await _owned_session(request, payload.session_id)
    try:
        resource_uri = validate_resource_uri(payload.resource_uri)
        with registry_workspace(str(session.workdir or "")):
            await MCPManager.initialize()
            _require_direct_server(payload.server_name)
            tool = MCPManager.get_server_tool(payload.server_name, payload.tool_name)
            if tool is None:
                raise HTTPException(404, "MCP App entry tool is unavailable")
            metadata = tool.get("_meta")
            if not tool_is_visible_to_model(metadata):
                raise HTTPException(403, "MCP App entry tool is not model-visible")
            discovered_uri = resource_uri_from_metadata(metadata, {})
            if discovered_uri != resource_uri:
                raise HTTPException(409, "MCP App descriptor no longer matches the server")
            app_id = build_app_id(payload.server_name, payload.tool_name, resource_uri)
            if payload.app_id and payload.app_id != app_id:
                raise HTTPException(409, "MCP App identity does not match the descriptor")
            allowed_tools = MCPManager.get_app_tools_for_resource(
                payload.server_name,
                resource_uri,
            )
            capability_digest = MCPManager.get_server_capability_digest(
                payload.server_name
            )
            token = token_store.issue(
                session_id=payload.session_id,
                server_name=payload.server_name,
                tool_name=payload.tool_name,
                resource_uri=resource_uri,
                app_id=app_id,
                allowed_tools=allowed_tools,
                allowed_resources=[resource_uri],
                capability_digest=capability_digest,
            )
            append_mcp_app_audit(
                str(session.workdir or ""),
                event="app.resolve",
                session_id=payload.session_id,
                server_name=payload.server_name,
                app_id=app_id,
                resource_uri=resource_uri,
                tool_name=payload.tool_name,
                details={
                    "allowed_tool_count": len(allowed_tools),
                    "capability_digest": capability_digest,
                },
            )
            tools = []
            for name in allowed_tools:
                definition = MCPManager.get_server_tool(payload.server_name, name)
                if definition is None:
                    continue
                tools.append(
                    {
                        key: definition[key]
                        for key in (
                            "name",
                            "title",
                            "description",
                            "inputSchema",
                            "outputSchema",
                            "annotations",
                        )
                        if key in definition
                    }
                )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"MCP App unavailable: {str(exc)[:1000]}") from exc
    return {
        "schema": "tcode.mcp-app-resolution.v1",
        "app_id": app_id,
        "resource_token": token,
        "expires_in": 600,
        "selections": [s for s in latest_selections(getattr(session, "messages", [])) if s.get("source") == {"app_id": app_id, "server_name": payload.server_name}],
        "tools": tools,
        "resources": [{"uri": resource_uri, "mimeType": MCP_APP_MIME_TYPE}],
    }


@router.get("/resources/{token}")
async def read_mcp_app_resource(token: str, request: Request):
    _enabled()
    _require_scope(request)
    record = token_store.get(token)
    if record is None:
        raise HTTPException(404, "MCP App resource token is invalid or expired")
    _consume(token, "resource", 60)
    session = await _owned_session(request, str(record["session_id"]))
    if str(record["resource_uri"]) not in set(record.get("allowed_resources") or []):
        raise HTTPException(403, "MCP App resource is outside the issued capability")
    try:
        with registry_workspace(str(session.workdir or "")):
            await MCPManager.initialize()
            _require_direct_server(str(record["server_name"]))
            _require_current_capability(record)
            append_mcp_app_audit(
                str(session.workdir or ""),
                event="resources.read",
                session_id=str(record["session_id"]),
                server_name=str(record["server_name"]),
                app_id=str(record.get("app_id") or ""),
                resource_uri=str(record["resource_uri"]),
            )
            contents = await MCPManager.read_resource(
                str(record["server_name"]), str(record["resource_uri"])
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"MCP App resource unavailable: {str(exc)[:1000]}") from exc
    requested_meta = {}
    if contents and isinstance(contents[0], dict):
        metadata = contents[0].get("_meta")
        if isinstance(metadata, dict):
            requested_meta = metadata.get("ui") if isinstance(metadata.get("ui"), dict) else {}
    resource_meta = grant_resource_ui_metadata(requested_meta)
    return {
        "resourceUri": record["resource_uri"],
        "mimeType": MCP_APP_MIME_TYPE,
        "contents": contents,
        "requestedResourceMeta": requested_meta,
        "resourceMeta": resource_meta,
        "resources": [
            {
                "uri": record["resource_uri"],
                "mimeType": MCP_APP_MIME_TYPE,
                "_meta": {"ui": resource_meta},
            }
        ],
        "sandbox": {
            "allow": ["scripts"],
            "sameOrigin": False,
            "network": bool(resource_meta.get("csp")),
        },
    }


@router.post("/resources/{token}/read")
async def read_mcp_app_bridge_resource(
    token: str,
    payload: ReadResourceBody,
    request: Request,
):
    """Proxy AppBridge resources/read within the token's resource allowlist."""

    _enabled()
    _require_scope(request)
    record = token_store.get(token)
    if record is None:
        raise HTTPException(404, "MCP App resource token is invalid or expired")
    _consume(token, "bridge-resource", 60)
    uri = payload.uri
    if uri not in set(record.get("allowed_resources") or []):
        raise HTTPException(403, "MCP App resource is outside the issued capability")
    session = await _owned_session(request, str(record["session_id"]))
    try:
        with registry_workspace(str(session.workdir or "")):
            await MCPManager.initialize()
            _require_direct_server(str(record["server_name"]))
            _require_current_capability(record)
            append_mcp_app_audit(
                str(session.workdir or ""),
                event="resources.read",
                session_id=str(record["session_id"]),
                server_name=str(record["server_name"]),
                app_id=str(record.get("app_id") or ""),
                resource_uri=uri,
            )
            contents = await MCPManager.read_resource(
                str(record["server_name"]),
                uri,
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"MCP App resource unavailable: {str(exc)[:1000]}") from exc
    return {"contents": contents}


@router.post("/tools/{token}/call")
async def call_mcp_app_tool(token: str, payload: CallToolBody, request: Request):
    """在宿主确认后，把 App 的 tools/call 限定转发到原 MCP server。"""

    _enabled()
    _require_scope(request)
    record = token_store.get(token)
    if record is None:
        raise HTTPException(404, "MCP App token is invalid or expired")
    _consume(token, "tool", 30)
    if len(json.dumps(payload.arguments, ensure_ascii=False, default=str).encode()) > 256_000:
        raise HTTPException(413, "MCP App tool arguments exceed 256 KB")
    session = await _owned_session(request, str(record["session_id"]))
    try:
        with registry_workspace(str(session.workdir or "")):
            await MCPManager.initialize()
            _require_direct_server(str(record["server_name"]))
            arguments_json = json.dumps(
                payload.arguments,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode()
            append_mcp_app_audit(
                str(session.workdir or ""),
                event="tools.call",
                session_id=str(record["session_id"]),
                server_name=str(record["server_name"]),
                app_id=str(record.get("app_id") or ""),
                resource_uri=str(record["resource_uri"]),
                tool_name=payload.name,
                status="requested",
                details={
                    "arguments_sha256": hashlib.sha256(arguments_json).hexdigest(),
                    "arguments_size": len(arguments_json),
                    "argument_keys": ",".join(sorted(payload.arguments))[:2_000],
                },
            )
            result = await MCPManager.call_app_tool(
                str(record["server_name"]),
                payload.name,
                payload.arguments,
                resource_uri=str(record["resource_uri"]),
                allowed_tools=list(record.get("allowed_tools") or []),
                capability_digest=str(record.get("capability_digest") or ""),
            )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502, f"MCP App tool unavailable: {str(exc)[:1000]}"
        ) from exc
    if not isinstance(result, dict):
        result = {
            "content": [{"type": "text", "text": str(result)}],
            "isError": False,
        }
    from ..presentation.result_selection import enabled, project_selection
    receipt = result.get("structuredContent", {})
    if isinstance(receipt, dict) and isinstance(receipt.get("result"), dict):
        receipt = receipt["result"]
    if not receipt:
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    receipt = json.loads(block.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(receipt, dict) and receipt.get("schema") == "tmcp.result-selection.v1":
                    break
    if not result.get("isError") and isinstance(receipt, dict) and receipt.get("schema") == "tmcp.result-selection.v1":
        if not enabled():
            return {"isError": True, "content": [{"type": "text", "text": "结果选择上下文已禁用，未写入会话。"}]}
        try:
            selection = await SessionManager.mutate(
                session.id, lambda current: project_selection(current, record, receipt),
                tenant_id=session.tenant_id, user_id=session.user_id, workspace_id=session.workspace_id,
            )
            if selection is None:
                raise ValueError("session unavailable")
            result["tcode_selection"] = selection
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return result


__all__ = [
    "call_mcp_app_tool",
    "read_mcp_app_bridge_resource",
    "read_mcp_app_resource",
    "resolve_mcp_app",
    "router",
]
