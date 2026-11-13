from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from .base import (
    SandboxConfig,
    SandboxJob,
    SandboxJobStatus,
    SandboxResult,
    SandboxSpec,
    strict_isolation_enabled,
)


class SandboxRunnerRequestError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = int(status_code)
        self.detail = str(detail or "sandbox runner request failed")
        super().__init__(
            f"Sandbox runner rejected request ({self.status_code}): {self.detail}"
        )


class SandboxRunnerTransportError(RuntimeError):
    """A bounded sandbox-runner transport failure with actionable context."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        operation: str,
        timeout_seconds: int,
        attempts: int,
        cause: BaseException,
    ):
        self.method = str(method or "GET").upper()
        self.path = str(path or "")
        self.operation = str(operation or "request")
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.attempts = max(1, int(attempts))
        self.cause_type = type(cause).__name__
        cause_text = str(cause or "").strip() or "no exception message"
        super().__init__(
            f"Sandbox runner {self.operation} failed after {self.attempts} "
            f"attempt(s): {self.method} {self.path} "
            f"(timeout={self.timeout_seconds}s, "
            f"cause={self.cause_type}: {cause_text})"
        )


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default) or default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.environ.get(name, default) or default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _read_token(config: SandboxConfig) -> str:
    if config.runner_token:
        return config.runner_token
    token_file = str(config.runner_token_file or "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise RuntimeError(f"Unable to read sandbox runner token file: {exc}") from exc


def sandbox_spec_payload(spec: SandboxSpec) -> dict[str, Any]:
    metadata = dict(spec.metadata or {})
    # ``workspace_id`` in owner metadata identifies the authenticated user
    # workspace.  The payload field identifies the concrete execution root,
    # which may be a nested task/protein run.  Keep the namespaces distinct.
    metadata.setdefault("runtime_workspace_id", spec.pathmap.workspace_id)
    return {
        "tool_name": spec.tool_name,
        "command": spec.command,
        "workspace_id": spec.pathmap.workspace_id,
        "host_workspace": spec.pathmap.host_workspace.as_posix(),
        "container_workspace": spec.pathmap.container_workspace,
        "host_cwd": spec.host_cwd.as_posix(),
        "container_cwd": spec.container_cwd,
        "timeout_seconds": spec.timeout_seconds,
        "network": spec.network,
        "env": dict(spec.env or {}),
        "image": spec.image,
        "memory": spec.memory,
        "cpus": spec.cpus,
        "pids_limit": spec.pids_limit,
        "read_only": spec.read_only,
        "user": spec.user,
        "home": spec.home,
        "tmpfs": spec.tmpfs,
        "shm_size": spec.shm_size,
        "lifecycle": spec.lifecycle,
        "idle_stop_seconds": spec.idle_stop_seconds,
        "retention_seconds": spec.retention_seconds,
        "mounts": [
            {
                "source": mount.source.as_posix(),
                "target": mount.target,
                "read_only": mount.read_only,
            }
            for mount in spec.mounts
        ],
        "expected_artifacts": list(spec.expected_artifacts or []),
        "metadata": metadata,
    }


class HttpSandboxRuntime:
    def __init__(self, config: SandboxConfig):
        self.config = config
        self.base_url = str(config.runner_url or "").rstrip("/")
        self.token = _read_token(config)
        if not self.base_url:
            raise RuntimeError("Sandbox runner URL is required")
        if config.require_runner and not self.token:
            raise RuntimeError("Sandbox runner token is required")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
        operation: str = "request",
        retries: int = 0,
        retry_http_statuses: tuple[int, ...] = (502, 503, 504),
        scoped_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        timeout_seconds = max(1, int(timeout))
        max_attempts = max(1, int(retries) + 1)
        url = f"{self.base_url}{path}"
        last_transport_error: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            try:
                # A fresh session after a failed attempt avoids reusing a poisoned
                # connection while still keeping retries tightly bounded.
                async with aiohttp.ClientSession(timeout=client_timeout) as session:
                    async with session.request(
                        method,
                        url,
                        headers={**self._headers(), **dict(scoped_headers or {})},
                        json=payload,
                    ) as response:
                        response_limit = _env_int(
                            "TCODE_SANDBOX_RUNNER_RESPONSE_MAX_BYTES",
                            8 * 1024 * 1024,
                            minimum=64 * 1024,
                            maximum=64 * 1024 * 1024,
                        )
                        content_stream = getattr(response, "content", None)
                        if content_stream is None:
                            # Lightweight unit-test responses do not implement
                            # aiohttp's streaming body API.
                            raw = await response.text()
                            try:
                                data = await response.json(content_type=None)
                            except Exception:
                                data = {"detail": raw.strip() or "invalid runner response"}
                        else:
                            body = bytearray()
                            async for chunk in content_stream.iter_chunked(64 * 1024):
                                body.extend(chunk)
                                if len(body) > response_limit:
                                    raise RuntimeError(
                                        "Sandbox runner response exceeded the configured "
                                        f"{response_limit}-byte limit"
                                    )
                            charset = str(getattr(response, "charset", "") or "utf-8")
                            try:
                                raw = bytes(body).decode(charset, errors="replace")
                            except LookupError:
                                raw = bytes(body).decode("utf-8", errors="replace")
                            try:
                                data = json.loads(raw)
                            except Exception:
                                data = {"detail": raw.strip() or "invalid runner response"}
                        if response.status >= 400:
                            detail = data.get("detail") if isinstance(data, dict) else data
                            error = SandboxRunnerRequestError(response.status, detail)
                            if (
                                response.status in retry_http_statuses
                                and attempt < max_attempts
                            ):
                                last_transport_error = error
                            else:
                                raise error
                        else:
                            if not isinstance(data, dict):
                                raise RuntimeError(
                                    "Sandbox runner returned a non-object response"
                                )
                            return data
            except SandboxRunnerRequestError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
                last_transport_error = exc

            if attempt < max_attempts:
                base_delay = _env_float(
                    "TCODE_SANDBOX_RUNNER_RETRY_BASE_SECONDS",
                    0.25,
                    minimum=0.0,
                    maximum=5.0,
                )
                await asyncio.sleep(min(5.0, base_delay * (2 ** (attempt - 1))))

        assert last_transport_error is not None
        raise SandboxRunnerTransportError(
            method=method,
            path=path,
            operation=operation,
            timeout_seconds=timeout_seconds,
            attempts=max_attempts,
            cause=last_transport_error,
        ) from last_transport_error

    async def run_once(self, spec: SandboxSpec) -> SandboxResult:
        data = await self._request(
            "POST",
            "/v1/run",
            payload=sandbox_spec_payload(spec),
            timeout=spec.timeout_seconds + 30,
            operation="run_once",
        )
        return SandboxResult(**data)

    async def start_job(self, spec: SandboxSpec) -> SandboxJob:
        payload = sandbox_spec_payload(spec)
        # The runner persists this key with the job. Retrying a POST after the
        # response socket is lost therefore returns the original job instead of
        # launching a duplicate command.
        payload["client_request_id"] = f"sbr_{uuid.uuid4().hex}"
        data = await self._request(
            "POST",
            "/v1/jobs",
            payload=payload,
            timeout=_env_int(
                "TCODE_SANDBOX_RUNNER_START_TIMEOUT_SECONDS",
                45,
                minimum=31,
                maximum=300,
            ),
            operation="start_job",
            retries=_env_int(
                "TCODE_SANDBOX_RUNNER_REQUEST_RETRIES",
                1,
                minimum=0,
                maximum=5,
            ),
            # Retrying a response-level 503 could duplicate a command if the
            # runtime failed after launch but before registration. Transport
            # retries are safe because client_request_id is idempotent.
            retry_http_statuses=(),
        )
        return SandboxJob(**data)

    async def job_status(self, job: SandboxJob | str) -> SandboxJobStatus:
        job_id = job.sandbox_job_id if isinstance(job, SandboxJob) else str(job)
        data = await self._request(
            "GET",
            f"/v1/jobs/{quote(job_id, safe='')}",
            timeout=_env_int(
                "TCODE_SANDBOX_RUNNER_STATUS_TIMEOUT_SECONDS",
                10,
                minimum=1,
                maximum=120,
            ),
            operation=f"job_status[{job_id}]",
            scoped_headers=self._job_scope_headers(job),
            retries=_env_int(
                "TCODE_SANDBOX_RUNNER_REQUEST_RETRIES",
                1,
                minimum=0,
                maximum=5,
            ),
        )
        return SandboxJobStatus(**data)

    async def job_output(self, job: SandboxJob | str, lines: int = 200) -> str:
        job_id = job.sandbox_job_id if isinstance(job, SandboxJob) else str(job)
        data = await self._request(
            "GET",
            f"/v1/jobs/{quote(job_id, safe='')}/output"
            f"?lines={max(1, min(5000, int(lines or 200)))}",
            timeout=_env_int(
                "TCODE_SANDBOX_RUNNER_OUTPUT_TIMEOUT_SECONDS",
                15,
                minimum=1,
                maximum=120,
            ),
            operation=f"job_output[{job_id}]",
            scoped_headers=self._job_scope_headers(job),
            retries=_env_int(
                "TCODE_SANDBOX_RUNNER_REQUEST_RETRIES",
                1,
                minimum=0,
                maximum=5,
            ),
        )
        return str(data.get("output") or "")

    async def terminate_job(self, job: SandboxJob | str) -> SandboxJobStatus:
        job_id = job.sandbox_job_id if isinstance(job, SandboxJob) else str(job)
        data = await self._request(
            "DELETE",
            f"/v1/jobs/{quote(job_id, safe='')}",
            timeout=35,
            operation=f"terminate_job[{job_id}]",
            scoped_headers=self._job_scope_headers(job),
            retries=_env_int(
                "TCODE_SANDBOX_RUNNER_REQUEST_RETRIES",
                1,
                minimum=0,
                maximum=5,
            ),
        )
        return SandboxJobStatus(**data)

    @staticmethod
    def _job_scope_headers(job: SandboxJob | str) -> dict[str, str]:
        if not strict_isolation_enabled():
            return {}
        execution_id = (
            str((job.metadata or {}).get("execution_id") or "").strip()
            if isinstance(job, SandboxJob)
            else ""
        )
        if not execution_id:
            raise ValueError("strict isolation requires the job execution_id")
        headers = {"X-Tcode-Execution-Id": execution_id}
        for key in ("tenant_id", "user_id", "workspace_id"):
            value = str(job.metadata.get(key) or "").strip()
            if not value:
                raise ValueError("strict isolation requires complete job owner identity")
            headers[f"X-Tcode-{key.replace('_', '-')}"] = value
        return headers

    async def list_persistent_runtimes(
        self,
        *,
        tenant_id: str = "",
        user_id: str = "",
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        query = "&".join(
            f"{key}={quote(str(value), safe='')}"
            for key, value in (
                ("tenant_id", tenant_id),
                ("user_id", user_id),
                ("workspace_id", workspace_id),
            )
            if str(value or "").strip()
        )
        data = await self._request(
            "GET",
            f"/v1/runtimes{f'?{query}' if query else ''}",
            timeout=35,
        )
        runtimes = data.get("runtimes")
        return [dict(item) for item in runtimes] if isinstance(runtimes, list) else []

    async def manage_persistent_runtime(
        self,
        runtime_id: str,
        action: str,
        *,
        owner: dict[str, str] | None = None,
        owner_scope: str = "workspace",
        force: bool = False,
        purge_data: bool = False,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/runtimes/{quote(runtime_id, safe='')}/{quote(action, safe='')}",
            payload={
                "owner": dict(owner or {}),
                "owner_scope": owner_scope,
                "force": bool(force),
                "purge_data": bool(purge_data),
            },
            timeout=45,
        )

    async def cleanup_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        purge_data: bool = False,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/workspaces/cleanup",
            payload={
                "tenant_id": str(tenant_id or "").strip(),
                "user_id": str(user_id or "").strip(),
                "workspace_id": str(workspace_id or "").strip(),
                "purge_data": bool(purge_data),
            },
            timeout=90,
        )

    async def restore_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/workspaces/restore",
            payload={
                "tenant_id": str(tenant_id or "").strip(),
                "user_id": str(user_id or "").strip(),
                "workspace_id": str(workspace_id or "").strip(),
            },
            timeout=30,
        )
