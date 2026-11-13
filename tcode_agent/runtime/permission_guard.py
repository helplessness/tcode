"""工具权限动作的生产级单调解析器。"""

from __future__ import annotations

from typing import Any, Literal


PermissionAction = Literal["allow", "ask", "deny"]
_VALID_ACTIONS = frozenset({"allow", "ask", "deny"})


def resolve_permission_action(
    value: Any,
    *,
    always_allowed: bool = False,
) -> PermissionAction:
    """解析权限动作并保证显式拒绝不可被覆盖。

    参数:
        value: 权限系统返回的动作；空值保持历史默认 ``allow``。
        always_allowed: 允许受信内建工具跳过 ``ask``，但不能跳过 ``deny``。

    返回:
        唯一可供 Policy 消费的 ``allow``、``ask`` 或 ``deny``。未知值失败
        关闭为 ``deny``。
    """

    normalized = str(value or "allow").strip().lower()
    if normalized not in _VALID_ACTIONS:
        return "deny"
    if normalized == "deny":
        return "deny"
    if always_allowed:
        return "allow"
    return normalized  # type: ignore[return-value]


def permission_contract_document() -> dict[str, object]:
    """返回单轨权限解析器的稳定契约。"""

    return {
        "contract_id": "tcode.permission-resolution.v1",
        "consumer": "ToolPolicyEngine",
        "single_track": True,
        "unknown_action": "deny",
        "explicit_deny_overridable": False,
        "always_allowed_can_skip_ask": True,
    }


__all__ = [
    "PermissionAction",
    "permission_contract_document",
    "resolve_permission_action",
]
