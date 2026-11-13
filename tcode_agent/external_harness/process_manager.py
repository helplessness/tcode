"""外部 harness 子进程生命周期、取消升级和输出边界。"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any


class HarnessProcessManager:
    def __init__(self) -> None:
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def register(self, run_id: str, process: asyncio.subprocess.Process) -> None:
        async with self._lock:
            if run_id in self._active:
                raise RuntimeError("harness process already exists for run")
            self._active[run_id] = process

    async def unregister(self, run_id: str, process: asyncio.subprocess.Process) -> None:
        async with self._lock:
            if self._active.get(run_id) is process:
                self._active.pop(run_id, None)

    async def cancel(self, run_id: str) -> bool:
        async with self._lock:
            process = self._active.get(run_id)
        if process is None:
            return False
        await self.terminate(process)
        return True

    async def is_active(self, run_id: str) -> bool:
        async with self._lock:
            process = self._active.get(run_id)
        return bool(process is not None and process.returncode is None)

    async def terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        await self._signal(process, signal.SIGINT)
        if await self._wait(process, 1.0):
            return
        await self._signal(process, signal.SIGTERM)
        if await self._wait(process, 2.0):
            return
        await self._signal(process, signal.SIGKILL)
        await self._wait(process, 2.0)

    @staticmethod
    async def _signal(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            return

    @staticmethod
    async def _wait(process: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


__all__ = ["HarnessProcessManager"]
