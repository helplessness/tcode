"""MCP 工具集成到 Agent 系统。"""

from __future__ import annotations

import json
import os
from typing import Any, Literal, Optional

import pydantic

from ..tools.base import ParameterSchema, Tool, ToolContext
from ..types import ToolResult
from ..delegation import build_permission_delegation_metadata
from ._utils import (
    _agents_allowed_to_route_server,
    _check_human_review_required,
    _context_control_workdir,
    _enforce_direct_whitelist,
    _get_agent_allowed_routed_servers,
    _get_turn_guard,
    _inject_context_workspace_root_hint,
    _mcp_sandbox_boundary_error,
    _mcp_sandbox_boundary_metadata,
    _normalize_routed_tool_args,
    _rewrite_mcp_result_paths,
    _set_turn_guard,
    _turn_guard_submission_lock,
)
from .recovery_policy import job_protocol_recovery_policy_error
from .apps import tool_is_visible_to_model


def _schema_python_type(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return Any

    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        non_null = [
            item
            for item in variants
            if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(non_null) == 1:
            return Optional[_schema_python_type(non_null[0])]
        return Any

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        try:
            return Literal.__getitem__(tuple(enum_values))
        except Exception:
            return Any

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        if len(non_null_types) == 1:
            return Optional[_schema_python_type({**schema, "type": non_null_types[0]})]
        return Any
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        return list[_schema_python_type(schema.get("items") or {})]
    if schema_type == "object" or "properties" in schema:
        return dict[str, Any]
    return Any


def _parameter_model_from_input_schema(tool_name: str, input_schema: dict) -> type[ParameterSchema]:
    properties = input_schema.get("properties") if isinstance(input_schema, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    required = set(input_schema.get("required") or []) if isinstance(input_schema, dict) else set()
    fields: dict[str, tuple[Any, Any]] = {}
    for name, raw_schema in properties.items():
        if name == '_tcode_context':
            continue  # Pydantic reserves underscore fields; publish the host JSON Schema below.
        schema = raw_schema if isinstance(raw_schema, dict) else {}
        annotation = _schema_python_type(schema)
        if name in required:
            default: Any = ...
        else:
            default = schema.get("default", None)
            if default is None and annotation is not Any:
                annotation = Optional[annotation]
        description = str(schema.get("description") or "").strip()
        field_default = pydantic.Field(default, description=description) if description else default
        fields[str(name)] = (annotation, field_default)
    return pydantic.create_model(
        f"MCP_{tool_name}_Parameters",
        __base__=ParameterSchema,
        **fields,
    )


class MCPTool(Tool):
    """MCP 工具适配器 - 注册到 Agent 系统。"""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict,
        metadata: dict | None = None,
    ):
        self.server_name = server_name
        self._tool_name = tool_name
        from .parameter_contract import agent_schema, contract_summary
        self._wire_input_schema = input_schema
        self._description = self._simplify_description(contract_summary(input_schema) or description)
        if server_name.lower() == 'job-protocol-mcp' and tool_name == 'submit_job':
            self._description += ' 提交前先用 describe_target_tool 查看目标的 call_context_schema；仅当目标返回 call_context_schema 时，才按其声明从用户明确需求提取变量并填入 _tcode_context。目标没有此声明时省略该字段。必填变量缺失或有歧义时先询问。'
        from .custom_targets import host_input_schema
        from .dynamic_headers import augment_schema, rules_for
        self._input_schema = augment_schema(agent_schema(host_input_schema(server_name, tool_name, input_schema)), rules_for(server_name))
        self._metadata = dict(metadata or {})
        self._parameter_model = _parameter_model_from_input_schema(tool_name, self._input_schema)
        from copy import deepcopy
        schema = deepcopy(self._input_schema)
        self._parameter_model.model_json_schema = classmethod(lambda cls, *args, **kwargs: deepcopy(schema))

    async def _emit_mcp_hook(self, event: str, context, data: dict) -> None:
        """Emit an MCP-level plugin hook (fire-and-forget)."""
        try:
            import asyncio as _asyncio

            loop = _asyncio.get_running_loop()

            async def _emit():
                try:
                    from ..plugins.manager import PluginManager

                    workdir = ""
                    if context and hasattr(context, "extra"):
                        workdir = str((context.extra or {}).get("workdir", "") or "").strip()
                    manager = await PluginManager.get_global(workdir or os.getcwd())
                    await manager.trigger(event, data, {})
                except Exception:
                    pass

            loop.create_task(_emit())
        except Exception:
            pass

    def _simplify_description(self, desc: str) -> str:
        """Keep complete instructions; structured TMCP contracts supply a short summary."""
        return str(desc or '').strip()

    @property
    def id(self) -> str:
        return f"mcp_{self.server_name}_{self._tool_name}"

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> type:
        return self._parameter_model

    @property
    def is_model_visible(self) -> bool:
        return tool_is_visible_to_model(self._metadata)

    def arguments_for_validation(self, args: dict) -> dict:
        """Preserve the documented job-protocol input_config compatibility alias."""
        properties = self._input_schema.get("properties") or {}
        if (self.server_name.lower() == "job-protocol-mcp"
                and self._tool_name in {"submit_job", "get_job_status", "collect_job_result"}
                and "input" in properties and "input_config" not in properties
                and "input" not in args and "input_config" in args):
            normalized = dict(args)
            inner = normalized.pop("input_config")
            normalized["input"] = inner.get("input", inner) if isinstance(inner, dict) else inner
            return normalized
        return args

    def validate_proxy_arguments(self, args: dict):
        """The LOCAL submission facade is not a remote shell-job payload.

        Reuse its existing parser; target schemas, permissions and biosafety
        remain enforced by _proxy_submit_job before any enqueue operation.
        """
        if self.server_name.lower() != "job-protocol-mcp":
            return None
        from ..runtime.tool_arguments import ToolArgumentValidation
        if self._tool_name in {"get_job_status", "collect_job_result"}:
            from ._utils import _extract_local_identifier_from_job_protocol_args
            if _extract_local_identifier_from_job_protocol_args(args):
                # LOCAL identifiers belong to the owner-scoped local tracker,
                # not the remote service's UUID/job-id contract.
                return ToolArgumentValidation(accepted=True, validator="job-protocol-local-query")
            return None
        if self._tool_name != "submit_job":
            return None
        from ._proxy import _simplified_submit_args
        parsed, error = _simplified_submit_args(args)
        if error is not None:
            return ToolArgumentValidation(accepted=False, reason=error.error or "Invalid local submission",
                                          location="arguments", validator="job-protocol-proxy")
        if parsed is not None:
            return ToolArgumentValidation(accepted=True, validator="job-protocol-proxy")
        return None

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """执行 MCP 工具。"""
        try:
            from ._utils import _check_context_workspace_conflicts
            _check_context_workspace_conflicts(self.server_name, args, context,
                                             tool_name=self._tool_name, schema=self._wire_input_schema)
            from ..runtime.async_resume import assert_submission_allowed
            # The proxy below handles target-specific admission for submit_job.
            if not (self.server_name == "job-protocol-mcp" and self._tool_name == "submit_job"):
                assert_submission_allowed(self.server_name, self._tool_name)
            from .manager_auto import MCPManager
            from .artifact_boundary import prepare_mcp_artifact_arguments

            lazy_artifact_uploads: list[dict[str, Any]] = []

            if MCPManager._server_configs.get(self.server_name, {}).get("file_transfer"):
                # Both a discovered tool call and submit_job use the same review,
                # authorization, upload, dedupe and durable queue path.
                result = await self._proxy_submit_job({"target_server": self.server_name,
                                                      "target_tool": self._tool_name,
                                                      "arguments": args}, context)
                if result is not None:
                    return result

            if (
                self._tool_name == "submit_job"
                and self.server_name.lower() == "job-protocol-mcp"
            ):
                proxy_result = await self._proxy_submit_job(args, context)
                if proxy_result is not None:
                    return proxy_result

            if self.server_name.lower() == "job-protocol-mcp" and self._tool_name in {
                "get_job_status",
                "collect_job_result",
            }:
                local_proxy_result = self._proxy_local_issue_job_query(args, context)
                if local_proxy_result is not None:
                    return local_proxy_result

            if self._tool_name in {
                "submit_job",
                "get_job_status",
                "collect_job_result",
            }:
                if "input" in args:
                    args = {"input": args["input"]}
                elif "input_config" in args:
                    inner = args["input_config"]
                    if (
                        isinstance(inner, dict)
                        and "input" in inner
                        and isinstance(inner["input"], dict)
                    ):
                        args = {"input": inner["input"]}
                    else:
                        args = {"input": inner}

            if isinstance(args, dict):
                args = _inject_context_workspace_root_hint(
                    self.server_name,
                    args,
                    context,
                    self._tool_name,
                )

            should_route = self._should_route_to_symphony(context)
            if should_route:
                allowed_servers = _get_agent_allowed_routed_servers(context)
                server_key = self.server_name.strip().lower()
                if allowed_servers is not None and server_key not in allowed_servers:
                    route_agents = _agents_allowed_to_route_server(
                        self.server_name,
                        _context_control_workdir(context),
                    )
                    return ToolResult(
                        title=f"MCP {self.server_name}.{self._tool_name}",
                        output="",
                        error=(
                            f"Agent '{getattr(context, 'agent', '')}' is not allowed "
                            f"to route MCP server '{self.server_name}'."
                        ),
                        metadata={
                            "validation": "target_server_not_allowed",
                            "agent": getattr(context, "agent", ""),
                            "target_server": self.server_name,
                            "allowed_servers": sorted(allowed_servers),
                            **build_permission_delegation_metadata(
                                allowed_agents=route_agents,
                                preferred_agent=(route_agents[0] if route_agents else ""),
                            ),
                            "retryable": False,
                            "should_end_turn": False,
                        },
                    )

            user_message = ""
            if context and hasattr(context, "extra"):
                user_message = str(context.extra.get("user_message", "") or "")

            review_check = await _check_human_review_required(
                self.server_name,
                self._tool_name,
                args,
                user_message,
                context=context,
            )
            approved_override = review_check.get("approved_tool_args_override")
            if isinstance(approved_override, dict) and approved_override:
                args = dict(approved_override)

            if review_check.get("requires_review", False):
                normalized_args = _normalize_routed_tool_args(args, self._tool_name)
                review_issue = await self._enqueue_human_review_issue(
                    normalized_args, context, review_check
                )
                return ToolResult(
                    title=f"MCP {self.server_name}.{self._tool_name}",
                    output=(
                        f"Human review required: {review_issue.get('identifier', 'created')} - "
                        f"{review_check.get('reason', '需要人工确认')} "
                        f"(trigger: {review_check.get('trigger', 'unknown')})"
                    ),
                    metadata={
                        **review_issue,
                        "human_review_required": True,
                        "should_end_turn": True,
                    },
                )

            if should_route:
                args, lazy_artifact_uploads = await prepare_mcp_artifact_arguments(
                    self.server_name,
                    self._tool_name,
                    args,
                    context,
                )
                normalized_args = _normalize_routed_tool_args(args, self._tool_name)
                validation_error = self._validate_routed_request(
                    normalized_args, context
                )
                if validation_error:
                    return ToolResult(
                        title=f"MCP {self.server_name}.{self._tool_name}",
                        output="",
                        error=validation_error,
                    )
                from .dynamic_headers import fingerprint_arguments
                guard_arguments = fingerprint_arguments(self.server_name, normalized_args, context)
                async with _turn_guard_submission_lock(
                    context,
                    self.server_name,
                    self._tool_name,
                    guard_arguments,
                ):
                    guarded = _get_turn_guard(
                        context,
                        self.server_name,
                        self._tool_name,
                        guard_arguments,
                    )
                    if guarded is not None:
                        existing_identifier = str(
                            guarded.get("symphony_identifier")
                            or guarded.get("identifier")
                            or "existing"
                        )
                        return ToolResult(
                            title=f"MCP {self.server_name}.{self._tool_name}",
                            output=(
                                "Reused existing submission in same turn: "
                                f"{existing_identifier} (guarded)"
                            ),
                            metadata={"route_mode": "turn-guard-deduped", **guarded},
                        )
                    local_issue = await self._enqueue_symphony_local_issue(
                        normalized_args, context
                    )
                    _set_turn_guard(
                        context,
                        self.server_name,
                        self._tool_name,
                        {
                            "symphony_identifier": str(
                                local_issue.get("identifier") or ""
                            ),
                            "status": local_issue.get("status", "queued"),
                            "execution_channel": local_issue.get(
                                "execution_channel", "symphony"
                            ),
                        },
                        guard_arguments,
                    )
                if local_issue.get("status") == "deduped":
                    return ToolResult(
                        title=f"MCP {self.server_name}.{self._tool_name}",
                        output=(
                            "检测到重复异步请求，沿用本地受理 ID: "
                            f"{local_issue.get('identifier', 'existing')}。"
                            "该 LOCAL-* 编号不是目标 MCP 的 Job ID。"
                        ),
                        metadata={
                            "accepted_async": True,
                            "should_end_turn": True,
                            **local_issue,
                        },
                    )
                refresh_text = (
                    "已触发"
                    if local_issue.get("refresh_triggered")
                    else "等待刷新"
                )
                return ToolResult(
                    title=f"MCP {self.server_name}.{self._tool_name}",
                    output=(
                        f"异步请求已进入 {local_issue.get('execution_channel', 'symphony')} 队列；"
                        f"本地受理 ID: {local_issue.get('identifier', 'created')}。"
                        "该 LOCAL-* 编号不是目标 MCP 的 Job ID，也不表示目标 MCP 已收到请求；"
                        f"队列刷新状态: {refresh_text}。"
                    ),
                    metadata={
                        "accepted_async": True,
                        "should_end_turn": True,
                        **local_issue,
                    },
                )

            if _enforce_direct_whitelist():
                if not MCPManager._is_direct_call_allowed(self.server_name):
                    route_agents = _agents_allowed_to_route_server(
                        self.server_name,
                        _context_control_workdir(context),
                    )
                    return ToolResult(
                        title=f"MCP {self.server_name}.{self._tool_name}",
                        output="",
                        error=(
                            f"Direct MCP call is disabled by policy for server '{self.server_name}'. "
                            "This tool must be routed to Symphony/job-protocol."
                        ),
                        metadata={
                            "validation": "direct_mcp_disabled",
                            "target_server": self.server_name,
                            **build_permission_delegation_metadata(
                                allowed_agents=route_agents,
                                preferred_agent=(route_agents[0] if route_agents else ""),
                            ),
                            "retryable": False,
                            "should_end_turn": False,
                        },
                    )

            boundary_error = _mcp_sandbox_boundary_error(
                self.server_name, self._tool_name
            )
            if boundary_error:
                return ToolResult(
                    title=f"MCP {self.server_name}.{self._tool_name}",
                    output="",
                    error=boundary_error,
                    metadata={
                        "mcp_sandbox_blocked": True,
                        **_mcp_sandbox_boundary_metadata(
                            self.server_name, self._tool_name
                        ),
                    },
                )

            args, lazy_artifact_uploads = await prepare_mcp_artifact_arguments(
                self.server_name,
                self._tool_name,
                args,
                context,
            )

            if not MCPManager.is_initialized():
                await MCPManager.initialize()

            if self.server_name == 'job-protocol-mcp' and self._tool_name == 'describe_target_tool':
                from ._utils import _context_workspace_claims
                claims = _context_workspace_claims(context)
                key = next((k for k in ('input_config', 'input', 'payload') if isinstance(args.get(k), dict)), 'input_config')
                args = {key: {**args.get(key, {}), **claims, 'issuer': 'tcode', 'subject': claims.get('user_id', ''), 'view': 'agent'}}

            from .dynamic_headers import split
            await self._emit_mcp_hook(
                "mcp.tool.call.before",
                context,
                {
                    "server": self.server_name,
                    "tool": self._tool_name,
                    "args_preview": str(split(args)[0])[:300],
                },
            )
            async def _report_mcp_progress(payload: dict[str, Any]) -> None:
                if not isinstance(payload, dict):
                    return
                current = payload.get("progress")
                total = payload.get("total")
                message = str(payload.get("message", "") or "").strip()
                if not message:
                    if current is not None and total is not None:
                        message = f"MCP 进度 {current}/{total}"
                    elif current is not None:
                        message = f"MCP 进度 {current}"
                    else:
                        return
                await context.report_progress(
                    {
                        "message": message,
                        "current": current,
                        "total": total,
                        "stream": "mcp",
                        "status": "running",
                    }
                )

            result = await MCPManager.call_tool(
                self.server_name,
                self._tool_name,
                args,
                progress_callback=_report_mcp_progress,
                progress_token=(
                    f"{context.session_id}:{context.call_id}"
                    if context.call_id
                    else None
                ),
            )

            if result.get("isError"):
                error, metadata = job_protocol_recovery_policy_error(
                    server_name=self.server_name,
                    tool_name=self._tool_name,
                    error=str(result.get("content", "Unknown error")),
                )
                return ToolResult(
                    title=f"MCP {self.server_name}.{self._tool_name}",
                    output="",
                    error=error,
                    metadata=metadata,
                )

            if lazy_artifact_uploads:
                result = dict(result)
                result.setdefault("_tcode", {})["lazy_artifact_uploads"] = lazy_artifact_uploads

            content = result.get("structuredContent") or result.get("content", [])
            if self.server_name.lower() == 'job-protocol-mcp' and self._tool_name == 'describe_target_tool':
                from .dynamic_headers import rules_for, context_schema
                request = args.get('input', args.get('input_config', args))
                rules = rules_for(str(request.get('target_server', '')), context)
                if rules:
                    if isinstance(content, list):
                        content = json.loads(next(b['text'] for b in content if b.get('type') == 'text'))
                    if isinstance(content, dict):
                        content = {**content, 'call_context_schema': context_schema(rules),
                                   'call_context_usage': '将这些业务变量放入本次目标参数 _tcode_context，不是远端业务参数；缺失或歧义先询问。'}
                    content = json.dumps(content, ensure_ascii=False)
            if isinstance(content, list) and len(content) > 0:
                blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        blocks.append(str(block))
                    elif block.get("type") == "text" or "text" in block:
                        blocks.append(str(block.get("text") or ""))
                    elif block.get("type") == "resource":
                        resource = block.get("resource") or {}
                        blocks.append(str(resource.get("text") or resource.get("uri") or "[resource]"))
                    else:
                        # Keep resource references without injecting base64 media
                        # into the text-only tool transcript.
                        blocks.append(json.dumps({key: value for key, value in block.items() if key not in {"data", "blob"}}, ensure_ascii=False))
                text_content = "\n\n".join(blocks)
            else:
                text_content = str(content)

            from .apps import build_mcp_app_descriptor

            mcp_app_descriptor = build_mcp_app_descriptor(
                session_id=str(getattr(context, "session_id", "") or ""),
                server_name=self.server_name,
                tool_name=self._tool_name,
                tool_input=args,
                tool_metadata=self._metadata,
                result=result,
            )

            await self._emit_mcp_hook(
                "mcp.tool.call.after",
                context,
                {
                    "server": self.server_name,
                    "tool": self._tool_name,
                    "result_preview": text_content[:500],
                },
            )

            try:
                from ..runtime.sandbox import tool_should_run_in_sandbox

                if tool_should_run_in_sandbox(
                    f"mcp_{self.server_name}_{self._tool_name}", context
                ):
                    text_content = _rewrite_mcp_result_paths(text_content, context)
            except Exception:
                text_content = _rewrite_mcp_result_paths(text_content, context)

            output_review_check = await _check_human_review_required(
                self.server_name,
                self._tool_name,
                args,
                user_message,
                text_content,
                context=context,
            )
            if (
                output_review_check.get("requires_review", False)
                and output_review_check.get("check_type") == "output_result"
            ):
                review_issue = await self._enqueue_human_review_issue(
                    args, context, output_review_check
                )
                return ToolResult(
                    title=f"MCP {self.server_name}.{self._tool_name}",
                    output=(
                        f"Tool executed, but output requires human review: "
                        f"{review_issue.get('identifier', 'created')} - "
                        f"{output_review_check.get('reason', '输出结果需要人工确认')} "
                        f"(trigger: {output_review_check.get('trigger', 'unknown')})\n\n"
                        f"Output preview: {text_content[:200]}..."
                    ),
                    metadata={
                        **review_issue,
                        "human_review_required": True,
                        "should_end_turn": True,
                    },
                )

            return ToolResult(
                title=f"MCP {self.server_name}.{self._tool_name}",
                output=text_content,
                metadata={
                    **(
                        dict(result)
                        if isinstance(result, dict)
                        else {"raw_mcp_result": result}
                    ),
                    **_mcp_sandbox_boundary_metadata(
                        self.server_name, self._tool_name
                    ),
                    **(
                        {"mcp_app_descriptor": mcp_app_descriptor}
                        if mcp_app_descriptor
                        else {}
                    ),
                },
            )
        except Exception as e:
            error_detail = str(e).strip() or type(e).__name__
            await self._emit_mcp_hook(
                "mcp.tool.call.error",
                context,
                {
                    "server": self.server_name,
                    "tool": self._tool_name,
                    "error": error_detail[:500],
                },
            )
            error, metadata = job_protocol_recovery_policy_error(
                server_name=self.server_name,
                tool_name=self._tool_name,
                error=error_detail,
            )
            return ToolResult(
                title=f"MCP {self.server_name}.{self._tool_name}",
                output="",
                error=error,
                metadata=metadata,
            )

    def _validate_routed_request(
        self,
        normalized_args: dict,
        context: Optional[ToolContext],
    ) -> Optional[str]:
        from ._symphony import validate_routed_request

        return validate_routed_request(
            server_name=self.server_name,
            tool_name=self._tool_name,
            normalized_args=normalized_args,
            context=context,
        )

    async def _proxy_submit_job(
        self,
        args: dict,
        context: ToolContext,
    ) -> Optional[ToolResult]:
        from ._proxy import proxy_submit_job

        return await proxy_submit_job(
            server_name=self.server_name,
            tool_name=self._tool_name,
            description=self.description,
            input_schema=self._input_schema,
            args=args,
            context=context,
        )

    def _proxy_local_issue_job_query(
        self,
        args: dict,
        context: ToolContext,
    ) -> Optional[ToolResult]:
        from ._proxy import proxy_local_issue_job_query

        return proxy_local_issue_job_query(
            server_name=self.server_name,
            tool_name=self._tool_name,
            args=args,
            context=context,
        )

    def _should_route_to_symphony(
        self, context: Optional[ToolContext] = None
    ) -> bool:
        from ._symphony import should_route_to_symphony

        return should_route_to_symphony(
            server_name=self.server_name,
            context=context,
        )

    def _build_job_protocol_submit_input(
        self,
        tool_args: dict,
        context: Optional[ToolContext],
    ) -> dict:
        from ._symphony import build_job_protocol_submit_input

        return build_job_protocol_submit_input(
            server_name=self.server_name,
            tool_name=self._tool_name,
            description=self.description,
            input_schema=self._input_schema,
            tool_args=tool_args,
            context=context,
        )

    async def _enqueue_symphony_local_issue(
        self,
        normalized_args: dict,
        context: Optional[ToolContext],
    ) -> dict:
        from ._symphony import enqueue_symphony_local_issue

        return await enqueue_symphony_local_issue(
            server_name=self.server_name,
            tool_name=self._tool_name,
            description=self.description,
            input_schema=self._input_schema,
            normalized_args=normalized_args,
            context=context,
        )

    async def _enqueue_human_review_issue(
        self,
        args: dict,
        context: Optional[ToolContext],
        review_check: dict,
    ) -> dict:
        from ._review import enqueue_human_review_issue

        return await enqueue_human_review_issue(
            server_name=self.server_name,
            tool_name=self._tool_name,
            description=self.description,
            input_schema=self._input_schema,
            args=args,
            context=context,
            review_check=review_check,
        )


class MCPToolIntegrator:
    """MCP 工具集成器 - 将 MCP 工具注册到 Agent。"""

    @classmethod
    async def register_to_agent(cls, agent) -> list[Tool]:
        """将所有 MCP 工具注册到 Agent。"""
        from ..tools.registry import ToolRegistry
        from .manager_auto import MCPManager

        await MCPManager.initialize()
        mcp_tools_dict = await MCPManager.get_tools()
        registered_tools = []

        for tool_info in mcp_tools_dict.values():
            if not tool_is_visible_to_model(tool_info.get("_meta")):
                continue
            mcp_tool = MCPTool(
                server_name=tool_info["server"],
                tool_name=tool_info["tool"],
                description=tool_info["description"],
                input_schema=tool_info["inputSchema"],
                metadata=tool_info.get("_meta"),
            )
            ToolRegistry.register_custom(mcp_tool)
            registered_tools.append(mcp_tool)

        return registered_tools

    @classmethod
    async def get_available_tools(cls) -> list[Tool]:
        """获取所有可用的 MCP 工具。"""
        from .manager_auto import MCPManager

        await MCPManager.initialize()
        mcp_tools_dict = await MCPManager.get_tools()

        tools = []
        for tool_info in mcp_tools_dict.values():
            if not tool_is_visible_to_model(tool_info.get("_meta")):
                continue
            tools.append(
                MCPTool(
                    server_name=tool_info["server"],
                    tool_name=tool_info["tool"],
                    description=tool_info["description"],
                    input_schema=tool_info["inputSchema"],
                    metadata=tool_info.get("_meta"),
                )
            )
        return tools
