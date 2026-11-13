"""Fail-closed macOS desktop computer-use tool."""

from __future__ import annotations

import os
import json
import hashlib
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from ...runtime.computer import (
    COMPUTER_MODEL_TOOLS, COMPUTER_TOOL_IDS, computer_operation, is_computer_tool,
    COMPUTER_OBSERVATION_REF_PATTERN,
    COMPUTER_WRITE_OPERATIONS,
    ComputerBrokerError,
    computer_broker_client,
)
from ...config.manager import ConfigManager
from ...vision import configured_model_supports_images
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext
from ..capabilities import get_tool_capability


_RECOVERABLE_ERRORS = {
    "failed_known", "stale_observation", "stale_capture", "target_not_found",
    "recovery_observe_required", "task_authorization_required", "target_ref_required",
    "observation_contract_required", "target_ambiguous", "window_not_ready", "target_kind_mismatch",
    "effect_unverifiable", "partial_effect", "suspected_noop",
    "bring_to_front_exact_window_unverified",
}

_KNOWN_ACTION_RECEIPTS = {"effect_unverifiable", "partial_effect", "suspected_noop"}

def _recovery_metadata(operation: str, error_code: str) -> dict[str, Any]:
    retryable = error_code in _RECOVERABLE_ERRORS
    if error_code in {"background_unavailable", "foreground_required"}:
        action = "request_foreground_grant"
    elif error_code == "bring_to_front_exact_window_unverified":
        action = "observe_then_wait_for_foreground"
    elif error_code == "task_authorization_required":
        action = "authorize_via_activate_app_or_observe"
    elif error_code in {"target_ref_required", "target_not_found"}:
        action = "list_apps_then_select_target"
    elif error_code == "target_ambiguous":
        action = "select_candidate_window_then_activate_observe"
    elif error_code == "window_not_ready":
        action = "list_windows_then_select_target"
    elif error_code == "target_kind_mismatch":
        # A kind mismatch is often evidence that the model already has the
        # *next* useful ref (for example, passing a window ref back to
        # list_windows).  Sending it to the beginning of discovery creates a
        # list_apps -> activate_app -> list_windows loop and can request a
        # second approval for an app which is already open.  Give a local,
        # operation-specific recovery instead.
        action = (
            "list_windows_then_select_window"
            if operation == "observe"
            else "use_window_ref_for_observe_or_action"
        )
    elif error_code == "observation_contract_required":
        action = "observe_then_replan"
    elif retryable:
        action = "observe_then_replan"
    else:
        action = "stop_and_request_user"
    return {
        "retryable": retryable,
        "known_action_receipt": error_code in _KNOWN_ACTION_RECEIPTS,
        "recovery": {
            "action": action,
            "operation": operation,
            "error_code": error_code,
            "max_attempts": 2 if retryable else 0,
            "max_validation_rounds": 3 if error_code in _KNOWN_ACTION_RECEIPTS else 0,
            "never_replay_unknown": True,
            **({"instruction": "Foreground was not confirmed; another application may have focus. The task is not complete. Rediscover this app, observe its exact window, and prefer background if supported. If foreground is required, tell the user that this step needs brief foreground access and wait; do not repeatedly steal focus or replay input."}
               if error_code == "bring_to_front_exact_window_unverified" else {}),
            **({"instruction": "Use the returned candidate window target_ref with computer_control activate_app, then computer_observe that same window. Do not retry the ambiguous app target_ref. Ask the user if task evidence cannot distinguish the windows."}
               if error_code == "target_ambiguous" else {}),
            **({"instruction": "Rediscover the user-requested app with computer_discover list_apps, select its newly returned target_ref, then list_windows if a window ref is needed. Do not observe or replay an expired ref, switch to another app/window silently, or replay an action whose outcome is unknown."}
               if error_code == "target_not_found" else {}),
            **({"instruction": "This request was NOT dispatched. Call computer_observe on the same target_ref now to verify the previous action, then replan from that fresh state. Do not replay a previous action whose outcome is unknown."}
               if error_code == "recovery_observe_required" else
               {"instruction": "The action was dispatched; its effect is not yet verified. Observe the same window now and inspect the actual state. Do not repeat the action with another tool or coordinate."}
               if error_code in _KNOWN_ACTION_RECEIPTS else {}),
        },
    }


class ComputerApprovedControl(ParameterSchema):
    label: str = Field(min_length=1, max_length=160, description="Exact user-requested visible control label, never a wildcard.")
    operations: list[Literal["click_element", "double_click", "right_click", "open_menu", "select_option", "set_value", "type_text"]] = Field(min_length=1, max_length=7)
    text: str | None = Field(default=None, max_length=1000, description="Exact user-approved input for set_value/type_text.")


class ComputerAuthorization(ParameterSchema):
    purpose: Literal["app_task", "media_search_play", "local_document_read", "direct_message_send"] = Field(
        description="Host-validated task category; never use a model-invented risk label."
    )
    display_name: str = Field(min_length=1, max_length=80, description="User-visible application name in Chinese or the user's language.")
    target_label: str = Field(min_length=1, max_length=160, description="User-visible task purpose, for example 中搜索并播放《山丘》.")
    query: str | None = Field(default=None, max_length=80, description="Exact bounded search query for media_search_play.")
    file_root: str | None = Field(default=None, max_length=512, description="Exact absolute approved fixture directory for local_document_read.")
    file_path: str | None = Field(default=None, max_length=1024, description="Exact absolute file requested by the user for local_document_read; it must exist inside file_root.")
    recipient: str | None = Field(default=None, min_length=1, max_length=80, description="Exact user-named recipient for direct_message_send.")
    message_text: str | None = Field(default=None, min_length=1, max_length=1000, description="Exact user-authored message for direct_message_send.")
    controls: list[ComputerApprovedControl] = Field(default_factory=list, max_length=20, description="For app_task, bounded user-requested controls/actions to approve once. Unknown controls or coordinate-only actions need separate confirmation.")
    window_scope: Literal["current", "same_app"] = Field(default="current", description="Current exact window only by default; same_app explicitly requests other windows of this same process.")


class ComputerParameters(ParameterSchema):
    operation: Literal[
        "capabilities", "list_apps", "list_windows", "observe", "click_element",
        "right_click", "open_menu", "select_option", "set_value", "click_at", "double_click", "scroll", "type_text", "press_key",
        "activate_app", "status", "release_control",
    ]
    target_ref: str | None = Field(
        default=None,
        min_length=39,
        max_length=64,
        description=(
            "Exact opaque host-issued target_ref. list_windows requires an app ref returned by list_apps; "
            "observe requires a window ref returned by list_windows; after list_windows, keep that window "
            "ref for observe and all element/window actions—do not pass it to list_windows again. "
            "Element/window actions require the "
            "window ref returned by list_windows or observe. Never substitute one ref kind for another. "
            "Never construct an app identity from its name or bundle id."
        ),
    )
    query: str | None = Field(
        default=None, max_length=120,
        description="Case-insensitive app name/bundle-id or window-title filter for list_apps/list_windows. Use it to find an installed app that is not running.",
    )
    cursor: int = Field(default=0, ge=0, description="Discovery page offset returned as next_cursor.")
    limit: int = Field(default=25, ge=1, le=50, description="Maximum discovery candidates returned in one complete JSON result.")
    observation_ref: str | None = Field(
        default=None, min_length=36, max_length=36, pattern=COMPUTER_OBSERVATION_REF_PATTERN,
        description="Opaque reference from the current observe result. Include it for element actions based on that observation.",
    )
    element_id: str | None = Field(
        default=None,
        description=(
            "Exact element_token/element_id from the current observe result. Pair it with that result's "
            "observation_ref; do not rediscover the app or window between observation and action."
        ),
    )
    text: str | None = Field(default=None, max_length=100_000)
    input_mode: Literal["insert", "replace"] | None = None
    key: str | None = Field(default=None, max_length=64)
    modifiers: list[Literal["cmd", "ctrl", "option", "shift"]] = Field(default_factory=list, max_length=4)
    authorization: ComputerAuthorization | None = Field(
        default=None,
        description=(
            "Include only when the user's task exactly matches one supported purpose and all purpose fields "
            "can be supplied truthfully. For other apps/tasks use app_task with exact user-requested controls; "
            "otherwise the host requests exact per-action approval. Never misuse a semantic purpose."
        ),
    )
    x: float | None = None
    y: float | None = None
    delta_x: float | None = None
    delta_y: float | None = None
    coordinate_space: Literal["pixel", "normalized"] | None = Field(
        default=None,
        description="Required for coordinate click_at/double_click: coordinates in the exact delivered observation image.",
    )
    reference_width: int | None = Field(default=None, gt=0, description="Required for coordinate click_at/double_click: delivered image width used for planning.")
    reference_height: int | None = Field(default=None, gt=0, description="Required for coordinate click_at/double_click: delivered image height used for planning.")
    count: int | None = Field(
        default=None,
        ge=1,
        le=3,
        strict=True,
        description=(
            "Click count for click_at (1, 2, or 3). Use 2 only when the observed UI convention "
            "requires a double-click, then verify the effect with a fresh observe."
        ),
    )
    delivery_mode: Literal["background", "foreground"] = Field(
        default="background",
        description=(
            "Default to background so the user can work in other applications. Foreground is an explicit "
            "fallback when the official Driver reports background unavailable; it requires its own host approval."
        ),
    )

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "ComputerParameters":
        if self.input_mode is not None and self.operation != "type_text":
            raise ValueError("input_mode is only valid for type_text")
        if self.input_mode == "replace" and (self.element_id or self.x is None or self.y is None or self.delivery_mode != "foreground"):
            raise ValueError("replace requires current screenshot coordinates without element_id and delivery_mode=foreground")
        requires_target = {
            "list_windows", "observe", "click_element", "right_click", "open_menu", "select_option", "set_value", "click_at", "double_click",
            "scroll", "type_text", "press_key", "activate_app",
        }
        if self.operation in requires_target and not self.target_ref:
            prerequisite = "call list_apps first" if self.operation == "list_windows" else "call list_windows and select a window first"
            raise ValueError(f"target_ref is required for {self.operation}; {prerequisite}")
        if self.operation in {"click_element", "open_menu", "select_option", "set_value"}:
            if not self.observation_ref:
                raise ValueError(f"observation_ref is required for {self.operation}; use the exact current observe result")
            if not self.element_id:
                raise ValueError(f"element_id is required for {self.operation}; use an element from that observation")
        if self.operation == "observe" and self.observation_ref:
            raise ValueError("observation_ref is output from observe and must not be sent back to observe; use only target_ref")
        if self.element_id and self.operation in {"type_text", "press_key", "scroll"} and not self.observation_ref:
            raise ValueError(f"observation_ref is required when {self.operation} uses element_id")
        if self.element_id and self.operation not in {"click_element", "right_click", "open_menu", "select_option", "double_click", "set_value", "type_text", "press_key", "scroll"}:
            raise ValueError(f"element_id is not supported for {self.operation}")
        if self.operation not in {"list_apps", "list_windows"} and (self.query is not None or self.cursor or self.limit != 25):
            raise ValueError("query, cursor, and limit are only valid for list_apps/list_windows")
        pixel_input = self.operation in {"type_text", "press_key"} and any(
            value is not None for value in (self.x, self.y, self.coordinate_space, self.reference_width, self.reference_height)
        )
        if self.operation in {"click_at", "double_click", "right_click"} or pixel_input:
            required = ("x", "y", "coordinate_space", "reference_width", "reference_height")
            missing = [name for name in required if getattr(self, name) is None]
            if self.operation in {"double_click", "right_click"} and self.element_id and self.observation_ref:
                missing = []
            if missing:
                raise ValueError(f"{self.operation} requires current observation fields: {', '.join(missing)}")
            if pixel_input and (not self.observation_ref or self.element_id):
                raise ValueError("pixel input requires observation_ref and cannot combine element_id with coordinates")
        if self.operation != "click_at" and self.count is not None:
            raise ValueError("count is only valid for click_at")
        if self.operation not in {"click_at", "double_click", "right_click", "type_text", "press_key"} and (
            self.x is not None or self.y is not None or self.coordinate_space is not None
            or self.reference_width is not None or self.reference_height is not None
        ):
            raise ValueError(f"coordinates are not supported for {self.operation}")
        if self.operation in {"double_click", "right_click"} and self.element_id and any(
            value is not None for value in (self.x, self.y, self.coordinate_space, self.reference_width, self.reference_height)
        ):
            raise ValueError(f"{self.operation} accepts either a current element or current observation coordinates, not both")
        if self.operation in {"set_value", "type_text"} and self.text is None:
            raise ValueError(f"text is required for {self.operation}")
        if self.operation == "press_key" and not self.key:
            raise ValueError("key is required for press_key")
        if self.operation == "scroll" and not (self.delta_x or self.delta_y):
            raise ValueError("scroll requires a non-zero delta_x or delta_y")
        if self.operation != "press_key" and self.modifiers:
            raise ValueError("modifiers are only valid for press_key")
        return self


class _ComputerFacadeParameters(ParameterSchema):
    model_config = {"extra": "forbid"}


class ComputerDiscoverParameters(_ComputerFacadeParameters):
    query: str | None = Field(default=None, max_length=120)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=50)


class ComputerObserveParameters(_ComputerFacadeParameters):
    operation: Literal["list_windows", "observe"]
    target_ref: str = Field(
        min_length=39, max_length=64,
        description="Exact app ref for list_windows, or exact window ref for observe.",
    )
    delivery_mode: Literal["background", "foreground"] = "background"


class ComputerElementActionParameters(_ComputerFacadeParameters):
    operation: Literal["click_element", "right_click", "open_menu", "select_option", "set_value", "double_click"]
    target_ref: str = Field(min_length=39, max_length=64)
    observation_ref: str = Field(min_length=36, max_length=36, pattern=COMPUTER_OBSERVATION_REF_PATTERN)
    element_id: str = Field(min_length=1, description="Exact element_token from the current observe result.")
    text: str | None = Field(default=None, max_length=100_000)
    delivery_mode: Literal["background", "foreground"] = "background"

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "ComputerElementActionParameters":
        if self.operation == "set_value" and self.text is None:
            raise ValueError("text is required for set_value")
        if self.operation != "set_value" and self.text is not None:
            raise ValueError("text is only valid for set_value")
        return self


class ComputerPointerActionParameters(_ComputerFacadeParameters):
    operation: Literal["click_at", "right_click", "double_click"]
    target_ref: str = Field(min_length=39, max_length=64)
    observation_ref: str = Field(min_length=36, max_length=36, pattern=COMPUTER_OBSERVATION_REF_PATTERN, description="Exact observation_ref of the screenshot used for these coordinates.")
    x: float = Field(description="X in delivered screenshot pixels, or 0..1 if coordinate_space is normalized. Never screen points.")
    y: float = Field(description="Y in delivered screenshot pixels, or 0..1 if coordinate_space is normalized. Never screen points.")
    coordinate_space: Literal["pixel", "normalized"]
    reference_width: int = Field(gt=0, description="Copy coordinate_contract.reference_width from observe, including when normalized. Never use window bounds or divide by screenshot scale.")
    reference_height: int = Field(gt=0, description="Copy coordinate_contract.reference_height from observe, including when normalized. Never use window bounds or divide by screenshot scale.")
    count: int | None = Field(default=None, ge=1, le=3, strict=True)
    delivery_mode: Literal["background", "foreground"] = "background"

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "ComputerPointerActionParameters":
        if self.operation != "click_at" and self.count is not None:
            raise ValueError("count is only valid for click_at")
        return self


class ComputerInputActionParameters(_ComputerFacadeParameters):
    operation: Literal["scroll", "type_text", "press_key"]
    target_ref: str = Field(min_length=39, max_length=64)
    observation_ref: str | None = Field(default=None, min_length=36, max_length=36, pattern=COMPUTER_OBSERVATION_REF_PATTERN)
    element_id: str | None = None
    text: str | None = Field(default=None, max_length=100_000)
    key: str | None = Field(default=None, max_length=64)
    modifiers: list[Literal["cmd", "ctrl", "option", "shift"]] = Field(default_factory=list, max_length=4)
    delta_x: float | None = None
    delta_y: float | None = None
    delivery_mode: Literal["background", "foreground"] = "background"
    x: float | None = Field(default=None, description="Optional atomic focus+input: X in the exact delivered screenshot, not screen points. Use with y and current observation_ref when AX cannot address the field.")
    y: float | None = Field(default=None, description="Y in the delivered screenshot for atomic focus+input.")
    coordinate_space: Literal["pixel", "normalized"] | None = None
    reference_width: int | None = Field(default=None, gt=0)
    reference_height: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "ComputerInputActionParameters":
        coords = (self.x, self.y, self.coordinate_space, self.reference_width, self.reference_height)
        if any(value is not None for value in coords):
            if self.operation == "scroll" or not all(value is not None for value in coords) or not self.observation_ref or self.element_id:
                raise ValueError("atomic pixel input requires all coordinate fields and observation_ref, without element_id; not valid for scroll")
        if self.element_id and not self.observation_ref:
            raise ValueError("observation_ref is required when element_id is provided")
        if self.operation == "type_text":
            if self.text is None:
                raise ValueError("text is required for type_text")
        elif self.text is not None:
            raise ValueError("text is only valid for type_text")
        if self.operation == "press_key":
            if not self.key:
                raise ValueError("key is required for press_key")
        elif self.key is not None or self.modifiers:
            raise ValueError("key and modifiers are only valid for press_key")
        if self.operation == "scroll":
            if not (self.delta_x or self.delta_y):
                raise ValueError("scroll requires a non-zero delta_x or delta_y")
        elif self.delta_x is not None or self.delta_y is not None:
            raise ValueError("delta_x and delta_y are only valid for scroll")
        return self


class _ComputerKeyboardTargetParameters(_ComputerFacadeParameters):
    target_ref: str = Field(min_length=39, max_length=64)
    observation_ref: str | None = Field(default=None, min_length=36, max_length=36, pattern=COMPUTER_OBSERVATION_REF_PATTERN)
    element_id: str | None = None
    delivery_mode: Literal["background", "foreground"] = "background"
    x: float | None = Field(default=None, description="Optional atomic focus: X from the current delivered screenshot; supply all coordinate fields and observation_ref, without element_id.")
    y: float | None = None
    coordinate_space: Literal["pixel", "normalized"] | None = None
    reference_width: int | None = Field(default=None, gt=0)
    reference_height: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_focus_contract(self):
        coords = (self.x, self.y, self.coordinate_space, self.reference_width, self.reference_height)
        if any(value is not None for value in coords) and (
            not all(value is not None for value in coords) or not self.observation_ref or self.element_id
        ):
            raise ValueError("atomic pixel input requires all coordinate fields and observation_ref, without element_id")
        if self.element_id and not self.observation_ref:
            raise ValueError("observation_ref is required when element_id is provided")
        return self


class ComputerTypeTextParameters(_ComputerKeyboardTargetParameters):
    operation: Literal["type_text"] = "type_text"
    text: str = Field(max_length=100_000, description="Literal text. Use input_mode=replace for a new search query, URL or filter; insert preserves existing content.")
    input_mode: Literal["insert", "replace"] = Field(default="insert", description="replace focuses the exact screenshot field, selects all and types once without clicking again. Requires all coordinate fields and foreground delivery. Use replace even when a search field may only show a placeholder.")


class ComputerPressKeyParameters(_ComputerKeyboardTargetParameters):
    operation: Literal["press_key"] = "press_key"
    key: str = Field(min_length=1, max_length=64, description="One key, such as a or return. Put modifiers in modifiers; never supply text.")
    modifiers: list[Literal["cmd", "ctrl", "option", "shift"]] = Field(default_factory=list, max_length=4)


class ComputerScrollParameters(_ComputerFacadeParameters):
    operation: Literal["scroll"] = "scroll"
    target_ref: str = Field(min_length=39, max_length=64)
    observation_ref: str | None = Field(default=None, min_length=36, max_length=36, pattern=COMPUTER_OBSERVATION_REF_PATTERN)
    element_id: str | None = None
    delta_x: float = 0
    delta_y: float = 0
    delivery_mode: Literal["background", "foreground"] = "background"

    @model_validator(mode="after")
    def validate_scroll_contract(self):
        if not (self.delta_x or self.delta_y):
            raise ValueError("scroll requires a non-zero delta_x or delta_y")
        if self.element_id and not self.observation_ref:
            raise ValueError("observation_ref is required when element_id is provided")
        return self


class ComputerControlParameters(_ComputerFacadeParameters):
    operation: Literal["activate_app", "capabilities", "status", "release_control"]
    target_ref: str | None = Field(default=None, min_length=39, max_length=64)
    delivery_mode: Literal["background", "foreground"] = "background"

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "ComputerControlParameters":
        if self.operation == "activate_app" and not self.target_ref:
            raise ValueError("target_ref is required for activate_app")
        if self.operation != "activate_app" and self.target_ref:
            raise ValueError("target_ref is only valid for activate_app")
        return self


class ComputerTool(Tool):
    @property
    def id(self) -> str:
        return "computer"

    @property
    def description(self) -> str:
        return (
            "Observe or operate a user-authorized macOS application through the TCode desktop host. "
            "Start with capabilities or list_apps. list_apps needs no target_ref; pass query to search installed apps by name/bundle id. "
            "Without query, list_apps intentionally returns running apps only; absence never proves an app is uninstalled, so search once with the user-visible app name before concluding that. "
            "list_windows requires the app target_ref from list_apps and returns window target_refs. Copy the matching window target_ref exactly; use that window ref for observe and every subsequent action, and never pass it back to list_windows. "
            "Use page.next_cursor with cursor when more candidates exist. Never invent or send app/window objects. "
            "Actions require an exact, current "
            "desktop grant; foreground use is never an automatic fallback from an explicitly requested "
            "background call. Use foreground consistently for interactive app tasks and reobserve after every action. "
            "For a user-requested multi-step task, include authorization={purpose, display_name, target_label, "
            "query?, file_root?, file_path?, recipient?, message_text?} on the first app-bound activate_app or observe (not on discovery), but only when the task truthfully matches that purpose. Omit authorization for unrelated apps/tasks; never reuse the nearest-sounding purpose. "
            "For local_document_read, file_path is the exact user-requested file and file_root is its containing "
            "test directory. Specific host-validated purposes are media_search_play, local_document_read, and "
            "direct_message_send. Other multi-step tasks use app_task with exact user-requested control labels/actions for one task-scoped approval. Unknown controls still need exact Host approval. For click_at, include coordinate_space plus the exact width and height of the "
            "delivered observation image; never reuse coordinates after a new observation. AX coverage may remain partial or unknown even when returned counts match. A listed element_token or delivered screenshot is actionable evidence, not proof of a complete tree or successful task. Act directly on an exact listed token instead of repeatedly observing merely to rediscover it: use right_click for a context-click, double_click for an open gesture, open_menu for an AX menu-capable control, and select_option for an observed option. Every action must be followed by a fresh observe and judged by visible/AX state change, not by dispatch success alone. "
            "For messaging, verify one exact human recipient "
            "and the chat title before drafting; the final send always receives a separate exact host confirmation. "
            "After approval use observe -> one action -> observe. The host may approve that exact task/app scope "
            "once; do not invent or broaden it."
        )

    @property
    def parameters(self) -> type:
        return ComputerParameters

    def available_to_agent(self, agent_name: str) -> bool:
        # Kept registered for replaying old sessions and internal callers, but
        # the model receives only the smaller flat facade schemas below.
        return False

    def runtime_available_to_agent(self, agent_name: str) -> bool:
        return (
            agent_name == "build"
            and os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
            and os.environ.get("TCODE_COMPUTER_USE_ENABLED") == "1"
            and computer_broker_client() is not None
        )

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ComputerParameters(**args)
        client = computer_broker_client()
        if client is None or os.environ.get("TCODE_DESKTOP_MANAGED") != "1" or os.environ.get("TCODE_COMPUTER_USE_ENABLED") != "1":
            return ToolResult(
                title="Computer unavailable",
                output="桌面电脑控制连接当前不可用，本轮已停止；不要重复调用工具。",
                error="computer_unavailable",
                metadata={
                    "retryable": False,
                    "terminal_tool_error": True,
                    "should_end_turn": True,
                    "requires_final_summary": True,
                    "execution_skipped": True,
                    "recovery": {
                        "action": "stop_and_request_user", "operation": parsed.operation,
                        "error_code": "computer_unavailable", "max_attempts": 0,
                        "max_validation_rounds": 0, "never_replay_unknown": True,
                    },
                },
            )
        payload = parsed.model_dump(exclude_none=True)
        operation = payload.pop("operation")
        progress_binding = bool(
            operation in {"click_element", "open_menu", "select_option", "set_value"}
            or (operation in {"right_click", "double_click"} and parsed.element_id and parsed.observation_ref)
        )
        action_signature = ""
        if operation in COMPUTER_WRITE_OPERATIONS:
            signature_payload = {
                key: value for key, value in payload.items()
                if key not in {"authorization", "observation_ref", "x", "y"}
            }
            if parsed.element_id:
                signature_payload["element_id"] = parsed.element_id.rsplit(":", 1)[-1]
            signature_payload["operation"] = operation
            action_signature = hashlib.sha256(json.dumps(
                signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
        if "observation_ref" in payload:
            payload["observation_id"] = payload.pop("observation_ref")
        payload["task_id"] = context.session_id
        payload["task_consent_required"] = True
        intent = (getattr(context, "extra", None) or {}).get("computer_task_intent")
        if isinstance(intent, dict) and intent.get("task_id") == context.session_id:
            payload["task_intent"] = dict(intent)
        payload["call_id"] = str(context.call_id or "")
        # Trusted runtime identity, never a model-supplied tool argument.
        provider = (getattr(context, "extra", None) or {}).get("provider")
        service = str(getattr(provider, "provider_id", "") or "").strip()
        model = str(getattr(provider, "_model", "") or getattr(provider, "model", "") or "").strip()
        if service:
            payload["screenshot_service"] = service
            payload["screenshot_model"] = f"{service}/{model}"
        try:
            result = await client.request(operation, payload)
        except ComputerBrokerError as exc:
            recovery = _recovery_metadata(operation, exc.code)
            return ToolResult(
                title="Computer action blocked",
                output=json.dumps(recovery["recovery"], ensure_ascii=False),
                error=exc.code,
                metadata={
                    "computer_audit": {"operation": operation, "status": "failed_known", "error_code": exc.code},
                    **recovery,
                },
            )
        public = dict(result.get("result") or {})
        if operation in COMPUTER_WRITE_OPERATIONS:
            public.setdefault("target_ref", public.get("window_target_ref") or parsed.target_ref)
        audit = dict(result.get("audit") or {})
        error_code = str(result.get("error_code") or "")
        if error_code == "computer_disabled":
            return ToolResult(
                title="Computer Use 尚未开启",
                output="电脑控制已关闭，本次未执行任何操作。请回复用户：打开桌面顶部的“电脑控制”，开启“允许 Computer Use”开关，然后告诉我继续。不要重复调用工具或尝试绕过开关。",
                error="computer_disabled",
                metadata={
                    "computer_audit": audit, "computer_disabled": True,
                    "user_action_required": True, "execution_skipped": True,
                    "retryable": False, "terminal_tool_error": True,
                    "should_end_turn": True, "requires_final_summary": True,
                    "recovery": {
                        "action": "stop_and_request_user", "operation": operation,
                        "error_code": "computer_disabled", "max_attempts": 0,
                        "max_validation_rounds": 0, "never_replay_unknown": True,
                    },
                },
            )
        if error_code in {"system_permission_denied", "system_permission_unknown"}:
            permissions = dict(public.get("permissions") or {})
            required = dict(public.get("required_permissions") or {})
            reasons = []
            if public.get("runtime_mismatch") is True:
                reasons.append("宿主程序已识别授权，但控制进程检测不一致。先重新检查；如仍不一致，保存工作后重启源码桌面端。不要要求用户重复开启已授权的权限")
            if permissions.get("identity_verified") is False:
                reasons.append("控制进程的权限归属校验未通过，需要检查桌面 Driver 的宿主身份；这不是重新切换开关能解决的问题")
            if permissions.get("accessibility") is False:
                reasons.append("当前控制进程尚未确认辅助功能权限；这不等于系统设置中的开关未开启。用户已授权时先重新检查当前进程状态")
            if required.get("screen_recording") and permissions.get("screen_recording") is False:
                reasons.append("当前控制进程尚未确认屏幕录制权限；用户已授权时先重新检查，必要时按系统提示重启应用")
            if not reasons:
                reasons.append("系统权限或控制进程身份检查未通过，当前回执未提供具体失败项；请查看电脑控制面板中的权限检查状态")
            guidance = "Computer Use 应用开关与 macOS 系统权限是两层不同的检查。本次被系统权限/身份检查阻断，不能据此声称开关未开启，也不要建议反复切换开关。桌面已请求展开权限诊断面板。用户已授权时优先点击‘我已开启，重新检查’；仅在用户确实未授权时引导使用‘查看辅助功能设置’或‘查看屏幕录制设置’。不要声称已经获准或自动重放操作。\n" + "\n".join(reasons)
            return ToolResult(title="Computer Use 系统权限检查未通过", output=guidance,
                error=error_code, metadata={"computer": public, "computer_audit": audit,
                    "user_action_required": True, "execution_skipped": True, "retryable": False,
                    "terminal_tool_error": True, "should_end_turn": True, "requires_final_summary": True,
                    **_recovery_metadata(operation, error_code)})
        model_images: list[dict[str, Any]] = []
        image = dict(public.pop("model_image", {}) or {})
        image_path = str(public.pop("model_image_path", "") or image.get("path") or "")
        image_forwarded = False
        if image_path:
            workdir = str((context.extra or {}).get("workdir") or os.getcwd())
            ConfigManager.load(workdir)
            config = ConfigManager.get_config_copy()
            provider = (context.extra or {}).get("provider")
            provider_id = str(getattr(provider, "provider_id", "") or "").strip()
            model_id = str(getattr(provider, "_model", "") or getattr(provider, "model", "") or "").strip()
            model_ref = f"{provider_id}/{model_id}" if provider_id and model_id else ""
            vision = configured_model_supports_images(config, model_ref) if model_ref else None
            service_approved = bool(provider_id and public.get("screenshot_service_authorized") == provider_id)
            if not service_approved or vision is not True:
                code = "model_vision_unsupported" if vision is False else "model_vision_unconfirmed" if vision is None else "screenshot_authorization_required"
                guidance = (
                    f"当前模型 {model_ref} 支持图像，但应用任务确认尚未授权当前截图接收服务。截图未发送。请使用桌面电脑控制的任务确认，不要另外发起截图授权问答，也不能说模型没有视觉能力。"
                    if vision is True else
                    f"当前模型 {model_ref or '（未识别）'} 的配置{'明确不支持图像，请切换到支持图像的模型' if vision is False else '尚未明确声明图像能力，请检查当前模型配置，不能据此断言模型没有视觉能力'}。截图未发送。"
                )
                return ToolResult(title="Computer Use 截图未发送", output=guidance, error=code,
                    metadata={"computer_audit": audit, "model_ref": model_ref,
                        "model_images": [], "user_action_required": True, "retryable": False,
                        "terminal_tool_error": True, "should_end_turn": True,
                        "requires_final_summary": True})
            if service_approved:
                model_images.append({
                    "path": image_path, "mime_type": image.get("mime_type", "image/png"),
                    "size": image.get("size"), "sha256": image.get("sha256"),
                    "detail": "high", "source": "computer_observation",
                    "artifact_root": client.capture_root,
                    "reference_width": public.get("screenshot_width"),
                    "reference_height": public.get("screenshot_height"),
                    "observation_ref": public.get("observation_ref"),
                })
                image_forwarded = True
        if operation == "observe":
            public["image_attached_to_model_input"] = image_forwarded
            public["image_size"] = image.get("size") if image else None
            public.pop("ax_state", None)
            public.pop("accessibility_state", None)
            coverage = str(dict(public.get("progress_evidence") or {}).get("semantic_coverage") or "unknown")
            if image_path and not image_forwarded and coverage != "complete":
                error_code = "vision_required"
        recovery = _recovery_metadata(operation, error_code)
        receipt = dict(public.get("action_receipt") or {})
        waiting = bool(not error_code and result.get("status") == "completed_known" and receipt.get("verification") == "pending")
        if waiting:
            foreground_hint = dict(public.get("escalation") or {}).get("recommended") == "foreground"
            recovery = {"retryable": False, "verification_required": True, "recovery": {
                "action": "observe_then_replan", "operation": operation, "max_attempts": 0,
                "never_replay_unknown": True,
                "instruction": "The action was dispatched; its effect is pending verification, not an execution failure or task success. Observe this exact target now. Inspect the fresh state before choosing a different next action; do not blindly repeat the dispatched action." + (
                    " The native background route does not move the hardware pointer; some apps hit-test its old location. If the intended control did not respond or a different control responded, do not try more background coordinates. Replan from the fresh image and request delivery_mode=foreground for the next necessary action, subject to Host approval. Foreground activation alone does not change later calls' delivery_mode. Never replay an unknown submission."
                    if foreground_hint else ""
                ),
            }}
            public["verification_required"] = True
            public["next_operation"] = "observe"
            public["recovery"] = recovery["recovery"]
        if error_code:
            public["recovery"] = recovery["recovery"]
        result_fingerprint = str(public.get("result_fingerprint") or "")
        return ToolResult(
            title=str(result.get("title") or f"Computer {operation}"),
            output=json.dumps(public, ensure_ascii=False, separators=(",", ":")),
            error=error_code or None,
            metadata={
                "computer": public,
                "computer_verification_pending": waiting,
                "computer_audit": audit,
                "model_images": model_images,
                "outcome_unknown": result.get("status") == "outcome_unknown",
                "semantic_readonly": operation in {"capabilities", "list_apps", "list_windows", "observe", "status"},
                "computer_progress_binding": progress_binding,
                **({"computer_action_signature": action_signature} if action_signature else {}),
                **({"computer_action_region": [
                    parsed.x * (parsed.reference_width if parsed.coordinate_space == "normalized" else 1),
                    parsed.y * (parsed.reference_height if parsed.coordinate_space == "normalized" else 1),
                ]} if parsed.x is not None and parsed.y is not None else {}),
                "computer_progress_evidence": public.get("progress_evidence"),
                **({"computer_visual_fingerprint": str(image.get("sha256"))} if image.get("sha256") else {}),
                **({"result_fingerprint": result_fingerprint} if result_fingerprint else {}),
                **recovery,
            },
        )

    @classmethod
    def register(cls) -> None:
        from ..registry import ToolRegistry
        ToolRegistry.register(cls())


def computer_input_argument_rejection(tool_name: str) -> ToolResult:
    instruction = (
        "This request was NOT dispatched. Observe the same target with a fresh computer_observe "
        "before replanning. Use computer_type_text with text only, computer_press_key with key/modifiers "
        "only, or computer_scroll with delta_x/delta_y only. Never combine typing and shortcuts, "
        "reuse a consumed image, or replay a previous action whose outcome is unknown."
    )
    return ToolResult(
        title=tool_name,
        error="invalid_computer_arguments",
        output=instruction,
        metadata={
            "execution_skipped": True,
            "computer_argument_rejected": True,
            "retryable": True,
            "terminal_tool_error": False,
            "should_end_turn": False,
            "execution_environment": {"location": "not_executed", "label": "Not executed", "sandboxed": False},
            "recovery": {
                "action": "observe_then_replan", "max_attempts": 2,
                "never_replay_unknown": True, "instruction": instruction,
            },
        },
    )


class _ComputerFacade(Tool):
    """Thin model-facing schema; execution and safety stay in ComputerTool."""

    tool_id: str
    schema: type[ParameterSchema]
    summary: str
    fixed_operation: str | None = None

    @property
    def id(self) -> str:
        return self.tool_id

    @property
    def description(self) -> str:
        return self.summary

    @property
    def parameters(self) -> type[ParameterSchema]:
        return self.schema

    @property
    def capability(self):
        return get_tool_capability("computer")

    def available_to_agent(self, agent_name: str) -> bool:
        return ComputerTool().runtime_available_to_agent(agent_name)

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            payload = self.parameters(**args).model_dump(exclude_none=True)
        except ValidationError:
            if self.id in {"computer_input_action", "computer_type_text", "computer_press_key", "computer_scroll"}:
                return computer_input_argument_rejection(self.id)
            raise
        if self.fixed_operation:
            payload["operation"] = self.fixed_operation
        return await ComputerTool().execute(payload, context)

    @classmethod
    def register(cls) -> None:
        from ..registry import ToolRegistry
        ToolRegistry.register(cls())


class ComputerDiscoverTool(_ComputerFacade):
    tool_id = "computer_discover"
    schema = ComputerDiscoverParameters
    fixed_operation = "list_apps"
    summary = "List running desktop applications. Optionally query installed apps by user-visible name; copy the returned opaque app ref exactly."


class ComputerObserveTool(_ComputerFacade):
    tool_id = "computer_observe"
    schema = ComputerObserveParameters
    summary = (
        "List windows for an exact app ref, or observe an exact window ref. First use list_windows, "
        "then copy one returned window ref into observe. An observe result contains actionable element_token "
        "values and a screenshot; partial or unknown AX coverage is not a reason to keep re-observing. "
        "Use an observed token or screenshot for one action, then observe again to verify the change. "
        "Host handles approval beside the tool call and resumes the same call after the user decides. Do not supply permission or approval fields."
    )


class ComputerElementActionTool(_ComputerFacade):
    tool_id = "computer_element_action"
    schema = ComputerElementActionParameters
    summary = "Act on one exact element from the current observation. set_value replaces the entire editable field contents; text='' clears it. Prefer it for native search/filter fields. Chromium/Electron AX writes may not reach the visible input: inspect a fresh screenshot, and if necessary use pixel focus, cmd+a, then computer_type_text once. Do not blindly append. Re-observe after every action."


class ComputerPointerActionTool(_ComputerFacade):
    tool_id = "computer_pointer_action"
    schema = ComputerPointerActionParameters
    summary = "Use only when no exact actionable element token is available. Act at coordinates from the delivered screenshot, never AX screen-point frame coordinates. reference_width/height must equal screenshot_width/height even for normalized coordinates, not window_bounds. Never reuse coordinates after re-observing."


class ComputerInputActionTool(_ComputerFacade):
    tool_id = "computer_input_action"
    schema = ComputerInputActionParameters
    summary = "Scroll, type text, or press a key in one exact target. When AX cannot address an editable field, prefer atomic focus+input: supply x,y from the current delivered screenshot plus observation_ref, coordinate_space and its exact reference_width/height, without element_id. This focuses and inputs in one official Driver call. Never divide pixels by backing scale or reuse coordinates after observing again. Always observe the actual effect; a dispatched key is not proof of results or playback."

    def available_to_agent(self, agent_name: str) -> bool:
        return False  # Historical calls retain validation/execution, not model exposure.


class ComputerTypeTextTool(_ComputerFacade):
    tool_id = "computer_type_text"
    schema = ComputerTypeTextParameters
    fixed_operation = "type_text"
    summary = "Type literal text once. For search queries, URLs and filters use input_mode=replace with current screenshot coordinates and delivery_mode=foreground: Host focuses, selects all and types without a second click. This works for empty fields, placeholders and existing text. insert preserves existing content; after separately selecting text, omit coordinates to avoid cancelling the selection. Observe the complete resulting value before submitting; dispatch is not proof of success."


class ComputerPressKeyTool(_ComputerFacade):
    tool_id = "computer_press_key"
    schema = ComputerPressKeyParameters
    fixed_operation = "press_key"
    summary = "Press one key with optional modifiers, without inserting text. When replacing an existing query with incomplete AX, PREFER current screenshot coordinates with key=a modifiers=[cmd] to atomically focus and select all in one official Cua call, using exact image dimensions, without element_id. For a shortcut use key=a and modifiers=[cmd], then observe before any next action. Unverifiable requires a fresh observation; delivery is not proof of selection, submission or playback."


class ComputerScrollTool(_ComputerFacade):
    tool_id = "computer_scroll"
    schema = ComputerScrollParameters
    fixed_operation = "scroll"
    summary = "Scroll one exact target or observed scrollable region to find a task target outside the current view. Use non-zero delta_y (positive down, negative up) or delta_x for horizontal content; deltas are not screenshot pixels. An element requires its current observation_ref. Make one small scroll, then computer_observe that same window before deciding another action; never batch scrolls or reuse old coordinates. Stop when the target appears or an explicit boundary is reached. Two consecutive scrolls with no task-relevant content movement require stopping that direction; correct the region or approved delivery route at most once. At most 8 scrolls per target/page by default, including corrections and reversal; do not reset on observation or direction changes. Animation or a new snapshot ID is not progress. Report only the searched range when unsuccessful. Do not insert text or press keys with this tool."


class ComputerControlTool(_ComputerFacade):
    tool_id = "computer_control"
    schema = ComputerControlParameters
    summary = "Activate an exact app or selected window, inspect status, or release control. After target_ambiguous use a candidate window target_ref, never retry the ambiguous app ref; ask the user if evidence cannot distinguish candidates. Host preserves a selected window only within the live approved task/process; a closed window or restarted process requires fresh discovery. Observe that same window after activation. Host handles task approval and resumes this call after the user decides; do not supply permission or approval fields."
