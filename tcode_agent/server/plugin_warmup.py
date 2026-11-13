from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from ..plugins.manager import PluginManager


_PLUGIN_WARMUP_TASKS: dict[str, asyncio.Task] = {}
_PLUGIN_STARTUP_TASKS: dict[str, asyncio.Task] = {}
_PLUGIN_STARTUP_DONE: set[str] = set()


def _workdir_key(workdir: Optional[str]) -> str:
    return str(Path(workdir or os.getcwd()).resolve())


def _float_env(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 60.0,
) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(minimum, min(value, maximum))


def plugin_startup_wait_seconds() -> float:
    return _float_env("TCODE_PLUGIN_STARTUP_WAIT_SECONDS", 1.5, maximum=30.0)


def plugin_request_wait_seconds() -> float:
    return _float_env("TCODE_PLUGIN_REQUEST_WAIT_SECONDS", 1.0, maximum=30.0)


def plugin_bootstrap_wait_seconds() -> float:
    return _float_env("TCODE_PLUGIN_BOOTSTRAP_WAIT_SECONDS", 2.0, maximum=60.0)


def _task_failed(task: asyncio.Task) -> bool:
    if not task.done():
        return False
    if task.cancelled():
        return True
    try:
        return task.exception() is not None
    except Exception:
        return True


def _consume_background_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        pass


async def _load_plugin_manager(workdir: str) -> PluginManager:
    return await PluginManager.get_global(workdir)


def schedule_plugin_warmup(workdir: Optional[str]) -> asyncio.Task:
    key = _workdir_key(workdir)
    task = _PLUGIN_WARMUP_TASKS.get(key)
    if task is not None and not _task_failed(task):
        return task

    task = asyncio.create_task(_load_plugin_manager(key))
    task.add_done_callback(_consume_background_exception)
    _PLUGIN_WARMUP_TASKS[key] = task
    return task


def schedule_plugin_startup(workdir: Optional[str]) -> asyncio.Task:
    key = _workdir_key(workdir)
    task = _PLUGIN_STARTUP_TASKS.get(key)
    if task is not None and not _task_failed(task):
        return task

    async def _run_startup_hook() -> PluginManager:
        manager = await schedule_plugin_warmup(key)
        if key not in _PLUGIN_STARTUP_DONE:
            await manager.trigger("system.startup", {"workdir": key}, {})
            _PLUGIN_STARTUP_DONE.add(key)
        return manager

    task = asyncio.create_task(_run_startup_hook())
    task.add_done_callback(_consume_background_exception)
    _PLUGIN_STARTUP_TASKS[key] = task
    return task


async def wait_for_plugin_warmup(workdir: Optional[str], *, timeout: Optional[float]) -> bool:
    task = schedule_plugin_warmup(workdir)
    if timeout is not None and timeout <= 0:
        return task.done() and not _task_failed(task)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return not _task_failed(task)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


async def wait_for_plugin_startup(workdir: Optional[str], *, timeout: Optional[float]) -> bool:
    task = schedule_plugin_startup(workdir)
    if timeout is not None and timeout <= 0:
        return task.done() and not _task_failed(task)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return not _task_failed(task)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


async def trigger_plugin_event(
    workdir: Optional[str],
    event: str,
    input_data: dict[str, Any],
    output_data: Optional[dict[str, Any]] = None,
    *,
    mode: str = "serial",
) -> dict[str, Any]:
    """等待插件加载与事件处理完成，供事务型生命周期调用。"""

    key = _workdir_key(workdir)
    manager = await schedule_plugin_warmup(key)
    if str(mode or "serial").strip().lower() == "serial":
        return await manager.trigger(event, input_data, output_data or {})
    return await manager.trigger(event, input_data, output_data or {}, mode=mode)


def trigger_plugin_event_background(
    workdir: Optional[str],
    event: str,
    input_data: dict[str, Any],
    output_data: Optional[dict[str, Any]] = None,
) -> asyncio.Task:
    key = _workdir_key(workdir)
    loop = asyncio.get_running_loop()

    async def _run_event() -> None:
        await trigger_plugin_event(key, event, input_data, output_data or {})

    task = loop.create_task(_run_event())
    task.add_done_callback(_consume_background_exception)
    return task
