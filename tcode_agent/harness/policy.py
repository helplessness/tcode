"""工具执行使用的单轨 Policy 决策层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from ..governance.biosafety import evaluate_tool_call
from ..runtime.execution_profile import profile_allows_tool
from ..runtime.permission_guard import resolve_permission_action
from ..runtime.sandbox import sandbox_tool_decision
from .execution_deny import evaluate_execution_deny_policy

if TYPE_CHECKING:
    from ..tools.capabilities import ToolCapability


PolicyAction = Literal["allow", "ask", "deny", "route_sandbox"]


@dataclass(frozen=True)
class ToolPolicyDecision:
    """保存工具管线唯一权威路由决定。"""

    action: PolicyAction
    source: str
    reason: str = ""
    capability: Optional[ToolCapability] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sandbox_decision: Any = None

    @property
    def allowed(self) -> bool:
        """仅在权威动作是直接允许时返回真。"""

        return self.action == "allow"


def capability_to_dict(capability: Optional[ToolCapability]) -> dict[str, Any]:
    """把工具能力投影为不会改变决策的元数据。

    参数:
        capability: 待执行工具可选的已注册能力。

    返回:
        与 JSON 兼容的能力字典；未注册时返回空映射。
    """

    if capability is None:
        return {}
    return {
        "tool_id": capability.tool_id,
        "side_effect_level": capability.side_effect_level,
        "supports_async_submit": capability.supports_async_submit,
        "supports_resume": capability.supports_resume,
        "supports_review": capability.supports_review,
        "artifact_contract": capability.artifact_contract,
        "concurrency_safe": capability.concurrency_safe,
        "interrupt_behavior": capability.interrupt_behavior,
        "result_budget_tokens": capability.result_budget_tokens,
        "readonly": capability.readonly,
        "preferred_profiles": list(capability.preferred_profiles),
        "tags": list(capability.tags),
    }


class ToolPolicyEngine:
    """按固定顺序解析 Biosafety、执行拒绝、Profile、Sandbox 与权限。"""

    @classmethod
    def evaluate(
        cls,
        *,
        tool_name: str,
        context: Any,
        execution_profile: Optional[str],
        permission_action: str,
        native_sandbox_tools: set[str],
        always_allowed: bool = False,
        tool_arguments: Optional[dict[str, Any]] = None,
        resolved_tool: Any = None,
    ) -> ToolPolicyDecision:
        """返回工具执行前唯一且失败关闭的 Policy 决定。

        参数:
            tool_name: 待执行工具名称。
            context: 当前工具上下文。
            execution_profile: 当前执行配置名称。
            permission_action: 已解析的 ``allow``、``ask`` 或 ``deny``。
            native_sandbox_tools: 自身实现 Sandbox 语义的工具集合。
            always_allowed: 是否可跳过 ``ask``；显式 ``deny`` 永远不可跳过。
            tool_arguments: 仅供 Biosafety 评估的参数副本。

        返回:
            ``allow``、``ask``、``deny`` 或 ``route_sandbox`` 决定。
        """

        from ..tools.capabilities import get_tool_capability
        from ..tools.registry import ToolRegistry

        normalized_tool = str(tool_name or "").strip()
        registered_tool = resolved_tool or ToolRegistry.get(normalized_tool)
        capability = (
            registered_tool.capability
            if registered_tool is not None
            else get_tool_capability(normalized_tool)
        )
        metadata = {"capability": capability_to_dict(capability)}
        extra = getattr(context, "extra", None) or {}
        if normalized_tool in (extra.get("request_denied_tools") or []):
            return ToolPolicyDecision(
                action="deny", source="user-request-constraint",
                reason="The user explicitly prohibited this tool category for this task.",
                capability=capability, metadata=metadata,
            )

        biosafety = evaluate_tool_call(
            tool_name=normalized_tool,
            arguments=dict(tool_arguments or {}),
            context=context,
        )
        if biosafety.metadata:
            metadata["biosafety"] = dict(biosafety.metadata)
        if not biosafety.allowed:
            return ToolPolicyDecision(
                action="deny",
                source="biosafety-policy",
                reason=biosafety.reason or "Biosafety policy denied this tool call.",
                capability=capability,
                metadata={
                    **metadata,
                    "biosafety_blocked": True,
                    "biosafety_action": biosafety.action,
                },
            )

        execution_deny = evaluate_execution_deny_policy(normalized_tool, context)
        if execution_deny.denied:
            return ToolPolicyDecision(
                action="deny",
                source="execution-deny-policy",
                reason=execution_deny.reason,
                capability=capability,
                metadata={**metadata, **dict(execution_deny.metadata or {})},
            )

        if not profile_allows_tool(execution_profile, normalized_tool, capability):
            profile = str(execution_profile or "").strip()
            return ToolPolicyDecision(
                action="deny",
                source="execution-profile",
                reason=f"Execution profile '{profile}' does not allow tool: {normalized_tool}",
                capability=capability,
                metadata={**metadata, "execution_profile": profile},
            )

        sandbox_decision = sandbox_tool_decision(normalized_tool, context)
        if sandbox_decision.blocked:
            return ToolPolicyDecision(
                action="deny",
                source="sandbox-policy",
                reason=str(sandbox_decision.reason or ""),
                capability=capability,
                metadata={
                    **metadata,
                    "sandbox_policy_blocked": True,
                    "sandbox_mode": sandbox_decision.mode,
                },
                sandbox_decision=sandbox_decision,
            )
        normalized_permission = resolve_permission_action(
            permission_action,
            always_allowed=always_allowed,
        )
        if normalized_permission == "deny":
            return ToolPolicyDecision(
                action="deny",
                source="permission",
                reason=f"Permission denied for tool: {normalized_tool}",
                capability=capability,
                metadata=metadata,
            )
        if callable(extra.get("__goal_checkpoint__")):
            server = str(getattr(registered_tool, "server_name", "") or "").lower()
            operation = str(getattr(registered_tool, "_tool_name", "") or "")
            submission = (server == "job-protocol-mcp" and operation == "submit_job") or normalized_tool == "mcp_job-protocol-mcp_submit_job"
            routed = False
            if server and server != "job-protocol-mcp":
                from ..mcp.manager_auto import MCPManager
                routed = MCPManager.get_server_route_info(server).get('route_mode') == 'job_protocol'
            # Background goal rounds cannot turn model/time budgets or remembered
            # grants into authority for new jobs, shell execution or delegation.
            execution = not capability.readonly and (
                (capability.supports_async_submit and "mcp" not in capability.tags)
                or bool(set(capability.tags) & {"shell", "orchestration", "team", "install", "browser", "desktop"})
            )
            # Observation of an owned existing process does not launch execution.
            if normalized_tool == "process" and str((tool_arguments or {}).get("action") or "").strip().lower() in {"list", "status", "output", "wait"}:
                execution = False
            if submission and server == "job-protocol-mcp" and operation == "submit_job":
                # This entry only stages a mandatory, one-shot human review in
                # the proxy. It cannot enqueue a computation in a goal round.
                return ToolPolicyDecision(action="allow", source="goal-review-request",
                                          capability=capability, metadata=metadata)
            if submission or routed or execution:
                return ToolPolicyDecision(
                    action="deny", source="goal-authorization", capability=capability,
                    reason="新增计算或启动执行需要单独确认；请切回普通模式审批执行，再回到目标模式复用已有结果。增加轮次或发送继续不代表计算授权。",
                    metadata={**metadata, "goal_action_confirmation_required": True},
                    sandbox_decision=sandbox_decision,
                )
        if normalized_permission == "ask":
            return ToolPolicyDecision(
                action="ask",
                source="permission",
                reason=f"Permission required for tool: {normalized_tool}",
                capability=capability,
                metadata=metadata,
                sandbox_decision=sandbox_decision,
            )
        if sandbox_decision.run_in_sandbox and normalized_tool not in native_sandbox_tools:
            return ToolPolicyDecision(
                action="route_sandbox",
                source="sandbox-policy",
                reason=str(sandbox_decision.reason or "run_in_sandbox"),
                capability=capability,
                metadata={**metadata, "sandbox_mode": sandbox_decision.mode},
                sandbox_decision=sandbox_decision,
            )
        return ToolPolicyDecision(
            action="allow",
            source="policy",
            capability=capability,
            metadata=metadata,
        )


__all__ = [
    "PolicyAction",
    "ToolPolicyDecision",
    "ToolPolicyEngine",
    "capability_to_dict",
]
