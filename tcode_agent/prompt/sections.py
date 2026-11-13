"""五个内建 Prompt Section 的单轨 Provider。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .templates import load_prompt_template


FRONTEND_IMAGE_SECTION_ID = "tcode.prompt.section.frontend-image-display@1"
IMAGE_UNDERSTANDING_SECTION_ID = "tcode.prompt.section.image-understanding@1"
PUBLIC_ACTION_COMMENTARY_SECTION_ID = "tcode.prompt.section.public-action-commentary@1"
ACTIVE_AGENT_CONTEXT_SECTION_ID = "tcode.prompt.section.active-agent-context@1"
DIRECT_EXECUTION_MODE_SECTION_ID = "tcode.prompt.section.direct-execution-mode@1"

PUBLIC_ACTION_COMMENTARY_ACTIVATION_POLICY_ID = (
    "tcode.prompt.activation.tools-without-json-schema@1"
)
ACTIVE_AGENT_CONTEXT_ACTIVATION_POLICY_ID = (
    "tcode.prompt.activation.agent-name-and-source-ready@1"
)
DIRECT_EXECUTION_MODE_ACTIVATION_POLICY_ID = (
    "tcode.prompt.activation.direct-execution-mode-enabled@1"
)


def _digest_text(value: str) -> str:
    """计算带算法前缀的 UTF-8 文本摘要。"""

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BuiltinPromptSection:
    """保存一个只在当前组装调用中持有正文的内建 Section。"""

    section_id: str
    title: str
    content: str = field(repr=False)
    order_anchor: str
    required: bool
    content_digest: str
    provider_id: str = "tcode.prompt.section.builtin"
    provider_version: str = "1"


class PublicActionCommentaryActivationState(str, Enum):
    """列出公开动作说明的稳定激活状态。"""

    ACTIVE = "active"
    INACTIVE_NO_TOOLS = "inactive-no-tools"
    INACTIVE_JSON_SCHEMA = "inactive-json-schema"


@dataclass(frozen=True, slots=True)
class PublicActionCommentaryActivation:
    """保存不含工具定义或 Schema 正文的激活结果。"""

    policy_id: str
    state: PublicActionCommentaryActivationState
    active: bool
    tool_definition_count: int
    output_format_kind: str
    output_schema_present: bool


class ActiveAgentContextActivationState(str, Enum):
    """列出活动 Agent 上下文的稳定激活状态。"""

    ACTIVE = "active"
    INACTIVE_NO_AGENT_NAME = "inactive-no-agent-name"
    INACTIVE_SOURCE_ERROR = "inactive-source-error"


@dataclass(frozen=True, slots=True)
class ActiveAgentContextActivation:
    """保存不含 Agent 原始身份的激活结果。"""

    policy_id: str
    state: ActiveAgentContextActivationState
    active: bool
    agent_name_present: bool
    source_ready: bool


@dataclass(frozen=True, slots=True)
class ActiveAgentContextMember:
    """保存当前调用栈中的可见团队成员。"""

    name: str
    role: str
    resident: bool


@dataclass(frozen=True, slots=True)
class ActiveAgentContextCandidateInput:
    """保存活动 Agent Section 的短生命周期动态输入。"""

    activation: ActiveAgentContextActivation
    agent_name: str = field(default="", repr=False)
    display_name: str = field(default="", repr=False)
    team_present: bool = False
    team_key: str = field(default="", repr=False)
    team_display_name: str = field(default="", repr=False)
    runtime_agent: str = field(default="", repr=False)
    members: tuple[ActiveAgentContextMember, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    source_error_type: str = ""


class DirectExecutionModeActivationState(str, Enum):
    """列出直执行模式的稳定激活状态。"""

    ACTIVE = "active"
    INACTIVE_MODE_OFF = "inactive-mode-off"


@dataclass(frozen=True, slots=True)
class DirectExecutionModeActivation:
    """保存不含 scope 正文的直执行模式激活结果。"""

    policy_id: str
    state: DirectExecutionModeActivationState
    active: bool
    scope_present: bool
    scope_utf8_bytes: int


@dataclass(frozen=True, slots=True)
class DirectExecutionModeCandidateInput:
    """保存直执行模式 Section 的短生命周期输入。"""

    activation: DirectExecutionModeActivation
    scope: str = field(default="", repr=False)


def evaluate_public_action_commentary_activation(
    *,
    tool_definition_count: int,
    output_format: Any,
) -> PublicActionCommentaryActivation:
    """判断公开动作说明是否进入本轮生产 Prompt。

    参数:
        tool_definition_count: 本轮模型可见工具数量。
        output_format: 当前输出格式，只判断是否为 JSON Schema。

    返回:
        不含工具定义与 Schema 正文的激活结果。
    """

    try:
        count = max(0, int(tool_definition_count))
    except (TypeError, ValueError):
        count = 0
    json_schema = bool(
        isinstance(output_format, dict)
        and output_format.get("type") == "json_schema"
    )
    if count <= 0:
        state = PublicActionCommentaryActivationState.INACTIVE_NO_TOOLS
    elif json_schema:
        state = PublicActionCommentaryActivationState.INACTIVE_JSON_SCHEMA
    else:
        state = PublicActionCommentaryActivationState.ACTIVE
    return PublicActionCommentaryActivation(
        policy_id=PUBLIC_ACTION_COMMENTARY_ACTIVATION_POLICY_ID,
        state=state,
        active=state is PublicActionCommentaryActivationState.ACTIVE,
        tool_definition_count=count,
        output_format_kind="json-schema" if json_schema else "default",
        output_schema_present=json_schema,
    )


def _active_agent_activation(
    state: ActiveAgentContextActivationState,
) -> ActiveAgentContextActivation:
    """根据稳定状态建立活动 Agent 激活结果。"""

    active = state is ActiveAgentContextActivationState.ACTIVE
    return ActiveAgentContextActivation(
        policy_id=ACTIVE_AGENT_CONTEXT_ACTIVATION_POLICY_ID,
        state=state,
        active=active,
        agent_name_present=(
            state is not ActiveAgentContextActivationState.INACTIVE_NO_AGENT_NAME
        ),
        source_ready=active,
    )


def build_active_agent_context_candidate_input(
    agent: Any,
) -> ActiveAgentContextCandidateInput:
    """从 Agent 与 TeamCatalog 构建唯一动态 Section 输入。

    参数:
        agent: 当前 PromptBuilder 所属 Agent。

    返回:
        只在本次调用中持有动态身份的不可变输入；来源异常会安全地停用
        Section，而不会把异常正文写入 Prompt。
    """

    agent_info = getattr(agent, "agent_info", None)
    agent_name = str(getattr(agent_info, "name", "") or "").strip()
    if not agent_name:
        return ActiveAgentContextCandidateInput(
            activation=_active_agent_activation(
                ActiveAgentContextActivationState.INACTIVE_NO_AGENT_NAME
            )
        )
    try:
        from ..agent.agents import BuiltinAgents
        from ..team.catalog import TeamCatalog

        display_name = agent_name
        options = getattr(agent_info, "options", {}) or {}
        if isinstance(options, dict):
            display_name = str(options.get("display_name") or display_name).strip()
        info = BuiltinAgents.get(agent_name)
        if info is not None:
            info_options = getattr(info, "options", {}) or {}
            display_name = str(
                info_options.get("display_name") or display_name or info.name
            ).strip()
        entry = TeamCatalog.get_by_agent_name(agent_name, available_only=False)
        members: list[ActiveAgentContextMember] = []
        if entry is not None:
            for worker in getattr(entry, "workers", ()) or ():
                if not bool(getattr(worker, "enabled", True)) or not bool(
                    getattr(worker, "visible", True)
                ):
                    continue
                name = str(getattr(worker, "name", "") or "").strip()
                if not name:
                    continue
                role = str(getattr(worker, "role", "") or "").strip()
                if role.lower() == name.lower():
                    role = ""
                members.append(
                    ActiveAgentContextMember(
                        name=name,
                        role=role,
                        resident=bool(getattr(worker, "resident", False)),
                    )
                )
            if not members:
                members = [
                    ActiveAgentContextMember(
                        name=str(name).strip(),
                        role="",
                        resident=False,
                    )
                    for name in getattr(entry, "worker_names", ()) or ()
                    if str(name).strip()
                ]
        return ActiveAgentContextCandidateInput(
            activation=_active_agent_activation(
                ActiveAgentContextActivationState.ACTIVE
            ),
            agent_name=agent_name,
            display_name=display_name or agent_name,
            team_present=entry is not None,
            team_key=str(getattr(entry, "key", "") or "") if entry else "",
            team_display_name=(
                str(getattr(entry, "display_name", "") or "") if entry else ""
            ),
            runtime_agent=(
                str(getattr(entry, "runtime_agent", "") or "") if entry else ""
            ),
            members=tuple(members),
        )
    except Exception as exc:
        return ActiveAgentContextCandidateInput(
            activation=_active_agent_activation(
                ActiveAgentContextActivationState.INACTIVE_SOURCE_ERROR
            ),
            source_error_type=type(exc).__name__,
        )


def build_direct_execution_mode_candidate_input(
    agent: Any,
) -> DirectExecutionModeCandidateInput:
    """从 Agent 字段构建唯一直执行模式输入。

    参数:
        agent: 当前 PromptBuilder 所属 Agent。

    返回:
        scope 不进入 repr 的短生命周期输入。
    """

    scope = str(getattr(agent, "_direct_execution_scope", "") or "").strip()
    active = bool(getattr(agent, "_direct_execution_mode", False))
    activation = DirectExecutionModeActivation(
        policy_id=DIRECT_EXECUTION_MODE_ACTIVATION_POLICY_ID,
        state=(
            DirectExecutionModeActivationState.ACTIVE
            if active
            else DirectExecutionModeActivationState.INACTIVE_MODE_OFF
        ),
        active=active,
        scope_present=bool(scope),
        scope_utf8_bytes=len(scope.encode("utf-8")),
    )
    return DirectExecutionModeCandidateInput(activation=activation, scope=scope)


def _section(
    *,
    section_id: str,
    title: str,
    content: str,
    order_anchor: str,
) -> BuiltinPromptSection:
    """建立带稳定摘要的必需内建 Section。"""

    return BuiltinPromptSection(
        section_id=section_id,
        title=title,
        content=content,
        order_anchor=order_anchor,
        required=True,
        content_digest=_digest_text(content),
    )


class BuiltinPromptSectionRegistry:
    """提供五个不可替换的内建 Prompt Section。"""

    @classmethod
    def provide_frontend_image_display(cls) -> BuiltinPromptSection:
        """返回前端图片展示协议 Section。"""

        content = load_prompt_template("frontend-image-display.md")
        return _section(
            section_id=FRONTEND_IMAGE_SECTION_ID,
            title="Frontend Image Display",
            content=content,
            order_anchor="after-agent-instructions-before-image-understanding",
        )

    @classmethod
    def provide_image_understanding(cls) -> BuiltinPromptSection:
        """返回图片理解路由协议 Section。"""

        content = load_prompt_template("image-understanding.md")
        return _section(
            section_id=IMAGE_UNDERSTANDING_SECTION_ID,
            title="Image Understanding",
            content=content,
            order_anchor="after-frontend-image-before-public-commentary",
        )

    @classmethod
    def provide_public_action_commentary(cls) -> BuiltinPromptSection:
        """返回公开动作说明 Section。"""

        content = load_prompt_template("public-action-commentary.md")
        return _section(
            section_id=PUBLIC_ACTION_COMMENTARY_SECTION_ID,
            title="Public Action Commentary",
            content=content,
            order_anchor="after-image-understanding-before-active-agent-context",
        )

    @classmethod
    def provide_active_agent_context(
        cls,
        candidate_input: ActiveAgentContextCandidateInput,
    ) -> BuiltinPromptSection:
        """根据当前 Agent 与团队输入返回动态 Section。

        参数:
            candidate_input: 已完成来源读取的活动 Agent 输入。

        返回:
            仅本次调用持有动态身份正文的 Section。
        """

        if not candidate_input.activation.active or not candidate_input.agent_name:
            raise ValueError("活动 Agent 上下文尚未激活")
        agent_name = candidate_input.agent_name
        display_name = candidate_input.display_name or agent_name
        lines = [
            f"Current active agent: `{agent_name}` ({display_name}).",
            (
                "Agent/mode changes are UI state, not user tasks. Do not treat "
                "a mode change itself as a request to initialize, inspect memory, "
                "scan files, or check processes."
            ),
            (
                "Answer subsequent user messages from this active agent identity, "
                "not from a previous mode."
            ),
        ]
        if candidate_input.team_present:
            lines.append(
                "Active team: "
                f"key={candidate_input.team_key}, "
                f"display_name={candidate_input.team_display_name}, "
                f"runtime_agent={candidate_input.runtime_agent}."
            )
            members: list[str] = []
            for member in candidate_input.members:
                qualifiers = [item for item in (member.role, "resident" if member.resident else "") if item]
                members.append(
                    f"{member.name}({', '.join(qualifiers)})"
                    if qualifiers
                    else member.name
                )
            if members:
                preview = ", ".join(members[:12])
                if len(members) > 12:
                    preview += f", ...(+{len(members) - 12})"
                lines.append(f"Visible configured members: {len(members)}; {preview}.")
                lines.append(
                    "If the user asks about my members/team members, answer for this active team."
                )
        return _section(
            section_id=ACTIVE_AGENT_CONTEXT_SECTION_ID,
            title="Active Agent Context",
            content="\n".join(lines),
            order_anchor="after-public-commentary-before-team-snapshot",
        )

    @classmethod
    def provide_direct_execution_mode(
        cls,
        candidate_input: DirectExecutionModeCandidateInput,
    ) -> BuiltinPromptSection:
        """根据直执行模式输入返回动态 Section。

        参数:
            candidate_input: 已计算激活状态与 scope 的输入。

        返回:
            只在本次调用持有 scope 正文的 Section。
        """

        if not candidate_input.activation.active:
            raise ValueError("直执行模式尚未激活")
        scope_text = f"（scope: {candidate_input.scope}）" if candidate_input.scope else ""
        content = (
            load_prompt_template("direct-execution-mode.md").replace("{scope_text}", scope_text)
        )
        return _section(
            section_id=DIRECT_EXECUTION_MODE_SECTION_ID,
            title="Direct Execution Mode",
            content=content,
            order_anchor="after-session-resources-before-project-rules",
        )


def prompt_section_contract_document() -> dict[str, Any]:
    """返回五个 Section 的单轨机器契约。"""

    return {
        "contract_id": "tcode.prompt-sections.v1",
        "single_track": True,
        "consumer": "AgentPromptBuilder",
        "section_ids": [
            FRONTEND_IMAGE_SECTION_ID,
            IMAGE_UNDERSTANDING_SECTION_ID,
            PUBLIC_ACTION_COMMENTARY_SECTION_ID,
            ACTIVE_AGENT_CONTEXT_SECTION_ID,
            DIRECT_EXECUTION_MODE_SECTION_ID,
        ],
        "shadow_consumer": False,
    }


__all__ = [
    "ACTIVE_AGENT_CONTEXT_ACTIVATION_POLICY_ID",
    "ACTIVE_AGENT_CONTEXT_SECTION_ID",
    "ActiveAgentContextActivation",
    "ActiveAgentContextActivationState",
    "ActiveAgentContextCandidateInput",
    "ActiveAgentContextMember",
    "BuiltinPromptSection",
    "BuiltinPromptSectionRegistry",
    "DIRECT_EXECUTION_MODE_ACTIVATION_POLICY_ID",
    "DIRECT_EXECUTION_MODE_SECTION_ID",
    "DirectExecutionModeActivation",
    "DirectExecutionModeActivationState",
    "DirectExecutionModeCandidateInput",
    "FRONTEND_IMAGE_SECTION_ID",
    "IMAGE_UNDERSTANDING_SECTION_ID",
    "PUBLIC_ACTION_COMMENTARY_ACTIVATION_POLICY_ID",
    "PUBLIC_ACTION_COMMENTARY_SECTION_ID",
    "PublicActionCommentaryActivation",
    "PublicActionCommentaryActivationState",
    "build_active_agent_context_candidate_input",
    "build_direct_execution_mode_candidate_input",
    "evaluate_public_action_commentary_activation",
    "prompt_section_contract_document",
]
