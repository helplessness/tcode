"""工具执行单轨管线。

本模块只负责一次工具调用的不可变请求边界、阶段顺序和结果交接。真正的
Policy、Permission、Sandbox、工具体与结果最终化仍由 ``AgentToolExecutor``
实现，但所有入口必须通过 ``ToolExecutionPipeline``，从而避免旧执行器、
Shadow 适配器和 Evidence 观察器并行消费同一次请求。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from ..types import ToolCall, ToolResult


TOOL_PIPELINE_CONTRACT_ID = "tcode.tool-execution-pipeline.v1"
TOOL_PIPELINE_SCHEMA_VERSION = 1


class ToolPipelineStage(str, Enum):
    """列出生产工具管线允许出现的稳定阶段。"""

    REQUEST_CAPTURED = "request.captured"
    CONTEXT_BUILT = "context.built"
    ARGUMENT_VALIDATION_PASSED = "argument-validation.passed"
    ARGUMENT_VALIDATION_REJECTED = "argument-validation.rejected"
    BEFORE_HOOK_ENTERED = "before-hook.entered"
    BEFORE_HOOK_COMPLETED = "before-hook.completed"
    POLICY_EVALUATED = "policy.evaluated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    ROUTE_SELECTED = "route.selected"
    TOOL_BODY_ENTERED = "tool-body.entered"
    TOOL_BODY_COMPLETED = "tool-body.completed"
    FINALIZE_ENTERED = "finalize.entered"
    AFTER_HOOK_ENTERED = "after-hook.entered"
    AFTER_HOOK_COMPLETED = "after-hook.completed"
    FINALIZE_COMPLETED = "finalize.completed"
    RESULT_HANDOFF = "result.handoff"


_STAGE_PHASE: dict[ToolPipelineStage, int] = {
    ToolPipelineStage.REQUEST_CAPTURED: 0,
    ToolPipelineStage.CONTEXT_BUILT: 1,
    ToolPipelineStage.BEFORE_HOOK_ENTERED: 2,
    ToolPipelineStage.BEFORE_HOOK_COMPLETED: 2,
    ToolPipelineStage.ARGUMENT_VALIDATION_PASSED: 3,
    ToolPipelineStage.ARGUMENT_VALIDATION_REJECTED: 3,
    ToolPipelineStage.POLICY_EVALUATED: 4,
    ToolPipelineStage.APPROVAL_REQUESTED: 5,
    ToolPipelineStage.APPROVAL_RESOLVED: 5,
    ToolPipelineStage.ROUTE_SELECTED: 6,
    ToolPipelineStage.TOOL_BODY_ENTERED: 7,
    ToolPipelineStage.TOOL_BODY_COMPLETED: 7,
    ToolPipelineStage.FINALIZE_ENTERED: 8,
    ToolPipelineStage.AFTER_HOOK_ENTERED: 9,
    ToolPipelineStage.AFTER_HOOK_COMPLETED: 9,
    ToolPipelineStage.FINALIZE_COMPLETED: 10,
    ToolPipelineStage.RESULT_HANDOFF: 11,
}


class ToolExecutionRequestError(ValueError):
    """表示工具请求无法形成不可变且可验证的执行输入。"""


class ToolPipelineOrderError(RuntimeError):
    """表示生产工具管线出现阶段倒序。"""


def _canonical_json(value: Any) -> str:
    """把参数序列化为拒绝 NaN 的稳定 JSON 文本。

    参数:
        value: 需要建立请求摘要的 JSON 兼容值。

    返回:
        按键排序且不包含多余空白的 JSON 文本。

    异常:
        ToolExecutionRequestError: 参数无法安全序列化时抛出。
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ToolExecutionRequestError("工具参数不是合法 JSON 值") from exc


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    """保存一次工具调用的不可变、无正文诊断身份。"""

    request_ref: str
    tool_identity_ref: str
    argument_digest: str
    argument_bytes: int
    contract_id: str = TOOL_PIPELINE_CONTRACT_ID
    schema_version: int = TOOL_PIPELINE_SCHEMA_VERSION

    @classmethod
    def capture(cls, tool_call: ToolCall) -> "ToolExecutionRequest":
        """从原始调用捕获不会随 Hook 修改的请求身份。

        参数:
            tool_call: 当前生产工具调用。

        返回:
            只保存不透明引用、摘要和长度的不可变请求。
        """

        arguments = _canonical_json(dict(tool_call.arguments or {}))
        call_material = str(tool_call.call_id or uuid.uuid4().hex)
        tool_name = str(tool_call.tool_name or "")
        return cls(
            request_ref="request:"
            + hashlib.sha256(call_material.encode("utf-8")).hexdigest()[:20],
            tool_identity_ref="tool:"
            + hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:20],
            argument_digest="sha256:"
            + hashlib.sha256(arguments.encode("utf-8")).hexdigest(),
            argument_bytes=len(arguments.encode("utf-8")),
        )


@dataclass(slots=True)
class ToolExecutionTrace:
    """记录单次请求的有界阶段序列并立即拒绝倒序。"""

    request: ToolExecutionRequest
    stages: list[dict[str, str]] = field(default_factory=list)
    _last_phase: int = field(default=-1, repr=False)

    def advance(
        self,
        stage: ToolPipelineStage,
        *,
        route: str = "",
        outcome: str = "",
        failure_type: str = "",
    ) -> None:
        """追加一个生产阶段，并验证阶段不能倒退。

        参数:
            stage: 即将进入的稳定阶段。
            route: 可选的稳定路由标签。
            outcome: 可选的稳定结果标签。
            failure_type: 只允许异常类型名，不保存异常正文。

        异常:
            ToolPipelineOrderError: 当前阶段早于已完成阶段时抛出。
        """

        phase = _STAGE_PHASE[stage]
        if phase < self._last_phase:
            raise ToolPipelineOrderError(
                f"工具管线阶段倒序：{stage.value} 不能位于阶段 {self._last_phase} 之后"
            )
        self._last_phase = phase
        record = {"stage": stage.value}
        if route:
            record["route"] = str(route)[:40]
        if outcome:
            record["outcome"] = str(outcome)[:40]
        if failure_type:
            record["failure_type"] = str(failure_type)[:80]
        self.stages.append(record)

    def to_dict(self) -> dict[str, Any]:
        """返回不含工具名、参数、路径和结果正文的诊断投影。"""

        return {
            "request": asdict(self.request),
            "stages": [dict(item) for item in self.stages],
        }


_CURRENT_TRACE: ContextVar[ToolExecutionTrace | None] = ContextVar(
    "tcode_tool_execution_trace",
    default=None,
)


def current_tool_pipeline_trace() -> ToolExecutionTrace | None:
    """返回当前异步上下文中的生产工具管线记录。"""

    return _CURRENT_TRACE.get()


def advance_tool_pipeline_stage(
    stage: ToolPipelineStage,
    *,
    route: str = "",
    outcome: str = "",
    failure_type: str = "",
) -> None:
    """把阶段提交给当前唯一生产管线。

    参数:
        stage: 当前已进入的稳定阶段。
        route: 可选路由标签。
        outcome: 可选结果标签。
        failure_type: 可选异常类型名。

    异常:
        ToolPipelineOrderError: 当前调用不在生产管线内或阶段倒序时抛出。
    """

    trace = _CURRENT_TRACE.get()
    if trace is None:
        raise ToolPipelineOrderError("工具阶段只能由 ToolExecutionPipeline 推进")
    trace.advance(
        stage,
        route=route,
        outcome=outcome,
        failure_type=failure_type,
    )


class ToolExecutionPipeline:
    """工具调用的唯一生产入口。"""

    def __init__(self, agent: Any) -> None:
        """保存接收最终无载荷诊断的 Agent。

        参数:
            agent: 当前执行工具的 Agent 实例。
        """

        self.agent = agent

    async def execute(
        self,
        tool_call: ToolCall,
        execute_stages: Callable[[ToolCall], Awaitable[ToolResult]],
    ) -> ToolResult:
        """绑定一次请求并调用唯一阶段实现。

        参数:
            tool_call: 当前原始工具调用。
            execute_stages: 实现 Context、校验、Policy、Permission、Sandbox、
                Tool Body 与 Finalize 的生产函数。

        返回:
            完成全部阶段后的原始 ``ToolResult``。
        """

        trace = ToolExecutionTrace(ToolExecutionRequest.capture(tool_call))
        token: Token[ToolExecutionTrace | None] = _CURRENT_TRACE.set(trace)
        trace.advance(ToolPipelineStage.REQUEST_CAPTURED)
        try:
            result = await execute_stages(tool_call)
            trace.advance(
                ToolPipelineStage.RESULT_HANDOFF,
                outcome="error" if result.error else "completed",
            )
            return result
        finally:
            try:
                self.agent._last_tool_pipeline_trace = trace.to_dict()
            finally:
                _CURRENT_TRACE.reset(token)


def tool_pipeline_contract_document() -> dict[str, Any]:
    """返回第二批单轨管线的稳定机器契约。"""

    return {
        "contract_id": TOOL_PIPELINE_CONTRACT_ID,
        "schema_version": TOOL_PIPELINE_SCHEMA_VERSION,
        "consumer": "ToolExecutionPipeline",
        "single_track": True,
        "production_default": True,
        "argument_writeback": False,
        "stages": [stage.value for stage in ToolPipelineStage],
        "retains_payloads": False,
    }


__all__ = [
    "TOOL_PIPELINE_CONTRACT_ID",
    "TOOL_PIPELINE_SCHEMA_VERSION",
    "ToolExecutionPipeline",
    "ToolExecutionRequest",
    "ToolExecutionRequestError",
    "ToolExecutionTrace",
    "ToolPipelineOrderError",
    "ToolPipelineStage",
    "advance_tool_pipeline_stage",
    "current_tool_pipeline_trace",
    "tool_pipeline_contract_document",
]
