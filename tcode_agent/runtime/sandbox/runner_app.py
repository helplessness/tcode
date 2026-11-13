from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from .base import SandboxJob, load_sandbox_config, strict_isolation_enabled
from .docker_gvisor import DockerGvisorRuntime
from .runner_policy import SandboxRunnerPolicy
from .plugin_worker_runtime import PluginWorkerContainerManager
from ...plugin_sdk.protocol import MAX_PROTOCOL_LINE_BYTES


def _runner_token() -> str:
    direct = str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN", "") or "").strip()
    if direct:
        return direct
    token_file = str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN_FILE", "") or "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise RuntimeError(f"Unable to read sandbox runner token file: {exc}") from exc


class RunnerJobRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._jobs = {
                    str(key): dict(value)
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
        except Exception:
            self._jobs = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp.write_text(
            json.dumps(self._jobs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def put(self, job: SandboxJob) -> None:
        with self._lock:
            self._jobs[job.sandbox_job_id] = asdict(job)
            self._save()

    def get_by_client_request_id(self, client_request_id: str, *, scope: str | None = None) -> SandboxJob | None:
        request_id = str(client_request_id or "").strip()
        if not request_id:
            return None
        with self._lock:
            for raw in self._jobs.values():
                if not isinstance(raw, dict):
                    continue
                metadata = raw.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                if str(metadata.get("client_request_id") or "") != request_id:
                    continue
                if scope is not None and metadata.get("client_request_scope") != scope:
                    continue
                try:
                    return SandboxJob(**raw)
                except Exception:
                    return None
        return None

    def get(self, job_id: str) -> SandboxJob | None:
        with self._lock:
            raw = self._jobs.get(str(job_id or "").strip())
            if not isinstance(raw, dict):
                return None
            try:
                return SandboxJob(**raw)
            except Exception:
                return None

    def update_status(self, job_id: str, status: str) -> None:
        with self._lock:
            raw = self._jobs.get(job_id)
            if not isinstance(raw, dict):
                return
            normalized = str(status or "")
            if str(raw.get("status") or "") == normalized:
                return
            raw["status"] = normalized
            self._save()

    def update_from_status(self, job_id: str, status: Any) -> None:
        with self._lock:
            raw = self._jobs.get(job_id)
            if not isinstance(raw, dict):
                return
            changed = False
            normalized_status = str(getattr(status, "status", "") or "")
            if str(raw.get("status") or "") != normalized_status:
                raw["status"] = normalized_status
                changed = True
            metadata = getattr(status, "metadata", None)
            if isinstance(metadata, dict) and metadata:
                merged_metadata = {
                    **dict(raw.get("metadata") or {}),
                    **metadata,
                }
                if merged_metadata != dict(raw.get("metadata") or {}):
                    raw["metadata"] = merged_metadata
                    changed = True
            exit_code = getattr(status, "exit_code", None)
            if isinstance(exit_code, int):
                current_exit_code = dict(raw.get("metadata") or {}).get("exit_code")
                if current_exit_code != exit_code:
                    raw.setdefault("metadata", {})["exit_code"] = exit_code
                    changed = True
            if changed:
                self._save()

    def active_jobs(self) -> list[SandboxJob]:
        with self._lock:
            jobs: list[SandboxJob] = []
            for raw in self._jobs.values():
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("status") or "") in {
                    "completed",
                    "conflict",
                    "failed",
                    "missing",
                    "terminated",
                    "timed_out",
                }:
                    continue
                try:
                    jobs.append(SandboxJob(**raw))
                except Exception:
                    continue
            return jobs

    def active_jobs_for_owner(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> list[SandboxJob]:
        terminal = {
            "completed",
            "conflict",
            "failed",
            "missing",
            "terminated",
            "timed_out",
        }
        return [
            job
            for job in self.jobs_for_owner(
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            if str(job.status or "") not in terminal
        ]

    def jobs_for_owner(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> list[SandboxJob]:
        expected = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        with self._lock:
            jobs: list[SandboxJob] = []
            for raw in self._jobs.values():
                if not isinstance(raw, dict):
                    continue
                try:
                    job = SandboxJob(**raw)
                except Exception:
                    continue
                if all(
                    str((job.metadata or {}).get(key) or "").strip() == value
                    for key, value in expected.items()
                ):
                    jobs.append(job)
            return jobs

    def delete_for_owner(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> list[SandboxJob]:
        jobs = self.jobs_for_owner(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        with self._lock:
            for job in jobs:
                self._jobs.pop(job.sandbox_job_id, None)
            if jobs:
                self._save()
        return jobs


def create_runner_app() -> FastAPI:
    config = load_sandbox_config()
    if strict_isolation_enabled() and (config.docker_runtime != "runsc" or not config.workspace_snapshots):
        raise RuntimeError("strict Runner requires runsc and workspace snapshots")
    required_runtime = str(
        os.environ.get("TCODE_SANDBOX_RUNNER_REQUIRED_RUNTIME", "runsc") or ""
    ).strip()
    if required_runtime and config.docker_runtime != required_runtime:
        raise RuntimeError(
            "Sandbox runner Docker runtime mismatch: "
            f"expected {required_runtime}, got {config.docker_runtime or '<empty>'}"
        )
    require_snapshots = str(
        os.environ.get("TCODE_SANDBOX_RUNNER_REQUIRE_SNAPSHOTS", "true") or "true"
    ).strip().lower() not in {"0", "false", "no", "off"}
    if require_snapshots and not config.workspace_snapshots:
        raise RuntimeError(
            "Sandbox runner requires TCODE_SANDBOX_WORKSPACE_SNAPSHOTS=true"
        )
    policy = SandboxRunnerPolicy.from_env(config)
    runtime = DockerGvisorRuntime(config)
    plugin_workers = PluginWorkerContainerManager(config)
    token = _runner_token()
    require_token = strict_isolation_enabled() or str(
        os.environ.get("TCODE_SANDBOX_RUNNER_REQUIRE_TOKEN", "true") or "true"
    ).strip().lower() not in {"0", "false", "no", "off"}
    if require_token and not token:
        raise RuntimeError(
            "Sandbox runner authentication is enabled but no runner token is configured"
        )

    state_path = Path(
        os.environ.get(
            "TCODE_SANDBOX_RUNNER_STATE_FILE",
            "/tmp/tcode-sandbox-runner/jobs.json",
        )
    ).expanduser()
    registry = RunnerJobRegistry(state_path)
    reaper_task: asyncio.Task | None = None
    blocked_workspaces: set[tuple[str, str, str]] = set()
    start_job_locks: dict[str, asyncio.Lock] = {}
    start_job_failures: dict[str, str] = {}

    def require_auth(authorization: str = Header(default="")) -> None:
        if not require_token:
            return
        scheme, _, provided = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not provided or not hmac.compare_digest(
            provided.strip(), token
        ):
            raise HTTPException(401, "Invalid sandbox runner token")

    def websocket_auth_valid(websocket: WebSocket) -> bool:
        """校验插件协议 WebSocket 使用的同一 Runner Bearer Token。"""

        if not require_token:
            return True
        scheme, _, provided = str(
            websocket.headers.get("authorization") or ""
        ).partition(" ")
        return bool(
            scheme.lower() == "bearer"
            and provided
            and hmac.compare_digest(provided.strip(), token)
        )

    def validated_spec(payload: dict[str, Any]):
        try:
            return policy.validate(payload, config)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def validated_client_request_id(payload: dict[str, Any]) -> str:
        request_id = str(payload.get("client_request_id") or "").strip()
        if not request_id:
            return ""
        if len(request_id) > 96 or re.fullmatch(r"[A-Za-z0-9_-]+", request_id) is None:
            raise HTTPException(400, "Invalid sandbox client_request_id")
        return request_id

    def workspace_key(values: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(values.get("tenant_id") or "").strip(),
            str(values.get("user_id") or "").strip(),
            str(values.get("workspace_id") or "").strip(),
        )

    def assert_workspace_not_blocked(spec: Any) -> None:
        key = workspace_key(dict(getattr(spec, "metadata", {}) or {}))
        if all(key) and key in blocked_workspaces:
            raise HTTPException(410, "workspace is deleted")

    def known_job(job_id: str, execution_id: str = "", request: Request | None = None) -> SandboxJob:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(404, "Sandbox job not found")
        if strict_isolation_enabled():
            expected = str((job.metadata or {}).get("execution_id") or "").strip()
            if not expected or not hmac.compare_digest(expected, str(execution_id or "").strip()):
                raise HTTPException(404, "Sandbox job not found")
            for key in ("tenant_id", "user_id", "workspace_id"):
                supplied = request.headers.get(f"x-tcode-{key.replace('_', '-')}", "") if request else ""
                expected = str(job.metadata.get(key) or "")
                if not expected or not hmac.compare_digest(expected, supplied):
                    raise HTTPException(404, "Sandbox job not found")
        return job

    async def reap_jobs() -> None:
        last_runtime_reap = 0.0
        try:
            interval = max(
                1.0,
                float(
                    os.environ.get(
                        "TCODE_SANDBOX_RUNNER_REAPER_INTERVAL_SECONDS",
                        "2",
                    )
                    or "2"
                ),
            )
        except Exception:
            interval = 2.0
        while True:
            try:
                for job in registry.active_jobs():
                    try:
                        status = await runtime.job_status(job)
                        registry.update_from_status(job.sandbox_job_id, status)
                    except Exception:
                        continue
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            if asyncio.get_running_loop().time() - last_runtime_reap >= 30:
                try:
                    await runtime.reap_persistent_runtimes()
                except Exception:
                    pass
                last_runtime_reap = asyncio.get_running_loop().time()
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal reaper_task
        if reaper_task is None or reaper_task.done():
            reaper_task = asyncio.create_task(reap_jobs())
        try:
            yield
        finally:
            await plugin_workers.stop_all()
            if reaper_task is not None:
                reaper_task.cancel()
                try:
                    await reaper_task
                except asyncio.CancelledError:
                    pass
                reaper_task = None

    runner = FastAPI(
        title="TCode Sandbox Runner",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @runner.get("/health")
    async def health():
        return {"status": "ok"}

    @runner.post("/v1/run", dependencies=[Depends(require_auth)])
    async def run_once(payload: dict[str, Any]):
        spec = validated_spec(payload)
        assert_workspace_not_blocked(spec)
        job_id = str(spec.metadata.get("sandbox_job_id") or "").strip()
        if not job_id:
            job_id = f"sbx_{uuid.uuid4().hex[:16]}"
            spec.metadata["sandbox_job_id"] = job_id
        try:
            result = await runtime.run_once(spec)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                503,
                f"Sandbox runtime unavailable: {exc}",
            ) from exc
        if not result.sandbox_job_id:
            result.sandbox_job_id = job_id
        result.metadata = {
            **dict(result.metadata or {}),
            "sandbox_job_id": result.sandbox_job_id,
            "tool_name": spec.tool_name,
            "image": spec.image or config.image,
            "network": spec.network or config.network,
        }
        try:
            registry.put(
                SandboxJob(
                    sandbox_job_id=result.sandbox_job_id,
                    container_id=result.container_id,
                    container_name=result.container_name,
                    command=spec.command,
                    status=result.status,
                    metadata={
                        **dict(result.metadata or {}),
                        "exit_code": result.exit_code,
                        "stdout_tail": str(result.stdout or "")[-4000:],
                        "stderr_tail": str(result.stderr or "")[-4000:],
                    },
                )
            )
        except OSError as exc:
            raise HTTPException(
                503,
                f"Sandbox runner state unavailable: {exc}",
            ) from exc
        return asdict(result)

    @runner.post("/v1/plugin-workers", dependencies=[Depends(require_auth)])
    async def start_plugin_worker(payload: dict[str, Any]):
        """用固定安全参数启动一个未知插件容器。"""

        try:
            return await plugin_workers.start(payload)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(503, f"Plugin container unavailable: {exc}") from exc
        except (KeyError, PermissionError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @runner.delete(
        "/v1/plugin-workers/{worker_id}",
        dependencies=[Depends(require_auth)],
    )
    async def stop_plugin_worker(worker_id: str):
        """回收一个精确插件容器，重复调用保持幂等。"""

        return await plugin_workers.stop(worker_id)

    @runner.websocket("/v1/plugin-workers/{worker_id}/channel")
    async def plugin_worker_channel(websocket: WebSocket, worker_id: str):
        """在认证 Host 与无网络插件容器之间转发有界协议帧。"""

        if not websocket_auth_valid(websocket):
            await websocket.close(code=4401, reason="Invalid sandbox runner token")
            return
        try:
            worker = await plugin_workers.get(worker_id)
        except KeyError:
            await websocket.close(code=4404, reason="Plugin worker not found")
            return
        if worker.channel_attached:
            await websocket.close(code=4409, reason="Plugin worker channel already attached")
            return
        worker.channel_attached = True
        await websocket.accept()

        async def host_to_container() -> None:
            """把 Host 文本帧写入容器 stdin。"""

            if worker.process.stdin is None:
                raise RuntimeError("plugin worker stdin is unavailable")
            while True:
                text = await websocket.receive_text()
                payload = text.encode("utf-8")
                if len(payload) > MAX_PROTOCOL_LINE_BYTES:
                    raise ValueError("plugin worker frame exceeds protocol limit")
                worker.process.stdin.write(payload.rstrip(b"\n") + b"\n")
                await worker.process.stdin.drain()

        async def container_to_host() -> None:
            """把容器 stdout 的完整 JSON 行发给 Host。"""

            if worker.process.stdout is None:
                raise RuntimeError("plugin worker stdout is unavailable")
            while True:
                line = await worker.process.stdout.readline()
                if not line:
                    raise RuntimeError(
                        "plugin worker protocol closed: " + worker.stderr_tail
                    )
                if len(line) > MAX_PROTOCOL_LINE_BYTES:
                    raise ValueError("plugin worker frame exceeds protocol limit")
                await websocket.send_text(line.decode("utf-8").rstrip("\n"))

        tasks = {
            asyncio.create_task(host_to_container()),
            asyncio.create_task(container_to_host()),
        }
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        finally:
            worker.channel_attached = False
            await plugin_workers.stop(worker_id)
            try:
                await websocket.close()
            except Exception:
                pass

    @runner.post("/v1/jobs", dependencies=[Depends(require_auth)])
    async def start_job(payload: dict[str, Any]):
        spec = validated_spec(payload)
        assert_workspace_not_blocked(spec)
        client_request_id = validated_client_request_id(payload)
        request_scope = hashlib.sha256(json.dumps(
            [str(spec.metadata.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id", "execution_id")],
            separators=(",", ":"),
        ).encode()).hexdigest()
        request_key = f"{request_scope}:{client_request_id}"
        fingerprint = hashlib.sha256(json.dumps(
            {key: value for key, value in payload.items() if key != "client_request_id"},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        lock = (
            start_job_locks.setdefault(request_key, asyncio.Lock())
            if client_request_id
            else asyncio.Lock()
        )
        async with lock:
            if client_request_id:
                existing = registry.get_by_client_request_id(client_request_id, scope=request_scope)
                if existing is not None:
                    if existing.metadata.get("client_request_fingerprint") != fingerprint:
                        raise HTTPException(409, "client_request_id was reused with different request contents")
                    return asdict(existing)
                previous_failure = start_job_failures.get(f"{request_key}:{fingerprint}")
                if previous_failure:
                    raise HTTPException(503, previous_failure)
            try:
                job = await runtime.start_job(spec)
            except (OSError, RuntimeError) as exc:
                detail = f"Sandbox runtime unavailable: {exc}"
                if client_request_id:
                    start_job_failures[f"{request_key}:{fingerprint}"] = detail
                raise HTTPException(
                    503,
                    detail,
                ) from exc
            if client_request_id:
                job.metadata["client_request_id"] = client_request_id
                job.metadata["client_request_scope"] = request_scope
                job.metadata["client_request_fingerprint"] = fingerprint
            try:
                registry.put(job)
            except OSError as exc:
                try:
                    await runtime.terminate_job(job)
                except Exception:
                    pass
                detail = f"Sandbox runner state unavailable: {exc}"
                if client_request_id:
                    start_job_failures[f"{request_key}:{fingerprint}"] = detail
                raise HTTPException(
                    503,
                    detail,
                ) from exc
            return asdict(job)

    @runner.get("/v1/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def job_status(job_id: str, request: Request, x_tcode_execution_id: str = Header(default="")):
        status = await runtime.job_status(known_job(job_id, x_tcode_execution_id, request))
        registry.update_from_status(job_id, status)
        return asdict(status)

    @runner.get("/v1/jobs/{job_id}/output", dependencies=[Depends(require_auth)])
    async def job_output(job_id: str, request: Request, lines: int = Query(default=200, ge=1, le=5000), x_tcode_execution_id: str = Header(default="")):
        return {"output": await runtime.job_output(known_job(job_id, x_tcode_execution_id, request), lines=lines)}

    @runner.delete("/v1/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def terminate_job(job_id: str, request: Request, x_tcode_execution_id: str = Header(default="")):
        status = await runtime.terminate_job(known_job(job_id, x_tcode_execution_id, request))
        registry.update_from_status(job_id, status)
        return asdict(status)

    @runner.get("/v1/runtimes", dependencies=[Depends(require_auth)])
    async def list_runtimes(
        tenant_id: str = Query(default=""),
        user_id: str = Query(default=""),
        workspace_id: str = Query(default=""),
    ):
        return {
            "runtimes": await runtime.list_persistent_runtimes(
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        }

    @runner.post(
        "/v1/runtimes/{runtime_id}/{action}",
        dependencies=[Depends(require_auth)],
    )
    async def manage_runtime(runtime_id: str, action: str, payload: dict[str, Any]):
        owner = payload.get("owner", {})
        if not isinstance(owner, dict):
            raise HTTPException(400, "owner must be an object")
        owner_scope = str(payload.get("owner_scope") or "workspace")
        fields = {"workspace": ("tenant_id", "user_id", "workspace_id"), "user": ("tenant_id", "user_id"), "tenant": ("tenant_id",)}.get(owner_scope)
        if fields is None or any(not str(owner.get(key) or "").strip() for key in fields):
            raise HTTPException(400, "complete runtime owner identity is required")
        for field in ("force", "purge_data"):
            if field in payload and not isinstance(payload[field], bool):
                raise HTTPException(400, f"{field} must be a boolean")
        try:
            return await runtime.manage_persistent_runtime(
                runtime_id,
                action,
                owner={
                    key: str(owner.get(key) or "").strip()
                    for key in ("tenant_id", "user_id", "workspace_id")
                    if str(owner.get(key) or "").strip()
                },
                owner_scope=owner_scope,
                force=payload.get("force", False),
                purge_data=payload.get("purge_data", False),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @runner.post(
        "/v1/workspaces/cleanup",
        dependencies=[Depends(require_auth)],
    )
    async def cleanup_workspace(payload: dict[str, Any]):
        owner = {
            key: str(payload.get(key) or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
        }
        if any(not value for value in owner.values()):
            raise HTTPException(400, "tenant_id, user_id and workspace_id are required")
        key = workspace_key(owner)
        blocked_workspaces.add(key)
        purge_data = bool(payload.get("purge_data", False))
        owned_jobs = registry.jobs_for_owner(**owner)

        terminated_job_ids: list[str] = []
        termination_warnings: dict[str, str] = {}
        for job in registry.active_jobs_for_owner(**owner):
            try:
                status = await runtime.terminate_job(job)
                registry.update_from_status(job.sandbox_job_id, status)
                terminated_job_ids.append(job.sandbox_job_id)
            except Exception as exc:
                termination_warnings[job.sandbox_job_id] = str(exc)
        try:
            cleanup = await runtime.cleanup_workspace(
                **owner,
                purge_data=purge_data,
            )
        except (ValueError, RuntimeError) as exc:
            blocked_workspaces.discard(key)
            raise HTTPException(409, str(exc)) from exc
        purged_job_ids: list[str] = []
        purged_artifacts: list[str] = []
        if purge_data:
            for job in owned_jobs:
                log_file = runtime._job_log_file(job)
                try:
                    log_file.unlink()
                    purged_artifacts.append(log_file.as_posix())
                except FileNotFoundError:
                    pass
                snapshot = (job.metadata or {}).get("workspace_snapshot")
                snapshot_id = (
                    str(snapshot.get("snapshot_id") or "").strip()
                    if isinstance(snapshot, dict)
                    else ""
                )
                if (
                    snapshot_id
                    and snapshot_id not in {".", ".."}
                    and "/" not in snapshot_id
                    and "\\" not in snapshot_id
                    and Path(snapshot_id).name == snapshot_id
                ):
                    for root_text in (config.snapshot_root, config.conflict_root):
                        root = Path(root_text).expanduser().resolve(strict=False)
                        candidate = (root / snapshot_id).resolve(strict=False)
                        try:
                            candidate.relative_to(root)
                        except ValueError:
                            continue
                        if candidate.exists():
                            shutil.rmtree(candidate)
                            purged_artifacts.append(candidate.as_posix())
            purged_job_ids = sorted(
                job.sandbox_job_id for job in registry.delete_for_owner(**owner)
            )
            blocked_workspaces.discard(key)
        return {
            **cleanup,
            "terminated_job_ids": sorted(set(terminated_job_ids)),
            "termination_warnings": termination_warnings,
            "purged_job_ids": purged_job_ids,
            "purged_job_artifacts": sorted(set(purged_artifacts)),
        }

    @runner.post(
        "/v1/workspaces/restore",
        dependencies=[Depends(require_auth)],
    )
    async def restore_workspace(payload: dict[str, Any]):
        owner = {
            key: str(payload.get(key) or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
        }
        if any(not value for value in owner.values()):
            raise HTTPException(400, "tenant_id, user_id and workspace_id are required")
        blocked_workspaces.discard(workspace_key(owner))
        return await runtime.restore_workspace(**owner)

    return runner
