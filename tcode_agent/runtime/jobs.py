"""统一 Job Provider、Owner、Cursor、Cancel 与脱敏输出合同。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from .task_runtime import TASK_RUNTIME_TERMINAL_STATUSES, TaskRuntimeStore


class JobAccessError(PermissionError):
    """表示 Job 不存在、Owner 不匹配或请求缺少工作区边界。"""


class JobNotFoundError(JobAccessError):
    """仅表示索引中没有记录，不包括归属失败或引用歧义。"""


class JobCursorConflict(RuntimeError):
    """表示调用方基于过期 Cursor 请求了有副作用的 Job 操作。"""


class JobCancellationUnsupported(RuntimeError):
    """表示 Provider 没有声明可验证的后端取消能力。"""


@dataclass(frozen=True, slots=True)
class JobOwner:
    """Job 访问必须同时匹配的租户、用户和工作区三元组。"""

    tenant_id: str
    user_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        """拒绝不完整 Owner，避免空值退化为跨工作区查询。"""

        if not all(
            str(value or "").strip()
            for value in (self.tenant_id, self.user_id, self.workspace_id)
        ):
            raise JobAccessError("job owner requires tenant_id, user_id, and workspace_id")

    def to_dict(self) -> dict[str, str]:
        """返回可序列化但不包含任何认证凭据的 Owner。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class JobEvent:
    """提供单调 Cursor 的脱敏 Job 生命周期事件。"""

    cursor: int
    event_id: str
    status: str
    phase: str
    created_at: float
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回 API 使用的事件字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class JobRecord:
    """跨后端一致的 Job 查询投影。"""

    job_id: str
    provider: str
    owner: JobOwner
    session_id: str
    external_id: str
    status: str
    phase: str
    cursor: int
    created_at: float
    updated_at: float
    cancellation_supported: bool
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回包含 Owner、Cursor 和脱敏元数据的稳定 API 投影。"""

        payload = asdict(self)
        payload["owner"] = self.owner.to_dict()
        return payload


def redact_job_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """递归删除 Job 诊断中的认证凭据并限制输出深度与规模。"""

    from .telemetry import redact_telemetry_value

    return redact_telemetry_value(
        value,
        key=key,
        depth=depth,
        text_limit=4_000,
        list_limit=100,
        dict_limit=100,
        depth_limit=6,
    )


@runtime_checkable
class JobProvider(Protocol):
    """所有 Job 后端必须实现的最小宿主合同。"""

    @property
    def name(self) -> str:
        """返回稳定 Provider 名称。"""

        ...

    async def read(self, job_id: str, owner: JobOwner) -> JobRecord:
        """按 Owner 返回一个 Job 的当前投影。"""

        ...

    async def events(
        self,
        job_id: str,
        owner: JobOwner,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[JobEvent, ...]:
        """返回指定 Cursor 之后的有界事件。"""

        ...

    async def cancel(
        self,
        job_id: str,
        owner: JobOwner,
        *,
        expected_cursor: int,
    ) -> JobRecord:
        """在 Cursor 比较成功后请求后端取消。"""

        ...


JobCanceller = Callable[[dict[str, Any], JobOwner], Awaitable[None]]


class TaskRuntimeJobProvider:
    """把现有 TaskRuntime 聚合索引适配为统一 Job Provider。

    该适配器可读取 MCP Job、Workflow 和其他已进入 TaskRuntimeStore 的后端。
    只有显式注册 Canceller 的 runtime_kind 才允许取消，未知后端失败关闭。
    """

    def __init__(
        self,
        *,
        workdir: Optional[str] = None,
        cancellers: Optional[dict[str, JobCanceller]] = None,
    ) -> None:
        """绑定 TaskRuntime 存储根和经过审核的取消处理器。"""

        self.workdir = workdir
        self._cancellers = dict(cancellers or {})

    @property
    def name(self) -> str:
        """返回聚合 Provider 的稳定名称。"""

        return "task-runtime"

    def _resolve_record(self, job_id: str) -> dict[str, Any]:
        """按 runtime_id 或外部 Job 标识查找唯一记录。"""

        normalized = str(job_id or "").strip()
        if not normalized:
            raise JobAccessError("job_id is required")
        direct = TaskRuntimeStore.get(normalized, workdir=self.workdir)
        if direct is not None:
            return dict(direct)
        matches = [
            record
            for record in TaskRuntimeStore.list(workdir=self.workdir)
            if normalized in TaskRuntimeStore._record_reference_values(record)
        ]
        if not matches:
            raise JobNotFoundError("job not found")
        if len(matches) != 1:
            raise JobAccessError("job not found")
        return dict(matches[0])

    @staticmethod
    def _owner_from_metadata(metadata: dict[str, Any]) -> dict[str, str]:
        """从 TaskRuntime 的多种历史嵌套位置提取 Owner。"""

        candidates = [metadata]
        submit_input = metadata.get("submit_input")
        if isinstance(submit_input, dict):
            candidates.append(submit_input)
            if isinstance(submit_input.get("workspace"), dict):
                candidates.append(submit_input["workspace"])
        if isinstance(metadata.get("owner"), dict):
            candidates.append(metadata["owner"])
        result = {"tenant_id": "", "user_id": "", "workspace_id": ""}
        for candidate in candidates:
            for field in result:
                result[field] = result[field] or str(candidate.get(field) or "").strip()
        return result

    async def _assert_owner(self, record: dict[str, Any], owner: JobOwner) -> None:
        """验证记录 Owner；旧记录缺字段时只允许从所属 Session 补齐。"""

        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        actual = self._owner_from_metadata(metadata)
        if not all(actual.values()):
            session_id = str(record.get("session_id") or "").strip()
            if session_id:
                from ..session.manager import SessionManager

                session = await SessionManager.get(
                    session_id,
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    workspace_id=owner.workspace_id,
                )
                if session is not None:
                    actual = owner.to_dict()
        if actual != owner.to_dict():
            raise JobAccessError("job owner mismatch")

    def _record_projection(self, record: dict[str, Any], owner: JobOwner) -> JobRecord:
        """把 TaskRuntime 行转换为无 Secret 的 JobRecord。"""

        runtime_kind = str(record.get("runtime_kind") or "runtime").strip()
        return JobRecord(
            job_id=str(record.get("runtime_id") or ""),
            provider=runtime_kind,
            owner=owner,
            session_id=str(record.get("session_id") or ""),
            external_id=str(record.get("external_id") or ""),
            status=str(record.get("status") or ""),
            phase=str(record.get("phase") or ""),
            cursor=max(0, int(record.get("event_count", 0) or 0)),
            created_at=float(record.get("created_at", 0.0) or 0.0),
            updated_at=float(record.get("updated_at", 0.0) or 0.0),
            cancellation_supported=runtime_kind in self._cancellers,
            metadata=redact_job_value(record.get("metadata") or {}),
        )

    async def read(self, job_id: str, owner: JobOwner) -> JobRecord:
        """读取一个 Owner 可见的统一 Job 投影。"""

        record = self._resolve_record(job_id)
        await self._assert_owner(record, owner)
        return self._record_projection(record, owner)

    async def events(
        self,
        job_id: str,
        owner: JobOwner,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[JobEvent, ...]:
        """为历史 TaskRuntime 事件附加确定性的单调 Cursor。"""

        record = self._resolve_record(job_id)
        await self._assert_owner(record, owner)
        runtime_id = str(record.get("runtime_id") or "")
        raw_events = TaskRuntimeStore.events(runtime_id=runtime_id, workdir=self.workdir)
        start = max(0, int(after_cursor))
        bounded_limit = max(1, min(int(limit), 500))
        projected: list[JobEvent] = []
        for cursor, event in enumerate(raw_events, start=1):
            if cursor <= start:
                continue
            projected.append(
                JobEvent(
                    cursor=cursor,
                    event_id=str(event.get("event_id") or ""),
                    status=str(event.get("status") or ""),
                    phase=str(event.get("phase") or ""),
                    created_at=float(event.get("created_at", 0.0) or 0.0),
                    metadata=redact_job_value(event.get("metadata") or {}),
                )
            )
            if len(projected) >= bounded_limit:
                break
        return tuple(projected)

    async def cancel(
        self,
        job_id: str,
        owner: JobOwner,
        *,
        expected_cursor: int,
    ) -> JobRecord:
        """以 Cursor CAS 调用经过审核的后端 Canceller，并记录取消终态。"""

        record = self._resolve_record(job_id)
        await self._assert_owner(record, owner)
        current_cursor = max(0, int(record.get("event_count", 0) or 0))
        if int(expected_cursor) != current_cursor:
            raise JobCursorConflict(
                f"job cursor changed: expected {expected_cursor}, current {current_cursor}"
            )
        status = str(record.get("status") or "").strip().lower()
        if status in TASK_RUNTIME_TERMINAL_STATUSES:
            return self._record_projection(record, owner)
        runtime_kind = str(record.get("runtime_kind") or "runtime").strip()
        canceller = self._cancellers.get(runtime_kind)
        if canceller is None:
            raise JobCancellationUnsupported(
                f"job provider {runtime_kind!r} has no verified cancellation adapter"
            )
        await canceller(dict(record), owner)
        updated = TaskRuntimeStore.record_event(
            runtime_id=str(record.get("runtime_id") or ""),
            runtime_kind=runtime_kind,
            status="cancelled",
            phase="cancelled",
            source="job-runtime-api",
            session_id=str(record.get("session_id") or ""),
            external_id=str(record.get("external_id") or ""),
            metadata={"cancelled_by_owner": owner.to_dict()},
            workdir=self.workdir,
        )
        return self._record_projection(updated, owner)


def job_contract_document() -> dict[str, Any]:
    """返回第三批 Job 统一合同的无业务载荷说明。"""

    return {
        "schema_version": 1,
        "contract_id": "tcode.jobs.v1",
        "owner_fields": ["tenant_id", "user_id", "workspace_id"],
        "states": [
            "queued",
            "running",
            "waiting_user",
            "waiting_review",
            "waiting_external",
            "collecting",
            "blocked",
            "completed",
            "failed",
            "cancelled",
        ],
        "cursor": "monotonic-event-index",
        "cancel": "owner-and-expected-cursor-required",
        "unknown_backend_cancel": "fail-closed",
        "diagnostics": "bounded-and-secret-redacted",
    }


__all__ = [
    "JobAccessError",
    "JobNotFoundError",
    "JobCancellationUnsupported",
    "JobCursorConflict",
    "JobEvent",
    "JobOwner",
    "JobProvider",
    "JobRecord",
    "TaskRuntimeJobProvider",
    "job_contract_document",
    "redact_job_value",
]
