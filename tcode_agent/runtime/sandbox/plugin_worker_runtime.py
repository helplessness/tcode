"""由 Sandbox Runner 独占 Docker 权限的插件容器生命周期管理器。"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import SandboxConfig


_WORKER_ID_PATTERN = re.compile(r"^plgw_[a-f0-9]{20}$")
_USER_PATTERN = re.compile(r"^(\d+):(\d+)$")


def _env_list(name: str) -> tuple[Path, ...]:
    """读取并规范化 Runner 允许只读挂载的宿主根目录。"""

    values: list[Path] = []
    for item in str(os.environ.get(name, "") or "").split(","):
        text = item.strip()
        if text:
            values.append(Path(text).expanduser().resolve())
    return tuple(values)


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    """判断路径是否严格位于任一管理员允许根内。"""

    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return path != root or path.is_dir()
    return False


def _non_root_user(raw: str) -> str:
    """校验插件容器用户格式，并永久拒绝 root UID 或 GID。"""

    value = str(raw or "").strip()
    match = _USER_PATTERN.fullmatch(value)
    if match is None or int(match.group(1)) == 0 or int(match.group(2)) == 0:
        raise RuntimeError("plugin container user must be a non-root UID:GID")
    return value


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取有上下界的整数配置，非法输入回退到安全默认值。"""

    try:
        value = int(str(os.environ.get(name, default) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(slots=True)
class RunnerPluginWorker:
    """保存一个插件容器及其协议管道和有界诊断。"""

    worker_id: str
    plugin_name: str
    container_name: str
    process: asyncio.subprocess.Process
    created_at: float = field(default_factory=time.time)
    stderr_tail: str = ""
    stderr_task: asyncio.Task[None] | None = None
    channel_attached: bool = False


class PluginWorkerContainerManager:
    """用固定 Docker 参数启动、连接和回收未知插件容器。"""

    def __init__(self, config: SandboxConfig) -> None:
        """绑定 Runner Docker 配置和管理员允许的包目录。"""

        self.config = config
        self.image = str(
            os.environ.get("TCODE_PLUGIN_CONTAINER_IMAGE", "tcode-plugin-runtime:py312")
            or "tcode-plugin-runtime:py312"
        ).strip()
        self.user = _non_root_user(
            str(
                os.environ.get("TCODE_PLUGIN_CONTAINER_USER", "65532:65532")
                or "65532:65532"
            )
        )
        self.allowed_plugin_roots = _env_list(
            "TCODE_PLUGIN_RUNNER_ALLOWED_PLUGIN_ROOTS"
        ) or ((Path.cwd() / "plugins").resolve(),)
        self.max_workers = _bounded_int(
            "TCODE_PLUGIN_CONTAINER_MAX_WORKERS",
            32,
            1,
            256,
        )
        self._workers: dict[str, RunnerPluginWorker] = {}
        self._lock = asyncio.Lock()

    def _validated_directory(
        self,
        raw: Any,
        *,
        roots: tuple[Path, ...],
        label: str,
    ) -> Path:
        """解析现存目录，并禁止未配置或越过允许根的挂载。"""

        if not roots:
            raise PermissionError(f"plugin runner {label} roots are not configured")
        path = Path(str(raw or "")).expanduser().resolve(strict=True)
        if not path.is_dir() or not _inside(path, roots):
            raise PermissionError(f"plugin runner {label} is outside allowed roots")
        return path

    def _docker_args(
        self,
        *,
        container_name: str,
        plugin_root: Path,
    ) -> list[str]:
        """构造无网络、只读、非 root 且无额外 capability 的固定命令。"""

        args = [
            self.config.docker_binary,
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--pull",
            "never",
            "--init",
        ]
        if self.config.docker_runtime:
            args.extend(["--runtime", self.config.docker_runtime])
        args.extend(
            [
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(max(8, min(self.config.pids_limit or 32, 64))),
                "--memory",
                str(os.environ.get("TCODE_PLUGIN_CONTAINER_MEMORY", "768m") or "768m"),
                "--cpus",
                str(os.environ.get("TCODE_PLUGIN_CONTAINER_CPUS", "1") or "1"),
                "--user",
                self.user,
                "--ulimit",
                "nofile=64:64",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
                "--label",
                "tcode.plugin-worker=true",
                "-e",
                "PYTHONPATH=/tcode/runtime",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PYTHONUNBUFFERED=1",
                "-e",
                "TCODE_PLUGIN_WORKER=1",
                "-v",
                f"{plugin_root.as_posix()}:/tcode/plugin:ro",
                "-w",
                "/tmp",
                self.image,
                "python3",
                "-m",
                "tcode_agent.plugin_sdk.worker",
            ]
        )
        return args

    async def _drain_stderr(self, worker: RunnerPluginWorker) -> None:
        """持续消费 Docker stderr，并只保留有界尾部。"""

        if worker.process.stderr is None:
            return
        while True:
            chunk = await worker.process.stderr.read(4096)
            if not chunk:
                break
            worker.stderr_tail = (
                worker.stderr_tail + chunk.decode("utf-8", errors="replace")
            )[-16_000:]

    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """验证固定镜像和只读目录后启动一个协议容器。"""

        plugin_root = self._validated_directory(
            payload.get("plugin_root"),
            roots=self.allowed_plugin_roots,
            label="plugin_root",
        )
        requested_image = str(payload.get("image") or self.image).strip()
        if requested_image != self.image:
            raise PermissionError("plugin runner image is fixed by the administrator")
        plugin_name = str(payload.get("plugin_name") or "plugin").strip()[:128]
        worker_id = f"plgw_{uuid.uuid4().hex[:20]}"
        container_name = f"tcode-plugin-{uuid.uuid4().hex[:18]}"
        async with self._lock:
            if len(self._workers) >= self.max_workers:
                raise RuntimeError("plugin container worker limit reached")
            process = await asyncio.create_subprocess_exec(
                *self._docker_args(
                    container_name=container_name,
                    plugin_root=plugin_root,
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
            worker = RunnerPluginWorker(
                worker_id=worker_id,
                plugin_name=plugin_name,
                container_name=container_name,
                process=process,
            )
            worker.stderr_task = asyncio.create_task(self._drain_stderr(worker))
            self._workers[worker_id] = worker
        await asyncio.sleep(0)
        if process.returncode is not None:
            await self.stop(worker_id)
            raise RuntimeError(
                f"plugin container exited during startup: {worker.stderr_tail}"
            )
        return {
            "worker_id": worker_id,
            "container_name": container_name,
            "image": self.image,
            "network": "none",
            "read_only": True,
        }

    async def get(self, worker_id: str) -> RunnerPluginWorker:
        """按不可预测 ID 获取活动 Worker。"""

        normalized = str(worker_id or "").strip()
        if _WORKER_ID_PATTERN.fullmatch(normalized) is None:
            raise KeyError("invalid plugin worker id")
        async with self._lock:
            worker = self._workers.get(normalized)
        if worker is None:
            raise KeyError("plugin worker not found")
        return worker

    async def stop(self, worker_id: str) -> dict[str, Any]:
        """终止精确容器进程组并删除内存记录。"""

        normalized = str(worker_id or "").strip()
        async with self._lock:
            worker = self._workers.pop(normalized, None)
        if worker is None:
            return {"stopped": False, "worker_id": normalized}
        process = worker.process
        if process.returncode is None:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        await self._force_remove_container(worker.container_name)
        if worker.stderr_task is not None:
            try:
                await asyncio.wait_for(worker.stderr_task, timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker.stderr_task.cancel()
        return {
            "stopped": True,
            "worker_id": normalized,
            "container_name": worker.container_name,
            "returncode": process.returncode,
            "stderr_tail": worker.stderr_tail,
        }

    async def _force_remove_container(self, container_name: str) -> None:
        """幂等删除本管理器创建的精确容器，避免 Docker 客户端异常留下孤儿。"""

        try:
            process = await asyncio.create_subprocess_exec(
                self.config.docker_binary,
                "rm",
                "-f",
                str(container_name),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def stop_all(self) -> None:
        """在 Runner 退出时回收全部插件容器。"""

        async with self._lock:
            worker_ids = list(self._workers)
        await asyncio.gather(
            *(self.stop(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )


__all__ = ["PluginWorkerContainerManager", "RunnerPluginWorker"]
